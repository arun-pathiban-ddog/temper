//! Supervised MCP stdio transport, including human request cancellation.
use crate::client_requests::{ClientRequester, PendingClientRequests, is_client_response};
use crate::protocol::{dispatch_json_value, json_rpc_error};
use crate::runtime::RuntimeContext;
use crate::trajectory_bounds::{MAX_STDIO_LINE_BYTES, StdioFrame, read_stdio_frame};
use anyhow::Result;
use serde_json::Value;
use std::sync::{Arc, Mutex};
use tokio::io::{AsyncBufRead, AsyncWrite, AsyncWriteExt};
use tokio::sync::{mpsc, oneshot};

/// Run the MCP server loop over an arbitrary transport.
///
/// The transport is split into a reader task and a writer task connected by
/// channels so the server can send correlated requests to the client (MCP
/// elicitation) while a `tools/call` is still being handled: the reader
/// routes JSON-RPC *responses* to the pending server→client request map and
/// queues client *requests* for the sequential dispatch loop. That queue also
/// guarantees at most one elicitation is in flight per session.
///
/// Frames are read through [`read_stdio_frame`], which bounds each frame to
/// `MAX_STDIO_LINE_BYTES`; oversized frames are dropped and invalid UTF-8
/// frames are skipped rather than aborting the session.
pub(crate) async fn run_loop<R, W>(mut ctx: RuntimeContext, reader: R, writer: W) -> Result<()>
where
    R: AsyncBufRead + Unpin + Send + 'static,
    W: AsyncWrite + Unpin + Send + 'static,
{
    // Do not let requests accumulate without limit during a human interaction.
    let (out_tx, out_rx) = mpsc::channel::<Value>(64);
    let mut writer_task = tokio::spawn(write_outbound(out_rx, writer));
    let pending = PendingClientRequests::default();
    let active = ActiveRequest::default();
    ctx.requester = Some(ClientRequester::new(out_tx.clone(), pending.clone()));
    let (in_tx, mut in_rx) = mpsc::channel::<Value>(64);
    let reader_pending = pending.clone();
    let reader_active = active.clone();
    let reader_out = out_tx.clone();
    let reader_task = tokio::spawn(async move {
        let result = read_inbound(
            reader,
            in_tx,
            reader_pending.clone(),
            reader_out,
            reader_active.clone(),
        )
        .await;
        reader_pending.fail_all();
        reader_active.cancel_current();
        result
    });

    let dispatch = async {
        while let Some(message) = in_rx.recv().await {
            let canceled = active.begin(message.get("id").cloned());
            let response = tokio::select! {
                biased;
                _ = canceled => {
                    pending.cancel_all(&out_tx).await;
                    None
                }
                response = dispatch_json_value(&mut ctx, message) => response,
            };
            active.clear();
            if let Some(response) = response {
                out_tx
                    .send(response)
                    .await
                    .map_err(|_| anyhow::anyhow!("MCP output closed"))?;
            }
        }
        Ok::<(), anyhow::Error>(())
    };
    let (mut result, writer_finished) = tokio::select! {
        result = dispatch => (result, false),
        result = &mut writer_task => (result.map_err(anyhow::Error::from).and_then(|r| r), true),
    };
    pending.fail_all();
    active.clear();
    // If dispatch drained, the reader has finished; otherwise stop it after
    // an output failure. Preserve actual I/O errors for the supervisor.
    if !reader_task.is_finished() {
        reader_task.abort();
    }
    match reader_task.await {
        Ok(reader_result) => result = result.and(reader_result),
        Err(error) if !error.is_cancelled() => result = result.and(Err(error.into())),
        Err(_) => {}
    }
    ctx.requester = None;
    drop(out_tx);
    if !writer_finished {
        if result.is_err() {
            writer_task.abort();
        } else {
            result = writer_task
                .await
                .map_err(anyhow::Error::from)
                .and_then(|r| r);
        }
    }
    ctx.finalize_trajectory().await;
    result
}

/// The single dispatched request, independently reachable by the reader.
#[derive(Clone, Default)]
struct ActiveRequest(Arc<Mutex<Option<RunningRequest>>>);

struct RunningRequest {
    id: Value,
    cancel: oneshot::Sender<()>,
}

impl ActiveRequest {
    fn begin(&self, id: Option<Value>) -> oneshot::Receiver<()> {
        let (tx, rx) = oneshot::channel();
        *self.0.lock().expect("active request lock") = Some(RunningRequest {
            id: id.unwrap_or(Value::Null),
            cancel: tx,
        });
        rx
    }

    fn clear(&self) {
        self.0.lock().expect("active request lock").take();
    }

    fn cancel(&self, id: &Value) {
        let mut active = self.0.lock().expect("active request lock");
        if active.as_ref().is_some_and(|current| current.id == *id)
            && let Some(current) = active.take()
        {
            let _ = current.cancel.send(());
        }
    }

    fn cancel_current(&self) {
        if let Some(current) = self.0.lock().expect("active request lock").take() {
            let _ = current.cancel.send(());
        }
    }
}

/// Responses and cancellation bypass the dispatch queue so they can unblock it.
async fn read_inbound<R: AsyncBufRead + Unpin>(
    mut reader: R,
    inbound: mpsc::Sender<Value>,
    pending: PendingClientRequests,
    outbound: mpsc::Sender<Value>,
    active: ActiveRequest,
) -> Result<()> {
    loop {
        let buf = match read_stdio_frame(&mut reader).await? {
            StdioFrame::Eof => return Ok(()),
            StdioFrame::TooLarge => {
                tracing::warn!(
                    limit = MAX_STDIO_LINE_BYTES,
                    "mcp.stdio.frame_too_large: dropped oversized frame"
                );
                continue;
            }
            StdioFrame::Line(buf) => buf,
        };
        let line = match std::str::from_utf8(&buf) {
            Ok(text) => text.trim(),
            Err(_) => {
                tracing::warn!("mcp.stdio.invalid_utf8: dropped frame");
                continue;
            }
        };
        if line.is_empty() {
            continue;
        }
        let message: Value = match serde_json::from_str(line) {
            Ok(value) => value,
            Err(error) => {
                outbound
                    .try_send(json_rpc_error(
                        None,
                        -32700,
                        format!("parse error: {error}"),
                    ))
                    .map_err(|_| anyhow::anyhow!("MCP output queue unavailable"))?;
                continue;
            }
        };
        if is_client_response(&message) {
            if !pending.resolve(message) {
                tracing::warn!("mcp.stdio.unmatched_response: dropped");
            }
            continue;
        }
        if message.get("method").and_then(Value::as_str) == Some("notifications/cancelled") {
            if let Some(id) = message.pointer("/params/requestId") {
                active.cancel(id);
            }
            continue;
        }
        // Awaiting capacity here would deadlock the response reader when the
        // dispatch loop is waiting for a human. Close an overloaded session.
        inbound
            .try_send(message)
            .map_err(|_| anyhow::anyhow!("MCP input queue unavailable"))?;
    }
}

/// Serialize outbound JSON-RPC messages and propagate broken-pipe errors.
async fn write_outbound<W: AsyncWrite + Unpin>(
    mut rx: mpsc::Receiver<Value>,
    mut writer: W,
) -> Result<()> {
    while let Some(message) = rx.recv().await {
        let encoded = serde_json::to_vec(&message)?;
        writer.write_all(&encoded).await?;
        writer.write_all(b"\n").await?;
        writer.flush().await?;
    }
    Ok(())
}

#[cfg(test)]
#[path = "stdio_test.rs"]
mod tests;
