// Dark Factory — Platform / SpecRegistry Cedar Policies
//
// Spec authoring is a design-time operator action. Without an explicit permit
// the tenant's own policy set (once loaded) makes submit_specs default-deny,
// which blocks the developer from iterating on their own specs.
//
// This grants the spec-authoring operators (verified supervisors/humans and
// the admin operator) the ability to submit specs and read the registry. It
// does NOT relax any entity-level Cedar gate — ApprovePlan/ApproveDeploy and
// the deploy/CI gates remain enforced by improvement_issue.cedar / deploy.cedar
// / ci_run.cedar.

// Admin operator (developer authoring specs via the CLI/operator key).
permit(
  principal is Admin,
  action in [Action::"submit_specs", Action::"read", Action::"list"],
  resource is SpecRegistry
);

// Verified supervisors/humans may also submit specs (Observe UI / governed flow).
permit(
  principal,
  action in [Action::"submit_specs", Action::"read", Action::"list"],
  resource is SpecRegistry
) when {
  ["supervisor", "human"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};
