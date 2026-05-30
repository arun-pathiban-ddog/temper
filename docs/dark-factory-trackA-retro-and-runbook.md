# Track A — Retro & Hackathon Runbook

Written after a full dry-run build of Track A (Helix telemetry → Datadog on GKE)
on 2026-05-24, ahead of the Autoresearch Systems Hackathon (May 30). The goal of
this doc: turn a multi-hour debugging slog into a ~30-minute repeatable path.

---

## TL;DR — what actually went wrong (and the fix)

Track A's *code* was quick. The time sink was **four infra/integration bugs**,
each of which presented as "metrics aren't showing up" with no error. Knowing
them in advance collapses Track A from ~hours to ~30 min.

| # | Symptom | Root cause | Fix (do this up front) |
|---|---|---|---|
| 1 | Pod `CrashLoopBackOff`, `create_dir_all /var/lib/helix: Permission denied` | Image runs as uid 1000; PVC mounts root-owned | Pod `securityContext: {runAsUser:1000, runAsGroup:1000, fsGroup:1000}` |
| 2 | Pods stuck `Pending`, `exceeded quota: cluster-wide` | Shared nodes 95-99% full; autoscaler quota-capped | Dedicated node pool (but see #4) |
| 3 | Metrics never arrive; exporter logs "enabled" | Used `DD_AGENT_HOST=status.hostIP`, but agent has **no hostPort** on 8125 | `DD_AGENT_HOST=gensim-datadog.datadog.svc.cluster.local` (Service DNS) |
| 4 | Still no metrics even via Service DNS; `nc` UDP send "succeeds" | The node-pool **taint** kept the DD agent DaemonSet off our nodes → DogStatsD blackholes | Node pool with **label only, NO taint** |
| — | `helix.commit.latency_ms` absent in Datadog | Emit placed on `write_internal` returns; Kafka path commits via the tick task | Move emit into the tick/apply path (or drop the metric) |

The biggest lesson: **#3 and #4 are a pair.** On this cluster, DogStatsD is only
reachable two ways — the cluster Service DNS, *and* only if a DD agent runs on
your node. Tainting the pool to "isolate" it silently breaks the second one.

---

## The single most useful upfront check

Before writing any exporter code, answer: **"How does an existing app on this
cluster send custom metrics to Datadog?"** Asking this *first* would have skipped
bugs #3 and #4 entirely. The answer was sitting in any app's pod spec:

```bash
# Find the proven-working pattern other apps use:
kubectl get pods -n ep-dogbank-20260516224649 -o yaml | grep -A6 DD_AGENT_HOST
# -> pulls DD_AGENT_HOST from a configmap = gensim-datadog.datadog.svc.cluster.local
kubectl get svc gensim-datadog -n datadog   # confirms 8125/UDP exists on the Service
kubectl get daemonset gensim-datadog -n datadog -o jsonpath='{.spec.template.spec.tolerations}'
# -> tells you what taints the agent tolerates (so you don't use one it doesn't)
```

**Mantra: match the platform's existing pattern before inventing your own.**

---

## What went well (keep doing)

- **Zero-dependency DogStatsD exporter** (raw `std::net::UdpSocket`). No new crate
  to vet, kept `forbid(unsafe_code)`, tiny review surface. Good call.
- **No-op when `DD_AGENT_HOST` unset.** Kept the 144 unit tests + DST clean,
  no network I/O in the deterministic core. The metrics seam lives only at the
  server I/O boundary — correct per the project's determinism rules.
- **Cloud Build for amd64** (Mac is arm64). `gcloud builds submit` was reliable;
  BuildKit cache made the v2 rebuild fast.
- **Surgical footprint:** ~350 lines, one new module, nothing in Raft/WAL core.
- **Bisecting with a raw `nc` probe** from inside the pod (bypassing Rust) is what
  finally proved the bug was *not* in my code — do this kind of test earlier.

---

## What to improve (process)

1. **Probe the path before trusting "enabled" logs.** The exporter logging
   "DogStatsD metrics export enabled" only means `connect()` succeeded (DNS + a
   bound socket). UDP gives no delivery confirmation, so "enabled" + "SEND_OK"
   from `nc` both lie about end-to-end delivery. Verify with a *received* metric,
   not a sent one.
2. **I burned time querying `search_datadog_metrics`** for confirmation — that
   index lags badly for brand-new custom metrics. Use `get_datadog_metric`
   (live timeseries) to confirm ingestion, not the name index.
3. **I changed two things at once** (Service DNS *and* histogram-vs-distribution)
   before re-testing. When debugging a silent pipeline, change one variable per
   test or you can't attribute the fix.
4. **`helix.commit.latency_ms` was wired on the wrong code path** and I didn't
   catch it until Datadog query time. Lesson: trace the *actual* request path
   (Kafka produce → tick-task apply) before placing instrumentation, rather than
   the most obvious-looking function.

---

## Hackathon Runbook — Track A in ~30 min

Prereqs: `gcloud` auth + `kubectl` context = `gke_datadog-sandbox_us-west3_gs-us-west3`.
All artifacts already exist in the repo from the dry run.

### Step 0 — clean up the leftover taint toleration (one-time, do before the event)
The manifests still carry an obsolete `dedicated/NoSchedule` toleration +
`nodeSelector: pool=dark-factory`. Since the winning approach is **label-only,
no taint**, simplify: keep `nodeSelector: pool=dark-factory`, delete the
`tolerations:` block from both `docker/k8s/helix-statefulset.yaml` and
`helix-loadgen.yaml`. (Harmless if left, but cleaner and removes the trap.)

### Step 1 — node pool (LABEL ONLY, NO TAINT) — ~4 min
```bash
gcloud container node-pools create dark-factory-pool \
  --cluster=gs-us-west3 --location=us-west3 \
  --num-nodes=1 --machine-type=e2-standard-4 --disk-size=50 \
  --node-labels=pool=dark-factory --no-enable-autoupgrade
# regional cluster => 3 nodes (1/zone). NO --node-taints. The DD agent will
# schedule onto these nodes automatically (that's bug #4 pre-empted).
```

### Step 2 — confirm a DD agent lands on each new node — ~1 min
```bash
for n in $(kubectl get nodes -l pool=dark-factory -o name | cut -d/ -f2); do
  kubectl get pods -n datadog --field-selector spec.nodeName=$n --no-headers \
    | grep gensim-datadog | grep -v cluster-agent
done   # expect one Running agent per node BEFORE deploying Helix
```

### Step 3 — build image (only if Helix code changed) — ~5 min
```bash
cd ~/notdd/helix
gcloud builds submit --config=/tmp/helix-cloudbuild.yaml \
  --substitutions=_TAG=metrics-v3 .
# else reuse the existing :metrics-v2 image — skip this step entirely.
```

### Step 4 — deploy Helix + load gen — ~3 min
```bash
kubectl apply -f docker/k8s/helix-statefulset.yaml
kubectl rollout status statefulset/helix -n dark-factory --timeout=180s
kubectl apply -f docker/k8s/helix-loadgen.yaml
```
The manifest already has the load-bearing fixes baked in: `fsGroup:1000` (#1),
`DD_AGENT_HOST=gensim-datadog.datadog.svc.cluster.local` (#3), modest resource
requests, podAntiAffinity for one-pod-per-node.

### Step 5 — verify metrics within ~2 min (NOT the name index)
```bash
# wait ~90s, then query LIVE data:
# avg:helix.produce.latency_ms.95percentile{*}  -> should return numbers
```
Dashboard already exists: **t8q-ntj-v83**
(https://gensim.datadoghq.com/dashboard/t8q-ntj-v83).

### Step 6 — teardown after the demo (avoid lingering cost)
```bash
kubectl delete namespace dark-factory
gcloud container node-pools delete dark-factory-pool \
  --cluster=gs-us-west3 --location=us-west3 --quiet
```

---

## Pre-stage checklist (do the night before, not at 9am)

- [ ] Step 0 manifest cleanup committed.
- [ ] Image `:metrics-v2` (or newer) confirmed present in Artifact Registry.
- [ ] Node pool created + DD agents confirmed on its nodes (Step 1-2).
- [ ] Helix deployed, metrics confirmed live in Datadog (Step 4-5).
- [ ] Dashboard loads with moving data.
- [ ] **Record the 90-sec "metrics flowing" clip now** as demo insurance.
- [ ] Decide: leave it running overnight (costs $) vs. teardown + redeploy day-of
      (redeploy is ~10 min if image + node pool persist).

## Known limitations to mention or fix before demo
- `helix.commit.latency_ms` not emitted on the Kafka path (see table). Either fix
  the emit placement or don't reference it in the demo.
- All Helix changes are **uncommitted** in the working tree — commit to a branch
  before the event so a teammate can rebuild.
- The dashboard is in the **gensim** Datadog org/sub-org — make sure demo viewers
  have access to `gensim.datadoghq.com`.
