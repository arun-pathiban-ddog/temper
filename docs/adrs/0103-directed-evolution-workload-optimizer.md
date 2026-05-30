# ADR-0103: Directed Evolution — Per-Cluster Workload Optimizer

- Status: Proposed
- Date: 2026-05-30
- Deciders: Temper core maintainers
- Related:
  - ADR-0102: Workload speciation (the sibling agent — propose → ticket → executor)
  - ADR-0095: Breeding harness / cargo-test cull (the verify gate)
  - ADR-0093: Symphony (ImprovementIssue board + claude -p code implementer)
  - ADR-0100: AgentRun + scheduler
  - `reference-apps/breeder/breeder/speciate*.py`, `reference-apps/symphony/`

## Context

ADR-0102 gave us an agent that *places* workloads onto niche-tuned clusters. The
next lever is making each cluster *better at its workload* through real code/config
changes to Helix — e.g. for a high-throughput-batch cluster, tune the append/batch
path in source; for a low-latency cluster, shrink batching. Today nothing closes
that loop: the speciation agent only sets a coarse genome at cluster-build time,
and Symphony can implement code tickets but nothing *proposes* workload-specific
code improvements from telemetry + source.

We want an autonomous agent that: reads each cluster's telemetry (helix.* +
workload.*), understands its workload character, reads the Helix source, proposes
concrete code/config improvements per workload type, files a governed Symphony
ticket per cluster, and then — gated on tests + verification — implements, deploys,
and **post-deploy verifies the metric actually improved**.

Unlike speciation (which does infra: create cluster + migrate queues, work
Symphony's implementer cannot do), this agent does **code changes** — which is
exactly Symphony's wheelhouse. But we want the full code→verify→deploy→confirm
pipeline as one governed unit, so we add a dedicated executor rather than relying
on Symphony's PR-only path.

## Decision

### Sub-Decision 1: A new `optimizer` agent (propose) + `optimizer-executor` (implement)

Mirror the speciation split:

- **`optimizer`** (propose-only): a `claude -p` agent with the Datadog MCP (read
  telemetry) AND filesystem read of the Helix repo. It reads
  `helix.produce.latency_ms` etc. per cluster, characterizes each cluster's
  workload, reads the relevant source, and proposes code/config improvements.
  Files **one Symphony ticket per cluster** (scoped), auto-advanced to Implementing.

- **`optimizer-executor`** (implement): claims an optimizer ticket and runs a
  4-stage pipeline (below). It uses Symphony's proven `claude -p`
  Edit/Read/Write implementer for the edit stage, but adds verify + deploy +
  post-verify, which Symphony's PR-only path does not do.

**Why a dedicated executor (not just Symphony):** Symphony stops at "edit →
commit → PR". We want "edit → test → verify → build → deploy → confirm metric",
as one auditable governed ticket lifecycle. The executor reuses Symphony's editor
seam but owns the deploy + post-verify stages.

### Sub-Decision 2: The 4-stage executor pipeline (gated)

For each claimed ticket (one cluster):

1. **Implement** — worktree off the cluster's current branch; `claude -p`
   (`--permission-mode acceptEdits --allowedTools "Edit Read Write"`) applies the
   ticket's proposed edits to the Helix source/config.
2. **Verify (gate)** — `cargo test` (pinned `RUSTUP_TOOLCHAIN=nightly-2026-02-08`)
   + the L0-L3 spec cascade where specs changed. **If anything fails → ticket
   Failed with the error; no deploy.** This is the "assuming tests pass" gate.
3. **Build + deploy** — commit the verified change, Cloud Build the image, deploy
   to *that cluster* (the same plumbing speciation uses).
4. **Post-deploy verify** — after the rollout, re-read the target metric from
   Datadog and confirm it improved (or at least did not regress) vs. a
   pre-deploy baseline. Record the before/after on the ticket. A regression marks
   the ticket Failed (and is a candidate for rollback) rather than silently Done.

The ticket advances Implementing → Verifying → Deploying → Done, or Fails at
whichever stage broke, with the stage + reason recorded.

### Sub-Decision 3: Reuse the governance + UI patterns from ADR-0102

- Tickets are `ImprovementIssue`s marked with `TargetFile = opt://<cluster>` (vs
  speciation's `speciation://`). Symphony's `claim_issue` skips `opt://` tickets
  (the optimizer-executor owns them), just as it skips `speciation://`.
- Backend-mediated filing/advancing via the observer/supervisor/breeder token
  chain (the agent runs as breeder; breeder can't drive issues itself, the
  control plane does — same as speciation).
- Human-readable: the AgentRun summary leads with the per-cluster proposals
  (`<cluster>: <n> change(s) — <one-line rationale>`); the run-detail and the
  Symphony card render the proposals structured (target file + rationale +
  expected effect), and the ticket shows the verify/deploy/post-verify outcome.
- New scheduler agents `optimizer` + `optimizer-executor`, default OFF.

## Consequences

### Positive
- Closes the code-improvement loop: telemetry + source → governed, tested,
  deployed change → confirmed by metrics. Fully autonomous, fully auditable.
- Reuses the speciation/Symphony machinery (ticket lifecycle, claude -p editor,
  build/deploy plumbing, human-readable UI) — little net-new infrastructure.

### Negative / Risks
- **The executor edits + deploys Helix from an LLM's proposal.** The verify gate
  (cargo test + L0-L3) is the safety net; a bad edit fails the gate and never
  deploys. Default-OFF; operator-gated.
- **Post-deploy metric verification is noisy** — short windows + workload
  variance can mask or fake an improvement. Mitigation: compare against a
  pre-deploy baseline over a matched window, require a clear delta, and only
  *flag* (not auto-rollback) on regression in v1.
- Real ~8-10 min build per ticket; one ticket per executor tick.

### DST Compliance
The agents are out-of-tree Python (`reference-apps/`). The CODE they may edit is
Helix (a separate repo), gated by Helix's own `cargo test`. No
temper-runtime/jit/server changes here.

## Non-Goals
- Auto-rollback on post-deploy regression (v1 flags it; rollback is manual/future).
- Cross-cluster or fleet-wide optimizations (each ticket is one cluster).
- Replacing Symphony — Symphony still handles human/observer-filed code tickets;
  this agent owns `opt://` tickets only.

## Alternatives Considered
1. **Reuse Symphony's implementer as-is (PR only)** — rejected: it stops at PR;
   we want verify + deploy + post-verify in one governed lifecycle.
2. **Full auto-deploy with no gate** — rejected: editing+deploying Helix from an
   LLM proposal without a test gate is unsafe.
3. **One ticket per change (not per cluster)** — rejected per the operator's
   choice: per-cluster tickets keep the board readable and scoped to a workload.

## Rollback Policy
Agents default OFF; disabling the scheduler entries stops the loop. A deployed
change is a normal cluster image — roll back via the existing cluster
redeploy/rollback path. The post-verify stage flags regressions for that.
