// Dark Factory — Queue Cedar Policies (ADR-0097)
//
// Default-deny. A Queue is a governed Kafka topic for the Workloads tab.
// AUTO-APPROVED workload management: there is NO human approval gate (unlike
// Deploy/ScaleOp). A verified workload-operator / supervisor / human may create
// and drive the lifecycle (Define / Activate / Delete). The entity lifecycle +
// Cedar identity + audit trail ARE the governance. Anyone may read/list.
//
// Mirrors ci_run.cedar (the non-gated model), not deploy/scale_op.cedar.

permit(
  principal,
  action in [Action::"read", Action::"list"],
  resource is Queue
);

// TELEMETRY FIREWALL (ADR-0099): the breeding agent is FORBIDDEN from reading
// workload shapes out of Temper. It must infer workload niches from Datadog
// telemetry alone. forbid wins over the permit above, so a breeder read attempt
// is denied (403 + a GovernanceDecision in the Observe UI) — making the
// "inferred from telemetry only" claim provable, not asserted.
forbid(
  principal,
  action in [Action::"read", Action::"list"],
  resource is Queue
) when {
  principal.agent_type == "breeder"
};

// Create + drive the queue lifecycle: verified workload-operator / supervisor /
// human (or symphony). No agent_type self-asserts; agentTypeVerified must hold.
permit(
  principal,
  action in [Action::"create", Action::"Define", Action::"Activate", Action::"Delete"],
  resource is Queue
) when {
  ["workload-operator", "supervisor", "human", "symphony"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};
