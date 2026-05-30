// Dark Factory — Agent Identity Registry Cedar Policies
//
// Governs the credential-registry bootstrap (ADR-0033). The operator/admin
// (the global TEMPER_API_KEY holder, i.e. the integrator acting as the human
// who provisions verified agent identities) may define agent types and issue
// credentials through the governed OData write path. This is the legitimate
// identity-management gate — it does NOT let any agent self-issue a credential
// (only Admin / verified supervisors), and it does NOT relax any of the loop's
// entity-level gates (ImprovementIssue / CIRun / Deploy).
//
// Read/list is open so the IdentityResolver and tooling can introspect.

// --- AgentType: admin operator manages the type registry ---

permit(
  principal is Admin,
  action,
  resource is AgentType
);

// Verified supervisors/humans may also manage agent types (governed flow).
permit(
  principal,
  action in [Action::"create", Action::"Define", Action::"Update", Action::"Deprecate", Action::"Reactivate"],
  resource is AgentType
) when {
  ["supervisor", "human"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};

permit(
  principal,
  action in [Action::"read", Action::"list"],
  resource is AgentType
);

// --- AgentCredential: admin operator issues/rotates/revokes credentials ---

permit(
  principal is Admin,
  action,
  resource is AgentCredential
);

// Verified supervisors/humans may also issue credentials (governed flow).
permit(
  principal,
  action in [Action::"create", Action::"Issue", Action::"Rotate", Action::"Revoke"],
  resource is AgentCredential
) when {
  ["supervisor", "human"].contains(principal.agent_type) &&
  context.agentTypeVerified == true
};

permit(
  principal,
  action in [Action::"read", Action::"list"],
  resource is AgentCredential
);
