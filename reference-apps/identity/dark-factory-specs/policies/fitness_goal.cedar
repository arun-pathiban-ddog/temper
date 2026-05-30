// Dark Factory — FitnessGoal Cedar Policies (ADR-0095)
//
// Default-deny. The FitnessGoal is the human's selective pressure for directed
// evolution. Defining + Activating a goal is the up-front governance beat. The
// autonomous breeder records generations against it.
//
// Anyone may read/list (the Observe UI shows the active pressures).

permit(
  principal,
  action in [Action::"read", Action::"list"],
  resource is FitnessGoal
);

// --- Define / Activate / Achieve / Retire: humans, supervisors, and the
//     verified breeder-supervisor (the autonomous run's pressure-setter) ---

permit(
  principal,
  action in [
    Action::"create",
    Action::"Define",
    Action::"Activate",
    Action::"Achieve",
    Action::"Retire"
  ],
  resource is FitnessGoal
) when {
  ["supervisor", "human", "breeder-supervisor"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};

// --- RecordGeneration: the autonomous breeder logs each completed generation ---

permit(
  principal,
  action == Action::"RecordGeneration",
  resource is FitnessGoal
) when {
  ["breeder", "breeder-supervisor", "supervisor", "human"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};
