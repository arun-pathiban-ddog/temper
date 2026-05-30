// Dark Factory — Cluster Cedar Policies
//
// Default-deny. Standing up a NEW Helix cluster is a governed, human-gated
// action: Approve is restricted to verified supervisors/humans. No agent_type
// can self-approve a cluster — the approval must come from a human identity
// through the governance loop (surface decision_id, poll, retry). Same gate
// shape as deploy.cedar (ADR-0096, ADR-0098).
//
// The build + kubectl-apply itself (Apply -> Building, RecordBuild -> Deploying,
// RecordResult -> MarkLive) is performed by the control-plane backend against
// the dark-factory namespace ONLY, deploying a per-cluster StatefulSet
// (helix-<name>) built from the cluster's own git worktree.

permit(
  principal,
  action in [Action::"read", Action::"list"],
  resource is Cluster
);

// Define / request a cluster: implementer/CI service/symphony or supervisors/humans.
permit(
  principal,
  action in [Action::"create", Action::"Define"],
  resource is Cluster
) when {
  ["ci-service", "implementer", "symphony", "supervisor", "human"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};

// Approve a cluster: verified supervisors/humans ONLY. This is the Cedar gate.
permit(
  principal,
  action == Action::"Approve",
  resource is Cluster
) when {
  ["supervisor", "human"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};

// Apply / build / deploy / land / teardown: the deploy-service backend
// (or supervisors/humans). Never agents on their own.
permit(
  principal,
  action in [Action::"Apply", Action::"RecordBuild", Action::"RecordResult", Action::"MarkLive", Action::"MarkFailed", Action::"Teardown"],
  resource is Cluster
) when {
  ["deploy-service", "supervisor", "human"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};
