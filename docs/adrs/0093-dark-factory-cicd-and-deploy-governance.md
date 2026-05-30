# ADR-0093: Dark Factory CI/CD and Deploy Governance Capability

- Status: Accepted
- Date: 2026-05-24
- Deciders: Temper core maintainers (Dark Factory hackathon, Track B)
- Related:
  - `docs/dark-factory-BC-contract.md` (the Track B/C integration contract — single source of truth)
  - ADR-0046 (unified `[[action.triggers]]`, removal of `is_system → Allow` Cedar bypass)
  - ADR-0031 (Agent Orchestration OS App — pattern for executor-backed entities)
  - `os-apps/project-management/policies/issue.cedar` (role-separation pattern this ADR mirrors)
  - `os-apps/dark-factory-cicd/` (the specs this ADR governs)

## Context

The "Dark Factory for Helix" demonstrates an autonomous improvement loop: an
Observer/Researcher agent (Track C2) reads live Datadog telemetry, forms a
hypothesis, and files an `ImprovementIssue`; a Symphony orchestrator (Track C1)
implements the fix in an isolated Helix git worktree and opens a PR; and a
CI/CD + governance capability (Track B, this ADR) verifies the branch and — only
after a human approves — rolls the change out to the live Helix StatefulSet on
GKE.

For the loop to be safe and demonstrable we need three Temper entities that
encode the workflow as verified state machines rather than ad-hoc scripts:

1. **`ImprovementIssue`** — the tracker that replaces Symphony's external issue
   system. It threads the whole pipeline (Observed → … → Done/Failed) and is the
   hand-off point between C2, C1, and B.
2. **`CIRun`** — a verification capability. Each run is `cargo test` (+ where
   DST/TLA would run) against a Helix branch, with a pass/fail recorded back.
3. **`Deploy`** — a Cedar-gated deploy capability. A deploy may not roll out to
   the cluster until a human approves it through the governance loop.

Without these as specs, every track would invent its own state tracking in
chat/markdown, the deploy gate would be a code-level `if` instead of a Cedar
policy, and there would be no audit trail of who approved what. The contract
fixes the entity/field/action names so the three tracks can build in parallel;
this ADR records the design decisions behind Track B's slice.

## Decision

### Sub-Decision 1: Three entities, one OS app (`dark-factory-cicd`)

The three entities live in a single OS app directory
(`os-apps/dark-factory-cicd/`) with the standard layout: `app.toml`, one
`specs/*.ioa.toml` per entity, one shared `specs/model.csdl.xml`, and one
`policies/*.cedar` per entity. This mirrors `project-management` and
`agent-orchestration` so the catalog loader and `submit_specs` treat it
identically.

The CSDL field set is copied **verbatim** from the contract §"Shared entities".
The contract is the lock; this ADR does not get to rename fields. Any field I
had to add to make the state machine verify is flagged in §Consequences and was
reported to the coordinator.

**Why this approach**: parallelism. C1 and C2 code against the contract names
before the specs are live; when B's specs deploy under tenant `dark-factory`,
they point at real entities with no rename churn.

### Sub-Decision 2: `ImprovementIssue` lifecycle and role separation

States: `Observed → Researching → Planned → Implementing → Verifying →
Deploying → Done`, plus a terminal `Failed` reachable from any active state.

Planning role separation mirrors `project-management/policies/issue.cedar` but
is **stricter**: the planner may NOT approve their own plan, and the implementer
may NOT approve their own deploy. The contract explicitly requires
`planner != approver`, so unlike the PM app (which treats self-approval as a
norm) we encode a Cedar `forbid` that fires when
`resource.PlannerId == principal.id` on `ApprovePlan`, and only
`agent_type in [supervisor, human]` with `context.agentTypeVerified == true`
may `ApprovePlan` / `ApproveDeploy`.

Boolean state vars (`has_plan`, `assignee_set`, `planner_set`, `ci_passed`,
`deploy_approved`) gate transitions and back invariants — e.g. `Deploying`
requires `ci_passed`, `Planned` requires `has_plan`. These let the verification
cascade prove that the issue can never reach `Deploying` without a passing CI
run, and can never be `Done` without having deployed.

### Sub-Decision 3: `CIRun` and `Deploy` as executor-backed capabilities

`CIRun`: `Pending → Running → Passed | Failed`. `Start` moves to `Running` and
is the signal for the **CIRun executor** (a shell/Python runner) to clone/check
out the Helix branch, run `cargo test --workspace`, note where DST and TLA model
checking would run, and call `RecordResult(status, report)` which lands the run
in `Passed` or `Failed`.

`Deploy`: `Pending → Approved → Rolling → Live | RolledBack`. `Request` creates
the deploy in `Pending`; `Approve` (Cedar-gated to humans) moves it to
`Approved`; `Apply` moves it to `Rolling` and is the signal for the **Deploy
executor** to run `kubectl set image` / `kubectl rollout` against the
`dark-factory` namespace only; `RecordResult` lands it in `Live` or
`RolledBack`.

**Why executors instead of WASM `http_fetch`**: `cargo test` and `kubectl` are
local processes against a local repo and a kubeconfig context — not HTTP
endpoints. They run as out-of-band runners that poll Temper for entities in the
trigger state, execute, and call back the recording action. This keeps the
spec's runtime contract pure (state + Cedar) and matches ADR-0031's
adapter-style execution. The executors are the only components allowed to touch
the Helix repo and the GKE cluster.

### Sub-Decision 4: Deploy safety — dry-run by default

The live Helix StatefulSet is shared by every other track during the hackathon.
The Deploy executor therefore **defaults to `kubectl --dry-run=client`** and
refuses a real rollout unless `DARK_FACTORY_DEPLOY_FOR_REAL=1` is set in its
environment. The kubectl context, namespace, and StatefulSet name are pinned
constants (`gke_datadog-sandbox_us-west3_gs-us-west3`, `dark-factory`, `helix`);
the executor asserts the namespace before issuing any command and never accepts
a namespace from entity data.

## Rollout Plan

1. **Phase 0 (this slice)** — Author the three specs + Cedar policies on disk,
   submit them to the running `dark-factory` tenant, iterate to PASS the
   verification cascade. Wire both executors as scripts; CIRun executor runs for
   real against Helix, Deploy executor runs dry-run only.
2. **Phase 1 (integration)** — Coordinator confirms specs are live; C1/C2 point
   at the real entities. End-to-end: C2 files an `ImprovementIssue`, drives it
   to `Implementing`, C1 attaches a PR, B's CIRun verifies, human approves a
   Deploy, dry-run rollout fires.
3. **Phase 2 (real rollout, gated)** — Only with explicit human sign-off, flip
   `DARK_FACTORY_DEPLOY_FOR_REAL=1` for a single demo deploy.

## Consequences

### Positive
- The whole improvement loop is a verified state machine with a Cedar audit
  trail; no track tracks pipeline state in markdown.
- The deploy gate is policy, not code — a human approval is structurally
  required before any cluster mutation.
- CIRun/Deploy are reusable capabilities beyond Helix.

### Negative
- Executors are out-of-band pollers, not in-process triggers, so there is a poll
  latency between a state change and execution. Acceptable for a demo.

### Risks
- A misconfigured Deploy executor could touch the wrong namespace. Mitigated by
  pinned constants, a namespace assertion, and dry-run-by-default with an
  explicit env-var override.

### DST Compliance
- No simulation-visible Rust crates change. The specs are data; the executors
  are external shell/Python scripts outside the deterministic core. No
  `// determinism-ok` annotations needed.

## Contract fields added / changed (FLAGGED — affects other agents)

The contract CSDL is the lock. To make the state machines verify and to give the
executors something to act on, Track B added the following **nullable** fields
(additive only — no field renamed or removed, so C1/C2 code keeps working):

- `ImprovementIssue.UpdatedAt` (DateTimeOffset) — bookkeeping; optional.
- `CIRun.ExitCode` (Int32) — lets the executor record the raw `cargo test`
  exit code alongside the human-readable `Report`.
- `Deploy.RolloutCmd` (String) — the exact kubectl command the executor ran
  (or would have run, in dry-run), for the audit trail.
- `Deploy.DryRun` (Boolean, default true) — records whether the rollout was a
  dry-run. Real rollout requires both human approval AND the executor's
  `DARK_FACTORY_DEPLOY_FOR_REAL` env flag.

All four are optional/defaulted; consumers that ignore them are unaffected.

## Non-Goals

- Multi-cluster or multi-namespace deploys. `dark-factory` namespace only.
- Automatic rollback orchestration beyond recording a `RolledBack` result.
- Replacing the PM OS app — `ImprovementIssue` is a purpose-built tracker for
  the Helix loop, not a general issue tracker.

## Alternatives Considered

1. **Reuse `project-management`'s `Issue` entity as the tracker** — rejected:
   the contract pins a different state set (`Observed → … → Deploying`) and
   different fields (`Hypothesis`, `CiStatus`, `BranchName`); shoehorning would
   break the contract lock and confuse the other tracks.
2. **In-spec WASM `http_fetch` triggers to run CI/deploy** — rejected: `cargo
   test` and `kubectl` are local processes, not HTTP calls; an HTTP shim would
   add a moving part with no benefit for a demo.
3. **No Cedar gate on Deploy, rely on the executor to refuse** — rejected:
   defeats the entire point. The approval must come from a human identity
   through the governance loop, not a code-level guard the agent could bypass.

## Rollback Policy

Specs are hot-reloaded, not deployed via CI. To roll back, resubmit the prior
spec version (git-tracked) via `submit_specs`; existing entities keep their
state. The Deploy executor's real-rollout path is off unless the env flag is
set, so disabling it is removing one environment variable.
