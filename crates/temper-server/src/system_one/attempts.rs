//! Immutable attempt reservation prevents resampling after structural spec edits.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use temper_jit::table::TransitionTable;
use temper_runtime::persistence::{EventMetadata, PersistenceEnvelope, PersistenceError};
use temper_runtime::scheduler::{sim_now, sim_uuid};
use temper_runtime::tenant::TenantId;

use super::evidence::json_digest;
use super::table_digest;
use crate::entity_actor::EntityState;
use crate::entity_actor::effects::entity_authorization_precondition;
use crate::storage::BoxedEventStore;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct AttemptBinding {
    principal: String,
    action: String,
    params_digest: String,
    expected_table: String,
    expected_state: String,
}

fn attempt_id(tenant: &TenantId, state: &EntityState, attempt: &str) -> Result<String, String> {
    let identity = serde_json::json!({"entity_type":state.entity_type,"entity_id":state.entity_id,"attempt":attempt});
    Ok(format!(
        "{tenant}:__SystemOneAttempt:{}",
        json_digest(&identity)?
    ))
}

fn binding(
    table: &TransitionTable,
    state: &EntityState,
    action: &str,
    params: &Value,
    principal: &str,
) -> Result<AttemptBinding, String> {
    Ok(AttemptBinding {
        principal: principal.into(),
        action: action.into(),
        params_digest: json_digest(params)?,
        expected_table: table_digest(table)?,
        expected_state: entity_authorization_precondition(state),
    })
}

async fn read_attempt(store: &BoxedEventStore, id: &str) -> Result<Option<AttemptBinding>, String> {
    let events = store
        .read_events(id, 0)
        .await
        .map_err(|_| "cannot read system_one attempt")?;
    if events.is_empty() {
        return Ok(None);
    }
    if events.len() != 1 || events[0].sequence_nr != 1 || events[0].event_type != "SystemOneAttempt"
    {
        return Err("invalid system_one attempt journal".into());
    }
    serde_json::from_value(events[0].payload.clone())
        .map(Some)
        .map_err(|_| "invalid system_one attempt binding".into())
}

fn check(saved: &AttemptBinding, current: &AttemptBinding, completed: bool) -> Result<(), String> {
    if saved.principal != current.principal
        || saved.action != current.action
        || saved.params_digest != current.params_digest
        || saved.expected_table != current.expected_table
        || (!completed && saved.expected_state != current.expected_state)
    {
        return Err("system_one action attempt was reused with different or stale inputs".into());
    }
    Ok(())
}

/// Validate any prior guarded attempt, including when a hot reload removed guards.
#[allow(clippy::too_many_arguments)]
pub(crate) async fn check_attempt(
    store: &BoxedEventStore,
    tenant: &TenantId,
    table: &TransitionTable,
    state: &EntityState,
    action: &str,
    params: &Value,
    principal: &str,
    attempt: &str,
    completed: bool,
) -> Result<bool, String> {
    let id = attempt_id(tenant, state, attempt)?;
    let Some(saved) = read_attempt(store, &id).await? else {
        return Ok(false);
    };
    check(
        &saved,
        &binding(table, state, action, params, principal)?,
        completed,
    )?;
    Ok(true)
}

/// Reserve an action attempt before inference; competing reservations must agree.
#[allow(clippy::too_many_arguments)]
pub(crate) async fn bind_attempt(
    store: &BoxedEventStore,
    tenant: &TenantId,
    table: &TransitionTable,
    state: &EntityState,
    action: &str,
    params: &Value,
    principal: &str,
    attempt: &str,
) -> Result<(), String> {
    if check_attempt(
        store, tenant, table, state, action, params, principal, attempt, false,
    )
    .await?
    {
        return Ok(());
    }
    let id = attempt_id(tenant, state, attempt)?;
    let current = binding(table, state, action, params, principal)?;
    let envelope = PersistenceEnvelope {
        sequence_nr: 1,
        event_type: "SystemOneAttempt".into(),
        payload: serde_json::to_value(&current)
            .map_err(|_| "cannot serialize system_one attempt")?,
        metadata: EventMetadata {
            event_id: sim_uuid(),
            causation_id: sim_uuid(),
            correlation_id: sim_uuid(),
            timestamp: sim_now(),
            actor_id: id.clone(),
        },
    };
    match store.append(&id, 0, &[envelope]).await {
        Ok(1) => Ok(()),
        Err(PersistenceError::ConcurrencyViolation { .. }) => {
            let saved = read_attempt(store, &id)
                .await?
                .ok_or("system_one attempt conflict without reservation")?;
            check(&saved, &current, false)
        }
        _ => Err("cannot persist system_one action attempt".into()),
    }
}
