# Workloads Tab — Spec

A third tab in the Directed Evolution app (`reference-apps/deploy-history/`,
port 4100) to **generate workloads** against Helix: create queues, add
producers, add consumers — with control over input rate, per-message process
time, and producer/consumer concurrency. Everything flows through governed
Temper entities; executors run real k8s Deployments in `dark-factory`.

## Governance posture (note)
Unlike Deploy/ScaleOp (risky → human approval gate), workload create/start/stop
is **routine workload management**, so it is **auto-approved**: still governed
and audited through the entity lifecycle + Cedar (a workload-operator/supervisor
identity is permitted), but NO manual approval click per producer — the tab
should feel like a fluid workload console. (Deploy/Scale keep their human gate.)
The entity lifecycle + audit trail are the governance; the gate is dropped for
these low-risk ops.

## Environment
- Temper http://127.0.0.1:3000, tenant `dark-factory`. Tokens in
  `reference-apps/identity/tokens.json` (`.token`): `operator` (create/read),
  `supervisor` (drives governed actions — Request/Approve/Apply/Stop; verified).
- Helix Kafka: brokers `helix-0..2.helix-headless.dark-factory.svc.cluster.local:9092`
  (or the `helix` Service), ns `dark-factory`, context
  `gke_datadog-sandbox_us-west3_gs-us-west3`. Topics auto-create on first produce.
- Existing pattern to mirror: `docker/k8s/helix-loadgen.yaml` (a producer Deployment).

## Three governed entities (build the specs + register in dark-factory)
Author `.ioa.toml` + add to `model.csdl.xml` + `.cedar` in
`reference-apps/identity/dark-factory-specs/`, hot-loaded into the tenant.

### Queue
- States: `Defined → Active` (+ `Deleted`).
- Fields: `name` (the Kafka topic), `partitions` (int).
- Actions: `Define(name, partitions)`, `Activate()`, `Delete()`.
- Executor on Activate: create the topic (Helix auto-creates on produce, but
  also support explicit partition count — if Helix lacks an admin create path,
  record intended partitions and let the first producer create it via
  auto-create-partitions; document whichever is real).

### Producer
- States: `Defined → Running → Stopped`.
- Fields: `queue` (topic), `rate_per_sec` (int, target msgs/sec), `msg_size`
  (bytes), `concurrency` (int, parallel producer workers/replicas), `result`.
- Actions: `Define(queue, rate_per_sec, msg_size, concurrency)`, `Start()`,
  `Stop()`, `RecordResult(result)`.
- Executor on Start: create k8s Deployment `producer-<id>` in dark-factory
  running the producer script (below) with the params; replicas/threads =
  concurrency. On Stop: `kubectl delete deployment producer-<id>`.

### Consumer
- States: `Defined → Running → Stopped`.
- Fields: `queue` (topic), `process_ms` (int, artificial per-message process
  time), `concurrency` (int, parallel consumers / group size), `result`.
- Actions: `Define(queue, process_ms, concurrency)`, `Start()`, `Stop()`,
  `RecordResult(result)`.
- Executor on Start: create k8s Deployment `consumer-<id>` running the consumer
  script with a consumer group; replicas = concurrency. On Stop: delete it.

Cedar for all three: a workload-operator/supervisor (verified) may Define/Start/
Stop/Activate/Delete. Mirror the existing deploy/scale_op cedar patterns. (No
human-gate requirement — auto-approved per the posture above.)

## Producer/consumer scripts (precise control of every knob)
Use **confluent-kafka python** (or a small image that has it; if not available,
fall back to a bash loop wrapping `kafka-console-producer`/`-consumer` + a rate
limiter + `sleep` for process time). Prefer custom scripts for real control:
- **producer**: rate-limited produce loop — emit `rate_per_sec` msgs/sec of
  `msg_size` bytes to `queue`; `concurrency` parallel workers (threads or
  replicas). Log throughput periodically.
- **consumer**: consume from `queue` in a consumer group of size `concurrency`;
  for each message `sleep(process_ms/1000)` to model processing time; commit;
  log consume rate + lag. The artificial sleep is the "time taken to process
  each message" knob.
Bundle the scripts inline in the Deployment (configmap or inline `python -c` /
heredoc) so there's no image to build — reuse `confluentinc/cp-kafka` (has a
python? if not, use a python image + `pip install confluent-kafka` in an init,
OR a bash+kafka-console fallback). Pick the simplest that actually runs; document.

## Backend (add to app.py)
- `GET /api/workloads` — list Queues + Producers + Consumers with live state
  (+ for running producers/consumers, their pod status from kubectl).
- `POST /api/workloads/queue {name, partitions}` — create+Activate a Queue.
- `POST /api/workloads/producer {queue, rate_per_sec, msg_size, concurrency}` —
  create Producer + Start (auto-approved) → executor deploys the producer pod.
- `POST /api/workloads/consumer {queue, process_ms, concurrency}` — create
  Consumer + Start → executor deploys the consumer pod.
- `POST /api/workloads/{kind}/{id}/stop` — Stop → kubectl delete the deployment.
- Executors: ns-pinned (`_assert_namespace`), context-pinned, like the deploy/
  scale executors. Reuse the kubectl chokepoint.

## Frontend (new "Workloads" tab)
- Tab nav gains "Workloads" (alongside Control Plane, Deployment History).
- **Queues** section: list + "Create queue" (name, partitions).
- **Producers** section: list (queue, rate, size, concurrency, status, pod
  health) + "Add producer" form (queue dropdown from Queues, rate/sec, msg size,
  concurrency) + Start/Stop. 
- **Consumers** section: list (queue, process-ms, concurrency, status, lag) +
  "Add consumer" form (queue dropdown, process-ms-per-msg, concurrency) +
  Start/Stop.
- Live status: show running pods + (if easy) throughput/lag from the pod logs or
  `helix.produce.latency_ms` count. Reuse the dark theme + existing components.

## Constraints
- ns `dark-factory` ONLY (assert), context-pinned. Real pods, real Kafka traffic.
- All create/start/stop go THROUGH the governed entity (create→Define→Start→
  executor). Auto-approved (no manual gate), but the entity transitions + Cedar
  still enforce who can act and record what happened.
- Don't break Control Plane / Deployment History tabs.
- Tolerate capacity limits gracefully (the dedicated pool is finite; many
  concurrent workload pods may not schedule — surface real pod state).
- READ-ONLY on everything except the workload Deployments it owns
  (producer-*/consumer-*) and the workload Temper entities.

## Verify (curl + then Chrome MCP)
- Create a queue `orders-2` (partitions 3).
- Add a producer to it (rate 500/s, 512B, concurrency 2) → confirm a
  producer-<id> Deployment runs + traffic appears (helix.produce.latency_ms.count
  rises in Datadog, or producer pod logs show sends).
- Add a consumer (process_ms 5, concurrency 2) → confirm consumer pods run +
  consume. Stop both → pods deleted. Entities end Stopped.
- Workloads tab lists everything with live status; console clean.
