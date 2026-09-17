use super::*;
use wiremock::matchers::{body_json, header, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

#[tokio::test]
async fn pinned_install_uses_governed_endpoint_and_retains_decision() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/api/genesis/apps/install"))
        .and(header("x-tenant-id", "tenant-a"))
        .and(body_json(serde_json::json!({"tenant":"tenant-a", "app_ref":"owner/app@0123456789abcdef"})))
        .respond_with(ResponseTemplate::new(403).set_body_json(serde_json::json!({
            "decision_id":"PD-install-test", "error":{"code":"AuthorizationDenied", "message":"denied (decision: PD-install-test)"}
        })))
        .expect(1)
        .mount(&server).await;
    let http = reqwest::Client::new();
    let base_url = server.uri();
    let context = DispatchContext {
        http: &http,
        base_url: &base_url,
        tenant: "tenant-a",
        agent_id: Some("test-agent"),
        session_id: Some("test-session"),
        entity_set_resolver: None,
        binary_path: None,
        api_key: Some("local-test-key"),
        internal_credential_issuer: None,
        allow_host_ops: false,
    };
    let result = dispatch_temper_method(
        &context,
        "install_app",
        &[MontyObject::String("owner/app@0123456789abcdef".into())],
        &[],
    )
    .await
    .expect("structured denial");
    assert_eq!(result["decision_id"], "PD-install-test");
    assert_eq!(result["pending_decision"], "PD-install-test");
    let unpinned = dispatch_temper_method(
        &context,
        "install_app",
        &[MontyObject::String("owner/app".into())],
        &[],
    )
    .await
    .expect_err("unpinned install rejected");
    assert!(
        unpinned.contains("pinned"),
        "pin validation must precede HTTP: {unpinned}"
    );
}
