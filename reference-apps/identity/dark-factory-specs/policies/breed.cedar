// Dark Factory — Breed Cedar Policies (ADR-0099)
//
// Default-deny. A Breed is one goal-directed variant the breeding agent
// produces for a cluster/niche. The breeder drives the autonomous selection
// lifecycle (propose -> build -> verify -> promote/cull); supervisors/humans may
// also drive it. Anyone may read/list (the Breeds tab is observability).
//
// NOTE: the firewall that matters lives in queue.cedar/producer.cedar/
// consumer.cedar (breeder is DENIED read there) — the breeder may freely write
// Breeds but may NOT read the workload spec it is breeding for; it must infer
// the niche from Datadog telemetry alone.

permit(
  principal,
  action in [Action::"read", Action::"list"],
  resource is Breed
);

// Propose / create a breed: the breeder (or supervisors/humans).
permit(
  principal,
  action in [Action::"create", Action::"Propose"],
  resource is Breed
) when {
  ["breeder", "supervisor", "human"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};

// Drive the build/verify/selection lifecycle: the breeder, the deploy-service
// executor, or supervisors/humans. Promotion + culling are the selection
// outcomes — automated for the demo (breeder), human-overridable.
permit(
  principal,
  action in [Action::"StartBuild", Action::"RecordBuild", Action::"RecordResult", Action::"MarkPromoted", Action::"MarkCulled", Action::"Cull"],
  resource is Breed
) when {
  ["breeder", "deploy-service", "supervisor", "human"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};
