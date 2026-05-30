// Dark Factory — CIRun Cedar Policies
//
// Default-deny. A CIRun is created and started by the implementer/CI service;
// the CIRun executor (which runs cargo test) records the result as the
// ci-service principal. Anyone may read/list runs.

permit(
  principal,
  action in [Action::"read", Action::"list"],
  resource is CiRun
);

// Create + Start: implementer (Symphony/C1), the CI service, or supervisors/humans.
permit(
  principal,
  action in [Action::"create", Action::"Start"],
  resource is CiRun
) when {
  ["ci-service", "implementer", "symphony", "supervisor", "human"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};

// Record result + land terminal: the CI executor service (or supervisors/humans).
permit(
  principal,
  action in [Action::"RecordResult", Action::"MarkPassed", Action::"MarkFailed"],
  resource is CiRun
) when {
  ["ci-service", "supervisor", "human"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};
