// Dark Factory — ImprovementIssue Cedar Policies
//
// Default-deny. Role separation is enforced two ways:
//   1. permit only the right principals for each action, AND
//   2. an explicit `forbid` so the planner cannot approve their own plan and
//      the implementer cannot approve their own deploy — even if some other
//      permit would otherwise allow it (forbid wins in Cedar).
//
// Per contract docs/dark-factory-BC-contract.md §"Shared entities":
//   planner != approver; only agent_type in [supervisor, human] with
//   agentTypeVerified may ApprovePlan / ApproveDeploy.

// --- Universal: any principal can read/list ---

permit(
  principal,
  action in [Action::"read", Action::"list"],
  resource is ImprovementIssue
);

// --- Observe / Research / triage: supervisors, humans, and the system loop ---
// The Observer/Researcher (C2) creates and drives the early phases.

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
  ["supervisor", "human", "observer", "researcher"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};

// --- Planning: the assigned planner drafts/revises the plan ---

permit(
  principal,
  action in [Action::"WritePlan", Action::"RevisePlan"],
  resource is ImprovementIssue
) when {
  resource.PlannerId == principal.id
};

// --- Plan approval: supervisors/humans ONLY ---

permit(
  principal,
  action == Action::"ApprovePlan",
  resource is ImprovementIssue
) when {
  ["supervisor", "human"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};

// Role separation: the planner may NOT approve their own plan (forbid wins).
forbid(
  principal,
  action == Action::"ApprovePlan",
  resource is ImprovementIssue
) when {
  resource.PlannerId == principal.id
};

// --- Implementation: the assigned implementer (Symphony / C1) ---

permit(
  principal,
  action in [
    Action::"StartWork",
    Action::"AttachPr",
    Action::"StartVerify",
    Action::"RecordCiResult",
    Action::"MarkCiPassed"
  ],
  resource is ImprovementIssue
) when {
  resource.AssigneeId == principal.id
};

// Verification recording may also be performed by the CI service principal.
permit(
  principal,
  action in [Action::"RecordCiResult", Action::"MarkCiPassed", Action::"StartVerify"],
  resource is ImprovementIssue
) when {
  ["ci-service", "supervisor", "human"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};

// --- Deploy approval: humans/supervisors ONLY ---

permit(
  principal,
  action in [Action::"ApproveDeploy", Action::"MarkDone"],
  resource is ImprovementIssue
) when {
  ["supervisor", "human"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};

// Role separation: the implementer may NOT approve their own deploy (forbid wins).
forbid(
  principal,
  action == Action::"ApproveDeploy",
  resource is ImprovementIssue
) when {
  resource.AssigneeId == principal.id
};
