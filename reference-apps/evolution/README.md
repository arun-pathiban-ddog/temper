# Directed-Evolution Breeding Harness (Dark Factory H2)

Turns the one-shot governed Helix loop into **directed evolution**: a human sets a
selective pressure (a `FitnessGoal`), and the system breeds Helix toward it over
generations, culling lethal mutations against Helix's own invariants. Two lineages
under two pressures diverge into two distinct, *safe* genomes; along the way the
latency lineage's "skip the fsync" shortcut is culled by a real verification DST.

See `docs/adrs/0095-directed-evolution-breeding-harness.md` and
`docs/dark-factory-evolution-harness-spec.md`.

## The fitness cascade (cheap → expensive)

```
Stage 0  cost_model.py   instant   does this gene move the goal the right way?
Stage 1  bench.py        ~minutes  rebuild image from MUTATED source + 3-node
                                    cluster + kafka-producer-perf-test (real numbers)
Stage 2  verify.py       ~1s+      WAL durability DST — THE CULL. sync_on_rotation=
                                    false is rejected here with the real DST message.
Stage 3  (H3, not here)            GKE champion deploy + Datadog confirm
```

A candidate that carries lethal risk is verified (Stage 2) *before* spending a bench;
safe candidates are benched then verified. A culled candidate falls back to the
next-best safe mutation.

## The gene set (genome.py)

Real, mutable knobs in the Helix source tree:

| Gene | Location | Effect |
|------|----------|--------|
| `linger_ms` | `helix-server/src/service/batcher.rs` | ↑ throughput, ↑ latency |
| `max_inflight` | `helix-raft/src/lib.rs` `MAX_INFLIGHT_APPEND_ENTRIES` | ↑ throughput |
| `append_batch_size` | `helix-raft/src/lib.rs` `APPEND_ENTRIES_BATCH_SIZE_MAX` | ↑ throughput / ↑ latency |
| `sync_on_rotation` | `helix-wal/src/wal.rs` | **LETHAL if false** (durability cull) |

`sync_on_rotation=false` is applied as the *proven* fsync-skip patch inside
`wal.rs:sync()` (skip `active.file.sync()` while still advancing `durable_index`) — the
exact lethal mutation that trips `test_dst_shared_wal_basic_durability`.

## Files

```
genome.py          read/write the gene set in a Helix worktree's source
cost_model.py      Stage 0 — cheap heuristic goal-alignment gate
bench.py           Stage 1 — real local Helix bench (pre-built; used as-is)
verify.py          Stage 2 — the durability DST cull (real cargo test)
harness.py         orchestrate ONE generation for ONE lineage + drive Temper
temper_breeder.py  thin Temper client for the verified breeder identities
main.py            CLI: --lineages latency,throughput --generations N
```

The `FitnessGoal` Temper entity is in `reference-apps/identity/dark-factory-specs/`
(`fitness_goal.ioa.toml` + the CSDL + `policies/fitness_goal.cedar`); the breeder
identities are in `tokens.json` and registered by `register_breeders.py`.

## Running it

```bash
# 0. one-time: register the verified breeder identities (real credential chain)
cd reference-apps/identity
export TEMPER_API_KEY="$(python3 -c 'import json;print(json.load(open("tokens.json"))["operator"]["token"])')"
python3 register_breeders.py --base-url http://127.0.0.1:3000 --tenant dark-factory

# 1. run two lineages for two generations with the REAL bench
cd ../evolution
python3 main.py --lineages latency,throughput --generations 2 --num-records 8000

# fast logic/cull check (skips the slow Docker bench; still real Stage-2 cull + commits)
python3 main.py --lineages latency,throughput --generations 2 --no-bench
```

Flags: `--num-records` (lower = faster bench), `--no-bench`, `--no-rebuild`,
`--base-ref` (common ancestor).

## What's real vs stubbed

- **Real:** the genome source edits, the Stage-2 durability cull (actually runs
  `cargo test`), the Stage-1 Docker bench (rebuilds from mutated source, real perf
  numbers), the git worktrees + commits, the `FitnessGoal`/`ImprovementIssue` Temper
  entities, and the Cedar governance (verified breeder credentials, narrow
  auto-approve, role separation preserved).
- **Out of scope (H3):** Stage 3 GKE champion deploy + Datadog confirm.

## Governance / autonomy

Two verified identities (issued via the real `AgentType.Define` +
`AgentCredential.Issue` chain, NOT header spoofing):
- `breeder` — drives FitnessGoal generations + ImprovementIssue planning (planner).
- `breeder-supervisor` — the distinct identity permitted to `ApprovePlan` and to
  Define/Activate FitnessGoals. A narrow Cedar permit auto-approves the plan; the
  existing `forbid (planner_id == principal.id)` keeps role separation — the breeder
  cannot approve its own plan.
```
