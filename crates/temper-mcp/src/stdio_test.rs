use super::*;
use std::pin::Pin;
use std::task::{Context, Poll};
use tokio::io::{AsyncRead, BufReader, ReadBuf};

struct BrokenReader;
impl AsyncRead for BrokenReader {
    fn poll_read(
        self: Pin<&mut Self>,
        _: &mut Context<'_>,
        _: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        Poll::Ready(Err(std::io::Error::other("injected input failure")))
    }
}

#[tokio::test]
async fn reader_failure_is_reported_by_session() {
    let ctx = RuntimeContext::from_config(&crate::McpConfig {
        temper_port: Some(1),
        temper_url: None,
        agent_id: None,
        agent_type: None,
        session_id: None,
        api_key: None,
    })
    .unwrap();
    let result = run_loop(ctx, BufReader::new(BrokenReader), tokio::io::sink()).await;
    assert!(
        result
            .unwrap_err()
            .to_string()
            .contains("injected input failure")
    );
}

#[tokio::test]
async fn saturated_queues_close_instead_of_blocking_response_reader() {
    for (input, expected) in [
        (
            "{\"id\":1,\"method\":\"ping\"}\n{\"id\":2,\"method\":\"ping\"}\n",
            "input",
        ),
        ("invalid\ninvalid\n", "output"),
    ] {
        let (in_tx, in_rx) = mpsc::channel(1);
        let (out_tx, out_rx) = mpsc::channel(1);
        let result = tokio::time::timeout(
            std::time::Duration::from_secs(1),
            read_inbound(
                input.as_bytes(),
                in_tx,
                PendingClientRequests::default(),
                out_tx,
                ActiveRequest::default(),
            ),
        )
        .await
        .expect("capacity cannot block the response reader");
        assert!(result.unwrap_err().to_string().contains(expected));
        assert!(in_rx.len() + out_rx.len() <= 1);
    }
}
