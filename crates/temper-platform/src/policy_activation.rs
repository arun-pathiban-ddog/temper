//! Install an approved `Policy` into the authorization engine.
//!
//! `Policy.Activate`'s hint is "Install the Cedar policy into the authorization
//! engine", and its only effect is `Emit { event: "PolicyActivated" }`. Nothing
//! consumed that event, so the governed path ran to a terminal success state and
//! granted nothing: a human approved the policy, the entity reached `Active`,
//! and the live tenant policy was unchanged. A denial gets investigated; a false
//! success does not (ARN-494).
//!
//! This is the consumer. Same merge-and-reload the operator bootstrap permit
//! already uses, driven by the entity reaching `Active` rather than by boot.

use temper_runtime::tenant::TenantId;
use temper_server::authz::persist_and_activate_policy;

use crate::operator_manage_policies::merge_cedar_statement;
use crate::state::PlatformState;

/// Watch for `Policy` rows reaching `Active` and install their statement.
pub fn spawn_policy_activation_reconciler(state: PlatformState) {
    let mut rx = state.server.event_tx.subscribe();
    tokio::spawn(async move {
        loop {
            match rx.recv().await {
                Ok(change) if change.entity_type == "Policy" => {
                    let tenant = TenantId::new(&change.tenant);
                    activate_policy_entity(&state, &tenant, &change.entity_id).await;
                }
                Ok(_) => continue,
                Err(tokio::sync::broadcast::error::RecvError::Lagged(skipped)) => {
                    // Policies are rare and activation is idempotent, but a
                    // dropped one silently un-grants something a human
                    // approved, so say so rather than continuing quietly.
                    tracing::warn!(
                        skipped,
                        "policy activation reconciler lagged; an approved policy may not be installed"
                    );
                }
                Err(tokio::sync::broadcast::error::RecvError::Closed) => {
                    tracing::info!("policy activation reconciler stopping (broadcast closed)");
                    return;
                }
            }
        }
    });
}

async fn activate_policy_entity(state: &PlatformState, tenant: &TenantId, entity_id: &str) {
    let Ok(found) = state
        .server
        .get_tenant_entity_state(tenant, "Policy", entity_id)
        .await
    else {
        return;
    };
    if found.state.status != "Active" {
        return;
    }
    let statement = found
        .state
        .fields
        .get("cedar_statement")
        .and_then(serde_json::Value::as_str)
        .unwrap_or_default()
        .trim()
        .to_string();
    if statement.is_empty() {
        tracing::warn!(
            tenant = %tenant,
            entity_id,
            "Policy reached Active with an empty cedar_statement; nothing to install"
        );
        return;
    }

    let tenant_str = tenant.as_str();
    let existing = state
        .server
        .authz
        .get_tenant_policy_text(tenant_str)
        .filter(|text| !text.trim().is_empty())
        .or_else(|| {
            state
                .server
                .tenant_policies
                .read()
                .ok()
                .and_then(|policies| policies.get(tenant_str).cloned())
        })
        .unwrap_or_default();
    let merged = merge_cedar_statement(&existing, &statement);
    if merged == existing {
        // Already present — activation is idempotent by design, because the
        // reconciler re-runs on every Policy transition.
        return;
    }

    // Reload first: a statement that does not parse must not be persisted as
    // active, and the entity should not be left claiming it is in force.
    if let Err(error) = state
        .server
        .authz
        .reload_tenant_policies(tenant_str, &merged)
    {
        tracing::error!(
            tenant = tenant_str,
            entity_id,
            error = %error,
            "approved Policy did not compile; it is NOT in force"
        );
        return;
    }
    if let Ok(mut policies) = state.server.tenant_policies.write() {
        policies.insert(tenant_str.to_string(), merged.clone());
    }
    persist_and_activate_policy(
        &state.server,
        tenant_str,
        entity_id,
        &statement,
        "policy-activation",
    )
    .await;

    tracing::info!(
        tenant = tenant_str,
        entity_id,
        "approved Policy installed into the authorization engine"
    );
}
