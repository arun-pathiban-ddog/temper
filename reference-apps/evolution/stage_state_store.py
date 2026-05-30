"""In-flight stage state persistence.

Persists the entire mid-stage state to a single JSON file at
/tmp/evolution-stage-progress.json. This lets the live loop resume after a
crash, OOM, network blip, or operator Ctrl-C — picking up at the exact phase
each variant had reached, rather than re-running research/code/DST.

State layout:
  {
    "schema": 1,
    "stage_num": <int>,
    "lineage", "metric", "direction", "goal_id", "champion_sha",
    "champion_fitness": {...} | null,
    "variant_count": <int>,
    "saved_at": "ISO-8601",
    "variants": {
      "<variant_num>": {
        "variant_num", "phase", "cluster_name", "cid", "commit_sha",
        "wt_path", "dst_passed",
        "hypothesis": {...}, "mutation": {...},
        "cloned_workload": [{...}, ...],
        "fitness": {...} | null,
        "delta_pct": <float> | null,
        "survived": <bool>
      }
    }
  }

Phase ladder (each phase implies all prior phases complete):
  pending           — slot exists, nothing done
  researched        — hypothesis ready
  coded             — mutation applied to worktree
  dst_passed        — DST verified
  committed         — commit made on evolve/<lineage>
  cluster_creating  — Temper Cluster entity created
  cluster_live      — cluster running
  workload_cloned   — producers/consumers cloned to variant cluster
  observed          — traffic window elapsed (logical: usually rolled into measured)
  measured          — fitness measured (terminal: success path)
  culled            — terminal failure (DST cull, build failure, etc.)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

STATE_PATH = Path("/tmp/evolution-stage-progress.json")

# Phase ordering. Higher index = further along.
PHASE_ORDER: list[str] = [
    "pending",
    "researched",
    "coded",
    "dst_passed",
    "committed",
    "cluster_creating",
    "cluster_live",
    "workload_cloned",
    "observed",
    "measured",
    "culled",
]


def phase_index(phase: str) -> int:
    try:
        return PHASE_ORDER.index(phase)
    except ValueError:
        return -1


def phase_at_or_after(phase: str, threshold: str) -> bool:
    """Return True if `phase` is `threshold` or later in the ladder."""
    return phase_index(phase) >= phase_index(threshold)


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------

def load_progress() -> dict | None:
    """Load the in-flight stage state from disk, or return None if absent."""
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  [state] could not load progress file ({exc}) — ignoring")
        return None


def save_progress(payload: dict) -> None:
    """Atomically write the progress JSON."""
    payload = {**payload, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def clear_progress() -> None:
    if STATE_PATH.exists():
        STATE_PATH.unlink()


# ---------------------------------------------------------------------------
# Stage-state helpers (compose with StageState from stage_manager)
# ---------------------------------------------------------------------------

def stage_state_to_dict(state, variants: dict[int, dict]) -> dict:
    """Serialize a StageState + variants map to the progress JSON shape."""
    cf = state.champion_fitness
    return {
        "schema": 1,
        "stage_num": state.stage_num,
        "lineage": state.lineage,
        "metric": state.metric,
        "direction": state.direction,
        "goal_id": state.goal_id,
        "champion_sha": state.champion_sha,
        "champion_fitness": _fitness_to_dict(cf) if cf else None,
        "variant_count": state.variant_count,
        "variants": {str(k): v for k, v in variants.items()},
    }


def _fitness_to_dict(f) -> dict:
    return {
        "cluster_tag": getattr(f, "cluster_tag", ""),
        "latency_p95_ms": getattr(f, "latency_p95_ms", None),
        "throughput_rps": getattr(f, "throughput_rps", None),
        "data_points": getattr(f, "data_points", 0),
        "error": getattr(f, "error", ""),
    }


def fitness_from_dict(d: dict | None):
    if not d:
        return None
    from fitness_live import FitnessResult
    return FitnessResult(
        cluster_tag=d.get("cluster_tag", ""),
        latency_p95_ms=d.get("latency_p95_ms"),
        throughput_rps=d.get("throughput_rps"),
        data_points=int(d.get("data_points", 0) or 0),
        error=d.get("error", ""),
    )


# ---------------------------------------------------------------------------
# Variant slot helpers
# ---------------------------------------------------------------------------

def variant_slot(
    variant_num: int,
    phase: str = "pending",
    **fields,
) -> dict:
    """Build a fresh variant slot dict with all expected keys."""
    return {
        "variant_num": variant_num,
        "phase": phase,
        "cluster_name": "",
        "cid": "",
        "commit_sha": "",
        "wt_path": "",
        "dst_passed": False,
        "hypothesis": None,
        "mutation": None,
        "cloned_workload": [],
        "fitness": None,
        "delta_pct": None,
        "survived": False,
        "hypothesis_title": "",
        **fields,
    }


def update_variant(progress: dict, variant_num: int, **fields) -> dict:
    """Update one variant slot in the progress dict (in place)."""
    key = str(variant_num)
    slot = progress.setdefault("variants", {}).get(key) or variant_slot(variant_num)
    slot.update(fields)
    progress["variants"][key] = slot
    return progress


def remove_variant(progress: dict, variant_num: int) -> None:
    progress.get("variants", {}).pop(str(variant_num), None)


def get_variants(progress: dict) -> dict[int, dict]:
    return {int(k): v for k, v in progress.get("variants", {}).items()}


# ---------------------------------------------------------------------------
# Cluster-name set: used by orphan-cluster scanner to skip our own.
# ---------------------------------------------------------------------------

def owned_cluster_names(progress: dict | None) -> set[str]:
    if not progress:
        return set()
    out: set[str] = set()
    for v in progress.get("variants", {}).values():
        nm = v.get("cluster_name") or ""
        if nm:
            out.add(nm)
    return out
