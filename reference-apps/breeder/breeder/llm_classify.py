"""LLM-driven, telemetry-only workload-niche classifier (ADR-0102).

Instead of static heuristic thresholds, this shells out to `claude -p` (Claude
Code in non-interactive mode) to classify each queue. claude -p already has auth
and the Datadog MCP server wired up, so the agent QUERIES the telemetry itself
(via get_datadog_metric) and reasons about each queue's workload shape — no API
keys, tokens, or SDK in our code.

The firewall (ADR-0099) is preserved structurally: we pass --allowedTools listing
ONLY the Datadog metric-read MCP tools, so the agent can read telemetry but has no
tool to read the workload's config (Queue/Producer/Consumer entities).

Returns NicheInference per queue (same shape as the heuristic infer_all), with the
genome attached from NICHE_PLAYBOOK and the LLM's reasoning as the motivation.
"""

from __future__ import annotations

import json
import os
import re
import subprocess

from breeder.infer import NICHE_PLAYBOOK, NicheInference

_MODEL = os.environ.get("SPECIATE_MODEL", "sonnet")
# Firewall: ONLY Datadog telemetry-read tools. No config-read tool is allowed, so
# the agent classifies from observed metrics, never from the workload spec.
_ALLOWED_TOOLS = [
    "mcp__datadog-mcp__get_datadog_metric",
    "mcp__datadog-mcp__search_datadog_metrics",
    "mcp__datadog-mcp__get_datadog_metric_context",
]
_TIMEOUT_S = int(os.environ.get("SPECIATE_LLM_TIMEOUT_S", "600"))


class LlmClassifyError(RuntimeError):
    pass


def _build_prompt(queues: list[str], window_min: int) -> str:
    niche_menu = "\n".join(
        f"- {n}: {p['rationale']}" for n, p in NICHE_PLAYBOOK.items()
    )
    qlist = ", ".join(queues)
    return f"""Classify these Helix (Kafka-replacement) message queues into workload niches \
using ONLY Datadog telemetry. You cannot see any workload's configuration — observe its behavior.

Queues: {qlist}

For each queue, call the get_datadog_metric MCP tool over the last {window_min} minutes with the query:
  sum:workload.producer.sent{{queue:QUEUE}}.as_rate()
(substitute each queue name). One query per queue is enough; you may also look at \
sum:workload.consumer.consumed{{queue:QUEUE}}.as_rate() or avg:workload.consumer.lag{{queue:QUEUE}} \
if a queue is ambiguous. Reason about the SHAPE of each load — magnitude AND variability. \
Produce latency is sub-millisecond and not a useful signal; use rate magnitude and rate variance.

Niches (pick the single best fit per queue):
{niche_menu}

Heuristics: bursty = rate swings between ~0 and high (high variance / zero-crossings). \
high-throughput-batch = high sustained rate, low variance. low-latency = steady moderate rate. \
steady = low flat trickle.

Respond with ONLY a JSON object (no prose, no markdown fences), exactly this shape:
{{"classifications":[{{"queue":"<name>","niche":"<one of: {'/'.join(NICHE_PLAYBOOK)}>","confidence":"high|medium|low","reasoning":"one sentence citing the rate and variance you observed"}}]}}"""


def _run_claude(prompt: str) -> dict:
    """Invoke `claude -p` non-interactively, return the parsed classifications JSON."""
    from breeder.claude_cmd import claude_argv
    cmd = [
        *claude_argv(), "-p",
        "--model", _MODEL,
        "--output-format", "json",
        "--allowedTools", *_ALLOWED_TOOLS,
    ]
    try:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, timeout=_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise LlmClassifyError(f"claude -p timed out after {_TIMEOUT_S}s") from exc
    if proc.returncode != 0:
        raise LlmClassifyError(f"claude -p exited {proc.returncode}: {proc.stderr[:400]}")
    if not proc.stdout.strip():
        raise LlmClassifyError(f"claude -p produced no output; stderr: {proc.stderr[:400]}")

    # The --output-format json envelope has a `result` string holding the model's
    # reply. Extract the JSON object embedded in it.
    try:
        envelope = json.loads(proc.stdout)
        body = envelope.get("result", proc.stdout)
        cost = envelope.get("total_cost_usd")
        if cost is not None:
            print(f"    [llm-classify] claude -p cost≈${cost:.4f}, "
                  f"{envelope.get('num_turns','?')} turns", flush=True)
    except json.JSONDecodeError:
        body = proc.stdout
    # Accept either a JSON object {...} (classification) or array [...] (discovery).
    m = re.search(r"(\{.*\}|\[.*\])", body, re.S)
    if not m:
        raise LlmClassifyError(f"no JSON in claude -p result: {body[:300]}")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise LlmClassifyError(f"could not parse JSON: {exc}") from exc


def _placement_prompt(window_min: int) -> str:
    niche_menu = "\n".join(f"- {n}: {p['rationale']}" for n, p in NICHE_PLAYBOOK.items())
    tunings = "; ".join(
        f"{n} {json.dumps(p.get('genome', {}))}" for n, p in NICHE_PLAYBOOK.items()
    )
    niche_enum = "/".join(NICHE_PLAYBOOK)
    return f"""You are a workload-placement planner for a Helix (Kafka-replacement) cluster fleet. \
Using ONLY Datadog telemetry (you cannot read any workload's configuration — observe behaviour), do this:

1. Discover the active queues: the distinct `queue` tag values on metric workload.producer.sent (last {window_min} min).
2. For each queue, query its produce-rate shape: sum:workload.producer.sent{{queue:QUEUE}}.as_rate() over {window_min} min. \
Reason about magnitude AND variability (look at workload.consumer.lag if a queue is ambiguous).
3. Classify each queue into a niche:
{niche_menu}
4. Propose a PLACEMENT. All queues currently run on the baseline cluster "cluster-001". Propose dedicated niche-tuned \
clusters and assign each queue to one. You MAY co-locate queues that share a niche on one cluster. For each proposed \
cluster give: a dns-safe name (lowercase a-z0-9-, start with a letter, <=20 chars, e.g. "niche-bursty"), the niche it \
serves, and a Helix tuning genome as a JSON object using knobs linger_ms (int), max_inflight (int), append_batch_size \
(int) — use {{}} for baseline (steady). Suggested tunings: {tunings}.

Respond with ONLY a JSON object (no prose, no fences), exactly:
{{"placements":[{{"cluster":"<name>","niche":"<{niche_enum}>","genome":{{...}},"queues":["..."],"reasoning":"one sentence citing the rate/variance you observed"}}]}}"""


def propose_placement_llm(window_min: int = 20) -> list[dict]:
    """LLM-proposed placement plan: the agent discovers queues, classifies them,
    and proposes target clusters (new or co-located) with tuning genomes — all
    from telemetry via claude -p. Returns a list of placement dicts:
    {cluster, niche, genome, queues, reasoning}. Telemetry-only (no config read)."""
    parsed = _run_claude(_placement_prompt(window_min))
    placements = parsed.get("placements", []) if isinstance(parsed, dict) else []
    out = []
    for pl in placements:
        niche = pl.get("niche") if pl.get("niche") in NICHE_PLAYBOOK else "steady"
        cluster = str(pl.get("cluster", f"niche-{niche}")).strip().lower()
        genome = pl.get("genome") if isinstance(pl.get("genome"), dict) else dict(NICHE_PLAYBOOK[niche].get("genome", {}))
        queues = [str(q) for q in pl.get("queues", []) if q]
        if not queues:
            continue
        out.append({
            "cluster": cluster, "niche": niche, "genome": genome, "queues": sorted(set(queues)),
            "motivation": f"LLM placement: {pl.get('reasoning', '')}".strip(),
        })
    return out


def discover_queues_llm(window_min: int = 30) -> list[str]:
    """Discover the active queue names FROM TELEMETRY via claude -p (the queue: tag
    values on workload.producer.sent). No config read — tag values only."""
    prompt = (
        "Using the Datadog MCP, find the distinct values of the `queue` tag on the metric "
        "`workload.producer.sent` over the last "
        f"{max(window_min, 30)} minutes (e.g. via get_datadog_metric with a query grouped by "
        "queue: sum:workload.producer.sent{*} by {queue}, or search_datadog_metrics / "
        "get_datadog_metric_context). Respond with ONLY a JSON array of the queue name strings, "
        'e.g. ["orders","checkout"]. No prose.'
    )
    try:
        out = _run_claude(prompt)
    except LlmClassifyError:
        return []
    # _run_claude returns a dict (it expects an object); for an array reply, re-extract.
    if isinstance(out, dict):
        # maybe {"queues":[...]} or it parsed the array under some key
        for v in out.values():
            if isinstance(v, list):
                return [str(x) for x in v]
        return []
    if isinstance(out, list):
        return [str(x) for x in out]
    return []


def classify_all_llm(src, queues: list[str], window_min: int = 15) -> list[NicheInference]:
    """LLM-driven classification via `claude -p`. `src` is accepted for signature
    parity with the heuristic infer_all but unused — claude -p queries Datadog
    directly through its own MCP connection."""
    prompt = _build_prompt(queues, window_min)
    parsed = _run_claude(prompt)

    by_queue = {c.get("queue"): c for c in parsed.get("classifications", [])}
    out: list[NicheInference] = []
    for q in queues:
        d = by_queue.get(q)
        if not d:
            d = {"niche": "steady", "confidence": "low",
                 "reasoning": "claude -p returned no classification for this queue; defaulted to steady."}
        niche = d.get("niche") if d.get("niche") in NICHE_PLAYBOOK else "steady"
        play = NICHE_PLAYBOOK[niche]
        out.append(NicheInference(
            queue=q, niche=niche, confidence=d.get("confidence", "medium"),
            gene=play["gene"], metric=play["metric"], direction=play["direction"],
            genome=dict(play.get("genome", {})),
            motivation=f"LLM: {d.get('reasoning', '')}".strip(),
            telemetry={},
        ))
    return out
