# ADR-0100: Directed Evolution — AgentRun (agent runs + per-run summary)

- Status: Proposed
- Date: 2026-05-25
- Deciders: Temper core maintainers
- Related:
  - ADR-0093: Symphony orchestrator (one C1 pass = a run)
  - ADR-0094: Observer/Researcher (sensing + hypothesis passes)
  - ADR-0095: Breeding harness (a generation/breed pass)
  - ADR-0099: Breeds (the Breed/FitnessGoal entities a breeder run produces)
  - `os-apps/evolution/evolution_run.ioa.toml` (the heavier EvolutionRun precedent)
  - `reference-apps/deploy-history/app.py` (control-plane backend + Runs tab)

## Context

The Directed Evolution loop is driven by four agents — Observer, Researcher,
Symphony (C1), Breeder — each of which runs as a CLI/orchestrator **pass** that
prints a summary to stdout and leaves artifacts behind (ImprovementIssue, CIRun,
Breed, Deploy). But there is **no persistent, queryable record of a run**: you
can see *what* was produced, but not "Observer ticked at 14:02, inferred 3
niches, and filed 2 issues" as a first-class, summarized unit.

For the demo and for operating the loop, we want to **see the agent runs and a
summary of each** — a timeline of who ran, when, with what outcome, and what
each run produced. Today that story only exists in scrollback.

## Decision

### Sub-Decision 1: A governed `AgentRun` entity

Add `AgentRun` (`agent_run.ioa.toml` + `policies/agent_run.cedar` + CSDL):

```
Running --Finish--> Succeeded
Running --Fail-----> Failed
```

Fields: `AgentType` (observer|researcher|symphony|breeder), `Trigger` (what
kicked it off), `StartedAt`, `EndedAt`, `Status`, `Summary` (a short narrative),
`Metrics` (JSON: e.g. niches_found / breeds_proposed / pr_url / p95_delta), and
`ProducedIds` (refs to the entities the run produced — ImprovementIssue / Breed
/ Deploy / CIRun). A run is created on `Start`, finalized with `RecordSummary` +
`Finish`/`Fail`.

**Why this approach**: makes "a run" a first-class, summarized, linkable object —
exactly what the Runs view needs — rather than reconstructing fuzzy run
boundaries from scattered artifacts. It mirrors the Deploy/Breed lifecycle
conventions (Running → terminal, summary recorded before landing) and is
generic across all four agents (vs. the heavier domain-specific EvolutionRun).

### Sub-Decision 2: Agents record their pass through the entity

Each agent's pass creates an AgentRun on start and finalizes it with the
summary it already prints. For the demo, the backend seeds representative runs
for each agent via the governed API; wiring the live agents to emit AgentRuns is
a small follow-up (they already compute the summary).

**Why this approach**: the summary already exists (the agents print it); routing
it through a governed entity makes it visible + auditable + Cedar-scoped, with
the loop's existing identity model (observer/researcher/symphony/breeder agent
types) authoring their own runs.

### Sub-Decision 3: A new "Runs" tab — reverse-chron timeline

A dedicated tab: newest-first timeline of all agent runs across the four agents,
each a card (agent badge, when, status, narrative summary, key metrics,
deep-links to produced entities — Symphony issue / Breeds / Deployment History).
Filter by agent; click → detail.

**Why this approach**: a single "where did the agents go and what did they do"
home, consistent with the other tabs (Clusters / Breeds / Symphony). The
produced-entity links tie the loop together (a breeder run → its Breeds; a
symphony run → its PR).

## Consequences

### Positive
- The loop becomes legible: a timeline of agent activity with summaries + links.
- First-class, governed, queryable run records (not stdout scrollback).
- Ties the loop's artifacts back to the run that produced them.

### Negative
- A new entity + the agents must emit runs (seeded for the demo; live wiring TBD).

### Risks
- Run boundaries for long-lived/looping agents are fuzzy — mitigated by treating
  one pass = one run (the natural unit for all four agents).
- Summary quality depends on each agent computing a good one (they already print
  one; we route it through).

### DST Compliance
Not applicable — `agent_run.ioa.toml`/Cedar specs (L0-L3 verified on edit) + the
`deploy-history` reference app. No simulation-visible Rust crate touched.

## Non-Goals
- Live streaming of in-progress run logs (the Runs view is run records, not a log tail;
  building-state logs already exist for clusters).
- Replacing the existing per-entity views — Runs complements them.

## Alternatives Considered
1. **Derive runs from existing artifacts** (group ImprovementIssues/Breeds/Deploys
   by agent + time) — no new entity, works on current data, but run boundaries
   and summaries are reconstructed/fuzzy. Rejected for a crisp run-with-summary view.
2. **Reuse EvolutionRun** (os-apps/evolution) — too domain-specific (a 12-state
   evolution loop), not generic across observer/researcher/symphony/breeder.

## Rollback Policy
Additive. Remove the Runs tab + `/api/runs`, unregister the AgentRun EntityType
from the CSDL. No other flow depends on it.
