//! File evidence text uses the ordinary authenticated stream boundary.
use axum::{
    Router,
    body::Bytes,
    extract::State,
    http::{HeaderMap, StatusCode, Uri},
    routing::put,
};
use monty::MontyObject;
use serde_json::json;
use std::sync::{Arc, Mutex};
use temper_sandbox::dispatch::{DispatchContext, dispatch_temper_method};

#[derive(Default)]
struct UploadCapture {
    request: Mutex<Option<(HeaderMap, String, Bytes)>>,
    denied: bool,
}

async fn upload(
    State(capture): State<Arc<UploadCapture>>,
    headers: HeaderMap,
    uri: Uri,
    body: Bytes,
) -> (StatusCode, &'static str) {
    *capture.request.lock().expect("capture lock") = Some((headers, uri.to_string(), body));
    if capture.denied {
        (
            StatusCode::FORBIDDEN,
            r#"{"error":{"code":"AuthorizationDenied","message":"Authorization denied for update on File('proof'). Decision PD-upload created."}}"#,
        )
    } else {
        (StatusCode::NO_CONTENT, "")
    }
}

async fn call_upload(
    values: &[&str],
    denied: bool,
) -> (Result<serde_json::Value, String>, Arc<UploadCapture>) {
    let capture = Arc::new(UploadCapture {
        denied,
        ..Default::default()
    });
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind");
    let url = format!("http://{}", listener.local_addr().expect("address"));
    let app = Router::new()
        .fallback(put(upload))
        .with_state(capture.clone());
    let server = tokio::spawn(async move {
        axum::serve(listener, app).await.expect("serve");
    });
    let http = reqwest::Client::new();
    let context = DispatchContext {
        http: &http,
        base_url: &url,
        tenant: "evidence-tenant",
        agent_id: None,
        session_id: Some("session-test"),
        entity_set_resolver: None,
        binary_path: None,
        api_key: Some("ordinary-agent-test-key"),
        internal_credential_issuer: None,
        allow_host_ops: false,
    };
    let args: Vec<_> = values
        .iter()
        .map(|s| MontyObject::String((*s).to_owned()))
        .collect();
    let result = tokio::time::timeout(
        std::time::Duration::from_secs(5),
        dispatch_temper_method(&context, "put_file_text", &args, &[]),
    )
    .await
    .expect("bounded request");
    server.abort();
    (result, capture)
}

#[tokio::test]
async fn put_file_text_preserves_bytes_and_caller_identity_without_host_access() {
    let body = "{\"proof\":\"évidence\\nready\"}";
    let (result, capture) = call_upload(&["proof", body, "application/json"], false).await;
    assert_eq!(result.expect("stream update succeeds"), json!(null));
    let guard = capture.request.lock().expect("capture lock");
    let (headers, uri, uploaded) = guard.as_ref().expect("request received");
    assert_eq!(uri, "/tdata/Files('proof')/$value");
    assert_eq!(headers["authorization"], "Bearer ordinary-agent-test-key");
    assert_eq!(headers["x-tenant-id"], "evidence-tenant");
    assert_eq!(headers["x-session-id"], "session-test");
    assert_eq!(headers["content-type"], "application/json");
    assert_eq!(uploaded.as_ref(), body.as_bytes());
}

#[tokio::test]
async fn put_file_text_preserves_denial_for_human_elicitation() {
    let (result, _) = call_upload(&["proof", "{}", "application/json"], true).await;
    let result = result.expect("structured denial");
    assert_eq!(result["status"], "authorization_denied");
    assert_eq!(result["decision_id"], "PD-upload");
    assert!(crate::elicit::denial_from_dispatch_value("evidence-tenant", &result).is_some());
}

#[tokio::test]
async fn put_file_text_rejects_unsafe_metadata_and_oversized_content_before_request() {
    let oversized = "é".repeat(524_289);
    for (id, body, kind, message) in [
        ("proof", "{}", "text/html", "content_type"),
        ("../proof", "{}", "application/json", "file_id"),
        ("proof?other", "{}", "application/json", "file_id"),
        ("proof", oversized.as_str(), "application/json", "1048576"),
    ] {
        let (result, capture) = call_upload(&[id, body, kind], false).await;
        assert!(result.expect_err("invalid input").contains(message));
        assert!(capture.request.lock().expect("lock").is_none());
    }
}
