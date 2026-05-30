"""Stage lifecycle manager for the live evolution loop.

Responsibilities:
  - Decide which lineage/metric to evolve (Observer role: pick active FitnessGoal).
  - Determine the champion baseline fitness at stage start.
  - Decide when a variant wins (fitness threshold exceeded).
  - Promote the champion to champion-metrics-pranav-clone and record the generation.
  - Determine when a stage is complete (variants exhausted or improvement found).
  - Write the evolution status JSON for the UI progress panel.

State persistence:
  All durable state lives in Git (commits on champion-metrics-pranav-clone / evolve/* branches)
  and in Temper (FitnessGoal generation records, Breed entities).
  The status file (/tmp/evolution_status.json) is ephemeral — it's only for the UI.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from config import EVOLUTION_STATUS_PATH, LiveEvolutionConfig
from fitness_live import FitnessResult, compute_delta_pct, measure_champion_baseline
from live_client import LiveClient, LiveClientError
from propose_live import StageHistoryEntry, TextFeedback


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class VariantResult:
    """Outcome of one variant tested in a batch."""
    variant_num: int
    cluster_name: str
    change_description: str
    mutation_type: str
    dst_passed: bool
    fitness: FitnessResult | None
    delta_pct: float | None      # None if DST failed or no data
    survived: bool               # True if delta_pct >= threshold
    feedback: TextFeedback = field(default_factory=lambda: TextFeedback(0, "no_data", ""))
    cid: str = ""                # Temper Cluster entity ID
    cloned_workload: list[dict] = field(default_factory=list)
    # Research-agent tracking (for history + UI).
    hypothesis_title: str = ""
    refinement_num: int = 0      # 0 = initial attempt, 1..N = refinement rounds


@dataclass
class StageState:
    """In-memory state for the current stage (rebuilt from Git/Temper on restart)."""
    stage_num: int
    lineage: str
    metric: str
    direction: str
    goal_id: str
    champion_sha: str
    champion_fitness: FitnessResult | None
    history: list[StageHistoryEntry] = field(default_factory=list)
    variant_count: int = 0
    champion_found: bool = False
    champion_variant_num: int = -1
    champion_delta_pct: float | None = None
    started_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Observer: determine active lineage
# ---------------------------------------------------------------------------

# Minimum improvement % required for a variant to become champion.
DEFAULT_IMPROVEMENT_THRESHOLD_PCT = 3.0


def observe_active_lineage(
    config: LiveEvolutionConfig, client: LiveClient
) -> tuple[str, str, str, str]:
    """Observer: pick the lineage to evolve next.

    Two-tier decision:
    1. Fast heuristic: if Datadog baseline is clear (p95 > 100ms → latency).
    2. claude -p with Datadog MCP: agent queries live metrics directly and
       reasons about which dimension needs improvement most.

    Returns (lineage, metric, direction, goal_id).
    """
    from fitness_live import measure_champion_baseline
    from claude_agent import make_observer_agent

    goal_ids = config.fitness_goal_ids
    meta = {
        "latency":    ("p95_latency", "minimize"),
        "throughput": ("throughput",  "maximize"),
    }

    # Tier 1: fast heuristic from our own Datadog client.
    baseline = None
    try:
        baseline = measure_champion_baseline(config)
    except Exception as exc:
        print(f"  [observer] baseline query failed ({exc}); trying MCP agent")

    if baseline and baseline.latency_p95_ms is not None:
        lineage = "latency" if baseline.latency_p95_ms > 100.0 else "throughput"
        metric, direction = meta[lineage]
        goal_id = goal_ids.get(lineage, f"goal-live-{lineage}")
        print(f"  [observer] heuristic → {lineage} "
              f"(p95={baseline.latency_p95_ms:.1f}ms, thr={baseline.throughput_rps}rps)")
        return lineage, metric, direction, goal_id

    # Tier 2: Observer Agent via claude -p with Datadog MCP.
    guide = config.evolution_guide()
    baseline_summary = baseline.summary() if baseline else "unavailable"
    import textwrap
    prompt = textwrap.dedent(f"""\
        You are the Observer for a Helix directed-evolution system.
        Use the Datadog MCP tools to query live metrics for the champion cluster
        (tag: cluster:{config.champion_cluster_dd_tag}).

        Metrics to check:
          - helix.produce.latency_ms.95percentile  (lower is better)
          - helix.produce.throughput               (higher is better)

        Our own baseline (may be unavailable): {baseline_summary}

        Evolution spec excerpt:
        {guide[:2000]}

        Based on live Datadog data and the spec's Champion History, which lineage
        needs improvement most right now?
        Reply with exactly one word: latency or throughput.
    """)

    try:
        agent = make_observer_agent(config.model_observer)
        result = agent.run(prompt, label="observer")
        if not result.is_error:
            answer = result.text.strip().lower()
            lineage = "throughput" if "throughput" in answer else "latency"
            print(f"  [observer] MCP agent → {lineage} (cost=${result.cost_usd:.3f})")
        else:
            print(f"  [observer] MCP agent error: {result.error_message[:80]} — defaulting to latency")
            lineage = "latency"
    except Exception as exc:
        print(f"  [observer] agent error ({exc}) — defaulting to latency")
        lineage = "latency"

    metric, direction = meta[lineage]
    goal_id = goal_ids.get(lineage, f"goal-live-{lineage}")
    print(f"  [observer] lineage: {lineage} ({metric}/{direction}) goal={goal_id}")
    return lineage, metric, direction, goal_id


# ---------------------------------------------------------------------------
# Stage initialisation
# ---------------------------------------------------------------------------

def init_stage(
    config: LiveEvolutionConfig,
    client: LiveClient,
    stage_num: int,
) -> StageState:
    """Initialise a new stage: determine lineage, measure baseline.

    If /tmp/evolution-stage-progress.json holds an in-flight stage matching
    `stage_num`, we restore lineage/baseline/etc. from disk and skip re-running
    the Observer + baseline DD query. This makes resume cheap and deterministic.
    """
    from stage_state_store import fitness_from_dict, load_progress

    saved = load_progress()
    if saved and saved.get("stage_num") == stage_num:
        cf = fitness_from_dict(saved.get("champion_fitness"))
        # Re-measure baseline live if the saved one is missing or empty —
        # we cannot evaluate variants without a real reference reading.
        if cf is None or (cf.data_points or 0) == 0:
            print(f"  [stage {stage_num}] saved baseline empty; re-measuring live…")
            try:
                cf = measure_champion_baseline(config)
                print(f"  [stage {stage_num}] live baseline: {cf.summary()}")
            except Exception as exc:
                print(f"  [stage {stage_num}] live baseline failed ({exc})")
                cf = None
        else:
            print(f"  [stage {stage_num}] resuming in-flight stage from disk "
                  f"(lineage={saved['lineage']}, baseline={cf.summary()})")

        state = StageState(
            stage_num=stage_num,
            lineage=saved["lineage"],
            metric=saved["metric"],
            direction=saved["direction"],
            goal_id=saved["goal_id"],
            champion_sha=saved.get("champion_sha", ""),
            champion_fitness=cf,
            variant_count=int(saved.get("variant_count", 0) or 0),
        )
        client.ensure_evolve_branch(state.lineage)
        _write_status(config, state, phase="resuming",
                      message=f"Resuming stage {stage_num}")
        return state

    lineage, metric, direction, goal_id = observe_active_lineage(config, client)

    # Resolve champion HEAD SHA.
    r = client.git("rev-parse", "HEAD")
    champion_sha = r.stdout.strip() if r.returncode == 0 else "unknown"

    # Measure champion baseline.
    print(f"  [stage {stage_num}] measuring champion baseline…")
    try:
        champion_fitness = measure_champion_baseline(config)
        print(f"  [stage {stage_num}] baseline: {champion_fitness.summary()}")
    except Exception as exc:
        print(f"  [stage {stage_num}] could not measure baseline ({exc}); proceeding without")
        champion_fitness = None

    # Ensure the evolve/<lineage> branch exists.
    client.ensure_evolve_branch(lineage)

    state = StageState(
        stage_num=stage_num,
        lineage=lineage,
        metric=metric,
        direction=direction,
        goal_id=goal_id,
        champion_sha=champion_sha,
        champion_fitness=champion_fitness,
    )
    _write_status(config, state, phase="running", message=f"Stage {stage_num} started")
    return state


# ---------------------------------------------------------------------------
# Variant outcome helpers
# ---------------------------------------------------------------------------

def evaluate_variant(
    config: LiveEvolutionConfig,
    state: StageState,
    cluster_name: str,
    fitness: FitnessResult,
    change_description: str,
    variant_num: int,
    improvement_threshold_pct: float = DEFAULT_IMPROVEMENT_THRESHOLD_PCT,
) -> VariantResult:
    """Compare variant fitness against champion baseline."""
    delta = compute_delta_pct(fitness, state.champion_fitness, state.direction, state.metric) \
        if state.champion_fitness and fitness.is_valid else None

    survived = delta is not None and delta >= improvement_threshold_pct

    if delta is None:
        feedback_outcome = "no_data"
        feedback_msg = f"no DD data for cluster {cluster_name}"
    elif survived:
        feedback_outcome = "fitness_win"
        feedback_msg = f"Δ={delta:+.1f}% ({state.metric})"
    else:
        feedback_outcome = "fitness_fail"
        feedback_msg = (
            f"measured Δ={delta:+.1f}% — below {improvement_threshold_pct:.0f}% threshold "
            f"({state.metric})"
        )

    return VariantResult(
        variant_num=variant_num,
        cluster_name=cluster_name,
        change_description=change_description,
        mutation_type="code",
        dst_passed=True,
        fitness=fitness,
        delta_pct=delta,
        survived=survived,
        feedback=TextFeedback(variant_num, feedback_outcome, feedback_msg),
    )


def history_entry(result: VariantResult, state: StageState) -> StageHistoryEntry:
    outcome = (
        "survived" if result.survived
        else "dst_culled" if not result.dst_passed
        else "fitness_culled" if result.fitness and result.fitness.is_valid
        else "no_data"
    )
    return StageHistoryEntry(
        stage_num=state.stage_num,
        variant_num=result.variant_num,
        description=result.change_description,
        outcome=outcome,
        delta_pct=result.delta_pct,
        hypothesis_title=result.hypothesis_title,
        refinement_num=result.refinement_num,
    )


# ---------------------------------------------------------------------------
# Stage completion check
# ---------------------------------------------------------------------------

def is_stage_complete(state: StageState, config: LiveEvolutionConfig) -> bool:
    """Return True if the stage should end (champion found or variant budget exhausted)."""
    if state.champion_found:
        return True
    if state.variant_count >= config.max_variants_per_stage:
        print(f"  [stage {state.stage_num}] variant budget exhausted "
              f"({state.variant_count}/{config.max_variants_per_stage})")
        return True
    return False


# ---------------------------------------------------------------------------
# Champion promotion
# ---------------------------------------------------------------------------

def promote_stage_champion(
    config: LiveEvolutionConfig,
    client: LiveClient,
    state: StageState,
    result: VariantResult,
    wt_path: Path,
) -> bool:
    """Promote the winning variant to champion-metrics-pranav-clone and record in Temper."""
    delta_str = f"{result.delta_pct:+.1f}%" if result.delta_pct is not None else "?"
    print(f"\n  *** CHAMPION: variant {result.variant_num} "
          f"({delta_str} {state.metric}) — promoting to {config.champion_branch} ***")

    success = client.promote_champion(
        state.lineage, state.stage_num,
        result.change_description, result.delta_pct or 0.0, wt_path
    )
    if success:
        state.champion_found = True
        state.champion_variant_num = result.variant_num
        state.champion_delta_pct = result.delta_pct
        _write_status(
            config, state, phase="champion",
            message=f"Champion: {result.change_description[:80]} ({delta_str})"
        )

        # Auto-redeploy the baseline cluster so the next stage's champion
        # baseline reflects the promoted change.
        if config.auto_redeploy_champion:
            print(f"\n  [redeploy] rolling baseline cluster {config.champion_cluster_name} "
                  f"to the promoted code…")
            _write_status(
                config, state, phase="redeploying_champion",
                message=f"Rebuilding {config.champion_cluster_name} on promoted code…",
            )
            try:
                ok = client.redeploy_champion_cluster(state.stage_num)
            except Exception as exc:
                print(f"  [redeploy] error ({exc}); next stage baseline will lag")
                ok = False
            if not ok:
                print(f"  [redeploy] FAILED — next stage compares against the OLD baseline. "
                      f"Run kubectl set image manually if you want the new code measured.")
            else:
                _write_status(
                    config, state, phase="champion_deployed",
                    message=f"Baseline {config.champion_cluster_name} now on promoted code",
                )
        else:
            print(f"  [redeploy] auto_redeploy_champion=False — baseline cluster unchanged")
    return success


# ---------------------------------------------------------------------------
# Status file (for UI panel)
# ---------------------------------------------------------------------------

def _write_status(
    config: LiveEvolutionConfig,
    state: StageState,
    phase: str = "running",
    message: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """Write /tmp/evolution_status.json for the UI progress panel."""
    history_data = [
        {
            "stage": e.stage_num,
            "variant": e.variant_num,
            "refinement": e.refinement_num,
            "hypothesis": e.hypothesis_title[:80] if e.hypothesis_title else "",
            "description": e.description[:120],
            "outcome": e.outcome,
            "delta_pct": round(e.delta_pct, 2) if e.delta_pct is not None else None,
        }
        for e in state.history
    ]
    baseline_summary = state.champion_fitness.summary() if state.champion_fitness else "N/A"
    payload: dict[str, Any] = {
        "phase": phase,
        "message": message,
        "stage": state.stage_num,
        "lineage": state.lineage,
        "metric": state.metric,
        "direction": state.direction,
        "goal_id": state.goal_id,
        "variant_count": state.variant_count,
        "max_variants": config.max_variants_per_stage,
        "champion_found": state.champion_found,
        "champion_variant_num": state.champion_variant_num,
        "champion_delta_pct": (
            round(state.champion_delta_pct, 2)
            if state.champion_delta_pct is not None else None
        ),
        "baseline": baseline_summary,
        "history": history_data,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **(extra or {}),
    }
    try:
        EVOLUTION_STATUS_PATH.write_text(json.dumps(payload, indent=2))
    except Exception as exc:
        print(f"  [status] could not write status file: {exc}")


def write_idle_status(config: LiveEvolutionConfig, message: str = "Idle") -> None:
    EVOLUTION_STATUS_PATH.write_text(json.dumps({
        "phase": "idle",
        "message": message,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, indent=2))


# ---------------------------------------------------------------------------
# Startup resume: recover state from Git + Temper
# ---------------------------------------------------------------------------

def startup_resume(
    config: LiveEvolutionConfig,
    client: LiveClient,
) -> tuple[int, list[StageHistoryEntry]]:
    """Recover the last completed stage number from Temper FitnessGoal records.

    Also tears down any orphaned variant clusters from a previous run.
    Returns (last_completed_stage_num, prior_history).
    """
    print("[startup] scanning for orphaned variant clusters…")
    _cleanup_orphaned_clusters(client)

    # Read generation records from Temper to determine last stage.
    last_stage = 0
    history: list[StageHistoryEntry] = []
    for lineage, goal_id in config.fitness_goal_ids.items():
        records = client.get_generation_records(goal_id)
        for rec in records:
            gen_num = int(rec.get("generation", "0") or "0")
            stage = gen_num // 100
            variant = gen_num % 100
            outcome = rec.get("outcome", "unknown")
            fitness_str = rec.get("fitness", "")
            try:
                delta = float(fitness_str) if fitness_str else None
            except ValueError:
                delta = None
            history.append(StageHistoryEntry(
                stage_num=stage, variant_num=variant,
                description=rec.get("gene", "unknown"), outcome=outcome,
                delta_pct=delta,
            ))
            last_stage = max(last_stage, stage)

    history.sort(key=lambda e: (e.stage_num, e.variant_num))
    print(f"[startup] last completed stage: {last_stage}, prior records: {len(history)}")
    return last_stage, history


def _cleanup_orphaned_clusters(client: LiveClient) -> None:
    """Tear down any evolution variant clusters NOT owned by an in-flight stage.

    Clusters whose names appear in /tmp/evolution-stage-progress.json (the
    mid-stage resume state) are kept so the loop can pick up where it left off.
    """
    from stage_state_store import load_progress, owned_cluster_names
    owned = owned_cluster_names(load_progress())
    if owned:
        print(f"  [cleanup] preserving in-flight clusters: {sorted(owned)}")
    try:
        clusters = client.list_clusters()
        for c in clusters:
            name = (c.get("name") or c.get("Name") or
                    (c.get("fields") or {}).get("Name") or "")
            cid = c.get("id") or c.get("Id") or (c.get("fields") or {}).get("Id") or name
            status = (c.get("status") or c.get("Status") or
                      (c.get("fields") or {}).get("Status") or "").lower()
            # Evolution variant clusters are named ev-<lineage>-s<N>-v<N>.
            if name.startswith("ev-") and status in ("live", "deploying", "building"):
                if name in owned:
                    print(f"  [cleanup] keeping in-flight cluster {name} ({status})")
                    continue
                print(f"  [cleanup] tearing down orphaned cluster {name} ({status})")
                try:
                    client.teardown_cluster(cid)
                except LiveClientError as exc:
                    print(f"  [cleanup] warning: could not tear down {name}: {exc}")
    except LiveClientError as exc:
        print(f"  [cleanup] warning: could not list clusters: {exc}")
