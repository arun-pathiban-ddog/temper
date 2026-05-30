# Directed Evolution — Handoff / Run Guide

This is the "Dark Factory for Helix" demo: a governed, self-improving Helix
(Kafka-replacement) loop on Temper — agents watch telemetry, breed performance
variants under human-set fitness goals, instrument new metrics, and ship verified
changes, all gated by Cedar. The operator UI is the **deploy-history** app
("Directed Evolution") on port 4100.

> ⚠️ **This is NOT a `git clone && run` project.** It depends on a second repo, a
> built Rust binary, secrets that are gitignored, and live cloud access. Read the
> "What you must obtain" section first. Several absolute paths are hardcoded to the
> original author's home dir and **must be edited** (see "Fix the hardcoded paths").

---

## 1. What you must obtain (not in this repo)

| Thing | Why | How to get it |
|---|---|---|
| **`helix` repo** | The agents shell out to it for git worktrees, diffs, code edits | Clone `arun-pathiban-ddog/helix` next to this repo (see layout below). It's a fork of upstream `jm424/helix`; the demo branches (`champion-metrics`, `instrument/system-cpu-microseconds-per-message`, `evolve/throughput`, `evolve/latency`) live on the fork. |
| **Built Temper binary** | The governance server | `cargo build --release` in this repo (~minutes, needs Rust 1.85 / edition 2024) |
| **`reference-apps/identity/tokens.json`** | Agent identity bearer tokens (operator/supervisor/breeder/…) — **gitignored, secret** | Get from the author, OR regenerate (see §4). Shape: `tokens.example.json` |
| **`reference-apps/deploy-history/.dd-env.json`** | Datadog API keys for the observer/researcher live-query path — **gitignored, secret** | Get from the author. Shape: `{"DD_API_KEY":"…","DD_APP_KEY":"…","DD_SITE":"datadoghq.com"}` |
| **`gcloud` + `kubectl` access** | Real cluster deploys / status | `gcloud container clusters get-credentials gs-us-west3 --project datadog-sandbox --region us-west3` → context `gke_datadog-sandbox_us-west3_gs-us-west3`, namespace `dark-factory` |
| **Datadog keys for the gensim org** | The `helix.*` metrics live there | The keys in `.dd-env.json` must be able to query `helix.produce.latency_ms.*` etc. |
| **`ANTHROPIC_API_KEY` (real `sk-ant-…`)** | Plain-English/intent goal parsing | Must be a direct Anthropic key (the code hits `api.anthropic.com`, not a gateway) |
| **Python deps** | The UI backend + agents | `pip install fastapi uvicorn httpx anthropic confluent-kafka` |

### Expected directory layout (paths are hardcoded to this!)
```
<root>/
  temper/                      # this repo
  helix/                       # the helix repo (jm424/helix)
  helix-worktrees/             # created by the app for per-cluster/metric builds
  evolution-worktrees/         # created by the evolution harness
  symphony-worktrees/          # created by Symphony
```
The original author's `<root>` was `/Users/arun.parthiban/notdd`. **Yours will
differ — see §3.**

---

## 2. The components & ports

| Port | Process | What |
|---|---|---|
| 3000 | `temper serve` | The governance server (Cedar + entities + OData) |
| 4100 | `uvicorn app:app` (reference-apps/deploy-history) | The **Directed Evolution UI** (Clusters, Breeds, Agent Runs, Symphony, Control Plane, Workloads, Deployment History) |
| 4000 | (optional) Observe UI (`ui/observe`, Next.js) | The Temper Observe UI — not required to run the demo |

The agents (Observer, Researcher, Symphony, Breeder, Evolution) are **CLI
one-shots** in `reference-apps/{observer,symphony,breeder,evolution}` — they run
on demand or via the in-app scheduler (Agent Runs tab).

---

## 3. Fix the hardcoded paths (REQUIRED on a different machine)

Every absolute path below points at the author's home dir. Edit each to your
`<root>`. (Search the repo for `/Users/arun.parthiban/notdd` to catch any others.)

| File:line | Constant | Change to |
|---|---|---|
| `reference-apps/deploy-history/app.py:57` | `HELIX_REPO` | `<root>/helix` |
| `reference-apps/deploy-history/app.py:59` | `TOKENS_PATH` | `<root>/temper/reference-apps/identity/tokens.json` |
| `reference-apps/deploy-history/app.py:2507` | cluster worktree root | `<root>/helix-worktrees/cluster-…` |
| `reference-apps/deploy-history/app.py:3004` | `SYMPHONY_WORKTREE_ROOT` | `<root>/symphony-worktrees` |
| `reference-apps/deploy-history/app.py:3584` | `_METRIC_CATALOG_PATH` | `<root>/temper/reference-apps/breeder/snapshots/metric_catalog.json` |
| `reference-apps/deploy-history/app.py:3861` | metric worktree root | `<root>/helix-worktrees/metric-…` |
| `reference-apps/deploy-history/app.py:4005` | `REF_APPS` | `<root>/temper/reference-apps` |
| `reference-apps/symphony/symphony/config.py:18` | `DEFAULT_HELIX_REPO` | `<root>/helix` |
| `reference-apps/observer/observer/research.py:32` | `HELIX_REPO_DEFAULT` | `<root>/helix` |
| `reference-apps/observer/observer/main.py:123` | `--helix-repo` default | `<root>/helix` |
| `reference-apps/evolution/main.py:33-34` | `HELIX_REPO`, `WORKTREE_ROOT` | `<root>/helix`, `<root>/evolution-worktrees` |
| `reference-apps/evolution/{bench,genome,verify}.py` | `HELIX_REPO` default | `<root>/helix` |

Also: the UI backend (`app.py`) is also pinned to the GKE context
`gke_datadog-sandbox_us-west3_gs-us-west3` and namespace `dark-factory`
(`app.py:70-71`). Keep these unless you're targeting a different cluster.

---

## 4. Run sequence

```bash
ROOT=<your root>            # the dir containing temper/ and helix/
cd $ROOT/temper

# (one-time) build Temper
cargo build --release

# (one-time, if you don't have tokens.json) register fresh identities:
#   the operator token is the global admin; tokens.json is generated alongside.
#   See reference-apps/identity/register_identities.py + register_breeders.py.

# 1. Start Temper WITH the admin key — CRITICAL.
#    Without TEMPER_API_KEY set, the operator loses admin and ALL governed
#    actions (Define/Approve/…) return 403. (See "Gotchas".)
OP=$(python3 -c "import json;print(json.load(open('reference-apps/identity/tokens.json'))['operator']['token'])")
TEMPER_API_KEY="$OP" ./target/release/temper serve --port 3000 \
  --app dark-factory=$ROOT/temper/reference-apps/identity/dark-factory-specs \
  --no-observe > /tmp/temper-server.log 2>&1 &

sleep 8

# 2. Register identities + breeders (restores verified agent_type; idempotent).
#    REQUIRED after EVERY temper restart, or governed actions 403.
TEMPER_API_KEY="$OP" python3 reference-apps/identity/register_identities.py
TEMPER_API_KEY="$OP" python3 reference-apps/identity/register_breeders.py

# 3. Start the UI backend.
cd reference-apps/deploy-history
python3 -m uvicorn app:app --host 127.0.0.1 --port 4100 > /tmp/deploy-history.log 2>&1 &

# open http://127.0.0.1:4100
```

Verify it's healthy:
```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "X-Tenant-Id: dark-factory" \
  -H "Authorization: Bearer $OP" http://127.0.0.1:3000/tdata/Clusters   # 200
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:4100/          # 200
```

---

## 5. Gotchas (the things that will bite you)

1. **Restarting Temper wipes runtime identities.** After ANY `temper serve`
   restart you MUST re-run `register_identities.py` + `register_breeders.py`
   (with `TEMPER_API_KEY` set), or every governed Define/Approve returns 403.
   The server even logs `deleted N uncommitted specs during startup recovery` —
   that's the identity wipe.

2. **`TEMPER_API_KEY` must be the operator token in the server's env**, or the
   operator isn't treated as admin and the genesis registration itself 403s
   (chicken-and-egg).

3. **Datadog org**: the `helix.*` metrics are in the **gensim** org. The keys in
   `.dd-env.json` must see them — validate with
   `avg:helix.produce.latency_ms.95percentile{*}` returning data. (Observer
   queries need the `{*}` scope — already handled in code.)

4. **The instrument-to-measure + cluster builds are real ~8–10 min Cloud Builds**
   that deploy to live GKE. Not instant. They background; watch the UI or
   `/api/clusters` / `/api/goals/instrument/status`.

5. **The agent scheduler (Agent Runs tab) is autonomous when ON** — it edits
   Helix code and deploys to GKE on a timer, unattended. It defaults OFF; only
   start it deliberately.

6. **metrics.rs lives on the `champion-metrics` branch, not `main`** — the
   instrument loop and the live image lineage build off `champion-metrics`
   (main predates the DogStatsD exporter). All demo branches are on the
   `arun-pathiban-ddog/helix` fork, **not** upstream `jm424/helix` (which is
   read-only): `champion-metrics` (base + the DogStatsD exporter + the
   `helix.consume.*` metrics), `instrument/system-cpu-microseconds-per-message`
   (the live agent-authored metric), `evolve/throughput` (the bred `linger_ms=21`
   champion), `evolve/latency`.

---

## 6. Using the UI (http://127.0.0.1:4100)

- **Clusters** — the git-lineage tree + live Helix clusters; create/approve/teardown clusters (real GKE).
- **Breeds** — set fitness goals (incl. plain-English "intent" goals → the agent discovers/creates metrics), see per-cluster breeds (speciation + culling), the breeder telemetry firewall, and **Migrations** (move a queue to a niche cluster, "+ Migrate queue").
- **Agent Runs** — timeline of every agent pass + the **Loop scheduler** (start/stop the cron, per-agent intervals, run-now).
- **Symphony** — the ImprovementIssue pipeline (Observed→…→Done).
- **Control Plane / Workloads / Deployment History** — deploy/scale/rollback, create queues/producers/consumers, governed deploy history with code diffs.

---

## 7. ADRs (the design record)
- `docs/adrs/0093` Symphony · `0094` Observer/Researcher · `0095` breeding harness
- `0096` control plane/ScaleOp · `0097` workloads · `0098` multi-cluster
- `0099` Breeds + telemetry firewall · `0100` AgentRun + scheduler · `0101` intent-driven metrics
