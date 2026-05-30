# ADR-0093: Symphony Orchestrator (Dark Factory for Helix)

## Status

Accepted

## Context

The "Dark Factory for Helix" hackathon pipeline turns an observed performance
problem into a merged code change with no human writing the patch:

```
[C2 Observer]   Datadog -> hypothesis -> ImprovementIssue (Temper, B's tracker)
                BeginPlanning/WritePlan -> (human ApprovePlan) -> StartWork
[C1 Symphony]   read Implementing issue -> isolated git worktree on Helix
                -> coding agent edits -> commit -> PR -> AttachPr back on issue
[B CIRun]       cargo test + DST + TLA on the branch -> pass/fail -> Deploy (Cedar)
```

Symphony (the upstream project this is modelled on) is a large orchestrator with
Linear integration, persistent job DB, SSH worker pools, retries, and an HTTP
dashboard. For the hackathon we need the **minimum viable subset** that proves
the worktree -> agent -> commit -> PR mechanics work end-to-end, while consuming
Agent-B's `ImprovementIssue` Temper entity (per `docs/dark-factory-BC-contract.md`).

Constraints from the contract and the harness:
- Tenant is always `dark-factory`. Temper server already runs at
  `http://127.0.0.1:3000` (Turso storage). C1 consumes the `ImprovementIssue`
  entity (states incl. `Implementing`; actions `StartWork`, `AttachPr`) but does
  NOT own its spec.
- B's specs may not be deployed yet, so C1 must be testable against a hardcoded
  fake issue without the tracker being live.
- All Helix edits go in an isolated `git worktree` on branch
  `darkfactory/<issue-id>`, never on `main`, never pushed to `main`.
- Symphony's three workspace invariants must hold: `cwd == workspace_path`,
  workspace contained under a configured root, issue key sanitized
  (`[^A-Za-z0-9._-]` -> `_`).

## Decision

Build a small Python package `reference-apps/symphony/` with **five components**,
each a swappable seam:

1. **Tracker adapter** (`tracker.py`) — `Tracker` protocol with
   `claim_issue()` / `attach_pr()`. Two implementations:
   - `FakeTracker` — hardcoded issue dict, used for the verified demo and tests.
   - `TemperTracker` — real HTTP client against the Temper OData data plane
     (`/tdata/ImprovementIssues?$filter=Status eq 'Implementing'` to read;
     `POST /tdata/ImprovementIssues('<id>')/Default.AttachPr` to write the PR
     url). Tenant via `X-Tenant-Id: dark-factory`. Falls back cleanly if the
     entity set is not yet deployed (404) so C1 is unblocked by B.

2. **Workspace manager** (`workspace.py`) — enforces the three Symphony
   invariants with assertions, then runs `git worktree add <root>/<key>
   -b darkfactory/<key>` against the Helix repo. The created worktree path is
   the agent's `cwd`; `assert os.getcwd()`/the passed cwd equals the worktree
   before the agent runs. Idempotent: reuses or recreates a clean worktree.

3. **Prompt builder** (`prompt.py`) — pure function rendering
   `(title, hypothesis, target_file, plan, acceptance_criteria)` into a single
   instruction string for the coding agent, with explicit "edit only TargetFile,
   make the minimal change" guidance.

4. **Coding-agent runner** (`agent.py`) — `CodingAgent` protocol with
   `run(prompt, cwd) -> AgentResult`. `ClaudeAdapter` (default) invokes Claude
   Code headless: `claude -p "<prompt>" --permission-mode acceptEdits
   --allowedTools "Edit Read Write" --output-format json` with `cwd` set to the
   worktree. `CodexAdapter` is a STUB raising `NotImplementedError` with a
   comment describing the `codex app-server` stdio JSON-RPC protocol it would
   speak. Adapter selected by `--agent` / `SYMPHONY_AGENT`, default `claude`.

5. **Orchestrator** (`orchestrator.py` + `__main__.py`) — one pass, single
   attempt, no retry: claim an `Implementing` issue -> make worktree -> build
   prompt -> run agent -> `git add -A && git commit` -> `gh pr create` (base
   `main`, head `darkfactory/<id>`) -> `AttachPr` the url back on the issue.
   If `gh` is unauthenticated or push is denied, it prints the diff and a clearly
   marked SIMULATED PR url, and still records that url via `AttachPr`.

### Explicitly cut (out of scope for the hackathon)

Persistence/DB, HTTP dashboard, SSH worker pools, retries/backoff, Linear.

## Consequences

**Easier:** each seam (tracker, agent, VCS) is swappable; the system is testable
with no Temper specs and no network via `FakeTracker` + a dry-run agent; the
worktree invariants make it safe to run against a live Helix checkout without
disturbing `main` or existing branches.

**Harder / accepted trade-offs:** single attempt with no retry means a flaky
agent run fails the pass; no persistence means a crash loses in-flight state
(acceptable for a demo). `TemperTracker` talks to the OData data plane directly
rather than through the sandboxed `temper.*` Python API, because the orchestrator
runs as an OS process (it must shell out to git/gh/claude) and cannot live inside
the no-filesystem/no-network MCP sandbox. It still goes through the same governed
HTTP surface and respects Cedar denials surfaced by the server.

## Options Considered

### Option 1: Drive Temper via the `temper mcp` stdio sandbox

**Pros:** uses the blessed `temper.*` API; identical to how chat agents act.

**Cons:** the sandbox forbids `os`, filesystem, subprocess, and network — but the
orchestrator's whole job is to run git/gh/claude as OS processes. Unworkable for
a process-level orchestrator.

### Option 2: HTTP client against the OData data plane (chosen)

**Pros:** the orchestrator is a normal process that can shell out; still uses the
governed `/tdata` surface with `X-Tenant-Id`; Cedar still applies server-side.

**Cons:** must track the OData URL conventions for bound actions; slightly more
code than calling a Python method.

## References

- `docs/dark-factory-BC-contract.md` — the B/C integration contract
- `skills/temper-agent/SKILL.md` — Temper Python API + OData conventions
- `crates/temper-server/src/router.rs`, `crates/temper-server/src/odata/write.rs`
  — the `/tdata/{*path}` bound-action routing this consumes
- Helix repo: `/Users/arun.parthiban/notdd/helix`
