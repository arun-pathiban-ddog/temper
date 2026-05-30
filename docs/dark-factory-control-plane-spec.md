# Control Plane UI — Spec

Extends the deploy-history mini-app (`reference-apps/deploy-history/`, port 4100)
into a unified observe + **control plane**: start deployments, rollback, and
scale Helix — all through governed Temper entities + Cedar approval gates, with
real kubectl execution.

## Core principle
Every control action flows through a **governed Temper entity** (not raw kubectl
from the button). The UI surfaces the Cedar approval gate inline; the human
approves (as supervisor); an executor does the real kubectl. This keeps the
governance story intact — the control plane IS the governed loop with a human
at the wheel.

## Environment
- Temper server http://127.0.0.1:3000, tenant `dark-factory`. Tokens in `reference-apps/identity/tokens.json` (`.token` field): `operator` (admin), `supervisor` (the human-gate approver). Use **supervisor** token for Approve actions, **operator** for create/read.
- Helix on GKE: StatefulSet `helix`, ns `dark-factory`, context `gke_datadog-sandbox_us-west3_gs-us-west3`, image repo `us-west3-docker.pkg.dev/datadog-sandbox/dark-factory-helix/helix-server`.
- Available image tags (deploy dropdown): query Artifact Registry, currently `champion-throughput`, `latest`, `metrics-v1`, `metrics-v2`.
- Capacity is contested: scaling >3 may not schedule (shared cluster). Surface failures gracefully.

## Governed entities

### Deploy (exists) — start deployment + rollback
- Lifecycle: `Pending --Request--> Pending --Approve[HUMAN GATE]--> Approved --Apply--> Rolling --MarkLive--> Live` (or `Rolling --Rollback--> RolledBack`).
- Bound actions (OData `/tdata/Deploys('<id>')/Default.<Action>`): `Request(issue_id, image_tag)`, `Approve()`, `Apply()`, `RecordResult(result)`, `MarkLive()`, `Rollback()`.
- **Start deploy** = create Deploy entity (id `deploy-<tag>-<ts>`) -> Request(image_tag) -> surface Approve gate -> on approve: Apply -> executor runs `kubectl set image` -> RecordResult -> MarkLive.
- **Rollback** = on a Live/Rolling deploy, drive Rollback -> executor runs `kubectl rollout undo statefulset/helix` -> RolledBack. (Rollback in the spec is from `Rolling`; if the deploy is already `Live` you may need a new Deploy of the previous image, OR add a Rollback-from-Live path — pick the simplest that works and document it.)

### ScaleOp (NEW — build this) — scale up/down, governed
- Author a Temper spec (`.ioa.toml` + CSDL + cedar) in `reference-apps/identity/dark-factory-specs/` and hot-load it (the breeder/identity specs load from there).
- States: `Requested --Approve[HUMAN GATE]--> Approved --Apply--> Scaling --MarkDone--> Done` (+ `Failed`).
- Fields: `target_replicas` (int), `current_replicas` (int), `result` (string), `approved` (bool).
- Actions: `Request(target_replicas)`, `Approve()`, `Apply()`, `RecordResult(result)`, `MarkDone()`, `Fail(reason)`.
- Cedar: only `agent_type in [supervisor, human]` (verified) may `Approve`; mirror deploy.cedar. Reuse the breeder-supervisor / supervisor identity already registered.

## Backend (add to deploy-history app.py)
- `GET /api/cluster` — current StatefulSet state: configured_replicas, ready_replicas, current image tag, per-pod status. (kubectl get, read-only.)
- `GET /api/images` — available image tags from the registry (for the deploy dropdown).
- `POST /api/actions/deploy {image_tag}` — create + Request a Deploy entity; return its id + pending-approval state.
- `POST /api/actions/scale {target_replicas}` — create + Request a ScaleOp entity; return id + pending state.
- `POST /api/actions/{kind}/{id}/approve` — drive Approve (supervisor token) -> Apply -> trigger the executor. Returns updated state.
- `POST /api/actions/deploy/{id}/rollback` — drive Rollback + run the rollback executor.
- Executors (can be inline functions called after Apply, or the existing executor scripts): deploy = `kubectl set image statefulset/helix helix=<repo>:<tag>` + `kubectl rollout status`; scale = `kubectl scale statefulset/helix --replicas=N`; rollback = `kubectl rollout undo statefulset/helix`. ALL pinned to ns dark-factory + the context. Guard real mutations behind an env flag if you want a dry-run mode, but for the control plane the actions should actually execute (that's the point) — default to real, ns-pinned, with the namespace assertion.

## Frontend (add a "Control Plane" view to the SPA)
- **Cluster status card**: current image tag, configured/ready replicas, per-pod health dots, lineage of the running image.
- **Start Deployment**: image-tag dropdown (from /api/images) + "Deploy" button -> creates governed Deploy -> shows an inline **approval gate** ("Pending your approval — [Approve] [Deny]") -> on Approve, shows Rolling -> Live, with the kubectl result.
- **Rollback**: button on the current live deploy -> governed Rollback -> rolling undo.
- **Scale**: a stepper `[-] N [+]` + "Apply scale" -> creates governed ScaleOp -> approval gate -> scales. Show a note that >3 may not schedule on the shared cluster.
- Each in-flight action shows its governed-entity status + (when relevant) the Cedar approval step the human just clicked through. Reuse the existing dark theme + components.
- Keep observe (history) and control in one app; a tab or side-nav to switch.

## Constraints
- ns `dark-factory` ONLY (assert it; never touch other namespaces). Pin the kube context.
- Actions execute for real (deploy/scale/rollback) — but ONLY via the governed entity flow after approval. No button should kubectl without the entity reaching its action state.
- Never approve on the agent's own behalf in a way that bypasses the gate — the UI's Approve button IS the human acting as supervisor (legit).
- Tolerate capacity failures (scale-up Pending) gracefully — surface the real kubectl/pod state.

## Verify (via Chrome MCP)
- Cluster status card shows the real 3-node champion cluster.
- Start a deploy of `metrics-v2` (a real different image) -> approval gate appears -> approve -> watch it roll -> cluster card updates to metrics-v2. Then deploy `champion-throughput` back.
- Scale to 2 -> approve -> cluster shows 2 ready. Scale back to 3.
- Rollback works or is clearly explained.
- Console clean.
