# Breeder — telemetry-only workload-niche inference (ADR-0099)

The breeding agent (`breeder` agent type) discovers workload *niches* from
**Datadog telemetry alone**. It is Cedar-FORBIDDEN from reading the
Queue/Producer/Consumer entities out of Temper (see `policies/{queue,producer,
consumer}.cedar` — `forbid ... when principal.agent_type == "breeder"`). So its
niche classification is real inference, not a spec lookup.

## The firewall, two layers

1. **Cedar (enforced):** a breeder read on a workload entity returns 403 + a
   `GovernanceDecision` (provable in the Observe UI).
2. **Code (disciplined):** this package imports NO Temper client. It only depends
   on `observer.observe`'s `MetricSource` (the Datadog v1 query API or a captured
   snapshot). `grep -L temper reference-apps/breeder/breeder/*.py` is empty by
   design.

## What it infers

For each queue (discovered from the `queue:` tag on `workload.*` metrics), it
reads:

| signal | metric |
|---|---|
| produce rate | `sum:workload.producer.sent{queue:Q}.as_rate()` |
| produce latency | `avg:workload.producer.send_latency_ms{queue:Q}` |
| consume rate | `sum:workload.consumer.consumed{queue:Q}.as_rate()` |
| fan-out (consumers per producer) | distinct `workload_id` w/ `role:consumer` vs `role:producer` |
| server commit latency | `avg:helix.produce.latency_ms{*}` (cluster-wide) |

and classifies the queue into a **niche**:

- `high-throughput-batch` — high produce rate, latency-tolerant.
- `high-fanout-latency-sensitive` — many consumers per producer, low latency budget.
- `low-volume-steady` — modest rate, stable.

Each niche maps to a candidate gene to mutate (e.g. batch niche → `linger_ms` up;
latency niche → `linger_ms` down / `append_batch_size` down) and a FitnessGoal
direction. That feeds breed proposals (the Breed entity), but the *proposal* is
written by the orchestrator with breeder creds — this module never reads Temper.

## Run

```bash
# Snapshot (offline demo): classify from a captured telemetry snapshot.
python3 -m breeder.infer --source snapshot --snapshot snapshots/niches.json

# Live: classify from the real Datadog org (needs DD_API_KEY/DD_APP_KEY).
python3 -m breeder.infer --source datadog --window 30m
```
