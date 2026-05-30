"""Per-cluster workload optimizer — propose phase (ADR-0103).

A `claude -p` agent that, for each live Helix cluster:
  1. reads the cluster's telemetry (helix.produce.latency_ms etc.) via the Datadog
     MCP to understand the workload running on it,
  2. reads the Helix source at the repo path (filesystem tools, read-only here),
  3. proposes concrete code + config improvements tailored to that workload type.

It files ONE Symphony ticket per cluster (scoped), each carrying the structured
proposal. The optimizer-EXECUTOR later implements + verifies + deploys them.

Telemetry comes through claude -p's own Datadog MCP; source comes through its
Read/Grep/Glob tools scoped to the Helix repo. No tokens handled here. The agent
is propose-only — it does not edit or deploy (that's the executor).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request

_MODEL = os.environ.get("OPTIMIZE_MODEL", "sonnet")
HELIX_REPO = os.environ.get("OPTIMIZE_HELIX_REPO", "/Users/arun.parthiban/notdd/helix")
UI_URL = os.environ.get("OPTIMIZE_UI_URL", "http://127.0.0.1:4100")
_TIMEOUT_S = int(os.environ.get("OPTIMIZE_TIMEOUT_S", "900"))

# Firewall-ish scoping: telemetry-read MCP tools + read-only source tools. NO
# Edit/Write here (this is propose-only); the executor does the editing.
_ALLOWED_TOOLS = [
    "mcp__datadog-mcp__get_datadog_metric",
    "mcp__datadog-mcp__search_datadog_metrics",
    "mcp__datadog-mcp__get_datadog_metric_context",
    "Read", "Grep", "Glob",
]


class OptimizeError(RuntimeError):
    pass


def _live_clusters() -> list[dict]:
    """Live Helix clusters (name + branch) the optimizer should reason over."""
    try:
        with urllib.request.urlopen(f"{UI_URL}/api/clusters", timeout=20) as r:
            doc = json.loads(r.read().decode())
    except (urllib.error.URLError, ValueError) as exc:
        raise OptimizeError(f"cannot reach UI /api/clusters: {exc}") from exc
    out = []
    for c in doc.get("clusters", []):
        if (c.get("status") or "") == "Live":
            out.append({"name": c.get("name"), "branch": c.get("branch") or "", "image_tag": c.get("image_tag") or ""})
    return out


def _build_prompt(clusters: list[dict]) -> str:
    names = ", ".join(f"helix-{c['name']}" for c in clusters)
    return f"""You are a Helix (Kafka-replacement) performance engineer. For EACH live cluster below, \
propose concrete code and config improvements tuned to the workload running on it.

Live clusters (by their cluster tag): {names}

For each cluster:
1. Read its telemetry via the Datadog MCP (get_datadog_metric) over the last 20 minutes — e.g.
   `avg:helix.produce.latency_ms.avg{{cluster:helix-<name>}}`, `.95percentile`, `.count`,
   `sum:helix.consume.fetched{{cluster:helix-<name>}}.as_count()`, `avg:helix.replication.lag{{cluster:helix-<name>}}`,
   and the per-queue `workload.producer.sent` / `workload.consumer.lag` for queues on that cluster.
   Characterize the workload (throughput-bound? latency-sensitive? bursty? lag-building?).
2. Read the Helix source at {HELIX_REPO} (use Read/Grep/Glob). Relevant tuning lives in:
   - helix-server/src/service/batcher.rs   (HELIX_BATCHER_LINGER_MS / linger)
   - helix-raft/src/lib.rs                  (MAX_INFLIGHT_APPEND_ENTRIES, APPEND_ENTRIES_BATCH_SIZE_MAX)
   - helix-wal/src/wal.rs                   (sync_on_rotation / fsync)
   - helix-server/src/kafka/handler.rs      (produce/fetch paths)
   Look at the ACTUAL code, don't guess.
3. Propose code/config improvements for THAT cluster's workload — each with the target file, the
   specific change (what to change and to what), the rationale tied to the telemetry you saw, and the
   metric you expect it to improve.

Respond with ONLY a JSON object (no prose, no fences), exactly:
{{"clusters":[{{"cluster":"<name without helix- prefix>","workload_summary":"one sentence on the workload from telemetry","proposals":[{{"target_file":"helix-server/src/...","change":"specific change, e.g. raise linger from 1 to 25","rationale":"why, citing the telemetry","expected_metric":"helix.produce.latency_ms.95percentile","expected_effect":"e.g. -20% p95"}}]}}]}}"""


def _run_claude(prompt: str) -> dict:
    """Invoke `claude -p` (Datadog MCP + read-only source) and parse the JSON result.
    --add-dir grants read access to the Helix repo outside the cwd."""
    from breeder.claude_cmd import claude_argv
    cmd = [
        *claude_argv(), "-p",
        "--model", _MODEL,
        "--output-format", "json",
        "--add-dir", HELIX_REPO,
        "--allowedTools", *_ALLOWED_TOOLS,
    ]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise OptimizeError(f"claude -p timed out after {_TIMEOUT_S}s") from exc
    if proc.returncode != 0:
        raise OptimizeError(f"claude -p exited {proc.returncode}: {proc.stderr[:400]}")
    if not proc.stdout.strip():
        raise OptimizeError(f"claude -p produced no output; stderr: {proc.stderr[:400]}")
    try:
        envelope = json.loads(proc.stdout)
        body = envelope.get("result", proc.stdout)
        cost = envelope.get("total_cost_usd")
        if cost is not None:
            print(f"    [optimizer] claude -p cost≈${cost:.4f}, {envelope.get('num_turns','?')} turns", flush=True)
    except json.JSONDecodeError:
        body = proc.stdout
    m = re.search(r"\{.*\}", body, re.S)
    if not m:
        raise OptimizeError(f"no JSON in claude -p result: {body[:300]}")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise OptimizeError(f"could not parse optimizer JSON: {exc}") from exc


def _file_tickets(plans: list[dict]) -> dict:
    """POST the per-cluster proposals to the backend, which files one Symphony
    ticket per cluster (auto-advanced to Implementing for the executor)."""
    body = json.dumps({"plans": plans}).encode()
    req = urllib.request.Request(f"{UI_URL}/api/optimizer/tickets", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, ValueError) as exc:
        raise OptimizeError(f"filing optimizer tickets failed: {exc}") from exc


def optimize(*, dry_run: bool) -> dict:
    clusters = _live_clusters()
    # Don't optimize niche-* clusters that were just built by speciation, nor
    # baseline 'helix' — focus on the real workload clusters. (Tunable.)
    targets = [c for c in clusters if c.get("name")]
    if not targets:
        print("optimizer: no live clusters to optimize.")
        return {"plans": [], "summary": "optimizer: no live clusters."}

    parsed = _run_claude(_build_prompt(targets))
    plans = parsed.get("clusters", []) if isinstance(parsed, dict) else []
    # keep only plans with at least one proposal
    plans = [p for p in plans if p.get("proposals")]

    if dry_run:
        print("DRY RUN — optimizer proposals (no tickets filed):\n")
        for p in plans:
            print(f"  cluster={p.get('cluster')}  — {p.get('workload_summary','')}")
            for pr in p.get("proposals", []):
                print(f"      • {pr.get('target_file')}: {pr.get('change')}")
                print(f"        ↳ {pr.get('rationale','')} (→ {pr.get('expected_effect','?')})")
            print()
        print(f"optimizer (dry-run): {len(plans)} cluster(s), "
              f"{sum(len(p.get('proposals',[])) for p in plans)} proposal(s).")
        return {"dry_run": True, "plans": plans}

    res = _file_tickets(plans)
    filed = res.get("filed", []) if isinstance(res, dict) else []
    ok_n = sum(1 for f in filed if f.get("ok"))
    # Human-readable summary leading with per-cluster proposal counts.
    parts = "; ".join(
        f"{p['cluster']}: {len(p.get('proposals',[]))} change(s) — {p.get('workload_summary','')[:60]}"
        for p in plans
    )
    summary = f"optimizer: proposed improvements for {len(plans)} cluster(s) → {ok_n} Symphony ticket(s): {parts}"
    print(summary)
    return {"filed": filed, "plans": plans, "summary": summary}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Per-cluster workload optimizer (propose phase, ADR-0103).")
    ap.add_argument("--dry-run", action="store_true", help="print proposals; file no tickets")
    args = ap.parse_args(argv)
    try:
        optimize(dry_run=args.dry_run)
    except OptimizeError as exc:
        print(f"optimizer: FAILED — {exc}", file=__import__("sys").stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
