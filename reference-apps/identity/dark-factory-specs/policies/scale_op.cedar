// Dark Factory — ScaleOp Cedar Policies (ADR-0096)
//
// Default-deny. The scale gate is a governance point of the control plane:
// Approve is restricted to verified supervisors/humans. No agent_type can
// self-approve a scale — the approval must come from a human identity through
// the governance loop (the UI Approve button = the human acting as supervisor).
//
// The kubectl scale itself (Apply -> Scaling, then RecordResult) is performed by
// the control-plane executor against the dark-factory namespace ONLY (ns +
// context pinned; see ADR-0096). Mirrors deploy.cedar.

permit(
  principal,
  action in [Action::"read", Action::"list"],
  resource is ScaleOp
);

// Request a scale: implementer/CI service/symphony or supervisors/humans.
permit(
  principal,
  action in [Action::"create", Action::"Request"],
  resource is ScaleOp
) when {
  ["ci-service", "implementer", "symphony", "supervisor", "human"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};

// Approve a scale: verified supervisors/humans ONLY. This is the Cedar gate.
permit(
  principal,
  action == Action::"Approve",
  resource is ScaleOp
) when {
  ["supervisor", "human"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};

// Apply / record / land: the deploy-service executor (or supervisors/humans).
permit(
  principal,
  action in [Action::"Apply", Action::"RecordResult", Action::"MarkDone", Action::"Fail"],
  resource is ScaleOp
) when {
  ["deploy-service", "supervisor", "human"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};
