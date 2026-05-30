# Demo Runbook — Autonomous Workload Speciation (ADR-0102)

**The story:** one Helix cluster runs a mix of workloads (bursty, low-latency,
high-throughput-batch, steady). An agent watches only the **telemetry** in
Datadog — it never reads the workload config — infers each workload's character,
then autonomously provisions a separate, niche-tuned Helix cluster per archetype
and migrates the matching queues onto it. Everything is a governed Temper entity,
Cedar-gated.

## Why it's not cheating

The classifier is an **LLM agent that queries the telemetry itself** via
`claude -p` (Claude Code non-interactive) with the **Datadog MCP** as its only
allowed tools (`get_datadog_metric` / `search_datadog_metrics` /
`get_datadog_metric_context`). It is given **no tool that reads workload config**
— so the niche is inferred purely from observed behaviour (produce-rate
magnitude, rate variance / zero-crossings, consumer lag), never from the `Shape`
field the workload was created with. The breeder identity is also
**Cedar-forbidden** from reading Queue/Producer/Consumer entities (ADR-0099),
so the firewall holds at both layers. A static-threshold heuristic
(`breeder/infer.py`) remains as an offline fallback (`--classifier heuristic`).

## Pieces (all built, ADR-0102)

| Piece | Where |
|---|---|
| LLM niche classifier (queries telemetry via `claude -p` + Datadog MCP) | `breeder/breeder/llm_classify.py` |
| Heuristic classifier (offline fallback) | `breeder/breeder/infer.py` |
| Speciation orchestrator agent (self-provisions niche clusters) | `breeder/breeder/speciate.py` |
| `Speciation` governed entity (convergence ledger) | `identity/dark-factory-specs/speciation.ioa.toml` + `policies/speciation.cedar` |
| Niche genomes (`df-niche/<niche>`) — authored by the agent at run time | committed live by `speciate.py` off `champion-metrics` |
| `GET /api/speciations` + scheduler `speciation` agent | `deploy-history/app.py` |

## Niche → tuning (real Helix genes, baked into the image)

| Niche | Cluster name | Genome (vs baseline linger=1, inflight=5, batch=1000) |
|---|---|---|
| `high-throughput-batch` | `niche-htb` | `linger_ms=50, append_batch_size=4000` |
| `bursty` | `niche-bursty` | `linger_ms=20, max_inflight=8` |
| `low-latency` | `niche-low-latency` | `linger_ms=0, append_batch_size=64` |
| `steady` | `niche-steady` | baseline (isolation is the win) |

## One-time prep (done; ~10–40 min of Cloud Build)

```bash
# 1. Create the niche branches with genomes committed (fast, local):
cd reference-apps/breeder && python3 prebuild_niches.py

# 2. Build + deploy a cluster per niche (real ~8-10 min Cloud Build each).
#    Done via the UI create+approve flow; the Phase-3 base fix makes each build
#    from its df-niche/<niche> branch (verified: worktree HEAD == branch SHA).
#    Cluster names (DNS-label, <=20 chars): niche-htb, niche-bursty,
#    niche-low-latency, niche-steady.
```

Check they're Live: `curl -s localhost:4100/api/clusters | jq '.clusters[]|select(.name|startswith("niche"))|{name,status}'`

## Demo run

1. **Set up mixed workloads on the baseline cluster.** Create queues + producers/
   consumers with distinct shapes (Workloads tab, or `/api/workloads/*`):
   - `orders` — high-rate batch producer (high-throughput-batch)
   - `checkout` — low process_ms, low_latency consumer (low-latency)
   - `flash-sale` — bursty producer (bursty)
   - `audit-log` — trickle producer (steady)
   Let them run a few minutes so telemetry accumulates in Datadog.

2. **(Preview) Dry-run the classifier** to show what it infers from telemetry:
   ```bash
   cd reference-apps/breeder
   python3 -m breeder.speciate --source datadog --queues orders,checkout,flash-sale,audit-log --dry-run
   ```
   Shows niche + the telemetry signals (burst-ratio, CV, p95, lag-slope) + the
   target cluster + genome — no writes.

3. **Flip the scheduler ON** (Agent Runs tab) for the `speciation` agent — or:
   ```bash
   curl -X POST localhost:4100/api/scheduler/agent/speciation -d '{"enabled":true}'
   curl -X POST localhost:4100/api/scheduler/agent/speciation/run-now   # fire immediately
   ```
   The agent (autonomous): classifies → opens a `Speciation` per niche →
   reuses the pre-built niche cluster (no live build) → migrates each matching
   queue onto it → marks Converged.

4. **Watch it converge.** `GET /api/speciations` shows niche → cluster →
   migrated queues. The Workloads tab shows the queues now running on their
   niche clusters. Datadog shows each niche cluster's `helix.*` metrics.

## Idempotency / convergence

The agent skips any niche already recorded `Converged` in the Speciation ledger
whose queue-set covers the current queues. A newly-appearing queue for an
isolated niche is migrated without rebuilding the cluster. Safe to run on a timer.

## Notes / honesty

- Migration **re-points** a queue's producer/consumer workloads at the niche
  cluster's brokers and stops the old ones; it does **not** move existing data or
  consumer offsets (the topic is auto-created fresh on the destination). This is
  *workload* speciation, not stateful data migration.
- The scheduler default for `speciation` is `--build-missing false`: the
  autonomous loop reuses pre-built niche images and never triggers an 8-10 min
  build on a timer. To let it build missing niche clusters live, run it manually
  with `--build-missing true`.
