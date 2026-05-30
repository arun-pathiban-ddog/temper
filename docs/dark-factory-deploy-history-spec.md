# Deployment-History UI — Spec

A standalone mini-app showing the history of Helix deployments, each with a
changelog and a click-in GitHub-style code diff. Demo artifact for the Dark
Factory project.

## Location
`reference-apps/deploy-history/` — FastAPI backend + single-page frontend.
Run on a free port (NOT 3000=Temper, NOT 4000=Observe UI). Suggest **4100**.

## Data sources (all confirmed live)
1. **Temper Deploy entities** — the deployment history.
   - List: `GET http://127.0.0.1:3000/tdata/Deploys` (header `X-Tenant-Id: dark-factory`, `Authorization: Bearer <operator token>`).
   - Detail: `GET .../tdata/Deploys('<id>')` → fields `{Status, image_tag, issue_id, approved, result, has_result}` + `events` array `[{action, from_status, to_status, timestamp, params}]` (the governed timeline: Created→Request→Approve→Apply→RecordResult→MarkLive).
   - Operator token: `reference-apps/identity/tokens.json` → `["operator"]["token"]`.
2. **Linked ImprovementIssue** — the changelog "why".
   - `GET .../tdata/ImprovementIssues('<issue_id>')` → fields `{Title, Hypothesis, Plan, TargetFile, BranchName, PrUrl}` (PascalCase; tolerate snake_case too).
3. **Git diff** — the actual code change.
   - Helix repo: `/Users/arun.parthiban/notdd/helix`.
   - **tag→branch→base map** (image_tag from the Deploy → branch → diff base):
     - `champion-throughput` → branch `evolve/throughput`, base `8260087` (ancestor)
     - `champion-latency` (if it appears) → `evolve/latency`, base `8260087`
     - `metrics-v1` / `metrics-v2` → branch `main`, base = `main~1` (or the commit before the metrics work; if unsure, diff the metrics files)
     - `latest` → resolve same as `champion-throughput`
     - fallback: if tag unknown, show "no diff mapping" gracefully.
   - Diff command: `git -C /Users/arun.parthiban/notdd/helix diff <base> <branch>` (unified). Also expose `--stat` for the file list.

## Backend endpoints
- `GET /api/deployments` — list, newest-first. Each item: `{id, status, image_tag, issue_id, deployed_at (from the MarkLive/last event timestamp), approved, result_summary, lineage (latency|throughput|baseline)}`.
- `GET /api/deployments/{id}` — detail: the deploy fields + the full **event timeline** (action, status, timestamp) + the joined **changelog** from the ImprovementIssue (title, hypothesis, plan, target_file).
- `GET /api/deployments/{id}/diff` — `{base, branch, stat: [{file, additions, deletions}], unified_diff: "<raw git diff text>"}`. Parse per-file so the frontend can render a file tree + per-file hunks.
- Serve the frontend static files from `/`.

## Frontend (single page, polished for demo)
- **History view**: vertical timeline / list, newest first. Each card: status badge (Live=green, RolledBack=red, Rolling=amber, Pending=gray), image_tag, lineage chip (⚡latency / 🚀throughput / baseline), deployed-at, one-line result. Click → detail.
- **Detail view**: 
  - Header: deploy id, status, image_tag, lineage.
  - **Changelog panel**: the linked issue's Title + Hypothesis + Plan + Target file. Plus the **governed event timeline** as a stepper (Created → Request → Approve [human gate] → Apply → Rolling → Live) with timestamps — this shows the deploy was governed.
  - **Code diff panel**: GitHub-style, syntax-highlighted, side-by-side (or unified toggle), with a file tree if multi-file. Use a JS diff lib (e.g. diff2html via CDN, or react-diff-viewer if going React). Red/green, line numbers, file headers.
- Keep it self-contained: vanilla JS + a CDN diff lib is fine (no build step), OR a tiny React if cleaner. Prioritize "looks good in a demo" + "click a deployment → see the real code diff."

## Constraints
- Read-only. Never mutate Temper entities or git. Never run kubectl.
- Tolerate missing data gracefully (e.g. probe-1 has no issue_id/image_tag → show as a minimal/placeholder row or filter it out).
- Don't depend on a build toolchain that isn't present; prefer CDN libs + static files served by FastAPI.

## Verify
Run it, hit /api/deployments (should list deploy-champion-throughput + deploy-imp-loop-3992e5), open the detail for deploy-champion-throughput, and confirm the diff renders the real batcher.rs linger_ms change (`.unwrap_or(5)` → `.unwrap_or(9)` for throughput; the champion is vs ancestor so `1`/default → `9`). Report the run command + a screenshot-able URL.
