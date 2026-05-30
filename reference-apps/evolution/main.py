"""Directed-evolution breeding harness CLI (ADR-0095).

Runs N generations across the named lineages, each under its own human-set
FitnessGoal, breeding Helix toward two different selective pressures off a common
ancestor. Two lineages diverge into two distinct, safe genomes; the latency
lineage's fsync shortcut is culled by the real WAL durability DST.

    python3 main.py --lineages latency,throughput --generations 3

Each lineage is a real git worktree + branch (evolve/latency, evolve/throughput),
NEVER main. A surviving mutation is a real commit; each generation records an
ImprovementIssue (visible in the Observe UI) and a generation against the goal.

The FitnessGoals + ImprovementIssues are driven with REAL verified breeder
credentials (register_breeders.py), not header spoofing.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from genome import read_from_source  # noqa: E402
from harness import Lineage, run_generation  # noqa: E402
from temper_breeder import BreederClient, TemperDenied, TemperUnavailable, load_tokens  # noqa: E402

HELIX_REPO = Path("/Users/arun.parthiban/notdd/helix")
WORKTREE_ROOT = Path("/Users/arun.parthiban/notdd/evolution-worktrees")

# Lineage definitions: name -> (branch, metric, direction, target).
LINEAGES = {
    "latency": ("evolve/latency", "p95_latency", "minimize", "50"),
    "throughput": ("evolve/throughput", "throughput", "maximize", "60000"),
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)


def _ensure_bench_override(wt: Path) -> None:
    """The docker-compose.local.yml image-pin override is UNTRACKED on main, so a
    fresh worktree won't have it. bench.py requires it (pins all 3 nodes to the one
    pre-built helix-server:local image, avoiding parallel-build OOM). Copy it in."""
    src = HELIX_REPO / "docker" / "docker-compose.local.yml"
    dst = wt / "docker" / "docker-compose.local.yml"
    if src.exists() and not dst.exists():
        dst.write_text(src.read_text())
        print(f"[setup] copied bench override into {dst}")


def ensure_worktree(name: str, branch: str, base_ref: str) -> Path:
    """Create (or reuse) the lineage's git worktree off the common ancestor."""
    wt = WORKTREE_ROOT / name
    existing = _git(HELIX_REPO, "worktree", "list").stdout
    if str(wt) in existing:
        print(f"[setup] reusing worktree {wt} ({branch})")
        _ensure_bench_override(wt)
        return wt
    WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    # Branch may already exist from a prior run; -B resets it to base_ref if new.
    branch_exists = _git(HELIX_REPO, "rev-parse", "--verify", branch).returncode == 0
    if branch_exists:
        res = _git(HELIX_REPO, "worktree", "add", str(wt), branch)
    else:
        res = _git(HELIX_REPO, "worktree", "add", "-b", branch, str(wt), base_ref)
    if res.returncode != 0:
        raise RuntimeError(f"worktree add failed for {name}: {res.stderr}")
    print(f"[setup] created worktree {wt} on {branch} (base {base_ref[:12]})")
    _ensure_bench_override(wt)
    return wt


def _goal_status(entity: dict | None) -> str | None:
    """Status from a /tdata entity representation (top-level or fields)."""
    if not isinstance(entity, dict):
        return None
    return entity.get("status") or entity.get("Status") or (entity.get("fields") or {}).get("Status")


def ensure_goal(
    supervisor: BreederClient, lineage: str, metric: str, direction: str, target: str
) -> str:
    """Create + Activate the lineage's FitnessGoal (the human's selective pressure).

    Reuses an existing Active goal; if the canonical id is terminal
    (Achieved/Retired), uses a fresh unique id so a new pressure can be set.
    """
    goal_id = f"goal-{lineage}"
    try:
        existing = supervisor.get("FitnessGoals", goal_id)
    except TemperUnavailable:
        existing = None
    status = _goal_status(existing)
    if status == "Active":
        print(f"[goal] reusing Active FitnessGoal {goal_id} ({metric}/{direction})")
        return goal_id
    if status in ("Achieved", "Retired"):
        goal_id = f"goal-{lineage}-{uuid.uuid4().hex[:6]}"  # canonical id is terminal
    # Create fresh.
    supervisor.create("FitnessGoals", {"id": goal_id})
    supervisor.action("FitnessGoals", goal_id, "Define",
                     {"lineage_id": lineage, "metric": metric,
                      "direction": direction, "target": target})
    supervisor.action("FitnessGoals", goal_id, "Activate", {})
    print(f"[goal] FitnessGoal {goal_id} Defined + Activated: {metric}/{direction} "
          f"target={target} (set by verified breeder-supervisor)")
    return goal_id


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Directed-evolution breeding harness (H2)")
    ap.add_argument("--lineages", default="latency,throughput",
                    help="comma-separated lineage names")
    ap.add_argument("--generations", type=int, default=3)
    ap.add_argument("--base-url", default="http://127.0.0.1:3000")
    ap.add_argument("--tenant", default="dark-factory")
    ap.add_argument("--num-records", type=int, default=20000,
                    help="kafka-producer-perf-test records per bench (lower = faster)")
    ap.add_argument("--no-bench", action="store_true",
                    help="skip Stage 1 bench (still does Stage 0 + Stage 2 cull + commits)")
    ap.add_argument("--no-rebuild", action="store_true",
                    help="do not rebuild the Helix image before benching")
    ap.add_argument("--base-ref", default="champion-metrics-pranav-clone",
                    help="common ancestor ref to branch lineages from")
    args = ap.parse_args(argv)

    names = [n.strip() for n in args.lineages.split(",") if n.strip()]
    for n in names:
        if n not in LINEAGES:
            print(f"ERROR: unknown lineage {n!r}; known: {sorted(LINEAGES)}", file=sys.stderr)
            return 2

    tokens = load_tokens()
    breeder = BreederClient(token=tokens["breeder"]["token"],
                            base_url=args.base_url, tenant=args.tenant)
    supervisor = BreederClient(token=tokens["breeder-supervisor"]["token"],
                               base_url=args.base_url, tenant=args.tenant)

    # Resolve the common ancestor sha once so both lineages share it.
    base_sha = _git(HELIX_REPO, "rev-parse", args.base_ref).stdout.strip()
    print("=" * 78)
    print("DIRECTED-EVOLUTION BREEDING HARNESS (H2)")
    print(f"  tenant={args.tenant}  common ancestor={base_sha[:12]}  "
          f"generations={args.generations}  num_records={args.num_records}")
    print("=" * 78)

    lineages: list[Lineage] = []
    for name in names:
        branch, metric, direction, target = LINEAGES[name]
        wt = ensure_worktree(name, branch, base_sha)
        goal_id = ensure_goal(supervisor, name, metric, direction, target)
        lineages.append(Lineage(name=name, worktree=wt, branch=branch,
                                goal_id=goal_id, metric=metric, direction=direction))

    all_results = []
    for gen in range(1, args.generations + 1):
        for lin in lineages:
            res = run_generation(
                lin, gen, breeder, supervisor,
                num_records=args.num_records,
                rebuild=not args.no_rebuild,
                do_bench=not args.no_bench,
            )
            all_results.append(res)

    # --- Final report: show the two genomes diverged ---
    print("\n" + "=" * 78)
    print("FINAL GENOMES (side by side)")
    print("=" * 78)
    finals = {}
    for lin in lineages:
        g = read_from_source(lin.worktree)
        finals[lin.name] = g
        print(f"  [{lin.name:11s}] {g.summary()}   branch={lin.branch}")
    if len(finals) == 2:
        a, b = list(finals.values())
        d = a.diff(b)
        print(f"\n  DIVERGENCE: {len(d)} gene(s) differ between lineages: {d}")

    print("\nCULLS (real Stage-2 DST rejections):")
    any_cull = False
    for r in all_results:
        for c in r.culls:
            any_cull = True
            print(f"  [{r.lineage} gen {r.generation}] {c['gene']}={c['value']} CULLED: "
                  f"{c['message'][:200]}")
    if not any_cull:
        print("  (none)")

    print("\nIMPROVEMENT ISSUES created:")
    for r in all_results:
        if r.issue_id:
            print(f"  [{r.lineage} gen {r.generation}] {r.issue_id} "
                  f"(chosen gene: {r.chosen_gene})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
