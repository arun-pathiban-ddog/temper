// Cedar policy stub — CrucibleScheduler
permit(principal, action, resource is CrucibleScheduler);

// ADR-0099: preserve read access under platform read-authz. Reads were ungated
// before read-authz landed; this broad read/list permit keeps them open so
// enabling default-deny read enforcement does not break existing read callers.
permit(
  principal,
  action in [Action::"read", Action::"list"],
  resource is CrucibleScheduler
);
