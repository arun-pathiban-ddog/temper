# Dark Factory — Track B & C Integration Contract

The single source of truth shared by Agent-B, Agent-C1, Agent-C2. Lock this; do
not change a field name without telling the coordinator (the human's main session).

## Environment (already running)
- **Temper server**: `http://127.0.0.1:3000` (started via `./target/release/temper serve --port 3000`, Turso storage). Shared by all agents.
- **MCP for agents**: `temper mcp --port 3000` (stdio). Agents drive Temper via the `execute` tool with the Python `temper.*` API. READ `skills/temper-agent/SKILL.md` FIRST for the exact API.
- **Tenant**: use `dark-factory` as the tenant id for all entities/specs in this project.
- **Helix repo** (Track C operates on it): `/Users/arun.parthiban/notdd/helix`.
- **Live telemetry** (Track C2 reads it): Datadog org `gensim.datadoghq.com`, metric `helix.produce.latency_ms.95percentile{*}` (+ `.avg/.median/.max/.count`), `helix.replication.lag`. Dashboard `t8q-ntj-v83`. Query via Datadog MCP `get_datadog_metric` (live), NOT `search_datadog_metrics` (index lags).

## The pipeline (who hands off to whom)
```
[C2 Observer/Researcher]  reads Datadog -> forms hypothesis -> creates an ImprovementIssue (B's tracker)
        -> drives it: BeginPlanning/WritePlan -> (human ApprovePlan via Observe UI)
        -> StartWork -> hands the issue to [C1 Symphony]
[C1 Symphony]  reads the issue -> isolated git worktree on Helix -> coding agent edits -> commit -> PR
        -> writes PR url back on the issue -> transitions issue to a verify state
[B CIRun]  runs cargo test + DST + TLA on the branch -> pass/fail on the issue
        -> (human approves Deploy via Cedar) -> [B Deploy] kubectl rollout to GKE
```

## Shared entities (Agent-B OWNS the specs; C1/C2 consume)
All three are Temper entities (`.ioa.toml` + `.csdl.xml` + `.cedar`) under tenant `dark-factory`.

### 1. `ImprovementIssue`  (the tracker — replaces Symphony's Linear)
- **States**: `Observed -> Researching -> Planned -> Implementing -> Verifying -> Deploying -> Done` (+ `Failed`).
- **Key fields (CSDL)**: `Id` (string), `Status` (string), `Title` (string), `Hypothesis` (string), `TargetFile` (string), `Plan` (string), `AcceptanceCriteria` (string), `AssigneeId` (string), `PlannerId` (string), `BranchName` (string), `PrUrl` (string), `CiRunId` (string), `CiStatus` (string), `Evidence` (string, JSON), `CreatedAt` (DateTimeOffset).
- **Actions**: `Observe(title, hypothesis, target_file)`, `BeginPlanning()`, `WritePlan(plan, acceptance_criteria)`, `ApprovePlan()` [human/supervisor only, Cedar], `StartWork(branch_name)`, `AttachPr(pr_url)`, `StartVerify(ci_run_id)`, `RecordCiResult(ci_status)`, `ApproveDeploy()` [human, Cedar], `MarkDone()`, `Fail(reason)`.
- **Cedar role separation**: planner != approver; only `agent_type in [supervisor,human]` with `agentTypeVerified` can `ApprovePlan`/`ApproveDeploy`. (Mirror `os-apps/project-management/policies/issue.cedar`.)

### 2. `CIRun`  (verification capability)
- **States**: `Pending -> Running -> Passed | Failed`.
- **Fields**: `Id`, `Status`, `IssueId`, `Repo`, `Ref` (branch/sha), `Report` (string), `StartedAt`, `FinishedAt`.
- **Actions**: `Start(issue_id, repo, ref)`, `RecordResult(status, report)`.
- The actual execution (running `cargo test`/DST/TLA) is an **executor** B wires up (a Python/shell runner invoked when a CIRun enters Running). For the demo it runs against the Helix repo working tree / branch.

### 3. `Deploy`  (deploy capability — Cedar-gated)
- **States**: `Pending -> Approved -> Rolling -> Live | RolledBack`.
- **Fields**: `Id`, `Status`, `IssueId`, `ImageTag`, `Namespace` (default `dark-factory`), `Result`.
- **Actions**: `Request(issue_id, image_tag)`, `Approve()` [human, Cedar], `Apply()` (executor: `kubectl set image`/`rollout` on the dark-factory StatefulSet), `RecordResult(result)`.
- Executor calls kubectl against `gke_datadog-sandbox_us-west3_gs-us-west3`, namespace `dark-factory` ONLY. Never touch other namespaces.

## Interface stubs so tracks don't block on each other
- **C1 & C2 do NOT need B's specs deployed to start.** They code against the entity/action names above. During integration the coordinator confirms specs are live and C1/C2 point at real entities.
- **C2 can start immediately on real Datadog data** (the loop's OBSERVE+RESEARCH phases) without any of B/C1.
- **C1 can build + test the Symphony worktree/runner/PR mechanics** against the Helix repo with a hardcoded fake issue, before B's tracker is live.

## Integration findings (discovered during build — READ THESE)
- **Two ways to reach Temper, pick by process type:**
  - In-sandbox agents (the MCP `execute` Python `temper.*` API) — for agents that only need Temper, no shell/fs/net. **C2 Observer uses this.**
  - **OData data plane** (`/tdata/{path}` HTTP with header `X-Tenant-Id: dark-factory`) — for OS processes that must shell out (git/gh/claude). **C1 Symphony uses this** (the MCP sandbox forbids fs/net/subprocess). Same Cedar governance applies either way. AttachPr example: `POST /tdata/ImprovementIssues('<id>')/Default.AttachPr`.
- **OData naming (Agent-B must match, else set 2 env vars):** EntitySet = `ImprovementIssues` (plural), bound-action namespace = `Default`. C1 made these overridable via `SYMPHONY_ENTITY_SET` / `SYMPHONY_ODATA_NAMESPACE`, and tolerates Pascal/snake case field serialization.
- **C1 status: DONE.** Package at `reference-apps/symphony/`. Verified worktree+commit+PR end-to-end (branch `darkfactory/DF-DEMO-1`, commit 4fdfcb7). Left a demo worktree/branch in the Helix repo as evidence — cleanup with `--cleanup` or `git -C /Users/arun.parthiban/notdd/helix worktree remove --force <path> && git branch -D darkfactory/DF-DEMO-1`.

## Hard rules (all agents)
- Tenant is always `dark-factory`. Read `skills/temper-agent/SKILL.md` before any MCP call.
- Cedar default-deny: when an action is denied, surface `decision_id` + poll; do NOT try to self-approve.
- Track C edits to Helix go in an **isolated git worktree/branch** (`darkfactory/<issue-id>`), never on `main`, never committed without the worktree pattern.
- Do NOT touch the running GKE Helix deployment except via the `Deploy` executor (namespace `dark-factory` only).
- Write an ADR for any significant new component (Symphony orchestrator, the agent loop) per each repo's ADR rules.
- Report back: what you built, file paths, what's tested, what's stubbed, and any contract field you had to add/change.
```
