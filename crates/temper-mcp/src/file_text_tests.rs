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
    let oversized = "é".repeat(65_537);
    for (id, body, kind, message) in [
        ("proof", "{}", "text/html", "content_type"),
        ("../proof", "{}", "application/json", "file_id"),
        ("proof?other", "{}", "application/json", "file_id"),
        ("proof", oversized.as_str(), "application/json", "131072"),
    ] {
        let (result, capture) = call_upload(&[id, body, kind], false).await;
        assert!(result.expect_err("invalid input").contains(message));
        assert!(capture.request.lock().expect("lock").is_none());
    }
}

async fn upload_through_stdio(body: String, should_succeed: bool) {
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
    let capture = Arc::new(UploadCapture::default());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind");
    let port = listener.local_addr().expect("address").port();
    let app = Router::new()
        .fallback(put(upload))
        .with_state(capture.clone());
    let backend = tokio::spawn(async move {
        axum::serve(listener, app).await.expect("serve");
    });
    let ctx = crate::runtime::RuntimeContext::from_config(&crate::McpConfig {
        temper_port: Some(port),
        temper_url: None,
        agent_id: None,
        agent_type: None,
        session_id: None,
        api_key: Some("ordinary-agent-test-key".to_owned()),
    })
    .expect("context");
    // Monty source spans use bounded columns; keep large text on short lines.
    let literals = body
        .as_bytes()
        .chunks(4096)
        .map(|chunk| {
            serde_json::to_string(std::str::from_utf8(chunk).expect("ASCII test payload"))
                .expect("Python string")
        })
        .collect::<Vec<_>>()
        .join("\n");
    let code = format!(
        "content = (\n{literals}\n)\nreturn await temper.put_file_text('evidence-tenant', 'proof', content, 'text/plain')"
    );
    let mut frame = serde_json::to_vec(&json!({"jsonrpc":"2.0", "id":1, "method":"tools/call", "params":{"name":"execute", "arguments":{"code":code}}})).expect("frame");
    assert!(
        frame.len() <= crate::trajectory_bounds::MAX_STDIO_LINE_BYTES,
        "escaped payload and envelope must fit the existing transport"
    );
    frame.push(b'\n');
    let (mut input, server_input) = tokio::io::duplex(4096);
    let (output, client_output) = tokio::io::duplex(4096);
    let server = crate::runtime::run_loop(ctx, BufReader::new(server_input), output);
    let client = async {
        input.write_all(&frame).await.expect("send frame");
        input.shutdown().await.expect("input EOF");
        let mut response = String::new();
        BufReader::new(client_output)
            .read_line(&mut response)
            .await
            .expect("tool response");
        serde_json::from_str::<serde_json::Value>(&response).expect("JSON response")
    };
    let (result, response) = tokio::time::timeout(std::time::Duration::from_secs(10), async {
        tokio::join!(server, client)
    })
    .await
    .expect("bounded stdio upload");
    result.expect("transport succeeds");
    assert_eq!(response["result"]["isError"], !should_succeed, "{response}");
    let guard = capture.request.lock().expect("capture lock");
    if should_succeed {
        let (headers, uri, uploaded) = guard
            .as_ref()
            .expect("four-argument MCP call reached File PUT");
        assert_eq!(uri, "/tdata/Files('proof')/$value");
        assert_eq!(headers["authorization"], "Bearer ordinary-agent-test-key");
        assert_eq!(headers["x-tenant-id"], "evidence-tenant");
        assert_eq!(uploaded.as_ref(), body.as_bytes());
    } else {
        assert!(guard.is_none(), "oversized body never reaches the API");
        assert!(response.to_string().contains("131072"));
    }
    backend.abort();
}

#[tokio::test]
async fn put_file_text_stdio_accepts_maximum_ascii_and_worst_escaped_text() {
    upload_through_stdio("x".repeat(128 * 1024), true).await;
    upload_through_stdio("\0".repeat(128 * 1024), true).await;
}

#[tokio::test]
async fn put_file_text_stdio_rejects_one_byte_above_payload_limit() {
    upload_through_stdio("x".repeat(128 * 1024 + 1), false).await;
}
