//! Correlated server-to-client requests for human approval prompts.
use serde_json::{Value, json};
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::sync::{mpsc, oneshot};

/// In-flight server→client requests keyed by the serialized request id.
#[derive(Clone, Default)]
pub(crate) struct PendingClientRequests {
    inner: Arc<Mutex<PendingState>>,
}

#[derive(Default)]
struct PendingState {
    requests: HashMap<String, oneshot::Sender<Value>>,
    closed: bool,
}

impl PendingClientRequests {
    fn insert(&self, key: String, tx: oneshot::Sender<Value>) -> bool {
        let mut state = self.inner.lock().expect("pending map lock");
        if state.closed {
            return false;
        }
        state.requests.insert(key, tx);
        true
    }

    /// Dismiss prompts owned by a canceled call, without closing the session.
    pub(crate) async fn cancel_all(&self, outbound: &mpsc::Sender<Value>) {
        let requests = std::mem::take(&mut self.inner.lock().expect("pending map lock").requests);
        for (key, _) in requests {
            let id: Value = serde_json::from_str(&key).expect("serialized request id");
            let _ = outbound
                .send(json!({
                    "jsonrpc": "2.0", "method": "notifications/cancelled",
                    "params": {"requestId": id, "reason": "Originating tool call canceled"}
                }))
                .await;
        }
    }

    fn remove(&self, key: &str) {
        self.inner
            .lock()
            .expect("pending map lock")
            .requests
            .remove(key);
    }

    /// Route a client response to the request waiting on its id. Returns
    /// false when no request was waiting (the response is dropped).
    pub(crate) fn resolve(&self, response: Value) -> bool {
        let Some(id) = response.get("id") else {
            return false;
        };
        let key = id.to_string();
        let Some(tx) = self
            .inner
            .lock()
            .expect("pending map lock")
            .requests
            .remove(&key)
        else {
            return false;
        };
        tx.send(response).is_ok()
    }

    /// Drop every in-flight request so its awaiter fails immediately with
    /// `Closed`. Called when the client
    /// stream ends mid-elicitation.
    pub(crate) fn fail_all(&self) {
        let mut state = self.inner.lock().expect("pending map lock");
        state.closed = true;
        state.requests.clear();
    }
}

/// A JSON-RPC message from the client is a *response* to a server→client
/// request when it carries an id and a result/error but no method.
pub(crate) fn is_client_response(message: &Value) -> bool {
    message.get("method").is_none()
        && message.get("id").is_some_and(|id| !id.is_null())
        && (message.get("result").is_some() || message.get("error").is_some())
}

/// Why a server→client request produced no response.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum ClientRequestError {
    /// The outbound channel or the waiting oneshot closed (session ending).
    Closed,
    /// The client did not answer within the timeout.
    Timeout,
}

/// Handle for sending correlated JSON-RPC requests to the connected client.
///
/// Ids are allocated from a server-side counter; responses are matched back
/// through [`PendingClientRequests`] by the stdio reader task.
#[derive(Clone)]
pub(crate) struct ClientRequester {
    outbound: mpsc::Sender<Value>,
    pending: PendingClientRequests,
    next_id: Arc<AtomicU64>,
}

impl ClientRequester {
    pub(crate) fn new(outbound: mpsc::Sender<Value>, pending: PendingClientRequests) -> Self {
        Self {
            outbound,
            pending,
            next_id: Arc::new(AtomicU64::new(1)),
        }
    }

    /// Send one request to the client and await its response.
    pub(crate) async fn request(
        &self,
        method: &str,
        params: Value,
        timeout: Option<Duration>,
    ) -> Result<Value, ClientRequestError> {
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let key = Value::from(id).to_string();
        let (tx, rx) = oneshot::channel();
        if !self.pending.insert(key.clone(), tx) {
            return Err(ClientRequestError::Closed);
        }

        let request = json!({
            "jsonrpc": "2.0",
            "id": id,
            "method": method,
            "params": params,
        });
        if self.outbound.send(request).await.is_err() {
            self.pending.remove(&key);
            return Err(ClientRequestError::Closed);
        }

        let Some(timeout) = timeout else {
            // A human prompt remains actionable until the human answers or
            // the reader closes the session and calls fail_all().
            return rx.await.map_err(|_| ClientRequestError::Closed);
        };
        match tokio::time::timeout(timeout, rx).await {
            Ok(Ok(response)) => Ok(response),
            Ok(Err(_closed)) => Err(ClientRequestError::Closed),
            Err(_elapsed) => {
                self.pending.remove(&key);
                // Tell the client that this prompt is no longer actionable.
                let _ = self
                    .outbound
                    .send(json!({
                        "jsonrpc": "2.0",
                        "method": "notifications/cancelled",
                        "params": {
                            "requestId": id,
                            "reason": "Approval request expired; the decision remains pending"
                        }
                    }))
                    .await;
                Err(ClientRequestError::Timeout)
            }
        }
    }
}
