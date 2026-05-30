# Dark Factory CI/CD

CI/CD + governance for the "Dark Factory for Helix" autonomous improvement loop.
Tracks one Helix improvement from observation through verification to a
human-approved, Cedar-gated rollout. See `docs/dark-factory-BC-contract.md` and
ADR-0093.

## Entity Types

### ImprovementIssue

The tracker that threads the whole pipeline (replaces Symphony's external issue
system). Owned by Track B; consumed by C1 (Symphony) and C2 (Observer).

**States**: Observed → Researching → Planned → Implementing → Verifying → Deploying → Done (+ Failed from any active state)

**Key actions**:
- **Observe(title, hypothesis, target_file)**: record the initial observation (C2)
- **BeginPlanning()**: Observed → Researching
- **AssignPlanner(planner_id) / Assign(assignee_id)**: set planner / implementer
- **WritePlan(plan, acceptance_criteria) / RevisePlan(feedback)**: draft the plan (planner)
- **ApprovePlan()**: Researching → Planned. Human/supervisor only; planner ≠ approver (Cedar `forbid`)
- **StartWork(branch_name)**: Planned → Implementing (assignee; requires approved plan)
- **AttachPr(pr_url)**: record the PR (C1)
- **StartVerify(ci_run_id)**: Implementing → Verifying (links the CIRun)
- **RecordCiResult(ci_status) / MarkCiPassed()**: record + gate the CI outcome
- **ApproveDeploy()**: Verifying → Deploying. Human only; implementer ≠ approver (Cedar `forbid`); requires passing CI
- **MarkDone()**: Deploying → Done
- **Fail(reason)**: → Failed

### CIRun

The verification capability. Runs `cargo test --workspace` (+ where DST/TLA
would run) against a Helix branch via an out-of-band executor.

**States**: Pending → Running → Passed | Failed

**Key actions**:
- **Start(issue_id, repo, ref)**: Pending → Running (executor picks it up here)
- **RecordResult(status, report, exit_code)**: executor records the outcome
- **MarkPassed() / MarkFailed()**: land terminal (requires a recorded result)

### Deploy

The Cedar-gated rollout capability. `kubectl set image` / `rollout` against the
`dark-factory` namespace only, dry-run by default.

**States**: Pending → Approved → Rolling → Live | RolledBack

**Key actions**:
- **Request(issue_id, image_tag)**: create the deploy in Pending
- **Approve()**: Pending → Approved. **Human/supervisor only — this is THE governance gate.** No agent_type can self-approve.
- **Apply()**: Approved → Rolling (executor runs kubectl from here)
- **RecordResult(result, rollout_cmd, dry_run) / MarkLive() / Rollback()**: executor records + lands

## OData integration (for C1 / C2)

Verified live against the running `dark-factory` tenant:

| Entity | OData EntitySet (collection name to call) | Internal entity type |
|--------|-------------------------------------------|----------------------|
| ImprovementIssue | `ImprovementIssues` | `ImprovementIssue` |
| CIRun | `CIRuns` | `CiRun` (platform canonicalizes the filename `ci_run` → `CiRun`) |
| Deploy | `Deploys` | `Deploy` |

- **Bound action invocation**: `POST /tdata/<Set>('<id>')/Default.<Action>` with
  header `X-Tenant-Id: dark-factory`. The OData layer ignores the namespace
  token before the action name, so `Default.AttachPr`,
  `Temper.DarkFactory.AttachPr`, or bare-prefixed all resolve to the same bound
  action. C1's `Default.AttachPr` assumption works as-is.
- **Entity creation**: `POST /tdata/<Set>` with a JSON body containing at least `Id`.
- All contract fields are present on `ImprovementIssue` verbatim
  (`Status, Title, Hypothesis, TargetFile, Plan, AcceptanceCriteria, AssigneeId,
  PlannerId, BranchName, PrUrl, CiRunId, CiStatus, Evidence, CreatedAt`).

## Executors

Out-of-band runners (not in-process triggers). They poll the tenant and act:

- `executors/ci_run_executor.py` — picks up CIRuns in `Running`, runs `cargo
  test --workspace` against `/Users/arun.parthiban/notdd/helix` on the run's
  ref, records pass/fail + report, lands the run. Connects as `ci-service`.
- `executors/deploy_executor.py` — picks up Deploys in `Rolling`, runs kubectl
  against the `dark-factory` namespace (context
  `gke_datadog-sandbox_us-west3_gs-us-west3`, StatefulSet `helix`). **Dry-run by
  default**; a real rollout requires `DARK_FACTORY_DEPLOY_FOR_REAL=1`. Connects
  as `deploy-service` (cannot Approve — that is human-only).
- `executors/submit_specs.py` — submits these specs + Cedar policies to a
  running server (the `submit_specs` path is Cedar-gated; surfaces a decision_id
  on denial).

## Setup

```
# Submit specs (developer/operator; design-time).
python3 executors/submit_specs.py

# Run the executors (demo).
python3 executors/ci_run_executor.py        # poll loop
python3 executors/deploy_executor.py        # poll loop, dry-run
```
