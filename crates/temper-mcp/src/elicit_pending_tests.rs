//! Pending outcomes remain observable without resolving backend decisions.
use super::*;

async fn check_pending_reply(reply: Value, expected: &str, upload_file: bool) {
    let (port, backend) = start_mock_backend().await;
    let (server, mut client) = wire_session(port);
    let script = async move {
        client.initialize(true).await;
        if upload_file {
            client.send(json!({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": { "name": "execute", "arguments": {
                    "code": "return await temper.put_file_text('demo', 'proof', '{}', 'application/json')"
                }}
            })).await;
        } else {
            client.call_denied_action().await;
        }
        let elicitation = client.recv().await;
        assert_eq!(elicitation["method"], "elicitation/create");
        let mut reply = reply;
        reply["jsonrpc"] = json!("2.0");
        reply["id"] = elicitation["id"].clone();
        client.send(reply).await;
        let response = client.recv().await;
        let result = tool_result_json(&response);
        assert_eq!(result["status"], "authorization_denied");
        assert_eq!(result["decision_id"], "PD-test123");
        assert_eq!(result["approval"], "pending human decision");
        assert_eq!(result["elicitation_status"], expected);
        drop(client);
    };
    let (server_result, ()) = tokio::join!(server, script);
    server_result.expect("server loop");
    assert!(backend.approve.lock().expect("lock").is_none());
    assert!(backend.deny.lock().expect("lock").is_none());
}

#[tokio::test]
async fn pending_response_reports_cancel_invalid_and_leave_pending() {
    for (reply, expected) in [
        (json!({"result":{"action":"cancel"}}), "cancelled"),
        (
            json!({"result":{"action":"accept","content":{"decision":"leave_pending"}}}),
            "left_pending",
        ),
        (
            json!({"result":{"action":"accept","content":{"decision":"unknown"}}}),
            "invalid_response",
        ),
        (
            json!({"error":{"code":-32601,"message":"unsupported method"}}),
            "invalid_response",
        ),
    ] {
        check_pending_reply(reply, expected, false).await;
    }
}

#[tokio::test]
async fn file_upload_denial_reaches_human_elicitation_through_mcp() {
    check_pending_reply(json!({"result":{"action":"decline"}}), "declined", true).await;
}

#[tokio::test]
async fn full_request_queue_still_delivers_human_reply() {
    let (port, backend) = start_mock_backend().await;
    let (server, mut client) = wire_session(port);
    let script = async move {
        client.initialize(true).await;
        client.call_denied_action().await;
        let elicitation = client.recv().await;
        for id in 10..26 {
            client
                .send(json!({"jsonrpc":"2.0", "id":id, "method":"ping"}))
                .await;
        }
        client
            .send(json!({"jsonrpc":"2.0", "id":elicitation["id"], "result":{"action":"decline"}}))
            .await;
        let response = client.recv().await;
        assert_eq!(
            tool_result_json(&response)["elicitation_status"],
            "declined"
        );
        for id in 10..26 {
            assert_eq!(client.recv().await["id"], id);
        }
        drop(client);
    };
    let (result, ()) = tokio::time::timeout(Duration::from_secs(10), async {
        tokio::join!(server, script)
    })
    .await
    .expect("human reply bypasses full queue");
    result.expect("clean EOF");
    assert!(backend.approve.lock().expect("lock").is_none());
    assert!(backend.deny.lock().expect("lock").is_none());
}

#[tokio::test]
async fn request_queue_overflow_ends_pending_elicitation_with_error() {
    let (port, backend) = start_mock_backend().await;
    let (server, mut client) = wire_session(port);
    let script = async move {
        client.initialize(true).await;
        client.call_denied_action().await;
        let elicitation = client.recv().await;
        assert_eq!(elicitation["method"], "elicitation/create");
        for id in 10..42 {
            client
                .send(json!({"jsonrpc":"2.0", "id":id, "method":"ping"}))
                .await;
        }
        drop(client);
    };
    let (result, ()) = tokio::time::timeout(Duration::from_secs(10), async {
        tokio::join!(server, script)
    })
    .await
    .expect("overflow ends promptly");
    assert!(
        result.is_err(),
        "unbounded queue must not accept the complete burst"
    );
    assert!(backend.approve.lock().expect("lock").is_none());
    assert!(backend.deny.lock().expect("lock").is_none());
}
