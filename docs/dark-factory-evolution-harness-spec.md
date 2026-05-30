# Directed-Evolution Breeding Harness — Spec (H2/H3)

The harness turns the one-shot governed loop into **directed evolution**: a human
sets a *selective pressure* (a FitnessGoal), and the system breeds Helix toward
it over generations, culling lethal mutations against Helix's invariants. Two
lineages under two pressures diverge into two distinct, safe "species" (#2);
along the way an unsafe shortcut gets culled by real verification (#1).

## The gene set (concrete mutable knobs in Helix source)
A "genome" is a small config the harness can read from + write to the Helix source:
- `linger_ms` — `helix-server/src/service/batcher.rs:69` (default field; also env `HELIX_BATCHER_LINGER_MS`). ↑ = throughput↑ + latency↑.
- `max_inflight` — `MAX_INFLIGHT_APPEND_ENTRIES` `helix-raft/src/lib.rs:55` (default 5). ↑ = throughput↑.
- `append_batch_size` — `APPEND_ENTRIES_BATCH_SIZE_MAX` `helix-raft/src/lib.rs:48` (default 1000).
- `sync_on_rotation` — `helix-wal/src/wal.rs:73` (default true). **LETHAL if false** — disabling for latency trips the WAL durability DST (the proven cull).

## Fitness cascade (cheap → expensive; mutation must clear each to advance)
- **Stage 0 — cost model** (instant, in-harness): does this genome plausibly move the lineage's goal in the right direction? Cheap heuristic to avoid wasting a bench on obviously-wrong moves.
- **Stage 1 — local bench** (~30-60s): `reference-apps/evolution/bench.py:measure_local()` — rebuild image from mutated source, run 3-node local cluster + kafka-producer-perf-test, parse {throughput_rps, p50/p95/p99}. REAL measured fitness. DONE / available.
- **Stage 2 — verification cascade** (~1-2s): run Helix's safety tests on the mutated source. THE CULL: `cargo test -p helix-tests --lib test_dst_shared_wal_basic_durability` (and the broader `--lib wal`) FAILS deterministically if a mutation breaks durability (proven: skip-fsync / sync_on_rotation=false → "synced but not recovered"). A culled mutation is rejected; the lineage falls back to a safe alternative.
- **Stage 3 — GKE deploy + Datadog confirm** (~8-12 min): CHAMPION ONLY (each lineage's final survivor). Real Cloud Build amd64 + kubectl rollout to ns dark-factory (un-guard the Deploy executor for the champion) + confirm helix.produce.latency_ms shifts in Datadog.

## Pressure = FitnessGoal (the human's knob)
A Temper entity per lineage: `FitnessGoal { lineage_id, metric: "p95_latency"|"throughput", direction: "minimize"|"maximize", target?: float }`. The Observer reads its lineage's goal each generation and proposes a goal-directed mutation. Changing the goal redirects evolution — that's the "directed" in directed evolution.

## A generation (one loop pass per lineage)
1. Read the lineage's current genome (from its git branch's Helix source) + its FitnessGoal.
2. Observer/Researcher proposes a goal-directed mutation (which gene, which direction) — reuse `reference-apps/observer/` research logic, parameterized by goal.
3. Stage 0 cost-model gate → Stage 1 bench → Stage 2 verify (cull) → if survives, apply the gene change to the lineage branch (real commit), record an `ImprovementIssue` (the governed-loop entity) so the lineage is visible in the Observe UI.
4. Next generation reads the NEW genome. Cumulative.

## Speciation (H3)
- 2 lineages on 2 real git branches off a common ancestor: `evolve/latency`, `evolve/throughput`.
- Latency lineage's goal tempts `sync_on_rotation=false` at some generation → Stage 2 culls it → falls back to lowering `linger_ms` (safe). Throughput lineage raises `linger_ms`/`max_inflight`.
- After N generations: two measurably different genomes from one ancestor. Show the divergent `ImprovementIssue` chains side by side in the Observe UI.
- Each lineage's champion → Stage 3 real GKE deploy + Datadog confirm.

## Autonomy + governance
Fully autonomous run (set goals, hit go). BUT B's Cedar policies require human approval for ApprovePlan/ApproveDeploy. For autonomous breeding, register a `breeder-supervisor` agent identity (verified, via the `reference-apps/identity/register_identities.py` chain) that policy permits to self-drive plan/deploy approvals in the breeding context — OR add an auto-approve Cedar policy scoped to FitnessGoal-driven issues. Keep the up-front pressure-setting + the cull as the visible "governance" beats.

## Tenant / env
- Tenant `dark-factory`, Temper server http://127.0.0.1:3000 (bearer tokens in `reference-apps/identity/tokens.json`, `.token` field).
- Helix repo `/Users/arun.parthiban/notdd/helix`. Lineage edits in worktrees/branches, NEVER main.
- Colima VM must be >=8GB (already set: `colima start --cpu 8 --memory 12`).

## Deliverables
- `reference-apps/evolution/` package: `genome.py` (read/write genes in Helix source), `cost_model.py` (Stage 0), `bench.py` (Stage 1, DONE), `verify.py` (Stage 2 cull), `harness.py` (orchestrate a generation + a lineage), `main.py` (CLI: `--lineages latency,throughput --generations 3`).
- A `FitnessGoal` Temper spec (entity) + register in dark-factory.
- An ADR.
- A verified run: 2 lineages × 3 generations, with at least one real cull in the latency lineage, ending in two divergent genomes (champion GKE deploy can be the final step / can be staged for rehearsal).
