// Dark Factory — Migration Cedar Policies (ADR-0099)
//
// Default-deny. A Migration moves a queue's workloads from one cluster to a
// specialized target cluster (speciation). The breeding agent may request +
// drive migrations (they remain recorded, governed actions); supervisors/humans
// may too. The deploy-service executor performs the kubectl re-point. Anyone may
// read/list.
//
// NOTE: requesting a migration names a `queue` by NAME (a string the breeder got
// from telemetry tags), NOT by reading the Queue entity — the breeder is still
// Cedar-denied read on Queue/Producer/Consumer (see those policies). The
// Migration executor (deploy-service, not the breeder) is what reads workload
// entities to recreate them on the target.

permit(
  principal,
  action in [Action::"read", Action::"list"],
  resource is Migration
);

// Request a migration: the breeder (or supervisors/humans).
permit(
  principal,
  action in [Action::"create", Action::"Request"],
  resource is Migration
) when {
  ["breeder", "supervisor", "human"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};

// Approve a migration: supervisors/humans, or the breeder for auto-approved
// speciation moves (still recorded as governed). The kubectl re-point itself
// (Apply/RecordResult/land) is the deploy-service executor.
permit(
  principal,
  action == Action::"Approve",
  resource is Migration
) when {
  ["breeder", "supervisor", "human"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};

permit(
  principal,
  action in [Action::"Apply", Action::"RecordResult", Action::"MarkDone", Action::"MarkFailed"],
  resource is Migration
) when {
  ["deploy-service", "supervisor", "human"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};
