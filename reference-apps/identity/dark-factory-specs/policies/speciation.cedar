// Dark Factory — Speciation Cedar Policies (ADR-0102)
//
// Default-deny. A Speciation is the governed convergence ledger for autonomous
// workload speciation: the breeder classifies queues into a niche FROM TELEMETRY
// ONLY, isolates that niche onto a niche-tuned cluster, and migrates the matching
// queues onto it. The breeder (or supervisors/humans) may propose + drive a
// speciation; supervisors/humans too. Anyone may read/list.
//
// FIREWALL (ADR-0099): this policy grants NO read/list on Queue/Producer/Consumer.
// The breeder names queues by string (from telemetry tags), never by reading the
// Queue entity — it remains Cedar-denied on workload entities (see
// queue.cedar / producer.cedar / consumer.cedar). The actual workload re-point is
// performed by the deploy-service executor via the linked Migration entities, not
// by the breeder.

permit(
  principal,
  action in [Action::"read", Action::"list"],
  resource is Speciation
);

// Propose / open a speciation: the breeder (or supervisors/humans).
permit(
  principal,
  action in [Action::"create", Action::"Propose"],
  resource is Speciation
) when {
  ["breeder", "supervisor", "human"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};

// Drive the speciation lifecycle: the breeder for autonomous runs (still recorded
// as governed actions), or supervisors/humans.
permit(
  principal,
  action in [
    Action::"ProvisionCluster",
    Action::"RecordCluster",
    Action::"BeginMigration",
    Action::"RecordMigrations",
    Action::"MarkConverged",
    Action::"MarkFailed"
  ],
  resource is Speciation
) when {
  ["breeder", "supervisor", "human"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};
