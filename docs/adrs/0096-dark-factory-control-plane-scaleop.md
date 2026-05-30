# ADR-0096: Dark Factory Control Plane — governed ScaleOp + control endpoints

- Status: Accepted
- Date: 2026-05-24
- Deciders: Temper core maintainers
- Related:
  - ADR-0093: Dark Factory CI/CD and Deploy governance (the Deploy entity + deploy executor + ns/context pinning this mirrors)
  - ADR-0033: Verified agent identity / credential registry (the supervisor token is the human gate)
  - `reference-apps/identity/dark-factory-specs/` (where the ScaleOp spec hot-loads)
  - `reference-apps/deploy-history/` (the mini-app extended into a control plane)

## Context

The deploy-history mini-app (ADR-0093 lineage) is a read-only viewer of Helix
deployments governed through the dark-factory tenant. The Dark Factory needs a
**control plane**: start deployments, rollback, and scale the Helix StatefulSet —
but every control action must remain governed. Raw `kubectl` from a button would
bypass the governance story that is the whole point of the platform.

The Deploy entity already covers deploy + rollback (Cedar-gated Approve). There
is no governed entity for **scaling** the cluster, so a scale button would have
nothing to route through.

## Decision

### Sub-Decision 1: New governed `ScaleOp` entity

Author a `ScaleOp` I/O Automaton (`.ioa.toml` + CSDL bound actions + Cedar) in
`reference-apps/identity/dark-factory-specs/` so it hot-loads into the running
`dark-factory` tenant (same bundle the server boots from). It mirrors the Deploy
pattern exactly:

- States: `Requested → Approved → Scaling → Done` (+ `Failed`).
- Fields: `target_replicas` (int), `current_replicas` (int), `result` (string),
  `approved` (bool).
- Actions: `Request(target_replicas, current_replicas)`, `Approve()` (HUMAN
  GATE), `Apply()` (guard: approved), `RecordResult(result)`, `MarkDone()`,
  `Fail(reason)`.
- Cedar: read/list open; `create`/`Request` for ci-service/implementer/symphony/
  supervisor/human; **`Approve` restricted to verified `supervisor`/`human`**
  (the gate); `Apply`/`RecordResult`/`MarkDone`/`Fail` for deploy-service/
  supervisor/human. Default-deny otherwise.

**Why this approach**: scaling is a distinct lifecycle from deploying (no image
tag, different kubectl verb, different failure mode — capacity), so it earns its
own entity rather than overloading Deploy. Mirroring Deploy keeps the governance
shape identical and re-uses the already-registered supervisor identity.

### Sub-Decision 2: Control endpoints on the deploy-history app

Extend `reference-apps/deploy-history/app.py` (previously read-only) with a
control surface that drives the governed entities, never raw kubectl from a
button:

- `GET /api/cluster` — read-only `kubectl get statefulset/pods` (configured/ready
  replicas, current image tag, per-pod status).
- `GET /api/images` — Artifact Registry tags for the deploy dropdown.
- `POST /api/actions/deploy {image_tag}` — create + `Request` a Deploy, return
  its id + pending-approval state.
- `POST /api/actions/scale {target_replicas}` — create + `Request` a ScaleOp.
- `POST /api/actions/{kind}/{id}/approve` — drive `Approve` (supervisor token) →
  `Apply` → run the inline executor → `RecordResult` → `MarkDone`/`MarkLive`.
- `POST /api/actions/deploy/{id}/rollback` — drive the rollback executor.

The Approve step uses the **real supervisor token** from `tokens.json` — the UI
Approve button is the human acting as supervisor, not a header-spoofed bypass.

### Sub-Decision 3: Real, ns-pinned, governed kubectl execution

Executors run for real (this is a control plane) but are pinned to namespace
`dark-factory` and context `gke_datadog-sandbox_us-west3_gs-us-west3`, asserted
in code. They only fire after the entity reaches its action state (`Rolling` for
deploy, `Scaling` for scale) — i.e. after the human Approve + Apply. The
namespace is never read from entity data.

## Consequences

- The control plane IS the governed loop: create entity → Request → human
  Approve (supervisor token via UI) → Apply → executor → record → land.
- Capacity failures on scale-up (>3 on the shared cluster) surface as real pod
  state + a `Failed`/`RecordResult` outcome, not a silent error.
- The read-only history view is untouched and keeps working.
