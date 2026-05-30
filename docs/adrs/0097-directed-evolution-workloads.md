# ADR-0097: Directed Evolution Workloads (governed Kafka load generation)

- Status: Accepted
- Date: 2026-05-24
- Deciders: Temper core maintainers
- Related:
  - ADR-0096: Dark Factory Control Plane + ScaleOp (the governed-entity → ns-pinned kubectl executor pattern this ADR mirrors)
  - ADR-0093: Dark Factory CI/CD and deploy governance (Cedar gate + executor pattern)
  - `reference-apps/deploy-history/app.py` (FastAPI backend extended here)
  - `reference-apps/identity/dark-factory-specs/` (the hot-loaded app bundle)
  - `docs/directed-evolution-workloads-spec.md` (the build contract)

## Context

The Directed Evolution app (deploy-history mini-app, port 4100) can observe the
Helix cluster and ACT on it (deploy/rollback/scale) through governed Temper
entities. What it cannot do is **generate workload** against Helix on demand:
create Kafka topics/queues, run producers at a chosen rate/size/concurrency, and
run consumers with a controllable per-message process time. Demonstrating
directed evolution under load requires being able to dial that load up and down.

Deploy/Scale are risky (they change the running cluster image / replica count),
so they keep a human approval gate (Cedar-gated `Approve`). Creating and tearing
down a producer/consumer pod is **routine, low-risk workload management** — a
fluid console, not an approval queue. So workloads are **auto-approved**: still
governed and audited through the entity lifecycle + Cedar (a verified
workload-operator/supervisor identity is permitted), but with NO manual approval
click per producer. The entity transitions + Cedar + audit trail are the
governance; the human gate is dropped for these ops.

## Decision

### Sub-Decision 1: Three governed entities (Queue, Producer, Consumer)

Authored as `.ioa.toml` + registered in `model.csdl.xml` + a `.cedar` policy in
`reference-apps/identity/dark-factory-specs/`, hot-loaded into the `dark-factory`
tenant on server restart. Each mirrors the ScaleOp/CIRun spec patterns.

- **Queue** `Defined → Active` (+ `Deleted`). Fields: `name` (Kafka topic),
  `partitions`. Actions: `Define(name, partitions)`, `Activate()`, `Delete()`.
- **Producer** `Defined → Running → Stopped`. Fields: `queue`, `rate_per_sec`,
  `msg_size`, `concurrency`, `result`. Actions:
  `Define(queue, rate_per_sec, msg_size, concurrency)`, `Start()`, `Stop()`,
  `RecordResult(result)`.
- **Consumer** `Defined → Running → Stopped`. Fields: `queue`, `process_ms`,
  `concurrency`, `result`. Actions:
  `Define(queue, process_ms, concurrency)`, `Start()`, `Stop()`,
  `RecordResult(result)`.

**Why this approach**: A lifecycle entity per workload makes "every workload is
governed" literally true — the running pod's existence is mirrored by an entity
in `Running`, and Stop is a real transition, not a fire-and-forget kubectl. The
`result` field carries the executor's audit string (the exact kubectl + pod
status), exactly like Deploy.RolloutCmd / ScaleOp.ScaleCmd.

### Sub-Decision 2: Auto-approved Cedar (no human gate), still verified

The three `.cedar` policies mirror `ci_run.cedar` (the non-gated model): a
verified `workload-operator`/`supervisor`/`human`/`symphony` principal may
Define/Activate/Start/Stop/Delete/RecordResult. There is **no Approve action**
and therefore no human gate. Default-deny still holds; an unverified principal
(e.g. the bare operator/admin token, which presents no `agent_type`) is denied
the lifecycle actions. So the backend CREATES with the operator token and DRIVES
the lifecycle with the **supervisor** token (verified) — the same identity split
proven in ADR-0096, minus the Approve step.

### Sub-Decision 3: Real pods via the ns-pinned kubectl chokepoint

Start executors create a real k8s `Deployment` (`producer-<id>` / `consumer-<id>`)
in the `dark-factory` namespace via `kubectl apply -f -` through the existing
`_kubectl()` chokepoint (`_assert_namespace()` + context pin). Stop executors
`kubectl delete deployment`. The namespace is NEVER read from entity data. The
app only ever touches Deployments named `producer-*`/`consumer-*` and the new
entities — never the helix StatefulSet, helix-loadgen, or other namespaces.

### Sub-Decision 4: Inline scripts (no image build)

- **Producer**: reuse `confluentinc/cp-kafka:7.6.0` (already proven to schedule
  on the dark-factory pool via helix-loadgen) running `kafka-producer-perf-test`
  with `--throughput=rate_per_sec`, `--record-size=msg_size`, looped forever;
  `concurrency` = Deployment replicas (parallel producer workers).
- **Consumer**: a python image (`python:3.11-slim`) with an inline
  `pip install confluent-kafka` + a heredoc consumer script that joins a
  consumer group (group size = `concurrency` replicas), `sleep(process_ms/1000)`
  per message (the process-time knob), commits, and logs lag.

Both scripts are bundled inline in the Deployment manifest (command + heredoc),
so there is no image to build. Pods are kept SMALL (low cpu/mem requests, modest
concurrency defaults 1-2) so they schedule on the capacity-constrained pool;
scheduling failures are surfaced from real pod state, never faked.

## Consequences

### Positive
- The Workloads tab is a fluid load console; every producer/consumer is a
  governed, audited entity with a real backing pod.
- Reuses the entire ADR-0096 executor chokepoint — one place enforces ns/context.

### Negative
- Auto-approve drops the per-op human gate for workloads (deliberate; Deploy/Scale
  keep theirs). Governance is the entity lifecycle + Cedar identity, not a click.

### Risks
- The dedicated pool is finite; many concurrent workload pods may not schedule.
  Mitigation: small requests, modest defaults, surface real `Pending` pod state.

### DST Compliance
Not applicable: the changed code is the FastAPI reference app (`app.py`) and
declarative spec/policy files — none are simulation-visible Temper crates
(`temper-runtime`/`temper-jit`/`temper-server`). The `.ioa.toml` specs go through
the standard L0-L3 verification cascade on hot-load.

## Non-Goals
- Admin topic creation with exact partition counts if Helix lacks an admin API
  (we record intended partitions; auto-create-on-produce makes the topic).
- Per-message throughput/lag charts in the UI beyond pod status + log tails.

## Alternatives Considered
1. **Raw kubectl from buttons** — rejected; violates the "every action through a
   governed entity" rule that defines this app.
2. **Human-gated workloads (mirror ScaleOp exactly)** — rejected; per the spec,
   workload create/start/stop is routine and should feel fluid, not gated.
3. **Custom producer/consumer container image** — rejected; inline scripts avoid
   a build/push step and keep the demo self-contained.
