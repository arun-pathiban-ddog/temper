# ADR-0094: Dark Factory Observer/Researcher Agent (C2)

- Status: Accepted
- Date: 2026-05-24
- Deciders: Dark Factory hackathon — Agent-C2
- Related:
  - `docs/dark-factory-BC-contract.md` (the locked B/C integration contract)
  - ADR-0093: Symphony orchestrator (Agent-C1, the downstream consumer)
  - ADR-0093: Dark Factory CI/CD and deploy governance (Agent-B, owns the specs)
  - `reference-apps/observer/` (this component)
  - Helix: `helix-server/src/service/batcher.rs`, `helix-raft/src/lib.rs`

## Context

The Dark Factory is an autonomous improvement loop for the Helix log store. Agent-C2
is the "brain": it watches live Helix telemetry, forms hypotheses about performance,
runs experiments to confirm one, and creates+drives an `ImprovementIssue` through
Temper until a human approves the plan and Symphony (C1) picks up the implementation.

The component must satisfy three constraints that shape the design:

1. **The OBSERVE + RESEARCH phases must run with zero Temper dependency** (BC contract
   §"Interface stubs"). C2 can start on real Datadog data before B/C1 exist.
2. **The reasoning must be real, not hardcoded** — the "autoresearch" core has to form
   competing hypotheses and rule them out with evidence, so that if the underlying
   Helix source or telemetry changes, the conclusion changes with it.
3. **Cedar governance is law.** When an action is denied, C2 surfaces the decision and
   stops. It never self-approves and never escalates its own identity to route around
   a gate.

## Decision

### Sub-Decision 1: A three-phase package, sources behind an interface

`reference-apps/observer/` is a plain-Python package (`observer/`) with three phases:

- **OBSERVE** (`observe.py`) — pulls `helix.produce.latency_ms.{95percentile,avg,
  median,max,count}` and `helix.replication.lag`, computes a steady-state baseline vs
  burst peak, and flags a tuning opportunity when the p95 peak runs `>= 3x` over
  baseline (or any bucket `>= 50ms`). The metric source is an interface with two
  implementations:
  - `DatadogMetricSource` — hits the Datadog v1 query API directly via
    `DD_API_KEY`/`DD_APP_KEY` (the production / GKE-job path).
  - `SnapshotMetricSource` — replays a JSON snapshot that is a byte-for-byte capture of
    a live Datadog MCP `get_datadog_metric` response (the agent-driven path). This is
    how the agent feeds real telemetry into the loop without DD API keys in-process.

  **Why an interface:** in this environment, Datadog is reachable through the MCP tool
  (an OAuth bearer scoped to the MCP resource server), not through the public query
  API. The snapshot source lets the same detection logic run over genuinely-live
  numbers while keeping a real direct-API path for deployments that have keys.

- **RESEARCH** (`research.py`) — the autoresearch core. It forms three competing,
  source-grounded hypotheses and runs an experiment that **reads the current values
  straight out of the Helix source tree** (regex over `batcher.rs` and `lib.rs`),
  then scores each hypothesis against the telemetry. The discriminator is
  `helix.replication.lag`: H2/H3 are replication-path causes that require lag to climb
  during the bursts; if lag stayed at 0, they are ruled out by evidence and the
  leader-side batcher (H1) is confirmed. Every confirm/rule-out carries its evidence
  string and a `path:line` source citation.

- **DRIVE** (`driver.py` + `temper_client.py`) — builds an `ImprovementIssue` payload
  from the confirmed hypothesis and drives it through Temper:
  `create -> Observe(title,hypothesis,target_file) -> BeginPlanning() ->
  WritePlan(plan,acceptance_criteria)`, then hands off (human `ApprovePlan`, then C1
  `StartWork`). It auto-detects whether Agent-B's spec is deployed and degrades
  gracefully if not.

### Sub-Decision 2: Talk to Temper over its real HTTP data plane

The MCP `execute` tool's `temper.*` API is not wired into the agent loop as a callable,
so `TemperClient` speaks the same HTTP API the MCP sandbox uses under the hood (see
`crates/temper-sandbox/src/dispatch.rs` + `http.rs`):

- action: `POST /tdata/{Set}('{id}')/Temper.{Action}`
- create: `POST /tdata/{Set}`; patch: `PATCH /tdata/{Set}('{id}')`
- spec presence: `GET /tdata/{Set}?$top=1` (404 `EntitySetNotFound` => not deployed)
- decisions: `GET /api/tenants/{tenant}/decisions`

Identity is conveyed with `X-Tenant-Id` + the local-dev `X-Temper-*` pass-through
headers. A Cedar denial is a 403 with `error.code == "AuthorizationDenied"` and a
`PD-<uuid>` in the message; the client parses it into a `TemperDenied`.

**Why the data plane, not `/observe/specs`:** the `/observe/*` admin route is
authorization-gated per tenant (returns 403 for `dark-factory`), but the OData data
plane is the contract-sanctioned surface. Probing the entity set is also a more honest
"is it deployed" test than reading an admin index.

### Sub-Decision 3: Never escalate to read or resolve a governance decision

The tenant decisions endpoint is gated behind `manage_policies`. The agent uses **only
its own identity**. If polling for a decision's status is itself denied, `poll_decision`
returns `no_read_access` and stops — it does not assert an `admin`/operator principal
to read the decision. Approving is never attempted at all. The human resolves
everything in the Observe UI.

## Consequences

### Positive
- OBSERVE + RESEARCH are fully runnable and tested today with no B/C1 dependency.
- The conclusion is reproducible and evidence-bound: re-running re-reads live source and
  live telemetry, so it self-corrects if `linger_ms` is later tuned or if lag changes.
- The DRIVE phase works against the real `ImprovementIssue` entity now that B's spec is
  live, and degraded gracefully before it was.

### Negative
- The live agent-driven path depends on a captured snapshot rather than an in-process
  Datadog query (no public API keys in this environment). The snapshot is real data,
  refreshed by re-running the MCP query, but it is a capture, not a stream.

### Risks
- If Agent-B renames a field/action, the driver's param names drift. Mitigation: the
  driver reads action/field names from the contract verbatim and was validated against
  the live `$metadata` (Observe/BeginPlanning/WritePlan + lowercase snake_case params
  all match).
- The `dark-factory` tenant currently has no Cedar permit policy for C2 to fire
  `ImprovementIssue.Observe`; the first action is denied. This is the expected default-
  deny posture — a human grants the policy once, then the loop proceeds.

### DST Compliance
Not applicable — this is an out-of-tree Python reference app, not a simulation-visible
Rust crate. It performs read-only Datadog queries and governed Temper calls.
