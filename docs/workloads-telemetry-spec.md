# Workloads Telemetry — Spec (metrics champion + workload metrics + dashboard)

Goal: a Datadog dashboard that clearly shows topics/queues, producers,
consumers, throughput, and queue lag — all live. Requires restoring the dark
helix.* metrics AND emitting new per-workload metrics.

## Part 1 — Metrics-enabled champion image (restore helix.* telemetry)
The running `champion-throughput` image (from `evolve/throughput`) lacks the
DogStatsD exporter, so `helix.produce.latency_ms.*` + `helix.replication.lag`
report NO live data. The exporter code is uncommitted on `main`'s working tree.

**Git topology (verified, no file overlap):**
- `evolve/throughput`: champion change = `batcher.rs` linger_ms `unwrap_or(1)→(9)` ONLY.
- `main` working tree (base = ancestor `8260087`, same as evolve/throughput's base):
  metrics work = new `helix-server/src/metrics.rs` (untracked) + changes to
  `helix-server/src/kafka/handler.rs`, `service/mod.rs`, `service/handlers/write.rs`,
  `lib.rs`. (See memory `helix-metrics-a1`.)

**Build a `champion-metrics` branch** off `evolve/throughput` carrying BOTH:
- `git worktree add -b champion-metrics <dir> evolve/throughput`
- Copy the metrics changes into it: the 4 modified files from main's working tree
  + the untracked `metrics.rs`. (They apply cleanly — disjoint from batcher.rs.)
  Simplest: `git -C <main-repo> diff 8260087 -- <4 files> | git -C <worktree> apply`
  then copy `metrics.rs` in, OR cherry-pick if the metrics work gets committed.
  Verify it compiles (`cargo check -p helix-server`) and `metrics.rs` is present.
- Commit on `champion-metrics`.

**Build + deploy:**
- Cloud Build amd64 (the Track A pattern: `gcloud builds submit` with
  `/tmp/helix-cloudbuild.yaml`, `_TAG=champion-metrics`, from the worktree).
- Deploy via the governed Deploy flow (preferred — create Deploy entity, image_tag
  `champion-metrics`, Approve, Apply, executor `kubectl set image`) OR direct
  `kubectl -n dark-factory set image statefulset/helix helix=...:champion-metrics`.
- The pods already have `DD_AGENT_HOST=gensim-datadog.datadog.svc.cluster.local`
  + `DD_DOGSTATSD_PORT=8125` (from the statefulset), so the exporter will emit
  on rollout. Confirm `helix.produce.latency_ms.*` + `helix.replication.lag`
  report live in Datadog (query last 5m, expect data once a producer runs).
- Leave the cluster on `champion-metrics`, 3/3.

## Part 2 — Per-workload DogStatsD (producer/consumer scripts)
The producer/consumer scripts (inline in `reference-apps/deploy-history/app.py`,
`_producer_manifest` / `_consumer_manifest`) currently only log to stdout. Add
DogStatsD emission so the dashboard can break down by producer/consumer/queue.

Send to `gensim-datadog.datadog.svc.cluster.local:8125` (UDP DogStatsD — the
proven path; the producer/consumer pods can resolve it like Helix does). Use the
`datadog` python pkg (`pip install datadog` alongside confluent-kafka) OR raw UDP
(the `name:value|type|#tags` format — see memory `helix-metrics-a1`). Tags on
every metric: `queue:<topic>`, `workload_id:<id>`, `role:producer|consumer`.

- **Producer** emits:
  - `workload.producer.sent` (count, `|c`) — messages sent (→ rate via .as_count())
  - `workload.producer.send_latency_ms` (`|h`) — per-message produce/ack latency
- **Consumer** emits:
  - `workload.consumer.consumed` (count, `|c`)
  - `workload.consumer.lag` (gauge, `|g`) — high-watermark − committed offset (the
    consumer already computes lag; emit it)
  - `workload.consumer.process_latency_ms` (`|h`) — wall time per message incl. the
    artificial process_ms sleep
- Keep it cheap/non-blocking (best-effort UDP, like the Helix exporter).
- Redeploy is automatic: new producers/consumers created from the UI pick up the
  updated script. (Existing running ones won't until restarted.)

## Part 3 — The dashboard (Datadog MCP — coordinator builds this)
Once Parts 1+2 flow, build "Directed Evolution — Workloads" dashboard:
- **Header note**: what this shows.
- **Cluster throughput**: `sum:helix.produce.latency_ms.count{*}.as_count()` timeseries (total msgs/s into Helix).
- **Produce latency**: `avg:helix.produce.latency_ms.median/95percentile{*}` (p50/p95) timeseries.
- **Replication lag**: `max:helix.replication.lag{*}` (gauge/timeseries).
- **Producers — throughput by producer/queue**: `sum:workload.producer.sent{*} by {queue,workload_id}.as_rate()` (or as_count) timeseries; a toplist of top producers.
- **Producer send latency**: `avg:workload.producer.send_latency_ms{*}` by queue.
- **Consumers — consume rate by queue**: `sum:workload.consumer.consumed{*} by {queue}.as_rate()`.
- **Consumer lag**: `max:workload.consumer.lag{*} by {queue,workload_id}` timeseries + a query_value (current max lag).
- **Consumer process latency**: `avg:workload.consumer.process_latency_ms{*}` (shows the process_ms knob's effect).
- **Queues/topics**: a note or query_table listing active queues (from the workload_id/queue tag cardinality, or just label the producer/consumer panels by queue).
- Tags let it all break down by queue + workload. Use distributions/histograms (`|h`) → p50/p95 server-side. Dark, clean, demo-ready.

## Constraints
- ns dark-factory only; DogStatsD to the cluster Service DNS (not node IP — proven).
- Don't break the running cluster mid-rollout (the governed deploy handles it).
- Workload metrics best-effort (never block the produce/consume hot path).

## Verify
- After deploy + a producer running: `helix.produce.latency_ms.count` and
  `workload.producer.sent` both return live data in Datadog. Consumer lag shows.
- The dashboard renders real data across all panels.
