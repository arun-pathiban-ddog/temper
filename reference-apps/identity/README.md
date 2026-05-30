# Dark Factory — Verified Agent Identity Bootstrap

Makes the governed Helix loop run on **real platform credentials** (ADR-0033),
not `X-Temper-Principal` header spoofing.

## Files
- `register_identities.py` — idempotent script that issues the four verified
  agent credentials via the real chain (`AgentType.Define` + `AgentCredential.Issue`).
- `tokens.json` — the operator (admin) token + the four agent bearer tokens and
  their `sha256` hashes / prefixes. (Local integrator secret store for the demo.)
- `dark-factory-specs/` — the corrected dark-factory app bundle the server loads:
  - `model.csdl.xml` — fixes the `CIRuns -> CiRun` entity-set mapping (the
    PD-019e5a99 CSDL fix) and adds `AgentTypes` / `AgentCredentials` entity sets
    so credentials can be issued through the governed OData write path.
  - `policies/*.cedar` — the loop's Cedar policies, fixed: snake_case field refs
    (`planner_id`/`assignee_id`), `CiRun` resource type, plus `agent_identity.cedar`
    (operator/supervisor may Define/Issue identities) and `platform.cedar`
    (`manage_policies` so the human can drive `temper decide`).

## The bootstrap mechanism (exact)
1. Server runs with `TEMPER_API_KEY=<operator token>`. The bearer-auth middleware
   then (a) resolves agent bearer tokens via the credential registry to VERIFIED
   identities (`agentTypeVerified=true`), and (b) injects
   `X-Temper-Principal-Kind: admin` for the operator token — the privileged path.
2. Server loads `dark-factory` from `dark-factory-specs/` (CSDL + Cedar policies +
   agent OS entity merge), preserving the Turso DB at `~/.local/share/temper/agents.db`.
3. `register_identities.py` issues the four credentials as the operator/admin.

```sh
export TEMPER_API_KEY="<operator token from tokens.json>"
python3 register_identities.py --base-url http://127.0.0.1:3000 --tenant dark-factory
```

Re-running is a no-op (AgentType.Define from Active; AgentCredential.Issue self-loop).

## Wiring each component to its token (Authorization: Bearer <token>, X-Tenant-Id: dark-factory)
| Component | Identity | Token (tokens.json key) | How it presents it |
|-----------|----------|--------------------------|--------------------|
| C2 Observer | `observer` | `observer.token` | `Authorization: Bearer` on its `/tdata` calls (Observe/BeginPlanning) |
| C2 Researcher | `researcher` | `researcher.token` | same; planner identity (`AssignPlanner(planner_id=researcher-agent)`, WritePlan) |
| C1 Symphony | `symphony` | `symphony.token` | `SYMPHONY_TEMPER_TOKEN` env → `TemperTracker` sends `Authorization: Bearer` (StartWork/AttachPr/StartVerify) |
| Human / Supervisor | `supervisor` | `supervisor.token` | drives ApprovePlan/ApproveDeploy + CIRun lifecycle; the operator/admin token drives `temper decide` / the governed decisions endpoint |

The `agent_instance_id` is the Cedar `principal.id` (e.g. `symphony-agent`), so the
issue's `assignee_id` / `planner_id` must equal that for the role-scoped gates.
