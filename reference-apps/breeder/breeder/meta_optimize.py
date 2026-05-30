"""Meta-agent — trace-driven prompt optimizer (ADR-0103 follow-on).

This is the agent that improves the OTHER agents. It runs `claude -p` with:
  - the chrome-devtools MCP, used ONLY to read the LLM-Observability traces in the
    lapdog web app (https://lapdog.datadoghq.com/), and
  - read-only filesystem access to the agents' own prompt source,
then proposes prompt improvements that make the speciation/optimizer agents more
EFFICIENT (fewer wasted/NO_DATA MCP calls, fewer redundant queries, lower
token/cost per run) and more CORRECT (classifications that match the telemetry).

It files ONE Symphony ticket per target agent (TargetFile meta://<agent>) carrying
the proposed prompt edits. The tickets ride the same board as speciation/optimizer.

lapdog-traced like the others (claude_argv() -> `lapdog claude`).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request

_MODEL = os.environ.get("META_MODEL", "sonnet")
# The agents whose prompts the meta-agent may tune, and the file each lives in.
# (Path is relative to the breeder package root, which we grant via --add-dir.)
BREEDER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../reference-apps/breeder
TARGET_AGENTS = {
    "speciation": "breeder/llm_classify.py",   # _build_prompt / _placement_prompt
    "optimizer": "breeder/optimize.py",        # _build_prompt
}
UI_URL = os.environ.get("META_UI_URL", "http://127.0.0.1:4100")
LAPDOG_URL = os.environ.get("META_LAPDOG_URL", "https://lapdog.datadoghq.com/")
_TIMEOUT_S = int(os.environ.get("META_TIMEOUT_S", "900"))

# chrome-devtools MCP (read the lapdog web app) + read-only source tools. NO Edit
# (propose-only) and NO Datadog MCP (the meta-agent reads TRACES via the web app,
# not raw telemetry).
_ALLOWED_TOOLS = [
    "mcp__chrome-devtools__list_pages",
    "mcp__chrome-devtools__navigate_page",
    "mcp__chrome-devtools__take_snapshot",
    "mcp__chrome-devtools__take_screenshot",
    "mcp__chrome-devtools__click",
    "mcp__chrome-devtools__evaluate_script",
    "Read", "Grep", "Glob",
]


class MetaError(RuntimeError):
    pass


def _build_prompt() -> str:
    agents = "\n".join(f"  - {a}: prompt in {f}" for a, f in TARGET_AGENTS.items())
    return f"""You are a meta-optimizer: you improve the PROMPTS of other LLM agents by studying \
their execution traces. Two agents run in this system, each a `claude -p` call:
{agents}

STEP 1 — Read the traces. Using ONLY the chrome-devtools MCP, open the lapdog LLM Observability \
web app at {LAPDOG_URL} (a tab is likely already open — use list_pages, then navigate_page to the \
lapdog tab/URL). Look at the recent `claude-code` sessions for these agents (their prompts start with \
"You are a workload-placement planner..." for speciation and "You are a Helix performance engineer..." \
for optimizer). For each, inspect the trace/span tree: the tool calls made (Datadog MCP get_datadog_metric \
queries, file Reads), how many turns/steps, token + cost totals, and especially any WASTE — \
tool calls that returned NO_DATA ("No data found for the specified queries"), redundant/duplicate queries, \
queries over windows with no data, or oversized context. Note total cost/tokens per run. Take snapshots \
to read values precisely. Do NOT click anything destructive; only read.

STEP 2 — Read the current prompts. Read the prompt-building code in each agent's file (the _build_prompt / \
_placement_prompt functions) to see exactly what instructions produce that behavior.

STEP 3 — Propose prompt improvements per agent for EFFICIENCY (cut wasted/NO_DATA MCP calls, avoid \
redundant queries, tighten context to lower tokens/cost) and CORRECTNESS (classifications/proposals that \
better match the observed telemetry). Each proposal: the target prompt file, the specific prompt change \
(quote the current instruction and the improved wording), the trace evidence that motivates it (e.g. \
"saw N NO_DATA queries for metric X"), and the expected effect (e.g. "-2 wasted MCP calls/run, ~-15% tokens").

Respond with ONLY a JSON object (no prose, no fences), exactly:
{{"agents":[{{"agent":"speciation|optimizer","trace_summary":"one sentence on what the traces showed","proposals":[{{"target_file":"breeder/...","change":"the prompt edit, quoting old->new wording","evidence":"trace evidence","expected_effect":"e.g. -2 MCP calls, -15% tokens"}}]}}]}}"""


def _run_claude(prompt: str) -> dict:
    from breeder.claude_cmd import claude_argv
    cmd = [
        *claude_argv(), "-p",
        "--model", _MODEL,
        "--output-format", "json",
        "--add-dir", BREEDER_DIR,
        "--allowedTools", *_ALLOWED_TOOLS,
    ]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise MetaError(f"claude -p timed out after {_TIMEOUT_S}s") from exc
    if proc.returncode != 0:
        raise MetaError(f"claude -p exited {proc.returncode}: {proc.stderr[:400]}")
    if not proc.stdout.strip():
        raise MetaError(f"claude -p produced no output; stderr: {proc.stderr[:400]}")
    try:
        env = json.loads(proc.stdout)
        body = env.get("result", proc.stdout)
        cost = env.get("total_cost_usd")
        if cost is not None:
            print(f"    [meta] claude -p cost≈${cost:.4f}, {env.get('num_turns','?')} turns", flush=True)
    except json.JSONDecodeError:
        body = proc.stdout
    m = re.search(r"\{.*\}", body, re.S)
    if not m:
        raise MetaError(f"no JSON in claude -p result: {body[:300]}")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise MetaError(f"could not parse meta JSON: {exc}") from exc


def _file_tickets(agents: list[dict]) -> dict:
    body = json.dumps({"agents": agents}).encode()
    req = urllib.request.Request(f"{UI_URL}/api/meta/tickets", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, ValueError) as exc:
        raise MetaError(f"filing meta tickets failed: {exc}") from exc


def meta_optimize(*, dry_run: bool) -> dict:
    parsed = _run_claude(_build_prompt())
    agents = parsed.get("agents", []) if isinstance(parsed, dict) else []
    agents = [a for a in agents if a.get("proposals") and a.get("agent") in TARGET_AGENTS]

    if dry_run:
        print("DRY RUN — meta prompt-improvement proposals (no tickets):\n")
        for a in agents:
            print(f"  agent={a.get('agent')}  — {a.get('trace_summary','')}")
            for pr in a.get("proposals", []):
                print(f"      • {pr.get('target_file')}: {pr.get('change')}")
                print(f"        evidence: {pr.get('evidence','')}  (→ {pr.get('expected_effect','?')})")
            print()
        print(f"meta (dry-run): {len(agents)} agent(s), "
              f"{sum(len(a.get('proposals',[])) for a in agents)} prompt proposal(s).")
        return {"dry_run": True, "agents": agents}

    res = _file_tickets(agents)
    filed = res.get("filed", []) if isinstance(res, dict) else []
    ok_n = sum(1 for f in filed if f.get("ok"))
    parts = "; ".join(
        f"{a['agent']}: {len(a.get('proposals',[]))} prompt fix(es) — {a.get('trace_summary','')[:55]}"
        for a in agents
    )
    summary = f"meta: analyzed traces, proposed prompt fixes for {len(agents)} agent(s) → {ok_n} Symphony ticket(s): {parts}"
    print(summary)
    return {"filed": filed, "agents": agents, "summary": summary}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Meta-agent: trace-driven prompt optimizer.")
    ap.add_argument("--dry-run", action="store_true", help="print proposals; file no tickets")
    args = ap.parse_args(argv)
    try:
        meta_optimize(dry_run=args.dry_run)
    except MetaError as exc:
        print(f"meta: FAILED — {exc}", file=__import__("sys").stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
