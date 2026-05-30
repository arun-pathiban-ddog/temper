// Cedar policy stub — SessionSchedule
permit(principal, action, resource is SessionSchedule);

// ADR-0099: preserve read access under platform read-authz. Reads were ungated
// before read-authz landed; this broad read/list permit keeps them open so
// enabling default-deny read enforcement does not break existing read callers.
permit(
  principal,
  action in [Action::"read", Action::"list"],
  resource is SessionSchedule
);
