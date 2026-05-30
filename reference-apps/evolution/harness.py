"""The breeding harness: orchestrate ONE generation for ONE lineage (ADR-0095).

Per generation, per lineage:

  1. Read the lineage's current genome (from its git worktree) + its FitnessGoal.
  2. Propose a goal-directed, ordered list of candidate mutations (reusing the
     observer's source-grounded research to justify the latency lineage's moves).
  3. Walk the cascade on the best candidate that clears Stage 0:
       Stage 0  cost-model gate   (cheap; reject obviously-wrong moves)
       Stage 1  local bench       (real measured fitness; bench.measure_local)
       Stage 2  verification cull  (real DST; lethal durability moves rejected)
     If a candidate is CULLED at Stage 2, fall back to the next-best SAFE candidate.
  4. On survival: apply the gene to the lineage branch (real git commit) and record
     an ImprovementIssue in Temper so the lineage is visible in the Observe UI.
  5. Record the generation against the FitnessGoal.

The genome read in generation N+1 is the genome committed in generation N —
evolution is cumulative on the branch.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# Local package imports (run from this directory).
sys.path.insert(0, str(Path(__file__).resolve().parent))
# Reuse the observer's source-grounded research (parameterized by goal).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "observer"))

import bench  # Stage 1, pre-built  # noqa: E402
import cost_model  # Stage 0  # noqa: E402
import verify  # Stage 2 cull  # noqa: E402
from genome import Genome, apply_to_source, read_from_source  # noqa: E402
from temper_breeder import BreederClient, TemperDenied, TemperUnavailable  # noqa: E402

# The observer research is optional context for the latency lineage's narrative.
try:
    from observer.observe import LatencyObservation  # noqa: E402
    from observer.research import research_latency  # noqa: E402

    _HAS_OBSERVER = True
except Exception:  # pragma: no cover - observer is best-effort context
    _HAS_OBSERVER = False


# --- mutation proposal (goal-directed) ---------------------------------------


@dataclass
class Candidate:
    """A proposed gene change for a generation, before the cascade runs."""

    gene: str
    new_value: object
    rationale: str


def propose_candidates(current: Genome, metric: str, direction: str) -> list[Candidate]:
    """Ordered candidate mutations for {metric, direction}, best-first.

    Latency lineage (p95_latency / minimize): the FIRST, most-tempting move is the
    fsync shortcut (sync_on_rotation=false) — exactly what a latency-greedy agent
    proposes. It will be culled at Stage 2; the harness then falls back to the SAFE
    moves (lower linger_ms). Throughput lineage (throughput / maximize): raise the
    batching/pipelining genes.
    """
    cands: list[Candidate] = []
    if metric == "p95_latency" and direction == "minimize":
        if current.sync_on_rotation:
            cands.append(
                Candidate(
                    "sync_on_rotation",
                    False,
                    "Skip the ~1.3ms fsync on the durability path to cut p95 — the "
                    "obvious latency win. (Stage 2 will test whether it's safe.)",
                )
            )
        if current.linger_ms > 1:
            cands.append(
                Candidate(
                    "linger_ms",
                    max(1, current.linger_ms - 2),
                    "Lower batcher linger_ms so the batcher flushes sooner, trimming "
                    "steady-state latency. Safe.",
                )
            )
        # Always offer a safe linger nudge toward 1 (or hold) as a last resort.
        if current.linger_ms != 1:
            cands.append(
                Candidate("linger_ms", 1, "Drop linger_ms to the floor (1ms). Safe.")
            )
        # When linger is already at the floor, the safe latency lever is the Raft
        # replication batch cap: a smaller cap trims the commit/ack tail. Safe —
        # it does not touch durability, so Stage 2 passes.
        if current.append_batch_size > 250:
            cands.append(
                Candidate(
                    "append_batch_size",
                    max(250, current.append_batch_size // 2),
                    "Halve APPEND_ENTRIES_BATCH_SIZE_MAX so commits flush in smaller, "
                    "lower-latency replication batches. Safe (durability untouched).",
                )
            )
    elif metric == "throughput" and direction == "maximize":
        cands.append(
            Candidate(
                "linger_ms",
                current.linger_ms + 4,
                "Raise batcher linger_ms so bursts coalesce into larger Raft "
                "proposals — higher batching efficiency, higher throughput.",
            )
        )
        cands.append(
            Candidate(
                "max_inflight",
                current.max_inflight + 5,
                "Raise MAX_INFLIGHT_APPEND_ENTRIES for deeper replication "
                "pipelining — more in-flight AppendEntries per follower.",
            )
        )
        cands.append(
            Candidate(
                "append_batch_size",
                current.append_batch_size + 1000,
                "Raise APPEND_ENTRIES_BATCH_SIZE_MAX so a burst ships in fewer "
                "round-trips.",
            )
        )
    else:
        raise ValueError(f"no proposer for metric={metric!r} direction={direction!r}")
    return cands


def _research_note(repo: Path, metric: str) -> str:
    """Best-effort: reuse the observer's source-grounded research for narrative."""
    if not _HAS_OBSERVER or metric != "p95_latency":
        return ""
    try:
        # A synthetic burst observation so research_latency runs its source probes.
        obs = LatencyObservation(
            p95_baseline_ms=2.0, p95_peak_ms=80.0, p95_mean_ms=20.0,
            avg_baseline_ms=1.0, avg_peak_ms=10.0, max_peak_ms=120.0,
            total_produces=100000, replication_lag_max=0.0, burst_ratio=40.0,
            opportunity_detected=True, window="lineage-gen", captured_at="harness",
        )
        res = research_latency(obs, helix_repo=str(repo))
        if res.confirmed:
            return f"Research (source-grounded): confirmed {res.confirmed.id} — {res.confirmed.title}."
    except Exception:
        pass
    return ""


# --- one generation ----------------------------------------------------------


@dataclass
class GenerationResult:
    """Outcome of one generation for one lineage."""

    lineage: str
    generation: int
    start_genome: Genome
    end_genome: Genome
    chosen_gene: str | None
    fitness_summary: str
    culls: list[dict] = field(default_factory=list)  # [{gene, message}]
    issue_id: str | None = None
    outcome: str = "survived"  # survived | no_candidate | error
    notes: list[str] = field(default_factory=list)


@dataclass
class Lineage:
    """A breeding lineage: a worktree + branch + its FitnessGoal."""

    name: str            # "latency" | "throughput"
    worktree: Path
    branch: str
    goal_id: str
    metric: str
    direction: str
    # The measured fitness value of the lineage's current (parent) genome for
    # its goal metric. Updated each surviving generation so the bench has real
    # selective power: a generation must IMPROVE on the parent (per direction)
    # to survive. None until the first benched generation establishes a baseline.
    parent_fitness: float | None = None


def _fitness_value(m, metric: str) -> float:
    """Extract the goal's scalar from a bench Measurement (lower=better for
    latency, higher=better for throughput — direction handled by the caller)."""
    if metric == "p95_latency":
        return float(m.p95_ms)
    if metric == "throughput":
        return float(m.throughput_rps)
    raise ValueError(f"no fitness extractor for metric {metric!r}")


def _is_improvement(new: float, parent: float | None, direction: str) -> bool:
    """Does `new` improve on `parent` for the goal direction? First measurement
    (parent is None) always counts as a baseline improvement."""
    if parent is None:
        return True
    return new < parent if direction == "minimize" else new > parent


import contextlib  # noqa: E402


@contextlib.contextmanager
def _bench_against(worktree: Path):
    """Redirect bench.py's module-level repo paths at a lineage worktree.

    bench.py (used as-is) hardcodes HELIX_REPO/COMPOSE_DIR to the main repo. To
    rebuild the Helix image from a lineage's MUTATED source, we point those at the
    worktree for the duration of the bench, then restore them.
    """
    old_repo, old_compose = bench.HELIX_REPO, bench.COMPOSE_DIR
    bench.HELIX_REPO = Path(worktree)
    bench.COMPOSE_DIR = Path(worktree) / "docker"
    try:
        yield
    finally:
        bench.HELIX_REPO, bench.COMPOSE_DIR = old_repo, old_compose


def _git(repo: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        timeout=timeout, check=False,
    )


def _commit_genome(lin: Lineage, genome: Genome, gene: str, gen: int) -> str:
    """Commit the applied genome to the lineage branch. Returns the commit sha.

    Only the gene source files are staged — the untracked bench override
    (docker-compose.local.yml) is deliberately left out of the lineage history.
    """
    _git(lin.worktree, "add",
         "helix-server/src/service/batcher.rs",
         "helix-raft/src/lib.rs",
         "helix-wal/src/wal.rs")
    msg = (
        f"evolve({lin.name} gen {gen}): {gene}={getattr(genome, gene)} "
        f"[{lin.metric}/{lin.direction}]\n\ngenome: {genome.summary()}\n\n"
        f"Directed-evolution breeding harness (ADR-0095)."
    )
    _git(lin.worktree, "commit", "-m", msg, "--no-verify")
    sha = _git(lin.worktree, "rev-parse", "HEAD").stdout.strip()
    return sha[:12]


def run_generation(
    lin: Lineage,
    gen: int,
    breeder: BreederClient,
    supervisor: BreederClient,
    num_records: int = 20000,
    rebuild: bool = True,
    do_bench: bool = True,
) -> GenerationResult:
    """Run one generation of the cascade for one lineage."""
    start_genome = read_from_source(lin.worktree)
    result = GenerationResult(
        lineage=lin.name,
        generation=gen,
        start_genome=start_genome,
        end_genome=start_genome,
        chosen_gene=None,
        fitness_summary="(not benched)",
    )
    note = _research_note(lin.worktree, lin.metric)
    if note:
        result.notes.append(note)

    candidates = propose_candidates(start_genome, lin.metric, lin.direction)
    print(f"\n=== [{lin.name}] generation {gen} ===")
    print(f"  start genome: {start_genome.summary()}")
    if note:
        print(f"  {note}")
    print(f"  proposed {len(candidates)} candidate mutation(s), best-first:")
    for c in candidates:
        print(f"    - {c.gene}={c.new_value}: {c.rationale}")

    # Walk candidates best-first; cull lethal ones; keep the first survivor.
    for cand in candidates:
        # --- Stage 0: cost-model gate ---
        verdict = cost_model.score_candidate(
            start_genome, cand.gene, cand.new_value, lin.metric, lin.direction
        )
        print(f"\n  [Stage 0] {cand.gene}={cand.new_value}: score={verdict.score:+.0f} "
              f"-> {'PASS' if verdict.passes() else 'reject'}")
        print(f"            {verdict.rationale}")
        if not verdict.passes():
            continue

        mutated = start_genome.with_gene(cand.gene, cand.new_value)
        changed = apply_to_source(mutated, lin.worktree)
        print(f"  applied {cand.gene}={cand.new_value} to source; files: {changed}")

        # --- Stage 2 FIRST for the cull demo? No: spec order is Stage1 then Stage2.
        #     But to avoid spending a (slow) bench on a move we're about to cull,
        #     run the cheap-ish DST cull before the multi-minute bench when the
        #     candidate carries lethal risk. Safe candidates: bench then verify.
        if verdict.lethal_risk:
            print(f"  [Stage 2 CULL] candidate carries lethal risk; verifying BEFORE bench.")
            vres = verify.verify_durability(lin.worktree)
            print(f"  {vres.summary()}")
            if not vres.test_ran:
                # The verifier itself is broken (test renamed/moved). Do NOT treat
                # as a cull and silently churn candidates — halt loudly so the
                # demo can't ship a false "no survivor" / unverified genome.
                apply_to_source(start_genome, lin.worktree)
                raise RuntimeError(f"Stage-2 verifier misconfigured: {vres.failure_message}")
            if not vres.passed:
                result.culls.append({"gene": cand.gene, "value": cand.new_value,
                                     "message": vres.failure_message})
                # revert the mutation on the worktree before trying the next candidate
                apply_to_source(start_genome, lin.worktree)
                print(f"  reverted {cand.gene}; falling back to next-best SAFE candidate.")
                continue
            # (Would only get here if a "lethal" move unexpectedly passed.)

        # --- Stage 1: local bench (real measured fitness) ---
        # bench.py hardcodes HELIX_REPO/COMPOSE_DIR to the main repo. Point them at
        # THIS lineage's worktree so the image is rebuilt from the MUTATED source.
        measured = None  # the goal's scalar this generation (None if not benched)
        if do_bench:
            print(f"  [Stage 1] rebuilding + benching from worktree (num_records={num_records}) ...")
            with _bench_against(lin.worktree):
                try:
                    m = bench.measure_local(rebuild=rebuild, num_records=num_records)
                    result.fitness_summary = m.summary()
                    measured = _fitness_value(m, lin.metric)
                    print(f"  [Stage 1] {m.summary()}  ({lin.metric}={measured})")
                except Exception as exc:  # bench infra failure: do NOT commit a blind genome
                    result.fitness_summary = f"(bench failed: {exc})"
                    print(f"  [Stage 1] WARNING bench failed: {exc} — skipping candidate (no fitness).")
                    apply_to_source(start_genome, lin.worktree)
                    continue
        else:
            result.fitness_summary = "(bench skipped)"

        # --- Stage 2: verification cull (for safe candidates, after bench) ---
        if not verdict.lethal_risk:
            print(f"  [Stage 2 CULL] verifying durability ...")
            vres = verify.verify_durability(lin.worktree)
            print(f"  {vres.summary()}")
            if not vres.test_ran:
                apply_to_source(start_genome, lin.worktree)
                raise RuntimeError(f"Stage-2 verifier misconfigured: {vres.failure_message}")
            if not vres.passed:
                result.culls.append({"gene": cand.gene, "value": cand.new_value,
                                     "message": vres.failure_message})
                apply_to_source(start_genome, lin.worktree)
                continue

        # --- Stage 1 selection: the measured fitness must IMPROVE on the parent.
        #     This is what gives the bench selective power — a regression is
        #     rejected (not committed), so the lineage only advances on genuine
        #     improvement for its goal. (Skipped only when bench is disabled.)
        if do_bench and measured is not None:
            if not _is_improvement(measured, lin.parent_fitness, lin.direction):
                par = lin.parent_fitness
                print(f"  [Stage 1] REGRESSION: {lin.metric}={measured} did not improve on "
                      f"parent={par} ({lin.direction}); rejecting candidate.")
                result.culls.append({"gene": cand.gene, "value": cand.new_value,
                                     "message": f"fitness regression: {lin.metric} {measured} vs parent {par}"})
                apply_to_source(start_genome, lin.worktree)
                continue

        # --- SURVIVED: commit + record ImprovementIssue ---
        result.chosen_gene = cand.gene
        result.end_genome = mutated
        if do_bench and measured is not None:
            lin.parent_fitness = measured  # the new baseline for the next generation
        sha = _commit_genome(lin, mutated, cand.gene, gen)
        result.notes.append(f"committed {sha} on {lin.branch}")
        print(f"  SURVIVED. committed {sha} on {lin.branch}.")

        result.issue_id = _record_improvement_issue(
            breeder, supervisor, lin, gen, start_genome, mutated, cand, result
        )
        _record_generation(breeder, lin, gen, cand.gene, result.fitness_summary, "survived")
        return result

    # No candidate survived.
    result.outcome = "no_candidate"
    _record_generation(breeder, lin, gen, "(none)", result.fitness_summary, "no_survivor")
    print(f"  no surviving candidate this generation.")
    return result


# --- Temper drive ------------------------------------------------------------


def _record_improvement_issue(
    breeder: BreederClient,
    supervisor: BreederClient,
    lin: Lineage,
    gen: int,
    start: Genome,
    end: Genome,
    cand: Candidate,
    result: GenerationResult,
) -> str | None:
    """Create + drive an ImprovementIssue for the surviving mutation.

    breeder drives Observe -> AssignPlanner(self) -> BeginPlanning -> WritePlan;
    breeder-supervisor (distinct identity) ApprovePlans. Role separation holds.
    """
    issue_id = f"evolve-{lin.name}-g{gen}-{uuid.uuid4().hex[:6]}"
    title = (
        f"[{lin.name} lineage gen {gen}] {cand.gene}="
        f"{getattr(end, cand.gene)} (goal: {lin.metric}/{lin.direction})"
    )
    diff = start.diff(end)
    hypothesis = (
        f"{cand.rationale} Genome change: {diff}. "
        f"Survived Stage-2 durability cull. Fitness: {result.fitness_summary}."
    )
    target_file = {
        "linger_ms": "helix-server/src/service/batcher.rs",
        "max_inflight": "helix-raft/src/lib.rs",
        "append_batch_size": "helix-raft/src/lib.rs",
        "sync_on_rotation": "helix-wal/src/wal.rs",
    }[cand.gene]
    evidence = json.dumps({
        "lineage": lin.name, "generation": gen, "branch": lin.branch,
        "goal": {"metric": lin.metric, "direction": lin.direction},
        "start_genome": start.to_dict(), "end_genome": end.to_dict(),
        "gene": cand.gene, "fitness": result.fitness_summary,
        "culls_this_gen": result.culls,
    }, indent=2)
    plan = (
        f"1. On branch {lin.branch}, set {cand.gene}={getattr(end, cand.gene)} "
        f"in {target_file}.\n"
        f"2. Rebuild + local bench (Stage 1).\n"
        f"3. Run the WAL durability DST (Stage 2 cull).\n"
        f"4. If it survives, commit to the lineage branch."
    )
    acceptance = (
        f"Genome moves {lin.metric} in the {lin.direction} direction without "
        f"tripping the durability DST; the lineage branch advances by one commit."
    )

    if not breeder.has_entity_set("ImprovementIssues"):
        print("  [Temper] ImprovementIssues not deployed; skipping issue record.")
        return None
    try:
        breeder.create("ImprovementIssues", {"id": issue_id})
        breeder.action("ImprovementIssues", issue_id, "Observe",
                       {"title": title, "hypothesis": hypothesis, "target_file": target_file})
        # breeder is the planner (sets planner_id to its OWN instance id).
        breeder.action("ImprovementIssues", issue_id, "AssignPlanner",
                       {"planner_id": "breeder-agent"})
        breeder.action("ImprovementIssues", issue_id, "BeginPlanning", {})
        breeder.action("ImprovementIssues", issue_id, "WritePlan",
                       {"plan": plan, "acceptance_criteria": acceptance})
        # breeder-supervisor (distinct identity) approves the plan — role separation
        # preserved (breeder cannot approve its own plan; forbid wins in Cedar).
        supervisor.action("ImprovementIssues", issue_id, "ApprovePlan", {})
        print(f"  [Temper] ImprovementIssue {issue_id} -> Planned (plan auto-approved "
              f"by verified breeder-supervisor).")
    except TemperDenied as denied:
        print(f"  [Temper] Cedar denied while driving {issue_id}: {denied}")
        result.notes.append(f"issue {issue_id} pending decision {denied.decision_id}")
    except TemperUnavailable as exc:
        print(f"  [Temper] error driving {issue_id}: {exc}")
        return issue_id
    return issue_id


def _record_generation(
    breeder: BreederClient, lin: Lineage, gen: int, gene: str, fitness: str, outcome: str
) -> None:
    """Record the generation against the FitnessGoal entity."""
    try:
        breeder.action("FitnessGoals", lin.goal_id, "RecordGeneration",
                       {"generation": str(gen), "gene": gene,
                        "fitness": fitness, "outcome": outcome})
    except (TemperDenied, TemperUnavailable) as exc:
        print(f"  [Temper] could not record generation on goal {lin.goal_id}: {exc}")
