// Dark Factory — Producer Cedar Policies (ADR-0097)
//
// Default-deny. A Producer is a governed Kafka load producer for the Workloads
// tab. AUTO-APPROVED workload management: there is NO human approval gate
// (unlike Deploy/ScaleOp). A verified workload-operator / supervisor / human may
// create and drive the lifecycle (Define / Start / Stop / RecordResult). The
// kubectl that creates/deletes the producer-<id> Deployment is performed by the
// app executor against the dark-factory namespace ONLY (ns + context pinned).
// The entity lifecycle + Cedar identity + audit trail ARE the governance.
//
// Mirrors ci_run.cedar (the non-gated model), not deploy/scale_op.cedar.

permit(
  principal,
  action in [Action::"read", Action::"list"],
  resource is Producer
);

// TELEMETRY FIREWALL (ADR-0099): the breeding agent is FORBIDDEN from reading
// producer shapes out of Temper — it must infer workload niches from Datadog
// telemetry alone. forbid wins, so a breeder read is denied (provable on stage).
forbid(
  principal,
  action in [Action::"read", Action::"list"],
  resource is Producer
) when {
  principal.agent_type == "breeder"
};

// Create + drive the producer lifecycle: verified workload-operator /
// supervisor / human (or symphony). agentTypeVerified must hold.
permit(
  principal,
  action in [Action::"create", Action::"Define", Action::"Start", Action::"Stop", Action::"RecordResult"],
  resource is Producer
) when {
  ["workload-operator", "supervisor", "human", "symphony"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};
