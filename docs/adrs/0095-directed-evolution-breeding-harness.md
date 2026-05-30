# ADR-0095: Directed-Evolution Breeding Harness (H2)

- Status: Accepted
- Date: 2026-05-24
- Deciders: Temper core maintainers
- Related:
  - ADR-0093: Dark Factory CI/CD and Deploy Governance (CIRun/Deploy entities)
  - ADR-0093: Symphony Orchestrator (C1 implementer)
  - ADR-0094: Dark Factory Observer/Researcher (C2 — reused research logic)
  - `docs/dark-factory-evolution-harness-spec.md` (the H2/H3 spec)
  - `reference-apps/evolution/` (this harness)
  - Memory `dark-factory-lethal-mutation-spike` (the proven WAL durability cull)

## Context

The Dark Factory governed loop (Observer → Symphony → CIRun → Deploy as Temper
entities) ran *once* to `Done`. That is a single, human-initiated improvement. The
project's thesis — a "Dark Factory for Helix" — needs the loop to become **directed
evolution**: a human sets a *selective pressure* (a fitness goal), and the system
breeds Helix toward it over generations, culling lethal mutations against Helix's own
invariants.

Two things must be true for this to be real rather than theater:

1. **The cull must be real.** A latency-greedy agent will eventually propose "skip the
   ~1.3 ms fsync." That is a believable optimization and it is *lethal*: it claims
   durability without persisting, so a crash loses data. Helix's verification cascade
   must actually catch it. The spike (memory `dark-factory-lethal-mutation-spike`)
   proved `cargo test -p helix-tests --lib test_dst_shared_wal_basic_durability` fails
   deterministically with "synced but not recovered" on this mutation.

2. **Two pressures must produce two species.** A latency lineage and a throughput
   lineage, breeding off one common ancestor under two different `FitnessGoal`s, must
   diverge into two measurably-different, *safe* genomes.

## Decision

### Sub-Decision 1: A genome is a small set of real Helix source knobs

The harness reads and writes four concrete tuning knobs in the Helix source tree:

| Gene | Location | Direction |
|------|----------|-----------|
| `linger_ms` | `helix-server/src/service/batcher.rs` (`unwrap_or(1)`) | ↑ throughput, ↑ latency |
| `max_inflight` | `helix-raft/src/lib.rs` `MAX_INFLIGHT_APPEND_ENTRIES` | ↑ throughput |
| `append_batch_size` | `helix-raft/src/lib.rs` `APPEND_ENTRIES_BATCH_SIZE_MAX` | ↑ throughput |
| `sync_on_rotation` | `helix-wal/src/wal.rs` `WalConfig` default + `sync()` body | **LETHAL if false** |

`genome.py` provides `read_from_source(repo)` and `apply_to_source(repo)`. The first
three are integer regex rewrites. `sync_on_rotation=false` is special: it does **not**
just flip the config bool (which alone does not trip `test_dst_shared_wal_basic_durability`,
because that test drives durability through explicit `wal.sync()`, not rotation). To
make the lethal gene actually lethal, `apply_to_source` patches the active-segment
fsync block inside `pub async fn sync()` to skip `active.file.sync().await` while still
letting `durable_index` advance — the *exact* proven mutation from the spike. This is
the latency-greedy shortcut, faithfully represented as a gene.

### Sub-Decision 2: The fitness cascade gates each mutation (cheap → expensive)

- **Stage 0 — cost model** (`cost_model.py`, instant): a heuristic that scores whether
  a proposed gene change plausibly moves the lineage's goal metric in the right
  direction. Avoids spending a bench on an obviously-wrong move.
- **Stage 1 — local bench** (`bench.py:measure_local()`, ~minutes): rebuild the Helix
  image from mutated source, run a 3-node local cluster + `kafka-producer-perf-test`,
  parse real `{throughput_rps, p50/p95/p99}`. Real measured fitness. (Pre-built; reused
  as-is.)
- **Stage 2 — verification cull** (`verify.py`, ~1 s once compiled): run
  `cargo test -p helix-tests --lib test_dst_shared_wal_basic_durability` on the mutated
  source. A genome that disables fsync durability is **rejected here** with the real DST
  failure message. The lineage then falls back to its next-best safe mutation.
- **Stage 3 — GKE deploy + Datadog** (champion only): explicitly **out of scope for
  H2** (H3). The harness stops at two divergent genomes + local evidence.

### Sub-Decision 3: FitnessGoal is a first-class Temper entity (the human's knob)

`FitnessGoal { lineage_id, metric, direction, target? }` is a new entity type in the
`dark-factory` tenant with a tiny state machine: `Set → Active → Achieved`/`Retired`.
The human creates one per lineage and `Activate`s it — that is the selective pressure.
The harness reads the active goal each generation and proposes a goal-directed
mutation. Changing the goal redirects evolution: this is the "directed" in directed
evolution. The goal is registered as `dark-factory-specs/fitness_goal.ioa.toml` +
CSDL + `policies/fitness_goal.cedar`, and hot-loaded into the running server.

### Sub-Decision 4: Speciation rides on real git branches + ImprovementIssue chains

Each lineage is a real `git worktree` off the common ancestor (`evolve/latency`,
`evolve/throughput`), NEVER `main`. A surviving mutation is a real commit on the
lineage branch. Each generation also records an `ImprovementIssue` (the existing
governed-loop entity) so the divergent lineage chains are visible in the Observe UI.
The genome read in generation N+1 is the genome committed in generation N — cumulative.

### Sub-Decision 5: Autonomy via a narrow, verified auto-approve policy (no spoofing)

`ImprovementIssue` Cedar requires a verified supervisor/human for `ApprovePlan`. For an
autonomous breeding run we register a verified `breeder-supervisor` AgentType (via the
real `register_identities.py` credential chain — *not* header spoofing) and add a
**narrow** Cedar permit: a verified `breeder-supervisor` may `ApprovePlan` on
ImprovementIssues. The driving identity is a separate verified `breeder` (the
planner/implementer), so the existing `forbid (planner_id == principal.id)` role
separation still holds — the breeder cannot approve its own plan; only the distinct
breeder-supervisor can. The up-front pressure-setting (creating + activating the
FitnessGoal) and the real Stage-2 cull remain the visible governance beats.

## Consequences

- The cull is genuinely real: H2's "invariant wall" moment is a deterministic DST
  failure, not a hand-wave.
- The harness composes with the existing entities (ImprovementIssue/CIRun/Deploy) and
  the existing C2 research logic, rather than replacing them.
- Stage 1 is slow (image rebuild per generation). Mitigated by a tunable
  `num_records` and BuildKit cache mounts; correctness first.
- `sync_on_rotation` is modeled as a source patch to `sync()` rather than a pure config
  flip — documented above and in `genome.py` so the gene's lethality is honest.

## What is out of scope (H3)

Stage 3 — the GKE champion deploy + Datadog confirm — is deliberately not implemented
here. H2 ends at two divergent genomes plus the local evidence (bench numbers + the
real cull message + the ImprovementIssue chain).
