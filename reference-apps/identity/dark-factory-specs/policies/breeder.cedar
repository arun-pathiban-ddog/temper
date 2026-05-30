// Dark Factory — Breeder Autonomy Cedar Policies (ADR-0095)
//
// Narrow, verified auto-approval for the directed-evolution breeding harness.
// This does NOT relax the loop's human gate in general — it scopes a small set
// of permits to the two VERIFIED breeder identities issued via the real
// credential chain (register_breeders.py), so a FitnessGoal-driven autonomous
// run can self-drive plan approval WITHOUT header spoofing.
//
// Role separation is preserved:
//   - `breeder`            drives Observe -> BeginPlanning -> WritePlan (planner+implementer)
//   - `breeder-supervisor` is the distinct identity that may ApprovePlan
//   The existing `forbid (planner_id == principal.id)` in improvement_issue.cedar
//   still holds: the breeder cannot approve its own plan. Only the separate
//   breeder-supervisor can. forbid wins in Cedar.

// --- The breeder drives the early phases (C2-equivalent) ---

permit(
  principal,
  action in [
    Action::"create",
    Action::"Observe",
    Action::"BeginPlanning",
    Action::"AssignPlanner",
    Action::"Assign",
    Action::"Fail"
  ],
  resource is ImprovementIssue
) when {
  ["breeder", "breeder-supervisor"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};

// --- The breeder is the assigned planner: drafts/revises the plan ---
// (improvement_issue.cedar already permits WritePlan/RevisePlan when
//  resource.planner_id == principal.id; the breeder sets planner_id to its own
//  instance id, so that permit covers it. No extra permit needed here.)

// --- ApprovePlan: the verified breeder-supervisor auto-approves ---
// Narrow: only the breeder-supervisor agent_type, only ApprovePlan. The
// forbid(planner_id == principal.id) in improvement_issue.cedar guarantees this
// can never approve a plan the SAME identity authored.

permit(
  principal,
  action == Action::"ApprovePlan",
  resource is ImprovementIssue
) when {
  principal.agent_type == "breeder-supervisor" &&
  context.agentTypeVerified == true
};
