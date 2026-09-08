use super::*;

#[tokio::test]
async fn self_callback_stops_at_the_runtime_budget_before_the_spec_guard() {
    let spec = r#"
[automaton]
name = "Job"
states = ["Idle"]
initial = "Idle"
strict_action_params = true
[[state]]
name = "ticks"
type = "counter"
initial = "0"
[[action]]
name = "Tick"
from = ["Idle"]
params = []
guard = "ticks < 12"
effect = [{type="increment",var="ticks"},{type="trigger",name="local_job"}]
[[integration]]
name = "local_job"
trigger = "local_job"
type = "wasm"
module = "local_job"
on_success = "Tick"
"#;
    let mut registry = SpecRegistry::new();
    registry.register_tenant(
        "default",
        parse_csdl(CSDL).unwrap(),
        CSDL.into(),
        &[("Job", spec)],
    );
    let state = ServerState::from_registry(ActorSystem::new("callback-budget"), registry);
    state
        .authz
        .reload_tenant_policies("default", "permit(principal, action, resource);")
        .unwrap();
    let payload = json!({"action":"Tick","params":{},"success":true}).to_string();
    let data = payload
        .bytes()
        .map(|byte| format!("\\{byte:02x}"))
        .collect::<String>();
    let wat = format!(
        r#"(module
        (import "env" "host_set_result" (func $result (param i32 i32)))
        (memory (export "memory") 1)
        (data (i32.const 0) "{data}")
        (func (export "run") (param i32 i32) (result i32)
          i32.const 0 i32.const {} call $result i32.const 0))"#,
        payload.len()
    );
    let hash = state.wasm_engine.compile_and_cache(wat.as_bytes()).unwrap();
    let tenant = TenantId::default();
    state
        .wasm_module_registry
        .write()
        .unwrap()
        .register(&tenant, "local_job", &hash);
    let result = tokio::time::timeout(
        std::time::Duration::from_secs(10),
        state.dispatch(temper_server::state::DispatchCommand {
            tenant: &tenant,
            entity_type: "Job",
            entity_id: "job",
            action: "Tick",
            params: json!({}),
            agent_ctx: &Default::default(),
            await_integration: true,
            await_reactions: true,
        }),
    )
    .await
    .expect("self callback did not terminate")
    .unwrap();
    assert!(!result.success);
    let actual = state
        .get_tenant_entity_state(&tenant, "Job", "job")
        .await
        .unwrap()
        .state;
    assert!(
        actual.counters["ticks"] <= 9,
        "callback chain bypassed runtime budget: {}",
        actual.counters["ticks"]
    );
    assert!(
        result
            .error
            .as_deref()
            .is_some_and(|error| error.contains("callback depth")),
        "{:?}",
        result.error
    );
}

#[tokio::test]
async fn failed_compensation_chain_keeps_the_callback_budget() {
    let spec = r#"
[automaton]
name = "Job"
states = ["Idle"]
initial = "Idle"
strict_action_params = true
[[state]]
name = "ticks"
type = "counter"
initial = "0"
[[action]]
name = "Fail"
from = ["Idle"]
params = ["error"]
guard = "ticks < 12"
effect = [{type="increment",var="ticks"},{type="trigger",name="failing_job"}]
[[integration]]
name = "failing_job"
trigger = "failing_job"
type = "wasm"
module = "failing_job"
"#;
    let mut registry = SpecRegistry::new();
    registry.register_tenant(
        "default",
        parse_csdl(CSDL).unwrap(),
        CSDL.into(),
        &[("Job", spec)],
    );
    let state = ServerState::from_registry(ActorSystem::new("compensation-budget"), registry);
    state
        .authz
        .reload_tenant_policies("default", "permit(principal, action, resource);")
        .unwrap();
    let hash = state.wasm_engine.compile_and_cache(br#"(module (memory (export "memory") 1) (func (export "run") (param i32 i32) (result i32) unreachable))"#).unwrap();
    let tenant = TenantId::default();
    state
        .wasm_module_registry
        .write()
        .unwrap()
        .register(&tenant, "failing_job", &hash);
    let result = state
        .dispatch(temper_server::state::DispatchCommand {
            tenant: &tenant,
            entity_type: "Job",
            entity_id: "job",
            action: "Fail",
            params: json!({"error":"local fixture"}),
            agent_ctx: &Default::default(),
            await_integration: false,
            await_reactions: true,
        })
        .await
        .unwrap();
    assert!(result.success);
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(2);
    loop {
        let ticks = state
            .get_tenant_entity_state(&tenant, "Job", "job")
            .await
            .unwrap()
            .state
            .counters["ticks"];
        assert!(
            ticks <= 9,
            "failure compensation bypassed callback budget: {ticks}"
        );
        if tokio::time::Instant::now() >= deadline {
            assert!(ticks > 1);
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    }
}
