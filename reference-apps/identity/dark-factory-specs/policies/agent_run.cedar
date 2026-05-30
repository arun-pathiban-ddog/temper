// Dark Factory — AgentRun Cedar Policies (ADR-0100)
//
// An AgentRun is one pass of a loop agent (observer|researcher|symphony|
// breeder). The agent records its own run; the Runs tab reads them. Anyone may
// read/list (observability). NOTE: read authorization is now enforced
// platform-wide (ADR-0099), so this read permit is required for the Runs tab to
// load — the breeder's workload-read firewall does NOT extend to AgentRun (a
// breeder may freely record + read its own runs).

permit(
  principal,
  action in [Action::"read", Action::"list"],
  resource is AgentRun
);

// Create + record + land a run: the four loop agent types, or supervisors/humans.
permit(
  principal,
  action in [Action::"create", Action::"Start", Action::"RecordSummary", Action::"Finish", Action::"Fail"],
  resource is AgentRun
) when {
  ["observer", "researcher", "symphony", "breeder", "supervisor", "human"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};
