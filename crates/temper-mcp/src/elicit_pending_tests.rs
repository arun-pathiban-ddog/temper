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
