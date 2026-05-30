"""One-off recovery: rebuild /tmp/evolution-stage-progress.json from existing
on-disk state so the live loop can resume mid-stage.

What this script inspects:
  • Worktrees under EVOLUTION_WORKTREES that match `proposal-s<N>-v<M>/`
  • Hypothesis JSON files in `/tmp/hypothesis-s*-v*.json`
  • Live Temper Cluster entities matching `ev-<lineage>-s<N>-v<M>`
  • The HEAD commit of each worktree (so the commit_sha field is populated)

What it writes:
  /tmp/evolution-stage-progress.json with one variant slot per `(s,v)` pair,
  each at phase `cluster_live` (the most common interrupted state). You can
  edit the file before running the loop if a cluster is at a different phase.

Usage:
  python3 recover_stage.py --stage 1 --lineage latency
  python3 recover_stage.py --stage 1 --lineage latency --phase committed
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import LiveEvolutionConfig
from live_client import LiveClient, LiveClientError
from stage_state_store import (
    PHASE_ORDER,
    save_progress,
    stage_state_to_dict,
    variant_slot,
)


VARIANT_RE = re.compile(r"^proposal-s(\d+)-v(\d+)$")


def _read_hypothesis_for(stage_num: int, variant_num: int) -> dict | None:
    """Find the hypothesis JSON file for (stage, variant). Tries multiple paths
    because the propose_live.py filename has been buggy at times (s0 vs sN)."""
    candidates = [
        Path(f"/tmp/hypothesis-s{stage_num}-v{variant_num}.json"),
        Path(f"/tmp/hypothesis-s0-v{variant_num}.json"),   # known bug — stage_history empty case
        Path(f"/tmp/hypothesis-refined-v{variant_num}-r1.json"),
    ]
    for p in candidates:
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
    return None


def _worktree_head(wt_path: Path) -> tuple[str, str]:
    """Return (full_sha, subject) for the worktree HEAD."""
    try:
        r = subprocess.run(
            ["git", "log", "-1", "--format=%H%n%s"],
            cwd=wt_path, capture_output=True, text=True, timeout=15, check=False,
        )
        if r.returncode != 0:
            return "", ""
        lines = r.stdout.splitlines()
        return (lines[0] if lines else "", lines[1] if len(lines) > 1 else "")
    except Exception:
        return "", ""


def _files_changed(wt_path: Path) -> list[str]:
    try:
        r = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
            cwd=wt_path, capture_output=True, text=True, timeout=15, check=False,
        )
        return [f for f in r.stdout.splitlines() if f.strip()]
    except Exception:
        return []


def _find_cluster_in_temper(
    client: LiveClient, cluster_name: str
) -> tuple[str, str]:
    """Return (cid, status) for the given cluster name, or ('','')."""
    try:
        for c in client.list_clusters():
            name = (c.get("name") or c.get("Name") or
                    (c.get("fields") or {}).get("Name") or "")
            if name != cluster_name:
                continue
            cid = (c.get("id") or c.get("Id") or
                   (c.get("fields") or {}).get("Id") or name)
            status = (c.get("status") or c.get("Status") or
                      (c.get("fields") or {}).get("Status") or "").lower()
            return cid, status
    except LiveClientError as exc:
        print(f"  [recover] could not list temper clusters: {exc}")
    return "", ""


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Reconstruct mid-stage resume file")
    p.add_argument("--stage", type=int, required=True)
    p.add_argument("--lineage", required=True, choices=["latency", "throughput"])
    p.add_argument("--phase", default="cluster_live",
                   help="Phase to assume for each variant (default: cluster_live)")
    args = p.parse_args(argv)

    if args.phase not in PHASE_ORDER:
        print(f"Bad --phase {args.phase!r}. Choices: {PHASE_ORDER}")
        return 2

    config = LiveEvolutionConfig()
    client = LiveClient(config)

    print(f"[recover] scanning worktrees under {config.evolution_worktrees}")
    variants_found: list[tuple[int, Path]] = []
    for wt in sorted(config.evolution_worktrees.iterdir()):
        if not wt.is_dir():
            continue
        m = VARIANT_RE.match(wt.name)
        if not m:
            continue
        s, v = int(m.group(1)), int(m.group(2))
        if s != args.stage:
            continue
        variants_found.append((v, wt))
    print(f"[recover] found {len(variants_found)} worktree(s) for stage {args.stage}")

    # Skip baseline measurement here — init_stage will re-measure live on
    # resume so we don't persist a stale/empty reading.
    baseline = None
    print("[recover] champion baseline will be re-measured live on resume.")

    # Resolve champion HEAD SHA.
    r = client.git("rev-parse", "HEAD")
    champion_sha = r.stdout.strip() if r.returncode == 0 else ""

    metric_map = {"latency": ("p95_latency", "minimize"),
                  "throughput": ("throughput", "maximize")}
    metric, direction = metric_map[args.lineage]
    goal_id = config.fitness_goal_ids.get(args.lineage, f"goal-live-{args.lineage}")

    variants: dict[str, dict] = {}
    for vnum, wt in variants_found:
        sha, subject = _worktree_head(wt)
        files = _files_changed(wt)
        hyp_data = _read_hypothesis_for(args.stage, vnum) or {}
        cname = f"ev-{args.lineage[:3]}-s{args.stage}-v{vnum}"
        cid, status = _find_cluster_in_temper(client, cname)

        # Build hypothesis dict
        hyp_dict = {
            "title": hyp_data.get("title", subject.split("evolve(")[-1].split(")")[0]
                     if "evolve(" in subject else "recovered"),
            "reasoning": hyp_data.get("reasoning", ""),
            "expected_mechanism": hyp_data.get("expected_mechanism", ""),
            "suggested_files": hyp_data.get("suggested_files", files),
            "confidence": hyp_data.get("confidence", "medium"),
            "variant_num": vnum,
            "refinement_num": 0,
            "research_session_id": "",
            "raw_response": "",
        }

        # Build mutation dict
        mut_dict = {
            "change_description": subject.split("] ")[-1] if "] " in subject else subject,
            "rationale": "",
            "mutation_type": "code",
            "edits": [],
            "control_plane": [],
            "files_changed": files,
            "coding_session_id": "",
            "raw_response": "",
        }

        # Decide the phase.
        if cid:
            inferred_phase = args.phase
        else:
            inferred_phase = "committed"
            print(f"  [v{vnum}] no live cluster — downgrading phase to 'committed'")

        slot = variant_slot(
            vnum,
            phase=inferred_phase,
            cluster_name=cname,
            cid=cid,
            commit_sha=sha,
            wt_path=str(wt),
            dst_passed=True,           # we got past DST when these were created
            hypothesis=hyp_dict,
            mutation=mut_dict,
            cloned_workload=[],
            hypothesis_title=hyp_dict["title"],
        )
        variants[str(vnum)] = slot
        print(f"  [v{vnum}] {cname}  sha={sha[:10]}  cluster_status={status or 'unknown'} "
              f"phase={inferred_phase}")

    if not variants:
        print("[recover] no variants matched — nothing to write.")
        return 1

    # variant_count = batch_start of the in-flight batch (smallest variant_num
    # among saved slots). It must NOT be `len(variants)` — that would make
    # is_stage_complete think the budget is already exhausted.
    min_vnum = min(int(k) for k in variants.keys())

    payload = {
        "schema": 1,
        "stage_num": args.stage,
        "lineage": args.lineage,
        "metric": metric,
        "direction": direction,
        "goal_id": goal_id,
        "champion_sha": champion_sha,
        "champion_fitness": None,  # init_stage re-measures live on resume
        "variant_count": min_vnum,
        "variants": variants,
    }
    save_progress(payload)
    print(f"[recover] wrote /tmp/evolution-stage-progress.json with {len(variants)} variant(s).")
    print(f"[recover] resume with: python3 live_loop.py --stages 1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
