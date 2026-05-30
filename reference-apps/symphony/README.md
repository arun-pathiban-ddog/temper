# Symphony-on-Temper — Dark Factory orchestrator for Helix

A minimal orchestrator (Agent-C1 in the Dark Factory pipeline) that turns an
`ImprovementIssue` in state `Implementing` into a real code change in an isolated
git worktree on the Helix repo: worktree → coding agent → commit → PR → PR url
written back onto the issue.

Design rationale: `../../docs/adrs/0093-symphony-orchestrator.md`.
Integration contract: `../../docs/dark-factory-BC-contract.md`.

## The five components

| # | File | Responsibility |
|---|------|----------------|
| 1 | `symphony/tracker.py` | Read candidate work from Temper's `ImprovementIssue` entity; write the PR url back via `AttachPr`. `TemperTracker` (real HTTP/OData) + `FakeTracker` (hardcoded demo issue). |
| 2 | `symphony/workspace.py` | `git worktree add` on Helix + `darkfactory/<id>` branch, enforcing the 3 Symphony invariants (cwd==workspace, contained-under-root, key sanitization). |
| 3 | `symphony/prompt.py` | Render the coding-agent prompt from the issue fields. |
| 4 | `symphony/agent.py` | Swappable `CodingAgent.run(prompt, cwd)`. `ClaudeAdapter` (default, headless `claude -p`), `CodexAdapter` (stub), `DryRunAgent` (offline demo). |
| 5 | `symphony/orchestrator.py` | One pass: claim → worktree → prompt → agent → commit → PR (real or simulated) → AttachPr. |

## Running it

```bash
cd reference-apps/symphony

# Offline end-to-end demo: fake issue + deterministic safe edit, no network/model.
# Creates a worktree on Helix, commits a comment near linger_ms, simulates a PR.
python3 -m symphony --tracker fake --dry-run-agent

# Real: read an Implementing ImprovementIssue from the live Temper server and run
# Claude Code headless to make the edit.
python3 -m symphony --tracker temper --agent claude

# Clean up the worktree afterwards
python3 -m symphony --tracker fake --dry-run-agent --cleanup
```

### Configuration (CLI flag or env var)

| Flag | Env | Default |
|------|-----|---------|
| `--tracker {temper,fake}` | `SYMPHONY_TRACKER` | `temper` |
| `--agent {claude,codex}` | `SYMPHONY_AGENT` | `claude` |
| `--dry-run-agent` | `SYMPHONY_DRY_RUN_AGENT` | off |
| `--helix-repo` | `HELIX_REPO` | `/Users/arun.parthiban/notdd/helix` |
| `--workspace-root` | `SYMPHONY_WORKSPACE_ROOT` | `<helix-parent>/symphony-worktrees` |
| `--temper-url` | `TEMPER_URL` | `http://127.0.0.1:3000` |
| `--tenant` | `TEMPER_TENANT` | `dark-factory` |

## Tests

```bash
python3 -m pytest -q   # 18 tests: invariants, tracker, prompt, agent stub, e2e
```

`tests/test_orchestrator_e2e.py` proves the full worktree→commit→simulated-PR→
AttachPr flow against a throwaway git repo (no Helix, no network, no model).

## What is real vs stubbed

- **Real:** worktree/branch mechanics, commit, `gh pr create` (with simulated
  fallback), `TemperTracker` HTTP against the live OData data plane, the
  `ClaudeAdapter` headless invocation.
- **Stubbed:** `CodexAdapter` (raises `NotImplementedError` with the
  `codex app-server` protocol it would speak); the simulated-PR path when `gh`
  cannot push (clearly marked `SIMULATED://` urls).
