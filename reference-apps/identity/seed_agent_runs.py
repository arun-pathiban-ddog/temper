#!/usr/bin/env python3
"""Seed representative AgentRun records (ADR-0100) for the Runs tab demo.

Each run is authored by the REAL agent identity (observer/researcher/symphony/
breeder tokens), driven through the governed lifecycle: create -> Start ->
RecordSummary -> Finish/Fail. Idempotent-ish (uses fixed ids; re-running is a
no-op once the run is terminal).
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:3000"
TENANT = "dark-factory"
TOKENS = json.load(open(Path(__file__).resolve().parent / "tokens.json"))


def tok(name: str) -> str:
    return TOKENS[name]["token"]


def post(path: str, token: str, body: dict) -> int:
    req = urllib.request.Request(
        f"{BASE}{path}", method="POST",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "X-Tenant-Id": TENANT, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001
        return 0


# (id, agent_token, agent_type, trigger, summary, metrics, produced_ids)
RUNS = [
    ("run-observer-001", "observer", "observer",
     "scheduled telemetry tick (30m window)",
     "Swept helix.produce.latency_ms + workload.* across 3 queues. p95 on the "
     "orders niche spiked 3.1x under burst; filed an ImprovementIssue.",
     {"queues_scanned": 3, "anomalies": 1, "issues_filed": 1},
     "imp-loop-3992e5"),
    ("run-researcher-001", "researcher", "researcher",
     "ImprovementIssue imp-loop-3992e5 entered Researching",
     "Hypothesis: batcher linger_ms=1 causes the p95 burst; raising it coalesces "
     "appends. Wrote plan + acceptance criteria; awaiting human ApprovePlan.",
     {"hypotheses": 1, "plan_written": True},
     "imp-loop-3992e5"),
    ("run-symphony-001", "symphony", "symphony",
     "imp-loop-3992e5 reached Implementing (assignee=symphony-agent)",
     "Spun an isolated Helix worktree on darkfactory/imp-loop-linger, ran the "
     "coding agent (linger_ms 1->5 in batcher.rs), committed, opened a PR, "
     "AttachPr'd it back on the issue.",
     {"worktree": "darkfactory/imp-loop-linger", "commits": 1, "pr": "SIMULATED://pr/imp-loop-3992e5"},
     "imp-loop-3992e5"),
    ("run-breeder-001", "breeder", "breeder",
     "global throughput goal active; orders niche telemetry suggested a variant",
     "Inferred high-fanout-latency-sensitive niche from Datadog telemetry "
     "(could NOT read the workload spec — Cedar-forbidden). Proposed breed "
     "linger_ms on cluster-001; built + verified; PROMOTED (-22% p95, CI passed).",
     {"niche": "high-fanout-latency-sensitive", "breeds_proposed": 1, "promoted": 1, "p95_delta": "-22%"},
     "breed-c001-001"),
    ("run-breeder-002", "breeder", "breeder",
     "latency goal on cluster-001; explored an aggressive WAL variant",
     "Proposed breed sync_on_rotation=false (latency-greedy). Built green, but "
     "the WAL durability DST CULLED it — durability invariant violated. Selection "
     "rejected the lethal mutation.",
     {"niche": "high-fanout-latency-sensitive", "breeds_proposed": 1, "culled": 1, "cull_reason": "WAL durability DST"},
     "breed-c001-002"),
]


def seed():
    for rid, agent_tok, atype, trigger, summary, metrics, produced in RUNS:
        t = tok(agent_tok)
        # operator creates the shell, the agent drives its own lifecycle.
        c = post("/tdata/AgentRuns", tok("operator"),
                 {"id": rid, "Status": "Running", "AgentType": atype,
                  "Trigger": trigger, "Namespace": TENANT})
        s1 = post(f"/tdata/AgentRuns('{rid}')/Default.Start", t,
                  {"agent_type": atype, "trigger": trigger})
        s2 = post(f"/tdata/AgentRuns('{rid}')/Default.RecordSummary", t,
                  {"summary": summary, "metrics": json.dumps(metrics),
                   "produced_ids": produced})
        s3 = post(f"/tdata/AgentRuns('{rid}')/Default.Finish", t, {})
        print(f"  {rid:22s} [{atype:10s}] create={c} start={s1} summary={s2} finish={s3}")


if __name__ == "__main__":
    print("Seeding AgentRun demo records (authored by real agent identities):")
    seed()
    print("DONE")
