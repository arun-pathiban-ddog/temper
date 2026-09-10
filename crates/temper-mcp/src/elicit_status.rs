//! Safe transport diagnostics for unresolved human approvals.
use serde_json::{Value, json};

use crate::elicit::DeniedDecision;
use crate::runtime::RuntimeContext;

#[derive(Clone, Copy)]
pub(crate) enum PendingStatus {
    Unavailable,
    Timeout,
    ConnectionClosed,
    Declined,
    Cancelled,
    InvalidResponse,
    LeftPending,
}

impl PendingStatus {
    pub(crate) fn label(self) -> &'static str {
        match self {
            Self::Unavailable => "unavailable",
            Self::Timeout => "timeout",
            Self::ConnectionClosed => "connection_closed",
            Self::Declined => "declined",
            Self::Cancelled => "cancelled",
            Self::InvalidResponse => "invalid_response",
            Self::LeftPending => "left_pending",
        }
    }
}

pub(crate) fn availability(ctx: &RuntimeContext) -> Value {
    json!({
        "enabled": ctx.elicit_approvals_enabled,
        "client_supports_elicitation": ctx.client_supports_elicitation,
        "requester_connected": ctx.requester.is_some(),
        "caller_credential_configured": ctx.api_key.is_some(),
        "approver_credential_configured": ctx.approver_key.is_some(),
    })
}

pub(crate) fn pending_annotation(
    ctx: &RuntimeContext,
    denial: &DeniedDecision,
    other_pending: &[String],
    status: PendingStatus,
) -> Value {
    let mut annotation = json!({
        "approval": "pending human decision",
        "decision_id": denial.decision_id,
        "elicitation_status": status.label(),
        "elicitation_availability": availability(ctx),
        "note": "The decision remains pending. No approval was granted; do not retry until it is resolved.",
    });
    if !other_pending.is_empty() {
        annotation["other_pending_decisions"] = json!(other_pending);
    }
    annotation
}
