//! Keep the authorization engine agreeing with the `Policy` rows.
//!
//! `Policy.Activate`'s hint is "Install the Cedar policy into the authorization
//! engine", and its only effect is `Emit { event: "PolicyActivated" }`. Nothing
//! consumed that event, so the governed path ran to a terminal success state and
//! granted nothing: a human approved the policy, the entity reached `Active`,
//! and the live tenant policy was unchanged. A denial gets investigated; a false
//! success does not (ARN-494).
//!
//! This is the consumer, and it is a *reconciler* rather than an activate-only
//! hook, because three things go wrong with the naive version:
//!
//! - **Revocation.** A row leaving `Active` has to take its statement back out.
//!   Installing on activate and never removing leaves a revoked policy in force,
//!   which is the same false success pointing the other way.
//! - **A dropped event.** The broadcast channel lags under load. A missed
//!   `PolicyActivated` used to be logged and abandoned, so an approved policy
//!   stayed uninstalled until something else happened to touch it.
//! - **Rows that were already `Active`.** Nothing announces them to a task that
//!   subscribes afterwards, so a restart left them uninstalled.
//!
//! So: every `Policy` change is applied by re-reading the row and making the
//! engine match it, and a lag or a fresh start triggers a sweep of every policy
//! row in the tenants whose policy set this process tracks.

use temper_runtime::tenant::TenantId;
use temper_server::authz::record_policy_change;

use crate::operator_manage_policies::merge_cedar_statement;
use crate::state::PlatformState;

/// Who the durable policy record is attributed to when this reconciler writes.
const WRITER: &str = "policy-activation";

/// Watch `Policy` rows and keep the authorization engine matching them.
pub fn spawn_policy_activation_reconciler(state: PlatformState) {
    let mut rx = state.server.event_tx.subscribe();
    tokio::spawn(async move {
        // Rows that reached Active before this task subscribed are never
        // announced on the channel. Sweep once so a restart installs them.
        reconcile_tracked_tenants(&state).await;

        loop {
            match rx.recv().await {
                Ok(change) if change.entity_type == "Policy" => {
                    let tenant = TenantId::new(&change.tenant);
                    apply_policy_entity(&state, &tenant, &change.entity_id).await;
                }
                Ok(_) => continue,
                Err(tokio::sync::broadcast::error::RecvError::Lagged(skipped)) => {
                    // A dropped Policy event silently un-grants something a
                    // human approved, so catch up rather than continuing.
                    tracing::warn!(
                        skipped,
                        "policy activation reconciler lagged; sweeping every tracked tenant"
                    );
                    reconcile_tracked_tenants(&state).await;
                }
                Err(tokio::sync::broadcast::error::RecvError::Closed) => {
                    tracing::info!("policy activation reconciler stopping (broadcast closed)");
                    return;
                }
            }
        }
    });
}

/// Sweep every tenant whose policy set this process already tracks.
///
/// There is no enumeration of tenants in the kernel, so the sweep covers the
/// tenants in `tenant_policies` — the ones whose policy text has been loaded.
/// A tenant outside that set still converges through its own `Policy` events;
/// what the sweep adds is recovery for the tenants this process is serving.
async fn reconcile_tracked_tenants(state: &PlatformState) {
    // Tenants come from the durable policy store FIRST. Reading them out of
    // `tenant_policies` alone would enumerate recovery targets from the very
    // in-memory state this sweep exists to rebuild: a Policy that reached
    // Active durably before this consumer wrote anything leaves no trace there,
    // so after a restart its tenant would never be visited and the approved
    // policy would stay uninstalled.
    let mut tenants: std::collections::BTreeSet<String> = std::collections::BTreeSet::new();
    if let Some(store) = state.server.policy_store()
        && let Ok(rows) = store.load_all_policies().await
    {
        tenants.extend(rows.into_iter().map(|row| row.tenant));
    }
    if let Ok(policies) = state.server.tenant_policies.read() {
        tenants.extend(policies.keys().cloned());
    }
    let tenants: Vec<String> = tenants.into_iter().collect();

    for tenant in tenants {
        let tenant = TenantId::new(&tenant);
        let ids = state.server.list_entity_ids_lazy(&tenant, "Policy").await;
        for entity_id in ids {
            apply_policy_entity(state, &tenant, &entity_id).await;
        }
    }
}

/// Read one `Policy` row and make the engine agree with it.
async fn apply_policy_entity(state: &PlatformState, tenant: &TenantId, entity_id: &str) {
    let Ok(found) = state
        .server
        .get_tenant_entity_state(tenant, "Policy", entity_id)
        .await
    else {
        return;
    };

    let statement = found
        .state
        .fields
        .get("cedar_statement")
        .and_then(serde_json::Value::as_str)
        .unwrap_or_default()
        .trim()
        .to_string();

    let should_be_in_force = found.state.status == "Active";

    if statement.is_empty() {
        if should_be_in_force {
            tracing::warn!(
                tenant = %tenant,
                entity_id,
                "Policy reached Active with an empty cedar_statement; nothing to install"
            );
        }
        return;
    }

    if !should_be_in_force
        && !this_row_owns_the_statement(state, tenant, entity_id, &statement).await
    {
        // Two rows can carry the same statement. Removing the text because one
        // was revoked would silently un-grant the other, which is still Active
        // and still approved. Drop this row's durable record and leave the
        // engine alone.
        disable_durable_record(state, tenant, entity_id).await;
        tracing::info!(
            tenant = %tenant,
            entity_id,
            "revoked Policy does not solely own its statement (shared with another \
             policy row, or never activated); the live policy text is unchanged"
        );
        return;
    }

    set_statement_in_force(state, tenant, entity_id, &statement, should_be_in_force).await;
}

/// Did THIS row put the statement into the live text, and is it the only source?
///
/// Ownership is the durable policy row, not text containment. Two things go
/// wrong when you infer ownership from "the live text contains this string":
///
/// - The same statement may have come from somewhere else entirely -- the
///   bootstrap permits, the legacy policy blob, another Policy row. Removing it
///   because one Policy was revoked deletes a rule nobody revoked, and if that
///   statement was a `forbid`, revoking a Policy quietly lifts a restriction.
/// - A row that was never activated owns nothing, so a revoke or reject that
///   never passed through Active must not remove text it never contributed.
///
/// So: this row owns the statement only if it has an enabled durable row of its
/// own, and no other enabled row carries the same text.
async fn this_row_owns_the_statement(
    state: &PlatformState,
    tenant: &TenantId,
    entity_id: &str,
    statement: &str,
) -> bool {
    let Some(store) = state.server.policy_store() else {
        // With no durable store there is no ownership record to consult, and
        // guessing from text is exactly what this function exists to avoid.
        return false;
    };
    let Ok(rows) = store.load_policies_for_tenant(tenant.as_str()).await else {
        return false;
    };

    let owns_enabled_row = rows
        .iter()
        .any(|row| row.policy_id == entity_id && row.enabled);
    if !owns_enabled_row {
        return false;
    }
    !rows
        .iter()
        .any(|row| row.policy_id != entity_id && row.enabled && row.cedar_text.contains(statement))
}

/// Stop a revoked policy's durable row from being reinstalled at the next boot.
async fn disable_durable_record(state: &PlatformState, tenant: &TenantId, entity_id: &str) {
    let Some(store) = state.server.policy_store() else {
        return;
    };
    if let Err(error) = store
        .toggle_policy_enabled(tenant.as_str(), entity_id, false)
        .await
    {
        tracing::error!(
            tenant = %tenant,
            entity_id,
            error = %error,
            "revoked Policy could not be disabled durably; the next boot may reinstall it"
        );
    }
}

/// Add or remove one statement from the tenant's live policy text.
async fn set_statement_in_force(
    state: &PlatformState,
    tenant: &TenantId,
    entity_id: &str,
    statement: &str,
    in_force: bool,
) {
    let tenant_str = tenant.as_str();
    let existing = live_policy_text(state, tenant_str);
    let desired = if in_force {
        merge_cedar_statement(&existing, statement)
    } else {
        remove_cedar_statement(&existing, statement)
    };

    if desired == existing {
        // Already in the state this row asks for. The reconciler re-runs on
        // every Policy transition and on every sweep, so this is the common
        // path and must stay cheap and silent.
        return;
    }

    // Reload before persisting: a statement that does not compile must not be
    // recorded as active, and the row must not be left claiming to be in force.
    if let Err(error) = state
        .server
        .authz
        .reload_tenant_policies(tenant_str, &desired)
    {
        tracing::error!(
            tenant = tenant_str,
            entity_id,
            in_force,
            error = %error,
            "Policy change did not compile; the authorization engine is unchanged"
        );
        return;
    }
    write_tracked_policy_text(state, tenant_str, &desired);

    // The engine has changed. If the durable write then fails, the running
    // process would enforce a rule that no restart would reproduce, so put the
    // engine back rather than leaving the two disagreeing.
    if let Some(store) = state.server.policy_store() {
        // Installing writes the row; revoking DISABLES it. Saving the statement
        // on the way out would leave an enabled durable policy that the next
        // boot loads straight back in, so a revoked policy would come back to
        // life on restart.
        let persisted = if in_force {
            store
                .save_policy(tenant_str, entity_id, statement, WRITER)
                .await
        } else {
            store
                .toggle_policy_enabled(tenant_str, entity_id, false)
                .await
        };
        match persisted {
            Ok(changed) => {
                if changed {
                    record_policy_change(&state.server, tenant_str, entity_id, WRITER);
                }
            }
            Err(error) => {
                tracing::error!(
                    tenant = tenant_str,
                    entity_id,
                    in_force,
                    error = %error,
                    "Policy could not be persisted; rolling the authorization engine back"
                );
                if let Err(rollback) = state
                    .server
                    .authz
                    .reload_tenant_policies(tenant_str, &existing)
                {
                    tracing::error!(
                        tenant = tenant_str,
                        entity_id,
                        error = %rollback,
                        "rollback of the authorization engine FAILED; \
                         the live policy set no longer matches the durable one"
                    );
                } else {
                    write_tracked_policy_text(state, tenant_str, &existing);
                }
                return;
            }
        }
    }

    if in_force {
        tracing::info!(
            tenant = tenant_str,
            entity_id,
            "approved Policy installed into the authorization engine"
        );
    } else {
        tracing::info!(
            tenant = tenant_str,
            entity_id,
            "revoked Policy removed from the authorization engine"
        );
    }
}

fn live_policy_text(state: &PlatformState, tenant: &str) -> String {
    state
        .server
        .authz
        .get_tenant_policy_text(tenant)
        .filter(|text| !text.trim().is_empty())
        .or_else(|| {
            state
                .server
                .tenant_policies
                .read()
                .ok()
                .and_then(|policies| policies.get(tenant).cloned())
        })
        .unwrap_or_default()
}

fn write_tracked_policy_text(state: &PlatformState, tenant: &str, text: &str) {
    if let Ok(mut policies) = state.server.tenant_policies.write() {
        policies.insert(tenant.to_string(), text.to_string());
    }
}

/// Take one statement back out of a tenant's policy text.
///
/// The inverse of `merge_cedar_statement`, and it shares that function's
/// assumption: a statement is identified by its exact text. Merge refuses to
/// add a statement the text already `contains`, so removal takes out the same
/// span, then tidies the blank line the removal leaves behind.
fn remove_cedar_statement(existing: &str, statement: &str) -> String {
    let statement = statement.trim();
    if statement.is_empty() || !existing.contains(statement) {
        return existing.trim_end().to_string();
    }

    let mut remaining = existing.replace(statement, "");
    while remaining.contains("\n\n\n") {
        remaining = remaining.replace("\n\n\n", "\n\n");
    }
    remaining.trim().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    const APP: &str = r#"permit(principal, action == Action::"read", resource is Issue);"#;
    const APPROVED: &str =
        r#"permit(principal, action == Action::"write", resource is Issue) when { true };"#;

    #[test]
    fn removing_a_statement_is_the_inverse_of_merging_it() {
        let merged = merge_cedar_statement(APP, APPROVED);
        assert!(merged.contains(APPROVED), "merge puts the statement in");

        let removed = remove_cedar_statement(&merged, APPROVED);
        assert!(
            !removed.contains(APPROVED),
            "a revoked statement must not survive: {removed}"
        );
        assert!(
            removed.contains("resource is Issue"),
            "the rest of the tenant's policy text is untouched: {removed}"
        );
        assert_eq!(
            removed,
            APP.trim(),
            "removing what was merged returns the original text"
        );
    }

    #[test]
    fn removing_a_statement_that_is_not_there_changes_nothing() {
        assert_eq!(remove_cedar_statement(APP, APPROVED), APP.trim());
        assert_eq!(remove_cedar_statement(APP, "   "), APP.trim());
        assert_eq!(remove_cedar_statement("", APPROVED), "");
    }

    #[test]
    fn removal_is_idempotent() {
        let merged = merge_cedar_statement(APP, APPROVED);
        let once = remove_cedar_statement(&merged, APPROVED);
        assert_eq!(remove_cedar_statement(&once, APPROVED), once);
    }

    #[test]
    fn the_last_statement_can_be_removed_leaving_an_empty_policy_set() {
        let only = merge_cedar_statement("", APPROVED);
        assert_eq!(only, APPROVED.trim());
        assert_eq!(remove_cedar_statement(&only, APPROVED), "");
    }
}
