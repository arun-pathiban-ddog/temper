# ADR-0101: Directed Evolution — Intent-driven goals + instrument-to-measure loop

- Status: Proposed
- Date: 2026-05-25
- Deciders: Temper core maintainers
- Related:
  - ADR-0099: Breeds (the FitnessGoal scope + breeder this redesigns)
  - ADR-0100: AgentRun (the runs that record this activity)
  - ADR-0094: Observer/Researcher (Datadog sensing this extends to metric discovery)
  - ADR-0093: Symphony (the real coding-agent that adds the metric)
  - `helix-server/src/metrics.rs` (the file edited to add a metric)
  - memory: directed-evolution-sigelman-framing (this is the observability-evolves angle)

## Context

The first plain-English-goal cut mapped a human's description onto a **fixed
vocabulary of four metrics** (`throughput`, `p95_latency`, `tail_latency`,
`cost_efficiency`) because the breeder and `FitnessGoal` only understood those.
This is backwards: it makes metrics the master and intent the servant. A goal the
system cannot already measure is rejected, and the agent can never pursue a novel
objective. The "LLM parse" was really keyword-matching onto a closed set.

The desired model inverts it: **intent is open-ended, and metrics are discovered
or created to serve it.** The human states what they want in natural language;
the agent figures out which existing telemetry bears on it, and — crucially —
when the goal references something **not currently measured (a blindspot)**, the
agent *adds the instrumentation*: a real edit to Helix's metrics, shipped through
the existing governed loop, so a brand-new metric starts flowing in Datadog and
can then be optimized against. The system evolves its own observability.

## Decision

### Sub-Decision 1: FitnessGoal carries open-ended Intent + a dynamic metric set

Add `Intent` (free-form NL) to `FitnessGoal`. The structured `Metric/Direction/
Target` stop being a required closed enum; instead the goal tracks a **growable
set of metrics** (`Metrics`, JSON) that serve the intent — existing ones found by
discovery, plus any created to close blindspots. A goal is no longer rejected for
referencing an unmeasured objective; that triggers metric creation instead.

**Why**: makes intent primary. The metric set is derived state that grows, not a
gate the intent must pass.

### Sub-Decision 2: Metric-coverage discovery (real Datadog, real reasoning)

Given an intent, the agent queries Datadog's **available metrics**
(`search_datadog_metrics` / `get_datadog_metric_context` — metric *catalog*, not
just values) and uses Claude to judge: which existing metrics serve this intent,
and is there a blindspot (the intent needs something nothing measures)? Output:
`{relevant_metrics[], blindspot?, proposed_metric}`. This replaces the map-to-4
parse entirely.

**Why**: this is the real "the agent explores Datadog and reasons about coverage"
step. It uses the metric catalog (what *can* be measured) not just current values.

### Sub-Decision 3: Instrument-to-measure loop (fully live)

On a blindspot, the agent closes it through the **existing governed loop**:
file an `ImprovementIssue` proposing the new metric → Symphony's real coding
agent (`claude -p`) edits `helix-server/src/metrics.rs` to emit a new `helix.*`
metric → CIRun verifies → Deploy to the live `helix-cluster-001` (real Cloud
Build, ~8-10 min) → the new metric appears in Datadog → it joins the goal's
metric set and the breeder optimizes against it.

**Why**: adding a metric *is* a Helix code change, which is exactly what the
Observer→Symphony→Deploy chain already does — so closing a blindspot reuses the
whole governed, verified, human-gated pipeline. Fully live (the user accepted the
~10-min deploy) makes the loop genuinely real, not simulated.

### Sub-Decision 4: Breeder + fitness read the dynamic metric set

The breeder's niche inference + fitness evaluation read the goal's `Metrics` set
(existing + newly-created) rather than the hardcoded four.

## Rollout Plan

1. **Phase 0 (this change)** — FitnessGoal Intent + dynamic Metrics (L0-L3);
   metric-coverage discovery; instrument-to-measure wiring; breeder reads dynamic
   set; intent goal UI. Replaces the fixed-vocab parse.
2. **Phase 1** — pre-warm/queue tactics for the slow deploy leg in the demo.

## Consequences

### Positive
- Open-ended goals; the system creates the observability it lacks.
- The strongest "directed evolution" beat: evolving not just toward metrics but
  evolving the metrics. Realizes the Sigelman "fitness functions" vision concretely.
- Reuses the entire governed loop for metric creation (no new mechanism).

### Negative
- The metric-creation leg is a real ~8-10 min Cloud Build — slow for a live demo.
- More moving parts; the discovery step depends on Claude + Datadog catalog access.

### Risks
- **Deploy latency** dominates the live loop. Mitigation: discovery + proposal +
  Symphony edit + CIRun run fast/live; pre-warm a metric for the hero moment.
- **LLM judgment** of coverage/blindspot can be wrong. Mitigation: the human sees
  the discovered metrics + proposed new metric and approves (the ImprovementIssue
  plan gate already exists) before any code ships.
- **Helix must stay up** (helix-cluster-001, currently 3/3) to deploy + observe.

### DST Compliance
Not applicable — FitnessGoal spec (L0-L3), the deploy-history reference app, and
the breeder/observer Python. No simulation-visible Rust crate. (The Helix
*metric edit* is in the helix repo, a separate codebase, not temper's sim crates.)

## Non-Goals
- Auto-approving the metric-adding code change — it flows through the existing
  human plan gate on the ImprovementIssue.
- A general metric-synthesis engine — the agent proposes one concrete `helix.*`
  metric per blindspot, in the established DogStatsD pattern.

## Alternatives Considered
1. **Keep fixed vocabulary, widen the list** — still rejects novel goals, no
   blindspot detection, no metric creation. Rejected (it's the thing being fixed).
2. **Discovery + proposal only (no creation)** — surfaces the blindspot but stops
   short of closing it; loses the instrument-to-measure payoff. Rejected per the
   "fully live" decision.

## Rollback Policy
The Intent field is additive (nullable). To roll back, the UI reverts to the
structured add-goal form and the discovery/creation path is removed; existing
goals (which still have Metric/Direction/Target) keep working.
