# Observer/Researcher Agent (Dark Factory C2)

Agent-C2 in the Dark Factory for Helix loop: the **brain** that watches live Helix
telemetry, forms hypotheses, runs an experiment to confirm one, and creates+drives an
`ImprovementIssue` through Temper until a human approves the plan and Symphony (C1)
takes over.

See `docs/adrs/0094-dark-factory-observer-researcher.md` for the design and
`docs/dark-factory-BC-contract.md` for the shared entity/action contract.

## The loop

```
OBSERVE   read helix.produce.latency_ms.* + helix.replication.lag (live Datadog)
          -> detect elevated p95 / tuning opportunity
RESEARCH  form 3 competing, source-grounded hypotheses
          -> EXPERIMENT: read current values from the Helix source tree
          -> confirm one, rule out the others WITH EVIDENCE
DRIVE     create ImprovementIssue -> Observe -> BeginPlanning -> WritePlan
          -> (human ApprovePlan via Cedar/Observe UI) -> hand off to C1
```

## Layout

```
observer/
  observe.py        OBSERVE: metric sources + opportunity detection
  research.py       RESEARCH: hypotheses + source-probe experiment (autoresearch core)
  driver.py         DRIVE: build + drive the ImprovementIssue
  temper_client.py  thin HTTP client for the Temper data plane + governance
  main.py           CLI entry point that wires the three phases
snapshots/
  helix_live_2026-05-24.json   real live telemetry captured via the Datadog MCP
tests/
  test_observe_research.py     OBSERVE + RESEARCH (against real snapshot + real source)
  test_driver.py               DRIVE orchestration (fake client)
```

## Running it

No third-party deps; standard-library only (Python 3.10+).

```bash
cd reference-apps/observer

# OBSERVE + RESEARCH only, over the captured live snapshot (real numbers, no keys)
python3 -m observer.main --source snapshot \
    --snapshot snapshots/helix_live_2026-05-24.json --no-drive

# full loop incl. DRIVE against the live Temper server (tenant dark-factory)
python3 -m observer.main --source snapshot \
    --snapshot snapshots/helix_live_2026-05-24.json

# observe against the live Datadog API directly (needs DD_API_KEY + DD_APP_KEY)
python3 -m observer.main --source datadog
```

Tests:

```bash
python3 tests/test_observe_research.py
python3 tests/test_driver.py
```

## Metric sources

- `DatadogMetricSource` — direct Datadog v1 query API (`DD_API_KEY`/`DD_APP_KEY`); the
  production / GKE-job path.
- `SnapshotMetricSource` — replays a JSON capture of a live Datadog MCP
  `get_datadog_metric` response. The agent fetches real timeseries via MCP, writes the
  snapshot, and the observer reasons over those real numbers. To refresh, re-run the
  MCP query for `helix.produce.latency_ms.*` + `helix.replication.lag` and overwrite the
  `series` map.

## What's real vs captured

- **Real, live:** the Helix source probes (read straight from `/Users/arun.parthiban/
  notdd/helix`), the Temper data-plane calls (create/Observe/BeginPlanning/WritePlan
  against the running server, tenant `dark-factory`), and the Cedar denial handling.
- **Captured live (not streamed):** the telemetry, because this environment reaches
  Datadog through the MCP tool rather than the public query API. Every number in the
  snapshot came from an actual MCP query against the Helix GKE cluster.

## Governance

The agent surfaces Cedar denials (`PD-<uuid>` decision ids) and stops. It never
self-approves and never escalates its own identity to read or resolve a decision —
the human resolves everything in the Observe UI.
