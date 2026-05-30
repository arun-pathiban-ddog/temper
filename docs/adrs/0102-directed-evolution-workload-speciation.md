# ADR-0102: Directed Evolution — Autonomous Workload Speciation

- Status: Proposed
- Date: 2026-05-30
- Deciders: Temper core maintainers
- Related:
  - ADR-0098: Multi-cluster deploy (per-cluster worktree + StatefulSet)
  - ADR-0099: Breeds + telemetry-only breeder firewall
  - ADR-0100: AgentRun + in-app scheduler
  - ADR-0101: Intent-driven metrics (open-ended NL goal → metric discovery)
  - `reference-apps/breeder/breeder/infer.py` (the telemetry-only classifier)
  - `reference-apps/deploy-history/app.py` (control plane: clusters, migrations, scheduler)
  - `reference-apps/evolution/genome.py` (source-baked Helix tuning)

## Context

The demo can today (a) run **mixed workloads** on one Helix cluster — producers
and consumers with distinct load *shapes* (bursty, low-latency, batch, steady;
`deploy-history/app.py` `PRODUCER_SHAPES`/`CONSUMER_SHAPES`), (b) emit
**per-workload telemetry** tagged `queue:`/`role:`/`workload_id:`
(`workload.producer.sent`, `workload.producer.send_latency_ms`,
`workload.consumer.consumed`, `workload.consumer.lag`,
`workload.consumer.process_latency_ms`), (c) **classify a workload's niche from
telemetry alone** (`breeder/breeder/infer.py:classify`) — and crucially the
breeder is **Cedar-forbidden from reading the workload's config** (ADR-0099
firewall), so classification is provably "no cheating", (d) **create a tuned
Helix cluster** on GKE (`/api/clusters` → worktree → Cloud Build →
StatefulSet), and (e) **migrate a queue's workloads** to another cluster
(`/api/migrations`).

What is **missing** is the orchestration that turns (c) into (d)+(e). The
classifier is dead-ended: the scheduler runs `breeder.infer` on a cron, captures
its JSON into an `AgentRun`, and **nothing parses or acts on the output**.
`classify()`/`infer_all()` are imported by nothing outside their own module. So
there is no agent that does the demo we want:

> one cluster of mixed workloads → an agent classifies each workload **from
> telemetry only** → it provisions a separate cluster optimized for each niche →
> it migrates the matching workloads onto their optimized cluster — autonomously.

This ADR fills that gap.

Two facts shape the design:

1. **Helix tuning is source-baked, not env-configurable.** The cluster manifest
   surfaces none of the four tuning knobs (`linger_ms`, `max_inflight`,
   `append_batch_size`, `sync_on_rotation`). They are compiled into the binary;
   `evolution/genome.py:apply_to_source` rewrites the `.rs` files via regex, then
   a ~8–10 min amd64 Cloud Build produces the image. So **a niche-optimized
   cluster = a niche genome committed to a branch + one Cloud Build**.
2. **`approve_cluster` ignores the requested base.** `POST /api/clusters` accepts
   `base`, but the approve→provision path hardcodes `base="main"`, so a niche
   genome committed on a branch would be silently ignored and the cluster would
   build vanilla `main`. This is the same silent "wrong image" bug class fixed
   for variant clusters in commit `260dae9e`. It must be fixed or the
   "niche-specific tuning" claim is fake.

## Decision

### Sub-Decision 1: A new autonomous `speciation` agent, inside `breeder/`

Add `reference-apps/breeder/breeder/speciate.py`, entrypoint
`python3 -m breeder.speciate`. It lives in `breeder/` because it reuses the
telemetry-only classifier and runs under the **breeder identity** — so the
ADR-0099 Cedar firewall (breeder cannot read Queue/Producer/Consumer entities)
applies unchanged. The agent's loop:

1. `infer.infer_all(src)` over Datadog telemetry — **no Temper read**.
2. Group queues by inferred niche.
3. For each niche not already isolated: ensure a niche cluster exists (reuse a
   Live cluster on `df-niche/<niche>`, else commit the niche genome and build),
   then migrate the matching queues onto it.
4. Record each as a governed `Speciation` entity.

**Why this approach**: it reuses the proven, firewalled classifier and the
existing governed control-plane endpoints rather than inventing a parallel path.
The "no cheating" property is inherited structurally, not re-asserted.

### Sub-Decision 2: Reuse Cluster + Migration + Breed; add one entity, `Speciation`

The niche cluster is a normal `Cluster`; each queue move is a normal
`Migration`. We add exactly **one** entity, `Speciation`, as a convergence
ledger:

```
states: Proposed → Provisioning → Migrating → Converged | Failed
Propose(niche, queues, target_cluster, motivation)   # queues = comma string from telemetry tags
RecordCluster(cluster_id, image_tag)                 # Provisioning
RecordMigrations(migration_ids, moved)               # Migrating
MarkConverged   (guard: cluster recorded + migrations recorded)
MarkFailed
invariants: ConvergedRequiresCluster, ConvergedIsFinal, FailedIsFinal
```

**Why a new entity**: nothing today records "niche X is isolated onto cluster Y
covering queues {…}". Without it, idempotency would depend on parsing cluster
names — fragile. The ledger makes the loop convergent and gives the UI a single
governed unit showing niche → cluster → migrated queues. Its Cedar policy
permits `breeder`/`supervisor`/`human` when `agentTypeVerified == true`, and
grants **no** read on Queue/Producer/Consumer — the firewall is preserved.

### Sub-Decision 3: Niche → concrete genome, built once per niche

Extend `NICHE_PLAYBOOK` from 3 coarse niches to the demo's archetypes, each
mapping to concrete `Genome` deltas built on the real knobs:

- `bursty` → coalesce under burst (raise `linger_ms`, e.g. 20) to absorb spikes
- `low-latency` → `linger_ms=0`, smaller `append_batch_size` (minimize p95)
- `high-throughput-batch` → high `linger_ms` (~50) + large `append_batch_size`
- `steady` → baseline genome (no change; isolation alone is the win)

Each niche genome is committed to `df-niche/<niche>` and built **once** (Phase 6,
pre-demo). The agent prefers reusing a Live cluster already on that branch and
only triggers a build when none exists — bounding live wall-clock to
classify+migrate.

### Sub-Decision 4: Time-series classification (burst vs steady vs batch)

The current classifier collapses each metric series to a single average, which
cannot separate bursty from steady. Extend `read_queue_telemetry` to keep the
per-bucket series (`query_series` already returns the full list) and compute
`burst_ratio = p95_peak / p95_baseline` and `cv = stddev/mean` (mirroring the
observer's existing burst logic), plus `lag_slope` over
`workload.consumer.lag`. `classify()` branches on these signals.

### Sub-Decision 5: Fix the `base` bug on the shared provision path

Persist the create-time `base` onto the `Cluster` entity and have
`approve_cluster`/`_provision_cluster` build from it (default `main` when
absent, preserving today's UI and `evolution`-agent behavior). Confirm
`_create_worktree(name, base)` force-resets `df-cluster/<name>` to that ref
before build.

## Rollout Plan

1. **Phase 0** — This ADR.
2. **Phase 1** — Time-series classifier (`infer.py`); verified via snapshot.
3. **Phase 2** — `Speciation` spec + Cedar; L0–L3 verified.
4. **Phase 3** — `base`-bug fix (riskiest; shared path).
5. **Phase 4** — `speciate.py` orchestrator; `--dry-run` then live single-pass.
6. **Phase 5** — `/api/speciations` + scheduler entry (`enabled:false`).
7. **Phase 6** — Pre-build niche images on `df-niche/<niche>`; demo runbook.

## Consequences

### Positive
- Closes the classify → provision → migrate loop: a genuine autonomous
  workload-speciation demo, telemetry-only end to end.
- Reuses governed entities and the Cedar firewall — the "no cheating" property
  is structural.
- The `base`-bug fix also hardens the human UI and `evolution` agent against
  silently building the wrong image.

### Negative
- One new entity + spec to maintain; the classifier grows more heuristics.
- Real per-niche tuning means a real Cloud Build per niche (mitigated by
  pre-building).

### Risks
- **`base`-bug fix is on the shared cluster-provision path** — a regression
  breaks *all* cluster builds (UI + evolution). Mitigation: default to `main`
  when no base is supplied; only diverge for the speciation agent's niche
  branches; verify worktree HEAD == requested ref before build.
- **Classifier misfires** (e.g. labels a steady workload bursty) → an
  unnecessary cluster. Mitigation: confidence levels; convergence ledger
  prevents thrashing; agent is `enabled:false` by default and operator-gated.
- **Migration re-points workloads but does not move data/offsets** — topics are
  auto-created fresh on the destination. Acceptable for *workload* speciation;
  called out as a Non-Goal.

### DST Compliance
The agent and classifier are out-of-tree Python (`reference-apps/`), not
simulation-visible crates. The `base`-bug fix touches `deploy-history/app.py`
(also Python, out of tree). No `temper-runtime`/`temper-jit`/`temper-server`
changes; no DST surface affected.

## Non-Goals
- Moving queue **data** or consumer **offsets** during migration (re-point only).
- Auto-tearing-down the original mixed cluster after speciation.
- Per-queue (vs per-niche) clusters — queues sharing a niche share a cluster.
- Reading workload config to classify (explicitly forbidden — the whole point).

## Alternatives Considered

1. **Env-var tuning instead of source-baked genomes** — would avoid per-niche
   builds. Rejected: Helix does not read these knobs from env today; adding that
   is a Helix change out of scope, and source-baked genomes are the established
   ADR-0098/evolution mechanism.
2. **No new entity; infer idempotency from cluster names** — rejected as fragile
   string-parsing with no governed record of what was isolated.
3. **Reuse the `evolution` harness** — rejected: it breeds throwaway *variants*
   of two hardcoded lineages and tears them down; it is not per-workload durable
   splitting and never consults the classifier.

## Rollback Policy
The agent is `enabled:false` by default; disabling the scheduler entry stops the
loop. Niche clusters and migrations are torn down via the existing
`/api/clusters/{id}/teardown` and by migrating queues back. The `base`-bug fix
defaults to `main`, so reverting that single change restores prior behavior.
