# Dark Factory for Helix — Hackathon Build Plan

**Event:** Autoresearch Systems Hackathon (Modal / OpenAI / Raindrop / Antler)
**When:** Sat May 30 2026, 8:00 AM – 8:00 PM PT · Antler HQ, SF · **Submission 6:15 PM**
**Theme fit:** Autoresearch = agents that iteratively plan, search, synthesize over an extended horizon in a compute-intensive domain. We frame distributed-systems performance work as *research*: hypothesis → experiment → evidence → verified change.

## One-liner
An autonomous research loop that watches a real Helix cluster in production, forms hypotheses about how to improve it, runs experiments, and ships verified fixes — with every consequential action governed by a human-approvable policy engine (Temper + Cedar).

## The loop (the demo)
```
Helix 3-node (GKE) + helix-workload  ──OTLP──>  Datadog
        │ Observer agent (Datadog MCP)
        ▼
 [OBSERVE]  p99 produce latency high / opportunity spotted
        ▼
 [RESEARCH] 3 hypotheses → helix-bench / DST seed experiments → confirm 1
        ▼
 [PLAN]   Temper PM Issue → BeginPlanning → WritePlan
          → ApprovePlan   ◄── CEDAR GATE / Observe UI (human)   ★WOW: governance
        ▼
 [IMPLEMENT] Symphony-on-Temper: isolated worktree → coding agent edits Helix → commit → PR
        ▼
 [VERIFY]  CIRun entity → cargo test + DST + TLA+ cascade
          → CEDAR GATE before Deploy ◄── human approves
        ▼
 [DEPLOY] Deploy entity → kubectl rollout to GKE
        ▼
 Datadog p99 dips back down ──────────────► back to OBSERVE   ★WOW: loop closed
```

## Locked decisions (2026-05-24)
- **GKE + Datadog ready now** — build the real cloud path; no cloud fallback detour.
- **Coding agent: decide on-site** — Symphony runner uses a swappable adapter (Claude Code | Codex).
- **Team: 2–3** — three parallel tracks with explicit integration contracts.
- **Must-have if short on time: the closed loop end-to-end (breadth > depth).** Thin research depth or the 2nd Cedar gate before ever thinning the loop.

## Demo-risk mitigation
GKE + Datadog + live coding-agent + live approvals in one 12-min window is risky. **At ~4:45 PM, record a flawless full-loop dry run as insurance; present live with the recording as fallback.** Everything stays real — we just don't bet the demo on zero infra flakiness at 6:15.

---

## Integration contracts (define FIRST so tracks don't collide)
1. **Telemetry contract** — Helix emits **DogStatsD distribution** metrics (not OTLP — the shared GKE Datadog agent has the OTLP receiver OFF, but DogStatsD :8125 with `NON_LOCAL_TRAFFIC=true` is ON) with stable names: `helix.produce.latency_ms` (distribution `:d`), `helix.commit.latency_ms` (distribution), `helix.replication.lag` (gauge), plus tags `service:helix`, `node_id:<n>`, `cluster:<id>`, `topic:<t>`. Helix reads `DD_AGENT_HOST` (set to node hostIP via fieldRef) + `DD_DOGSTATSD_PORT=8125`; exporter is a no-op when `DD_AGENT_HOST` is unset (local/test). Observer queries these exact names via Datadog MCP.
   - **Why DogStatsD:** shared cluster agent (`gensim-datadog` DaemonSet, ns `datadog`) exposes 8125 (DogStatsD) + 8126 (APM) but NOT 4317/4318 (OTLP). Reconfiguring it would disrupt other tenants. Distributions give server-side p50/p95/p99.
2. **Tracker contract** — a Temper entity `ImprovementIssue` (states: `Observed → Researching → Planned → Implementing → Verifying → Deploying → Done`) is the single source of truth all agents read/write. Symphony reads candidates from it; Observer creates them; CIRun/Deploy update it.
3. **Capability contract** — `CIRun.execute(repo, ref)` → runs verification, returns pass/fail + report; `Deploy.apply(image_tag)` → `kubectl rollout`, returns status. Both Cedar-gated (default-deny → human approve).
4. **PR contract** — Symphony outputs a branch `darkfactory/<issue-id>` + PR URL written back onto the `ImprovementIssue`.

---

## Tracks (parallel)

### Track A — Helix telemetry + GKE  *(start first, long pole — but creds are ready)*
- **A1** Add OTLP exporter to `helix-server` (wrap existing `tracing`; add the 3 contract metrics). ~2h
  - Targets to instrument: produce path (`service/handlers/write.rs`), commit latency (raft apply), replication lag (leader commit − follower apply).
- **A2** GKE: build/push image, 3-node StatefulSet, Datadog agent + OTLP intake, dashboard with the loop's hero graph (p99 produce latency). ~3h
- **A3** `helix-workload` as a steady-load Deployment driving Kafka traffic. ~1h

### Track B — Temper capabilities (CI/CD + deploy governance)
- **B1** Author `CIRun` + `Deploy` Temper apps: `.ioa.toml` (states/actions/guards) + `.csdl.xml` (data contract) + `.cedar` (default-deny, human-approval gate). Pattern: copy `os-apps/agent-orchestration`. Submit via `temper.submit_specs`, approve in Observe UI. ~2h
- **B2** Wire executors: `CIRun.execute` → `cargo test --workspace` + Helix DST + TLA+ check; `Deploy.apply` → `kubectl set image`/`rollout`. ~1.5h
- **B3** Author the `ImprovementIssue` tracker entity (or reuse PM app's Issue with the contract's state names). ~0.5h

### Track C — Agent loop + Symphony-on-Temper
- **C1** Minimal Symphony (5 components, all marked safe-to-build in research): Temper-entity tracker adapter · sanitized+contained git worktree manager (enforce cwd==workspace, path containment, key sanitization) · prompt builder · coding-agent runner (swappable Claude/Codex adapter) · orchestrator + commit/PR handoff. Cut: persistence, HTTP dashboard, SSH workers, retries/backoff (max_turns=1), Linear. ~3h
- **C2** Observer/Researcher agent: Datadog MCP query → detect regression → form hypotheses → run `helix-bench` experiment(s) → create `ImprovementIssue` → drive plan→approve→implement→verify→deploy via Temper API (handle Cedar denial → poll_decision → retry). Reference the deleted `temper-topology-engine` pattern (Datadog MCP → reason → Temper model → scopes). ~2.5h

### Track D — Integration + demo  *(all hands, afternoon)*
- **D1** End-to-end wire-up; both Cedar gates; the closing latency dip. ~2h
- **D2** Record insurance dry-run + rehearse 12-min script. ~1h

---

## Suggested timeline (small team)
| Time | Track A (Helix/GKE) | Track B (Temper caps) | Track C (Agents/Symphony) |
|---|---|---|---|
| 9:30–11:30 | A1 OTLP exporter | B1 CIRun/Deploy apps | C1 Symphony skeleton |
| 11:30–13:30 | A2 GKE deploy | B2 executors + B3 tracker | C1 finish + C2 start |
| 13:30–15:30 | A3 workload + dashboard | help B→D integration | C2 Observer/Researcher |
| 15:30–16:45 | **D1 integration (all hands)** | | |
| 16:45–17:45 | **D2 record dry-run + rehearse** | | |
| 18:15 | **SUBMIT** | | |

## Demo script (12 min) — every claim real, recording as fallback
1. (1m) Show Helix on GKE + Datadog dashboard, steady p99.
2. (1m) Inject/observe a regression (or point at a standing inefficiency, e.g. batcher linger).
3. (2m) Observer agent: "p99 elevated → 3 hypotheses." Researcher runs helix-bench → confirms cause.
4. (1m) Creates ImprovementIssue, writes plan → **human approves in Observe UI** (Cedar gate #1).
5. (3m) Symphony spins isolated worktree, coding agent edits `batcher.rs`, opens PR.
6. (2m) CIRun runs cargo test + DST + TLA+ → green → **human approves Deploy** (Cedar gate #2).
7. (2m) Deploy rolls out to GKE → **Datadog p99 dips** → loop closes. Mic drop.
