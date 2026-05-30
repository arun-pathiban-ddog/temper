# Dark Factory — Deployment History

A standalone, read-only mini-app that shows the history of **Helix** deployments
governed through the Temper `dark-factory` tenant. Each deployment carries a
changelog (the linked improvement issue), a governed event timeline, and a
GitHub-style code diff of the actual change.

## What it joins

1. **Temper Deploy entities** (`/tdata/Deploys`) — the deployment history and the
   governed timeline (`Created → Request → Approve → Apply → RecordResult → MarkLive`).
2. **Linked `ImprovementIssue`** — the changelog "why" (title, hypothesis, plan,
   target file, branch).
3. **Helix git repo** (`/Users/arun.parthiban/notdd/helix`) — the real `git diff`
   between the deploy's resolved branch and its base.

Everything is **read-only**: it never mutates Temper, writes git, or runs kubectl.

## Run

FastAPI 0.121 + uvicorn are available globally. From this directory:

```bash
cd /Users/arun.parthiban/notdd/temper/reference-apps/deploy-history
python3 -m uvicorn app:app --host 127.0.0.1 --port 4100
```

Then open: **http://127.0.0.1:4100**

(Port 4100 is used because 3000 = Temper and 4000 = the Observe UI.)

## Prerequisites

- The Temper server running at `http://127.0.0.1:3000` (tenant `dark-factory`).
- Operator token present at `reference-apps/identity/tokens.json`.
- The Helix clone at `/Users/arun.parthiban/notdd/helix` with branches
  `evolve/throughput`, `evolve/latency`, `darkfactory/imp-loop-linger`, `main`.

## API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/deployments` | List, newest-first. Joins lineage + `deployed_at`. |
| GET | `/api/deployments/{id}` | Detail: deploy fields + full event timeline + joined changelog. |
| GET | `/api/deployments/{id}/diff` | `{base, branch, stat[], unified_diff}` from `git diff`. |
| GET | `/api/health` | Backend + Temper reachability. |
| GET | `/` | The single-page frontend (static). |

## Diff resolution

For each deploy, the branch/base is resolved in this order:

1. The linked issue's `branch_name` (base = merge-base with the ancestor `8260087`).
2. A static `image_tag → (branch, base)` map (`champion-throughput`, `latest`,
   `champion-latency`, `metrics-v1/v2`).
3. Otherwise the diff panel shows a graceful "no diff mapping".

## Notes

- `probe-1` (no tag / no issue) is filtered out as a smoke-test row.
- Missing linked data is tolerated — panels degrade gracefully.
- Frontend uses [diff2html](https://diff2html.xyz/) via CDN; no build step.
