// Dark Factory — Deploy Cedar Policies
//
// Default-deny. The deploy gate is THE governance point of the loop:
// Approve is restricted to verified supervisors/humans. No agent_type can
// self-approve a rollout — the approval must come from a human identity
// through the governance loop (surface decision_id, poll, retry).
//
// The kubectl rollout itself (Apply -> Rolling, then RecordResult) is performed
// by the deploy-service executor against the dark-factory namespace ONLY
// (dry-run by default; see ADR-0093 and executors/deploy_executor.py).

permit(
  principal,
  action in [Action::"read", Action::"list"],
  resource is Deploy
);

// Request a deploy: implementer/CI service or supervisors/humans.
permit(
  principal,
  action in [Action::"create", Action::"Request"],
  resource is Deploy
) when {
  ["ci-service", "implementer", "symphony", "supervisor", "human"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};

// Approve a deploy: verified supervisors/humans ONLY. This is the Cedar gate.
permit(
  principal,
  action == Action::"Approve",
  resource is Deploy
) when {
  ["supervisor", "human"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};

// Apply / record / land: the deploy-service executor (or supervisors/humans).
permit(
  principal,
  action in [Action::"Apply", Action::"RecordResult", Action::"MarkLive", Action::"Rollback"],
  resource is Deploy
) when {
  ["deploy-service", "supervisor", "human"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};
