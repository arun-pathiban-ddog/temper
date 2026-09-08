use super::*;
use temper_runtime::persistence::EventStore;

#[tokio::test]
async fn existing_child_initializer_compares_logical_blob_values() {
    let parent = r#"
[automaton]
name = "Order"
states = ["Draft", "Submitted"]
initial = "Draft"
[[action]]
name = "SpawnChild"
from = ["Draft"]
to = "Submitted"
params = ["child_id", "expected"]
effect = [{type="spawn",entity_type="Customer",entity_id_source="child_id",initial_action="Initialize"}]
"#;
    let child = r#"
[automaton]
name = "Customer"
states = ["Draft", "Ready"]
initial = "Draft"
strict_action_params = true
[[state]]
name = "Name"
type = "string"
initial = ""
[[action]]
name = "Write"
from = ["Draft"]
params = ["Name"]
[[action]]
name = "Initialize"
from = ["Draft"]
to = "Ready"
params = ["expected"]
[[action.constraints]]
kind = "param_equals_field"
param = "expected"
field = "Name"
"#;
    let dir = tempfile::tempdir().unwrap();
    let store = temper_store_turso::TursoEventStore::new(
        dir.path().join("events.db").to_str().unwrap(),
        None,
    )
    .await
    .unwrap();
    let (mut state, _) = common::build_single_tenant_state(
        0,
        "existing-child",
        "default",
        &[("Order", parent), ("Customer", child)],
    );
    state.data_dir = dir.path().to_path_buf();
    state.set_storage_stack(temper_server::StorageStack::from_turso(store));
    state
        .authz
        .reload_tenant_policies("default", "permit(principal, action, resource);")
        .unwrap();
    let tenant = TenantId::default();
    let large = "N".repeat(512 * 1024);
    let written = state
        .dispatch_tenant_action(
            &tenant,
            "Customer",
            "child",
            "Write",
            json!({"Name":large}),
            &Default::default(),
        )
        .await
        .unwrap();
    assert!(written.success);
    let descriptor = written.state.fields["Name"].clone();
    assert!(descriptor.is_object(), "fixture did not overflow to a blob");
    state
        .get_or_create_tenant_entity(&tenant, "Order", "parent", json!({}))
        .await
        .unwrap();
    let mut events = state.entity_observe_tx.subscribe();
    state
        .dispatch_tenant_action(
            &tenant,
            "Order",
            "parent",
            "SpawnChild",
            json!({"child_id":"child", "expected":large}),
            &Default::default(),
        )
        .await
        .unwrap();
    tokio::time::timeout(std::time::Duration::from_secs(3), async {
        for _ in 0..32 {
            let event = events.recv().await.unwrap();
            assert_ne!(
                event.event_name, "integration_callback_rejected",
                "{event:?}"
            );
            if event.entity_id == "child" && event.data["action"] == "Initialize" {
                return;
            }
        }
        panic!("existing child initializer never completed");
    })
    .await
    .unwrap();
}

#[tokio::test]
async fn invalid_absent_action_contracts_leave_no_entity_or_persistence() {
    let spec = SPEC
        .replace(
            "params = [\"Notes\"]",
            r#"params = ["Notes", "expected"]
[[action.constraints]]
kind = "param_nonempty"
param = "Notes"
[[action.constraints]]
kind = "param_equals_field"
param = "expected"
field = "revision"
"#,
        )
        .replace(
            "[[action]]",
            r#"[[state]]
name = "revision"
type = "counter"
initial = "7"
[[action]]"#,
        );
    for (index, body) in [
        json!({"Notes":"valid","expected":7,"extra":true}),
        json!({"Notes":"valid"}),
        json!({"Notes":"","expected":7}),
        json!({"Notes":"valid","expected":6}),
    ]
    .into_iter()
    .enumerate()
    {
        let store = temper_store_sim::SimEventStore::no_faults(467 + index as u64);
        let mut state = state_with_spec(common::CSDL_XML, &spec);
        state.set_storage_stack(temper_server::StorageStack::from_sim(store.clone(), None));
        assert_eq!(
            request(
                &state,
                "POST",
                "/tdata/Orders('absent')/Temper.SubmitOrder",
                body
            )
            .await,
            StatusCode::CONFLICT
        );
        assert_eq!(
            state.active_actor_count(),
            0,
            "invalid input materialized an actor"
        );
        assert!(!state.entity_exists(&TenantId::default(), "Order", "absent"));
        assert!(
            store
                .read_events("default:Order:absent", 0)
                .await
                .unwrap()
                .is_empty()
        );
        assert!(
            store
                .load_snapshot("default:Order:absent")
                .await
                .unwrap()
                .is_none()
        );
        assert_eq!(
            request(
                &state,
                "POST",
                "/tdata/Orders('absent')/Temper.SubmitOrder",
                json!({"Notes":"valid","expected":7})
            )
            .await,
            StatusCode::OK
        );
        assert_eq!(state.active_actor_count(), 1);
    }
}

#[tokio::test]
async fn rejected_child_initializer_leaves_no_child_or_persistence() {
    let parent = r#"
[automaton]
name = "Order"
states = ["Draft", "Submitted"]
initial = "Draft"
[[action]]
name = "SpawnChild"
from = ["Draft"]
to = "Submitted"
params = ["child_id", "payload"]
effect = [{type="spawn",entity_type="Customer",entity_id_source="child_id",initial_action="Initialize"}]
"#;
    let child = r#"
[automaton]
name = "Customer"
states = ["Draft", "Ready"]
initial = "Draft"
strict_action_params = true
[[action]]
name = "Initialize"
from = ["Draft"]
to = "Ready"
params = ["payload"]
[[action.constraints]]
kind = "param_nonempty"
param = "payload"
"#;
    let (mut state, _) = common::build_single_tenant_state(
        0,
        "refused-child",
        "default",
        &[("Order", parent), ("Customer", child)],
    );
    let store = temper_store_sim::SimEventStore::no_faults(467);
    state.set_storage_stack(temper_server::StorageStack::from_sim(store.clone(), None));
    state
        .authz
        .reload_tenant_policies("default", "permit(principal, action, resource);")
        .unwrap();
    let tenant = TenantId::default();
    state
        .get_or_create_tenant_entity(&tenant, "Order", "parent", json!({}))
        .await
        .unwrap();
    let mut events = state.entity_observe_tx.subscribe();
    state
        .dispatch_tenant_action(
            &tenant,
            "Order",
            "parent",
            "SpawnChild",
            json!({"child_id":"child","payload":""}),
            &Default::default(),
        )
        .await
        .unwrap();
    tokio::time::timeout(std::time::Duration::from_secs(2), async {
        for _ in 0..32 {
            let event = events.recv().await.unwrap();
            if event.entity_id == "parent" && event.event_name == "integration_callback_rejected" {
                assert_eq!(event.data["action"], "Initialize");
                return;
            }
        }
        panic!("initializer refusal was not observable");
    })
    .await
    .unwrap();
    assert_eq!(
        state.active_actor_count(),
        1,
        "rejected initializer created a child"
    );
    assert!(!state.entity_exists(&tenant, "Customer", "child"));
    assert!(
        store
            .read_events("default:Customer:child", 0)
            .await
            .unwrap()
            .is_empty()
    );
    assert!(
        store
            .load_snapshot("default:Customer:child")
            .await
            .unwrap()
            .is_none()
    );
}
