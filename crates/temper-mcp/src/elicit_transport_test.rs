//! Connection and cancellation regressions using the same test transport.
use super::*;

#[tokio::test]
async fn cancel_tool_call_dismisses_prompt_and_keeps_session_usable() {
    let (port, backend) = start_mock_backend().await;
    let (server, mut client) = wire_session(port);
    let script = async move {
        client.initialize(true).await;
        client.call_denied_action().await;
        let prompt = client.recv().await;
        client
            .send(json!({"jsonrpc":"2.0", "method":"notifications/cancelled",
            "params":{"requestId":2}}))
            .await;
        let cancellation = client.recv().await;
        assert_eq!(cancellation["method"], "notifications/cancelled");
        assert_eq!(cancellation["params"]["requestId"], prompt["id"]);
        // Even a late accept must not resolve the canceled tool's decision.
        client
            .send(json!({"jsonrpc":"2.0", "id":prompt["id"],
            "result":{"action":"accept","content":{"decision":"approve_narrow"}}}))
            .await;
        client
            .send(json!({"jsonrpc":"2.0", "id":3, "method":"tools/call",
            "params":{"name":"execute","arguments":{"code":"return 1"}}}))
            .await;
        let resumed = client.recv().await;
        assert_eq!(resumed["id"], 3);
        assert_eq!(resumed["result"]["content"][0]["text"], "1");
        drop(client);
    };
    let (result, ()) = tokio::time::timeout(Duration::from_secs(3), async {
        tokio::join!(server, script)
    })
    .await
    .expect("canceled prompt must not wedge dispatch");
    result.expect("server loop");
    assert!(backend.approve.lock().unwrap().is_none());
}

#[tokio::test]
async fn broken_output_ends_session_even_when_input_stays_open() {
    let (port, _) = start_mock_backend().await;
    let (server, client) = wire_session(port);
    let FakeClient { mut writer, reader } = client;
    drop(reader);
    writer
        .write_all(b"{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"ping\"}\n")
        .await
        .unwrap();
    let result = tokio::time::timeout(Duration::from_secs(3), server)
        .await
        .expect("writer failure must terminate the loop");
    assert!(result.is_err(), "transport failure must propagate");
    drop(writer);
}
