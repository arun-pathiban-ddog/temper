# ADR-0098: Governed multi-cluster Helix deploy from per-cluster worktrees

- Status: Proposed
- Date: 2026-05-25
- Deciders: Temper core maintainers
- Related:
  - ADR-0096: Dark Factory control plane + ScaleOp (the single-cluster Deploy/ScaleOp flow this extends)
  - ADR-0095: Directed Evolution breeding harness (worktree-per-variant pattern this borrows)
  - ADR-0097: Directed Evolution workloads (the inline k8s manifest + ns-pinned executor pattern reused here)
  - `reference-apps/deploy-history/app.py` (control-plane backend)
  - `reference-apps/identity/dark-factory-specs/` (governed entity specs)
  - `/Users/arun.parthiban/notdd/helix/docker/k8s/helix-statefulset.yaml` (the manifest templated per cluster)

## Context

Today the Directed Evolution control plane (ADR-0096) operates on exactly **one**
Helix cluster: a hardcoded StatefulSet named `helix` in namespace `dark-factory`.
The governed `Deploy` entity only ever *re-images that one StatefulSet*
(`kubectl set image`). There is no way to stand up a *new, independent* Helix
cluster from the UI, and no per-cluster source of truth that could later be
edited and re-deployed.

We want the ability to deploy new Helix clusters from the UI, where **each
deployment owns its own git worktree** of the helix repo and is built+deployed
from that worktree — so a cluster's source can be modified later and the cluster
rebuilt/redeployed from its own branch. This mirrors the breeding harness's
worktree-per-variant model (ADR-0095) but brings it under the human-gated
control plane.

## Decision

### Sub-Decision 1: A new governed `Cluster` entity (not an overloaded `Deploy`)

Add a first-class `Cluster` entity (`cluster.ioa.toml` + `policies/cluster.cedar`,
registered in `model.csdl.xml`). Lifecycle:

```
Defined --Approve(human)--> Approved --Apply--> Building --RecordBuild--> Deploying
   --RecordResult--> (MarkLive -> Live | MarkFailed -> Failed)
Live --Teardown--> Torndown
```

State vars mirror `Deploy`: `approved`, `built`, `has_result`, plus terminal
invariants (`LiveIsFinal`-style, `ApplyRequiresApproval`). Action params:
`name`, `branch`, `worktree_path`, `image_tag`, `replicas`.

**Why this approach**: `Deploy` means "re-image the one running cluster"; its
Cedar policy, invariants, and the existing executor all assume a single target.
Overloading it with cluster-creation semantics would muddy that contract and the
deployment-history timeline. A separate entity keeps each concept coherent and
keeps the platform-philosophy "one entity, one lifecycle" property. The **Approve
action stays the Cedar human gate** — identical policy shape to `deploy.cedar`
(only verified `supervisor`/`human` may Approve; agents may Request/create).

### Sub-Decision 2: Separate StatefulSet per cluster, same namespace

Each cluster is its own StatefulSet `helix-<name>` + two Services
(`helix-<name>`, `helix-<name>-headless`) in `dark-factory`. The existing
single `helix` cluster is untouched.

**Why this approach**: keeps the existing kubectl safety pin
(`namespace == dark-factory`, asserted in `_assert_namespace`) fully intact —
no new-namespace machinery, no widening of the chokepoint. Separate namespaces
would give stronger isolation but break that pin and the executor's hard-coded
ns; rejected for this iteration (see Non-Goals).

**Critical correctness constraint**: `helix-statefulset.yaml` hardcodes
`app: helix`, `serviceName: helix-headless`, and derives Raft peers from
`helix-headless.dark-factory.svc.cluster.local`. If reused verbatim, two
clusters would share the `app: helix` selector — their Services would route to
each other's pods and Raft peers would cross-wire. The per-cluster manifest
template **must** parametrize: StatefulSet name, both Service names, the `app`
label/selector (`app: helix-<name>`), `serviceName`, and the peer-DNS domain
(`helix-<name>-headless.dark-factory.svc.cluster.local`). This is the central
implementation risk and is covered by the manifest-template task.

### Sub-Decision 3: Build the image from the cluster's worktree

On `Apply`, the backend (a) creates a git worktree of the helix repo on a new
branch `df-cluster/<name>` off `main`, (b) submits a Cloud Build from that
worktree producing a per-cluster image tag (`cluster-<name>`), (c) on build
success, `kubectl apply`s the templated manifest pinned to that tag.

**Why this approach**: the user explicitly wants "deploy from the worktree" so
the worktree is the editable source of truth — future changes to
`helix-worktrees`/`<name>` rebuild that cluster. Building (vs. picking an
existing tag) is the faithful interpretation; the ~8-10 min amd64 Cloud Build is
accepted as the cost. The entity's `Building`/`Deploying` states make that
latency observable in the UI rather than a silent hang.

## Rollout Plan

1. **Phase 0 (this change)** — ADR; `Cluster` spec + Cedar + CSDL (verified via
   L0-L3); per-cluster manifest template; worktree+build+deploy backend
   endpoints; Control Plane UI section. Build path defaults to real (the user
   asked to build); teardown removes the StatefulSet+Services (ns-pinned) and
   optionally prunes the worktree.
2. **Phase 1 (follow-up)** — wire a cluster-aware executor (mirror
   `deploy_executor.py`) so Apply→Building→Deploying is driven out-of-band like
   `Deploy`, rather than inline in the request handler; add per-cluster
   workload targeting (producers/consumers choosing which cluster).
3. **Phase 2** — edit-and-rebuild a cluster from its worktree through the UI.

## Consequences

### Positive
- Multiple independent Helix clusters, each with an editable source worktree,
  all under the human-gated governed flow.
- The existing single-cluster `Deploy`/`ScaleOp`/Workloads flows are unchanged.
- Reuses the proven ns-pinned kubectl chokepoint and Cloud Build path.

### Negative
- Per-cluster Cloud Builds are slow (~8-10 min); cluster creation is not instant.
- More moving parts in `app.py` (worktree mgmt, build polling, manifest templating).
- Worktrees accumulate on disk until torn down/pruned.

### Risks
- **Selector/peer collision** if the manifest is not fully parametrized — two
  clusters cross-wire. Mitigation: parametrize every name/label/DNS; verify with
  a second cluster before declaring done.
- **Shared-cluster capacity**: N×3 Helix pods on the dark-factory node pool may
  exhaust capacity. Mitigation: small default `replicas`, surface scheduling
  failures in the cluster state; the existing scale note already warns about this.
- **Worktree drift / dirty trees** built into images silently. Mitigation: build
  from a clean branch off `main`; record the branch + commit on the entity.

### DST Compliance
Not applicable — all new code lives in the `deploy-history` reference app and
`.ioa.toml`/Cedar specs, none of which are simulation-visible Temper crates
(`temper-runtime`/`temper-jit`/`temper-server`). The `Cluster` spec is verified
by the standard L0-L3 cascade on `.ioa.toml` edit.

## Non-Goals

- Namespace-per-cluster isolation (keeps the ns pin; revisit if needed).
- Multi-region / multi-GKE-cluster deploys.
- Automatic capacity management or autoscaling of the node pool.
- Cross-cluster Raft / federation — each cluster is independent.

## Alternatives Considered

1. **Extend the `Deploy` entity with cluster fields** — fewer new specs, but
   overloads "re-image the one cluster" with "create a cluster," muddying the
   Cedar policy, invariants, and history timeline. Rejected for coherence.
2. **Pick an existing image tag, worktree only for later edits** — no build wait,
   but doesn't actually "deploy from the worktree" as asked; the first deploy
   wouldn't reflect the worktree's source. Rejected per the user's intent.
3. **Namespace per cluster** — stronger isolation but breaks the hardcoded
   ns=dark-factory safety pin and the executor's single-ns assumption. Deferred.

## Rollback Policy

The feature is additive: the `Cluster` entity, its endpoints, and the UI section
are new surfaces. To roll back, remove the UI section + `/api/clusters*`
endpoints and unregister the `Cluster` EntityType from the CSDL (the existing
`helix` cluster and all other flows are unaffected). Any clusters already created
are torn down via the ns-pinned `kubectl delete statefulset/service` for their
`helix-<name>` resources, and their worktrees pruned with `git worktree remove`.
