# ADR-0099: Directed Evolution — Breeds (telemetry-only breeding agent, global/per-cluster goals)

- Status: Proposed
- Date: 2026-05-25
- Deciders: Temper core maintainers
- Related:
  - ADR-0095: Directed Evolution breeding harness (lineages, FitnessGoal, the cull)
  - ADR-0098: Governed multi-cluster Helix deploy from per-cluster worktrees (the Cluster entity this builds on)
  - ADR-0097: Directed Evolution workloads (Queue/Producer/Consumer + the tagged metrics)
  - ADR-0093: Symphony orchestrator (the ImprovementIssue a breed's motivation links to)
  - `reference-apps/identity/dark-factory-specs/` (entities + Cedar)
  - `reference-apps/deploy-history/app.py` (control-plane backend + Breeds tab)
  - memory: directed-evolution-sigelman-framing (the industry-vision framing this realizes)

## Context

Directed Evolution today can breed a *lineage* toward a single FitnessGoal and
cull lethal mutations (ADR-0095), and can stand up independent Helix clusters
from worktrees (ADR-0098). What it cannot yet do is operate as a **breeding
program over a population of clusters**: discover that different queues have
different *workload shapes*, spin up clusters specialized for each niche, move
load to them, and run per-niche selective pressure under a system-wide budget.

This is the concrete realization of Sigelman's "natural selection in production"
(see memory): a population of variants coexisting in production, each evaluated
by fitness functions, with humans setting the pressure and pulling the system
toward it. The distinctive, demonstrable claim we want to make is that the
**breeding agent discovers niches from telemetry alone** — it is *architecturally
forbidden* from reading the workload spec out of Temper — so its niche inference
is real, not a lookup.

We need: global + per-cluster fitness goals; a governed unit of work (a "breed")
with motivation and result; a way to migrate a queue to a specialized cluster;
and a hard, provable telemetry firewall on the breeding agent.

## Decision

### Sub-Decision 1: The breeding agent infers workload shapes from telemetry ONLY — Cedar-enforced

The `breeder` agent type (already registered via `register_breeders.py`) gets
Cedar policies that **deny `read`/`list` on `Queue`, `Producer`, and `Consumer`**
entities. The breeder reaches **Datadog only** (the metrics API / MCP) and infers
workload niches from the already-tagged metrics — `queue:`, `workload_id:`,
`role:` tags on `workload.producer.*` / `workload.consumer.*`, plus server-side
`helix.produce.latency_ms` / `helix.consume.*`. From those it clusters queues
into niches (e.g. *high-throughput batch* vs *high-fanout latency-sensitive*).

**Why this approach**: the firewall is the credibility of the whole story. If the
breeder could read the Queue entity's `partitions`/`rate`/`msg_size`, "it
inferred the niche from telemetry" would be a lie. Enforcing it in Cedar (not
convention) makes it provable: a breeder read attempt returns **403 + a
`GovernanceDecision`** visible in the Observe UI — we can show on stage that it
*cannot* peek. This is also the honest version of Sigelman's point that the hard
part is the evaluation/observation layer, not the planning.

### Sub-Decision 2: Global + per-cluster FitnessGoals

Extend `FitnessGoal` with `scope` (`global` | `cluster`) and `cluster_id`
(additive, nullable). Global goals (e.g. `cost_efficiency/maximize`,
`throughput/maximize`) are the population-wide pressure; cluster goals (e.g.
`p95_latency/minimize` for the order-ingest niche) are narrower. The breeder
**reconciles**: a cluster breed must improve its own goal *without violating the
global budget*.

**Why this approach**: extends the proven entity rather than adding a parallel
one; the existing lineage goals keep working (scope defaults to the lineage
behavior). The global-vs-cluster tension is exactly Sigelman's "conflicting
objectives → humans remain most needed" — surfaced concretely, with the human at
the FitnessGoal + approval gates.

### Sub-Decision 3: A governed `Breed` entity

New entity: `Proposed → Building → Verifying → (Promoted | Culled)`. Fields:
`cluster_id`, `niche`, `fitness_goal_id`, `issue_id` (the Symphony
ImprovementIssue = motivation), `gene` (the mutation), `build_result`,
`perf_delta`, `cull_reason`. The breeder Proposes; CIRun/DST drive
Verifying; Promote/Cull are the selection outcomes.

**Why this approach**: gives the "all breeds produced for this cluster, with
motivation → Symphony link and result" view a first-class backing object, and
makes **culling** an explicit, recorded terminal state (with the reason) rather
than an inferred absence. Mirrors Deploy/Cluster lifecycle conventions.

### Sub-Decision 4: A governed `Migration` entity (queue move) — and closing the broker-targeting gap

New entity: `Requested → Approved → Migrating → (Done | Failed)` for moving a
queue's producers/consumers onto a specialized cluster. Its executor creates the
topic on `helix-<name>` and **redeploys that queue's workloads pointed at the new
cluster's broker DNS** (`helix-<name>.dark-factory.svc.cluster.local:9092`). This
closes the known gap from ADR-0097/0098 where workload create endpoints hardcode
the baseline brokers — `create_producer`/`create_consumer` gain a `cluster`/
`brokers` target.

**Why this approach**: makes the move auditable (Cedar-gated, in the timeline)
consistent with everything else being governed, and the broker-targeting fix is
needed anyway for per-cluster workloads to be real (not just labeled).

## Rollout Plan

1. **Phase 0 (this change)** — ADR; FitnessGoal scope; Breed + Migration specs +
   Cedar + CSDL (L0-L3 verified); breeder read-deny Cedar; Datadog→niche
   inference module; per-cluster broker targeting in the workload executors;
   backend endpoints; verified via curl.
2. **Phase 1** — Breeds tab UI (global goals + per-cluster niche sections + breed
   cards with motivation→Symphony link + results).
3. **Phase 2** — wire the breeder to run the full discover→propose→spin-up→migrate
   →verify→promote/cull cycle autonomously for the demo.

## Consequences

### Positive
- Speciation and culling become *visible, governed, motivated* per cluster.
- The telemetry firewall is provable, not asserted — the strongest part of the demo.
- Closes the per-cluster workload broker-targeting gap (real per-cluster load).
- Reuses Cluster (0098), Symphony (0093), FitnessGoal/cull (0095), tagged metrics (0097).

### Negative
- More entities/Cedar to maintain; the demo depends on Helix being up on GKE.
- Niche inference from telemetry is heuristic — needs enough metric history to
  distinguish shapes (mitigated by running representative workloads first).

### Risks
- **Firewall leak**: if any breeder code path reads a workload entity, the claim
  breaks. Mitigation: Cedar deny is the backstop; the breeder's HTTP client must
  also have no workload-read calls. Verify with a deliberate read → expect 403.
- **GKE capacity**: N specialized clusters × 3 pods may exhaust the dark-factory
  node pool. Mitigation: small replicas; surface scheduling failures (as 0098).
- **Conflicting global vs cluster goals** with no safe move — by design the human
  resolves these; the UI must make the conflict legible, not silently pick.

### DST Compliance
Not applicable — all changes are in the `deploy-history` reference app, the
`.ioa.toml`/Cedar specs, and Python breeder/inference modules; none are
simulation-visible Temper crates. New specs are L0-L3 verified on edit.

## Non-Goals
- Auto-resolving conflicting objectives (human-gated by design).
- Cross-cluster Raft / data replication between niches (each cluster independent).
- A general workload-classification ML model — niche inference is a small,
  explainable heuristic over a few metrics for the demo.

## Alternatives Considered
1. **Convention-only firewall** (breeder just "doesn't" read workloads) — rejected:
   not provable on stage, and one stray read silently breaks the core claim.
2. **Reuse ImprovementIssue + FitnessGoal generation as "the breed"** — workable
   but culling/promotion and the per-cluster breed list are cleaner with a
   first-class Breed entity; rejected for the demo's clarity.
3. **Simulated cluster spin-up / queue moves** — rejected per the "most real"
   decision; we do real GKE clusters + real re-points (accepting the GKE dependency).

## Rollback Policy
Additive. To roll back: unregister Breed/Migration EntityTypes from the CSDL,
remove the breeder read-deny policies (breeder reverts to its 0095 capabilities),
drop the FitnessGoal `scope`/`cluster_id` columns (nullable, safe), and remove the
Breeds tab. Any specialized clusters are torn down via the 0098 Cluster teardown.
