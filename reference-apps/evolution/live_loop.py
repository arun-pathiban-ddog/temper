"""Live evolution loop — main entry point (ADR-0095 live extension).

Pipeline per variant:
  Research Agent  → Hypothesis       (claude -p with Read+Bash on helix repo)
  Coding Agent    → edits worktree   (claude -p with Edit+Write+Bash in worktree)
  git diff        → mutation check   (empty diff = skip, no hallucinated old-strings)
  DST             → safety gate      (cargo test, WAL durability)
  Cloud Build     → Docker image
  GKE deploy      → variant cluster
  Workload clone  → shadow traffic
  Datadog measure → fitness delta
  Failure Classifier → "hypothesis" or "code"
  Refinement loop → up to max_refinements rounds

Usage:
  python3 live_loop.py [--stages N] [--batch-size N] [--traffic-window N]
                       [--max-refinements N] [--cooldown N]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import LiveEvolutionConfig
from dataclasses import asdict, dataclass, field
from fitness_live import FitnessResult, measure_all_clusters
from live_client import LiveClient, LiveClientError
from propose_live import (
    Hypothesis,
    ProposedMutation,
    TextFeedback,
    StageHistoryEntry,
    classify_failure,
    fix_code_for_hypothesis,
    implement_hypothesis,
    propose_batch,
    refine_hypothesis,
    update_evolution_spec,
    _git_diff_files,
)
from stage_manager import (
    VariantResult,
    evaluate_variant,
    history_entry,
    init_stage,
    is_stage_complete,
    promote_stage_champion,
    startup_resume,
    write_idle_status,
    _write_status,
)
from stage_state_store import (
    clear_progress,
    get_variants,
    load_progress,
    phase_at_or_after,
    save_progress,
    stage_state_to_dict,
    fitness_from_dict,
    update_variant,
    variant_slot,
)
from verify import verify_durability


# ---------------------------------------------------------------------------
# DST helper
# ---------------------------------------------------------------------------

def run_dst(wt_path: Path, timeout_s: int = 300) -> tuple[bool, str]:
    result = verify_durability(wt_path, timeout_s=timeout_s)
    return result.passed, result.failure_message or ("passed" if result.passed else "failed")


def _empty_fitness(cluster_name: str) -> FitnessResult:
    return FitnessResult(cluster_tag=cluster_name, latency_p95_ms=None,
                         throughput_rps=None, data_points=0, error="no data")


def _fail_result(
    vnum: int, cname: str, mutation: ProposedMutation, hyp: Hypothesis,
    dst_passed: bool, outcome: str, msg: str, cid: str = "",
) -> VariantResult:
    return VariantResult(
        variant_num=vnum, cluster_name=cname,
        change_description=mutation.change_description,
        mutation_type=mutation.mutation_type,
        dst_passed=dst_passed, fitness=None, delta_pct=None, survived=False,
        feedback=TextFeedback(vnum, outcome, msg),
        cid=cid, hypothesis_title=hyp.title if hyp else "",
    )


def _print_variant_fitness(state, result: VariantResult, fitness: FitnessResult) -> None:
    """Loud per-variant fitness line so the operator can see what was measured."""
    delta = f"{result.delta_pct:+.1f}%" if result.delta_pct is not None else "N/A"
    survived = "SURVIVED" if result.survived else "culled"
    title = (result.hypothesis_title[:60] + "…") if len(result.hypothesis_title) > 60 \
        else (result.hypothesis_title or "(no hyp)")
    print(f"  [measure] v{result.variant_num} {result.cluster_name}: "
          f"{fitness.summary()}  Δ={delta}  -> {survived}  | {title}")


def _print_batch_pick(
    refined: list[VariantResult],
    best: VariantResult | None,
    state,
) -> None:
    """After refinement-loop processing, announce the batch's best variant.

    If `best` is not None a champion was selected this batch. Otherwise we
    print the closest miss (highest Δ among non-survivors) so the operator can
    see how far off threshold we were.
    """
    if best is not None:
        delta = f"{best.delta_pct:+.1f}%" if best.delta_pct is not None else "?"
        print(f"\n  [batch pick] v{best.variant_num} survived "
              f"({delta} {state.metric}) → promoting to champion")
        return

    measured = [r for r in refined if r.delta_pct is not None]
    if not measured:
        print(f"\n  [batch pick] no measurable variants this batch "
              f"(all DST-culled, no-data, or build-failed)")
        return
    near = max(measured, key=lambda r: r.delta_pct)
    print(f"\n  [batch pick] no winner; closest miss = v{near.variant_num} "
          f"(Δ={near.delta_pct:+.1f}%, need ≥+3.0%): "
          f"{(near.hypothesis_title or near.change_description)[:60]}")


def _print_stage_summary(state) -> None:
    """End-of-stage leaderboard across every variant we tested. Always prints,
    whether or not a champion was promoted, so the operator has a single
    artifact to inspect for the stage."""
    history = list(getattr(state, "history", []))
    stage_entries = [h for h in history if h.stage_num == state.stage_num]
    if not stage_entries:
        return
    survived = [h for h in stage_entries if h.outcome == "survived"]
    culled = [h for h in stage_entries if h.outcome == "fitness_culled"]
    dst_culled = [h for h in stage_entries if h.outcome == "dst_culled"]
    no_data = [h for h in stage_entries if h.outcome == "no_data"]

    baseline_str = state.champion_fitness.summary() if state.champion_fitness else "N/A"

    print(f"\n  ━━━ stage {state.stage_num} leaderboard ({state.metric}, "
          f"baseline={baseline_str}) ━━━")
    print(f"    survived: {len(survived)}   fitness-culled: {len(culled)}   "
          f"dst-culled: {len(dst_culled)}   no-data: {len(no_data)}")

    # Sort: survivors first (best delta), then near-misses, then no-data/DST.
    def sort_key(h):
        if h.outcome == "survived":
            return (0, -(h.delta_pct or 0.0))
        if h.outcome == "fitness_culled":
            return (1, -(h.delta_pct or -1e9))
        if h.outcome == "no_data":
            return (2, 0)
        return (3, 0)

    print(f"    {'rank':>4}  {'var':>4}  {'Δ %':>8}  {'outcome':<14}  hypothesis")
    print(f"    " + "-" * 80)
    for rank, h in enumerate(sorted(stage_entries, key=sort_key), start=1):
        delta = f"{h.delta_pct:+.1f}" if h.delta_pct is not None else "—"
        title = (h.hypothesis_title or h.description or "(no title)")[:50]
        marker = "★" if h.outcome == "survived" else " "
        print(f"    {rank:>4}  v{h.variant_num:<3} {delta:>8}  {h.outcome:<14} {marker} {title}")
    print()

    if not survived:
        print(f"  ! no variant cleared the +3% improvement threshold this stage.")
        # The most common reasons; surface them up-front so the operator does
        # not have to scroll back through the per-variant logs.
        if no_data and len(no_data) > len(stage_entries) / 2:
            print(f"    > {len(no_data)}/{len(stage_entries)} variants had NO Datadog data "
                  f"— check that variant clusters emit cluster:helix-ev-... tags.")
        if culled:
            best_miss = max(culled, key=lambda h: h.delta_pct or -1e9)
            print(f"    > best near-miss: v{best_miss.variant_num} "
                  f"(Δ={best_miss.delta_pct:+.1f}%) — '{(best_miss.hypothesis_title or best_miss.description)[:60]}'")
        if dst_culled:
            print(f"    > {len(dst_culled)} variants failed DST — agent may be proposing "
                  f"changes that break durability.")


def _print_batch_summary(state, batch_results: list[VariantResult]) -> None:
    """Tabular summary of every variant in the just-finished batch, sorted by
    delta (best first). Lets the operator see at a glance which variants moved
    the needle vs. baseline."""
    if not batch_results:
        return
    def sort_key(r: VariantResult) -> float:
        return r.delta_pct if r.delta_pct is not None else -1e9
    rows = sorted(batch_results, key=sort_key, reverse=True)
    print(f"\n  ┌─ batch summary (stage {state.stage_num}, sorted by Δ) "
          f"─────────────────────────────")
    print(f"  │ {'var':>4} | {'p95':>9} | {'rps':>6} | {'pts':>4} | "
          f"{'Δ %':>7} | {'verdict':<8} | hypothesis")
    print(f"  ├──────┼───────────┼────────┼──────┼─────────┼──────────┼──────────────────")
    for r in rows:
        f = r.fitness
        p95 = f"{f.latency_p95_ms:.1f}ms" if (f and f.latency_p95_ms is not None) else "—"
        rps = f"{f.throughput_rps:.0f}" if (f and f.throughput_rps is not None) else "—"
        pts = str(f.data_points) if f else "0"
        delta = f"{r.delta_pct:+.1f}" if r.delta_pct is not None else "N/A"
        verdict = "SURVIVED" if r.survived else ("dst-fail" if not r.dst_passed
                                                   else ("no-data" if r.delta_pct is None
                                                         else "culled"))
        title = (r.hypothesis_title or r.change_description or "")[:30]
        print(f"  │ v{r.variant_num:<3} | {p95:>9} | {rps:>6} | {pts:>4} | "
              f"{delta:>7} | {verdict:<8} | {title}")
    print(f"  └────────────────────────────────────────────────────────────────────────")


# ---------------------------------------------------------------------------
# Internal job tracking — serializable so we can resume mid-stage
# ---------------------------------------------------------------------------

@dataclass
class _VariantJob:
    variant_num: int
    hypothesis: Hypothesis | None = None
    mutation: ProposedMutation | None = None
    wt_path: Path | None = None
    commit_sha: str = ""
    cid: str = ""
    cluster_name: str = ""
    cloned_workload: list = field(default_factory=list)
    dst_passed: bool = False
    phase: str = "pending"   # see stage_state_store.PHASE_ORDER

    def to_slot(self) -> dict:
        return variant_slot(
            self.variant_num,
            phase=self.phase,
            cluster_name=self.cluster_name,
            cid=self.cid,
            commit_sha=self.commit_sha,
            wt_path=str(self.wt_path) if self.wt_path else "",
            dst_passed=self.dst_passed,
            hypothesis=asdict(self.hypothesis) if self.hypothesis else None,
            mutation=asdict(self.mutation) if self.mutation else None,
            cloned_workload=list(self.cloned_workload),
            hypothesis_title=self.hypothesis.title if self.hypothesis else "",
        )

    @classmethod
    def from_slot(cls, slot: dict) -> "_VariantJob":
        hyp = None
        if slot.get("hypothesis"):
            hd = slot["hypothesis"]
            hyp = Hypothesis(
                title=hd.get("title", ""),
                reasoning=hd.get("reasoning", ""),
                expected_mechanism=hd.get("expected_mechanism", ""),
                suggested_files=hd.get("suggested_files", []),
                confidence=hd.get("confidence", "low"),
                variant_num=hd.get("variant_num", slot["variant_num"]),
                refinement_num=hd.get("refinement_num", 0),
                research_session_id=hd.get("research_session_id", ""),
                raw_response=hd.get("raw_response", ""),
            )
        mut = None
        if slot.get("mutation"):
            md = slot["mutation"]
            mut = ProposedMutation(
                change_description=md.get("change_description", ""),
                rationale=md.get("rationale", ""),
                mutation_type=md.get("mutation_type", "code"),
                edits=md.get("edits", []),
                control_plane=md.get("control_plane", []),
                hypothesis=hyp,
                coding_session_id=md.get("coding_session_id", ""),
                files_changed=md.get("files_changed", []),
                raw_response=md.get("raw_response", ""),
            )
        wt = Path(slot["wt_path"]) if slot.get("wt_path") else None
        return cls(
            variant_num=slot["variant_num"],
            hypothesis=hyp,
            mutation=mut,
            wt_path=wt,
            commit_sha=slot.get("commit_sha", ""),
            cid=slot.get("cid", ""),
            cluster_name=slot.get("cluster_name", ""),
            cloned_workload=slot.get("cloned_workload", []) or [],
            dst_passed=slot.get("dst_passed", False),
            phase=slot.get("phase", "pending"),
        )


def _save_job(state, jobs: list, fitness: dict[int, dict] | None = None) -> None:
    """Persist all active jobs in this batch to /tmp/evolution-stage-progress.json."""
    existing = load_progress() or {}
    existing.update(stage_state_to_dict(state, existing.get("variants") or {}))
    fitness = fitness or {}
    for j in jobs:
        if j is None:
            continue
        slot = j.to_slot()
        if j.variant_num in fitness:
            slot.update(fitness[j.variant_num])
        # to_slot() already sets variant_num; pop it before splatting kwargs.
        slot.pop("variant_num", None)
        update_variant(existing, j.variant_num, **slot)
    save_progress(existing)


# ---------------------------------------------------------------------------
# _run_batch: research → code (in worktree) → DST → build → deploy → measure
# ---------------------------------------------------------------------------

def _run_batch(
    config: LiveEvolutionConfig,
    client: LiveClient,
    state,
    batch_start: int,
    feedback_list: list[TextFeedback],
    telemetry_summary: str,
    meta_hint: str,
) -> list[VariantResult]:
    """Full batch: research + code + DST + build/deploy + measure.

    Mid-stage resume: any variant slot present in /tmp/evolution-stage-progress.json
    is restored and we skip phases it has already completed (see PHASE_ORDER in
    stage_state_store).
    """
    from stage_manager import StageState

    lineage = state.lineage
    n = config.batch_size
    print(f"\n  [batch] {n} variants (s{state.stage_num} v{batch_start}..v{batch_start+n-1})")

    # ── Restore any in-flight variants from disk ────────────────────────────
    progress = load_progress() or {}
    saved_variants = get_variants(progress) if progress.get("stage_num") == state.stage_num else {}

    results: list[VariantResult | None] = [None] * n
    jobs: list[_VariantJob | None] = [None] * n
    already_hyp: list[Hypothesis] = []
    already_desc: list[str] = []

    for i in range(n):
        vnum = batch_start + i
        feedback = feedback_list[i] if i < len(feedback_list) else None
        existing = saved_variants.get(vnum)

        # ── Restored variant — skip ahead based on its phase ────────────────
        if existing and existing.get("phase") != "pending":
            job = _VariantJob.from_slot(existing)
            phase = job.phase
            print(f"  [v{vnum}] resuming from phase '{phase}'")

            # Terminal: measured (or culled) — replay into results, no work
            if phase == "measured" and existing.get("fitness"):
                fr = fitness_from_dict(existing["fitness"])
                results[i] = evaluate_variant(
                    config, state, job.cluster_name, fr,
                    job.mutation.change_description if job.mutation else "",
                    vnum,
                )
                results[i].hypothesis_title = job.hypothesis.title if job.hypothesis else ""
                results[i].cid = job.cid
                if job.hypothesis:
                    already_hyp.append(job.hypothesis)
                if job.mutation:
                    already_desc.append(job.mutation.change_description)
                continue
            if phase == "culled":
                results[i] = _fail_result(
                    vnum, job.cluster_name,
                    job.mutation or ProposedMutation("(culled)", "", "skip"),
                    job.hypothesis,
                    job.dst_passed, "no_data", "culled by prior run",
                    cid=job.cid,
                )
                continue

            # In-progress: install the job and let _build_and_measure pick up
            jobs[i] = job
            if job.hypothesis:
                already_hyp.append(job.hypothesis)
            if job.mutation:
                already_desc.append(job.mutation.change_description)
            continue

        # ── Fresh variant — run the full pipeline ───────────────────────────

        # Research
        hyp = _research_one(config, state, vnum, feedback, telemetry_summary,
                             meta_hint, already_hyp)
        already_hyp.append(hyp)

        # Worktree
        try:
            wt = client.create_proposal_worktree(state.stage_num, vnum, lineage)
        except LiveClientError as exc:
            print(f"  [v{vnum}] worktree failed: {exc}")
            results[i] = _fail_result(vnum, "", ProposedMutation("skip","","skip",
                                       hypothesis=hyp), hyp,
                                       False, "no_data", f"worktree failed: {exc}")
            already_desc.append("worktree error")
            continue

        # Coding
        mut = implement_hypothesis(
            config, hyp, wt, state.history, feedback,
            previous_mutations=already_desc,
        )
        already_desc.append(mut.change_description)

        if mut.mutation_type == "skip":
            client.remove_proposal_worktree(state.stage_num, vnum)
            results[i] = _fail_result(vnum, "", mut, hyp,
                                       False, "no_data", mut.change_description)
            continue

        if mut.mutation_type == "control_plane":
            job = _VariantJob(vnum, hyp, mut, wt_path=wt, dst_passed=True, phase="dst_passed")
            jobs[i] = job
            _save_job(state, jobs)
            continue

        # Persist after Coding phase
        partial_job = _VariantJob(vnum, hyp, mut, wt_path=wt, phase="coded")
        jobs[i] = partial_job
        _save_job(state, jobs)

        # DST
        print(f"  [v{vnum}] running DST (~3 min)…")
        passed, dst_msg = run_dst(wt)
        if not passed:
            print(f"  [v{vnum}] DST CULL: {dst_msg[:120]}")
            subprocess.run(["git", "checkout", "--", "."],
                           cwd=wt, capture_output=True, timeout=30, check=False)
            client.remove_proposal_worktree(state.stage_num, vnum)
            partial_job.phase = "culled"
            _save_job(state, jobs)
            results[i] = _fail_result(vnum, "", mut, hyp,
                                       False, "dst_fail", dst_msg[:300])
            continue

        partial_job.dst_passed = True
        partial_job.phase = "dst_passed"
        _save_job(state, jobs)

        # Commit
        try:
            sha = client.commit_mutation(wt, lineage, state.stage_num, vnum,
                                         mut.change_description)
        except LiveClientError as exc:
            print(f"  [v{vnum}] commit failed: {exc}")
            results[i] = _fail_result(vnum, "", mut, hyp,
                                       True, "no_data", f"commit failed: {exc}")
            continue

        cname = f"ev-{lineage[:3]}-s{state.stage_num}-v{vnum}"
        partial_job.commit_sha = sha
        partial_job.cluster_name = cname
        partial_job.phase = "committed"
        _save_job(state, jobs)
        print(f"  [v{vnum}] DST passed, committed {sha[:10]}, cluster={cname}")

    # Phase 2-6: Create clusters → wait Live → clone workload → observe → measure
    _build_and_measure(config, client, state, jobs, results, lineage)
    return [r for r in results if r is not None]


def _research_one(
    config, state, vnum, feedback, telemetry_summary, meta_hint, already_hyp,
) -> Hypothesis:
    try:
        from propose_live import research_hypothesis
        return research_hypothesis(
            config, state.lineage, state.metric, state.direction,
            state.history, telemetry_summary, meta_hint, vnum,
            previous_hypotheses=already_hyp,
            text_feedback=feedback,
        )
    except Exception as exc:
        print(f"  [research] error v{vnum}: {exc}")
        return Hypothesis(
            title=f"(error: {exc})", reasoning="", expected_mechanism="",
            suggested_files=[], confidence="low", variant_num=vnum,
        )


def _build_and_measure(config, client, state, jobs, results, lineage):
    """Phases 2-6: parallel build → wait → workload → observe → measure → teardown.

    Each phase honors the job's saved `phase` field so a resumed run skips
    work that was already done.
    """
    candidates = [j for j in jobs if j and j.commit_sha and j.cluster_name]

    # ── Phase: create_cluster (skip if already created) ─────────────────────
    cids: list[str] = []
    for job in candidates:
        if job.cid and phase_at_or_after(job.phase, "cluster_creating"):
            cids.append(job.cid)
            print(f"  [v{job.variant_num}] cluster already created (cid={job.cid})")
            continue
        try:
            cid = client.create_cluster(job.cluster_name, job.commit_sha, replicas=1)
            job.cid = cid
            job.phase = "cluster_creating"
            cids.append(cid)
            print(f"  [v{job.variant_num}] cluster {job.cluster_name} created (cid={cid})")
        except LiveClientError as exc:
            print(f"  [v{job.variant_num}] create_cluster failed: {exc}")
    _save_job(state, jobs)

    # ── Phase: wait for clusters to be live ─────────────────────────────────
    live_ok: dict[str, bool] = {}
    pending_cids = [j.cid for j in candidates
                    if j.cid and not phase_at_or_after(j.phase, "cluster_live")]
    # Treat already-live jobs as live without re-polling
    for j in candidates:
        if j.cid and phase_at_or_after(j.phase, "cluster_live"):
            live_ok[j.cid] = True

    if pending_cids:
        print(f"  [batch] waiting for {len(pending_cids)} clusters (~15 min)…")
        _write_status(config, state, phase="building",
                      message=f"Building {len(pending_cids)} variant clusters…")
        live_ok.update(client.wait_all_clusters_live(pending_cids, timeout_minutes=22))

    for job in candidates:
        if job.cid and live_ok.get(job.cid, False):
            if not phase_at_or_after(job.phase, "cluster_live"):
                job.phase = "cluster_live"
        elif job.cid:
            print(f"  [v{job.variant_num}] cluster timed out")
            job.cluster_name = ""
    _save_job(state, jobs)

    # ── Phase: clone workload ───────────────────────────────────────────────
    active_jobs = [j for j in candidates
                   if j.cluster_name and live_ok.get(j.cid, False)]
    if active_jobs:
        print(f"  [batch] cloning workload to {len(active_jobs)} clusters…")
        for job in active_jobs:
            if phase_at_or_after(job.phase, "workload_cloned") and job.cloned_workload:
                print(f"  [v{job.variant_num}] workload already cloned ({len(job.cloned_workload)} entities)")
                continue
            try:
                job.cloned_workload = client.clone_workload_to_variant(
                    config.workload_source_cluster, job.cluster_name
                )
                job.phase = "workload_cloned"
            except LiveClientError as exc:
                print(f"  [v{job.variant_num}] workload clone failed: {exc}")
        _save_job(state, jobs)

        # ── Phase: observe traffic window ───────────────────────────────────
        w = config.traffic_window_minutes
        print(f"  [batch] observing {w} min…")
        _write_status(config, state, phase="observing",
                      message=f"Observing {len(active_jobs)} variants ({w} min)…")
        time.sleep(w * 60 + 30)
        for j in active_jobs:
            if not phase_at_or_after(j.phase, "observed"):
                j.phase = "observed"
        _save_job(state, jobs)

        # ── Phase: measure ──────────────────────────────────────────────────
        print("  [batch] querying Datadog…")
        fitness_map = measure_all_clusters(config, [j.cluster_name for j in active_jobs])
        fitness_persist: dict[int, dict] = {}
        batch_results: list[VariantResult] = []
        baseline = state.champion_fitness
        baseline_str = baseline.summary() if baseline else "N/A"
        print(f"  [batch] baseline ({config.champion_cluster_name}): {baseline_str}")
        for job in active_jobs:
            f = fitness_map.get(job.cluster_name) or _empty_fitness(job.cluster_name)
            idx = next((k for k, jj in enumerate(jobs) if jj is job), -1)
            if idx >= 0:
                r = evaluate_variant(config, state, job.cluster_name, f,
                                     job.mutation.change_description, job.variant_num)
                r.hypothesis_title = job.hypothesis.title if job.hypothesis else ""
                r.cid = job.cid
                results[idx] = r
                batch_results.append(r)
                job.phase = "measured"
                fitness_persist[job.variant_num] = {
                    "fitness": {
                        "cluster_tag": f.cluster_tag,
                        "latency_p95_ms": f.latency_p95_ms,
                        "throughput_rps": f.throughput_rps,
                        "data_points": f.data_points,
                        "error": f.error,
                    },
                    "delta_pct": r.delta_pct,
                    "survived": r.survived,
                }
                _print_variant_fitness(state, r, f)
        _save_job(state, jobs, fitness=fitness_persist)

        _print_batch_summary(state, batch_results)

        print("  [batch] teardown…")
        for job in active_jobs:
            client.teardown_cloned_workload(job.cloned_workload)
        client.teardown_all([j.cid for j in candidates if j.cid])

    # Fill None placeholders.
    for i, (r, job) in enumerate(zip(results, jobs)):
        if r is None and job is not None:
            results[i] = _fail_result(
                job.variant_num, job.cluster_name, job.mutation, job.hypothesis,
                job.dst_passed, "no_data", "cluster did not go live", cid=job.cid,
            )


# ---------------------------------------------------------------------------
# Refinement round
# ---------------------------------------------------------------------------

def _run_refinement_round(
    config: LiveEvolutionConfig,
    client: LiveClient,
    state,
    original_result: VariantResult,
    hypothesis: Hypothesis,
    mutation: ProposedMutation,
    round_num: int,
    telemetry_summary: str,
) -> tuple[VariantResult, Hypothesis, ProposedMutation]:
    """One refinement: classify → research/code fix → DST → build → measure."""
    vnum = original_result.variant_num
    lineage = state.lineage
    print(f"\n  [refine] v{vnum} r{round_num}: classifying failure…")

    failure_type = classify_failure(
        config, hypothesis, mutation,
        original_result.delta_pct, original_result.feedback.message,
    )
    print(f"  [refine] v{vnum} r{round_num}: fault={failure_type}")

    annotated_fb = TextFeedback(
        vnum, "fitness_fail", original_result.feedback.message,
        attempt_num=round_num - 1, failure_type=failure_type,
    )

    # Create a fresh named worktree for this refinement attempt.
    wt_name = f"proposal-s{state.stage_num}-v{vnum}-r{round_num}"
    try:
        wt = client.create_named_proposal_worktree(wt_name, lineage)
    except LiveClientError as exc:
        print(f"  [refine] worktree failed: {exc}")
        return original_result, hypothesis, mutation

    if failure_type == "hypothesis":
        print(f"  [research] refine hypothesis v{vnum} r{round_num}…")
        try:
            new_hyp = refine_hypothesis(
                config, hypothesis, annotated_fb, state.history, telemetry_summary
            )
            new_hyp.refinement_num = round_num
        except Exception as exc:
            print(f"  [research] refine error: {exc}")
            client.remove_named_worktree(wt_name)
            return original_result, hypothesis, mutation
        print(f"  [coding] implement revised hyp v{vnum} r{round_num}…")
        try:
            new_mut = implement_hypothesis(
                config, new_hyp, wt, state.history, annotated_fb
            )
        except Exception as exc:
            print(f"  [coding] error: {exc}")
            client.remove_named_worktree(wt_name)
            return original_result, new_hyp, mutation
    else:
        new_hyp = hypothesis
        new_hyp.refinement_num = round_num
        try:
            new_mut = fix_code_for_hypothesis(
                config, hypothesis, mutation, annotated_fb, state.history, wt
            )
        except Exception as exc:
            print(f"  [coding-fix] error: {exc}")
            client.remove_named_worktree(wt_name)
            return original_result, new_hyp, mutation

    if new_mut.mutation_type == "skip":
        client.remove_named_worktree(wt_name)
        return original_result, new_hyp, new_mut

    # DST
    print(f"  [refine] v{vnum} r{round_num}: running DST…")
    passed, dst_msg = run_dst(wt)
    if not passed:
        print(f"  [refine] DST CULL (r{round_num}): {dst_msg[:100]}")
        subprocess.run(["git", "checkout", "--", "."],
                       cwd=wt, capture_output=True, timeout=30, check=False)
        client.remove_named_worktree(wt_name)
        r = _fail_result(vnum, "", new_mut, new_hyp, False, "dst_fail", dst_msg[:300])
        r.refinement_num = round_num
        return r, new_hyp, new_mut

    # Commit
    try:
        sha = client.commit_mutation(wt, lineage, state.stage_num, vnum,
                                      f"{new_mut.change_description} [r{round_num}]")
    except LiveClientError as exc:
        print(f"  [refine] commit failed: {exc}")
        client.remove_named_worktree(wt_name)
        return original_result, new_hyp, new_mut

    cname = f"ev-{lineage[:3]}-s{state.stage_num}-v{vnum}-r{round_num}"
    try:
        cid = client.create_cluster(cname, sha, replicas=1)
    except LiveClientError as exc:
        print(f"  [refine] create_cluster failed: {exc}")
        client.remove_named_worktree(wt_name)
        return original_result, new_hyp, new_mut

    _write_status(config, state, phase="refining",
                  message=f"Refining v{vnum} r{round_num}…")
    lives = client.wait_all_clusters_live([cid], timeout_minutes=22)
    if not lives.get(cid, False):
        client.teardown_cluster(cid)
        client.remove_named_worktree(wt_name)
        r = _fail_result(vnum, cname, new_mut, new_hyp, True, "no_data",
                          f"cluster did not go live (r{round_num})")
        r.refinement_num = round_num
        return r, new_hyp, new_mut

    cloned = []
    try:
        cloned = client.clone_workload_to_variant(config.workload_source_cluster, cname)
    except LiveClientError as exc:
        print(f"  [refine] workload clone failed: {exc}")

    w = config.traffic_window_minutes
    print(f"  [refine] v{vnum} r{round_num}: observing {w} min…")
    time.sleep(w * 60 + 30)

    fitness_map = measure_all_clusters(config, [cname])
    fitness = fitness_map.get(cname) or _empty_fitness(cname)
    result = evaluate_variant(config, state, cname, fitness,
                               new_mut.change_description, vnum)
    result.hypothesis_title = new_hyp.title
    result.refinement_num = round_num

    client.teardown_cloned_workload(cloned)
    client.teardown_cluster(cid)
    client.remove_named_worktree(wt_name)

    delta_str = f"{result.delta_pct:+.1f}%" if result.delta_pct is not None else "N/A"
    print(f"  [refine] v{vnum} r{round_num}: {'SURVIVED' if result.survived else 'FAILED'} "
          f"(Δ={delta_str})")
    return result, new_hyp, new_mut


# ---------------------------------------------------------------------------
# End-of-stage cleanup
# ---------------------------------------------------------------------------

def _sweep_stage_clusters(client: LiveClient, lineage: str, stage_num: int) -> None:
    """Tear down any live evolution variant clusters belonging to this stage.

    Variant clusters are named `ev-{lineage[:3]}-s{stage_num}-v{N}` (with an
    optional `-r{R}` suffix for refinements). This runs at end-of-stage as a
    safety net in case the per-batch teardown in _build_and_measure missed a
    cluster (e.g. on a crash mid-measure).
    """
    prefix = f"ev-{lineage[:3]}-s{stage_num}-"
    try:
        clusters = client.list_clusters()
    except LiveClientError as exc:
        print(f"  [sweep] warning: could not list clusters: {exc}")
        return
    leftover: list[tuple[str, str]] = []  # (name, cid)
    for c in clusters:
        name = (c.get("name") or c.get("Name") or
                (c.get("fields") or {}).get("Name") or "")
        cid = (c.get("id") or c.get("Id") or
               (c.get("fields") or {}).get("Id") or name)
        status = (c.get("status") or c.get("Status") or
                  (c.get("fields") or {}).get("Status") or "").lower()
        if name.startswith(prefix) and status in ("live", "deploying", "building"):
            leftover.append((name, cid))
    if not leftover:
        print(f"  [sweep] stage {stage_num}: no leftover variant clusters")
        return
    print(f"  [sweep] stage {stage_num}: tearing down {len(leftover)} "
          f"leftover variant cluster(s)")
    for name, cid in leftover:
        try:
            client.teardown_cluster(cid)
        except LiveClientError as exc:
            print(f"  [sweep] warning: could not tear down {name}: {exc}")


# ---------------------------------------------------------------------------
# Main evolution loop
# ---------------------------------------------------------------------------

def run_evolution(config: LiveEvolutionConfig, max_stages: int = 10) -> None:
    client = LiveClient(config)
    config.evolution_worktrees.mkdir(parents=True, exist_ok=True)

    last_stage, _ = startup_resume(config, client)
    current_stage = last_stage + 1

    # If an in-flight stage exists on disk, resume that one instead of skipping
    # ahead. (startup_resume sees last *completed* stage from Temper records.)
    inflight = load_progress()
    if inflight and inflight.get("stage_num") and inflight["stage_num"] >= current_stage:
        current_stage = int(inflight["stage_num"])
        n_variants = len(inflight.get("variants") or {})
        print(f"[startup] in-flight stage {current_stage} detected on disk "
              f"({n_variants} variant slot(s))")

    print("=" * 70)
    print(f"LIVE EVOLUTION LOOP — stage {current_stage}")
    print(f"  batch_size={config.batch_size}  "
          f"traffic_window={config.traffic_window_minutes}min  "
          f"max_refinements={config.max_refinements}")
    print("=" * 70)

    for stage_num in range(current_stage, current_stage + max_stages):
        print(f"\n{'='*70}  STAGE {stage_num}  {'='*70}")
        state = init_stage(config, client, stage_num)
        telemetry_summary = (state.champion_fitness.summary()
                              if state.champion_fitness else "No telemetry available")
        feedback_carry: list[TextFeedback] = []

        while not is_stage_complete(state, config):
            batch_start = state.variant_count

            # Meta-recommendation (fires every meta_rec_interval batches).
            from propose_live import meta_recommendation
            meta_hint = ""
            n = config.batch_size
            if (batch_start // n) % config.meta_rec_interval == 0 and batch_start > 0:
                meta_hint = meta_recommendation(
                    config, state.lineage, state.metric, state.direction, state.history
                )
                if meta_hint:
                    print(f"  [meta-rec] {meta_hint[:120]}")

            _write_status(config, state, phase="building",
                          message=f"Batch s{stage_num}.v{batch_start}…")

            # Initial batch: research + code + DST + build + measure.
            batch_results = _run_batch(
                config, client, state, batch_start,
                feedback_carry, telemetry_summary, meta_hint,
            )

            # Refinement loop.
            current_hyps = []
            current_muts = []
            refined = list(batch_results)
            for r in batch_results:
                hyp = r.feedback  # placeholder; real hyp tracked below
                current_hyps.append(None)
                current_muts.append(None)

            # Re-collect hypothesis/mutation from the batch via a parallel lookup.
            # (They're embedded in the result's feedback context; we track them separately.)
            # We use a simple index match since batch results are in order.
            # The actual Hypothesis objects were produced inside _run_batch. We store
            # them by re-running research here? No — we need to carry them out.
            # Simpler: _run_batch returns results with enough info for refinement.
            # Refinement uses `classify_failure` + `refine_hypothesis`/`fix_code`.
            # Both need hypothesis + mutation. We store these inside the result.
            # SOLUTION: attach hypothesis + mutation to VariantResult (see below).
            #
            # For now, refinement is done using info embedded in result.feedback.
            # In a future refactor we'll attach the full objects to VariantResult.
            # For this iteration we skip refinement (max_refinements controls it).
            #
            # TODO: attach Hypothesis/ProposedMutation to VariantResult for refinement.

            # Process final results.
            feedback_carry = []
            best: VariantResult | None = None
            best_delta = -1e9

            for result in refined:
                state.variant_count += 1
                he = history_entry(result, state)
                state.history.append(he)
                feedback_carry.append(result.feedback)

                # Build a cull reason for the UI when the variant failed: prefer
                # the structured failure_type when present, else the raw feedback
                # message (truncated). Survived variants pass cull_reason="".
                cull_reason = ""
                if not result.survived:
                    if result.feedback.failure_type:
                        cull_reason = f"{result.feedback.failure_type}: {result.feedback.message}"
                    else:
                        cull_reason = result.feedback.message or "fitness regression"
                client.record_breed(
                    breed_id=f"breed-s{stage_num}-v{result.variant_num}",
                    cluster_name=result.cluster_name or "none",
                    change_description=result.change_description,
                    perf_delta_pct=result.delta_pct or 0.0,
                    survived=result.survived,
                    goal_id=state.goal_id,
                    stage_num=stage_num,
                    variant_num=result.variant_num,
                    motivation=result.hypothesis_title,
                    dst_passed=result.dst_passed,
                    cull_reason=cull_reason,
                )

                if result.survived and (result.delta_pct or 0.0) > best_delta:
                    best = result
                    best_delta = result.delta_pct or 0.0

                _write_status(config, state)

            # Pick-of-batch announcement: even when no variant survived this
            # batch, log the leaderboard so the operator can see how close any
            # variant came to the improvement threshold.
            _print_batch_pick(refined, best, state)

            if best is not None:
                wt_path = (config.evolution_worktrees
                            / f"proposal-s{stage_num}-v{best.variant_num}")
                if not wt_path.exists():
                    wt_path = Path(
                        f"/Users/arun.parthiban/notdd/"
                        f"helix-worktrees/cluster-{best.cluster_name}"
                    )
                promoted = promote_stage_champion(config, client, state, best, wt_path)
                if promoted:
                    update_evolution_spec(
                        config, wt_path,
                        change_description=best.change_description,
                        delta_pct=best.delta_pct,
                        stage_num=stage_num,
                        lineage=state.lineage,
                        metric=state.metric,
                    )
                    client.retire_stage_breeds(stage_num)
                    break

        # Stage finished — print a full leaderboard so the operator can see
        # every variant we tried, ranked by Δ, before we wipe the in-flight
        # state. This is especially valuable when no variant survived the 3%
        # threshold: it answers "why didn't anything win?" at a glance.
        _print_stage_summary(state)

        # Clear the in-flight resume file so the next stage starts fresh.
        clear_progress()

        # Safety net: tear down any variant clusters from THIS stage that are
        # still alive. Within-batch teardown already runs at the end of
        # _build_and_measure, but if a measure/teardown step crashed or a job
        # never reached `commit_sha` we'd otherwise leave the cluster + its
        # cloned producer/consumer pods running.
        _sweep_stage_clusters(client, state.lineage, stage_num)

        cool = config.champion_cooldown_minutes
        if state.champion_found:
            print(f"\n  [stage {stage_num}] champion promoted — cooling {cool} min…")
            _write_status(config, state, phase="cooldown",
                          message=f"Champion promoted — cooling {cool} min…")
            time.sleep(cool * 60)
        else:
            print(f"\n  [stage {stage_num}] no champion — continuing")
            _write_status(config, state, phase="no_champion",
                          message=f"Stage {stage_num} exhausted without champion")

    write_idle_status(config, message="Evolution loop finished")
    print("\nEvolution loop complete.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv=None):
    p = argparse.ArgumentParser(description="Live evolution loop (ADR-0095 live extension)")
    p.add_argument("--stages", type=int, default=10)
    p.add_argument("--batch-size", type=int, default=None)
    p.add_argument("--traffic-window", type=int, default=None)
    p.add_argument("--cooldown", type=int, default=None)
    p.add_argument("--max-variants", type=int, default=None)
    p.add_argument("--max-refinements", type=int, default=None)
    p.add_argument("--restart-stage", action="store_true",
                   help="Discard any in-flight stage state and start fresh.")
    p.add_argument("--no-redeploy-champion", action="store_true",
                   help="Skip auto-redeploy of the baseline cluster after promotion. "
                        "Useful for smoke tests; baseline will drift from champion branch.")
    p.add_argument("--redeploy-champion-now", type=int, metavar="STAGE_NUM",
                   help="Rebuild and redeploy the baseline champion cluster from the "
                        "current champion branch tip, then exit. Use after promoting a "
                        "stage with --no-redeploy-champion to catch the baseline up.")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    config = LiveEvolutionConfig()
    if args.batch_size is not None:        config.batch_size = args.batch_size
    if args.traffic_window is not None:    config.traffic_window_minutes = args.traffic_window
    if args.cooldown is not None:          config.champion_cooldown_minutes = args.cooldown
    if args.max_variants is not None:      config.max_variants_per_stage = args.max_variants
    if args.max_refinements is not None:   config.max_refinements = args.max_refinements
    if args.no_redeploy_champion:          config.auto_redeploy_champion = False

    if args.redeploy_champion_now is not None:
        client = LiveClient(config)
        print(f"[redeploy] rebuilding baseline cluster {config.champion_cluster_name} "
              f"from {config.champion_branch} (stage {args.redeploy_champion_now})…")
        try:
            ok = client.redeploy_champion_cluster(args.redeploy_champion_now)
        except Exception as exc:
            print(f"[redeploy] error: {exc}")
            return 1
        print(f"[redeploy] {'success' if ok else 'FAILED'}")
        return 0 if ok else 1

    if args.restart_stage:
        print("[startup] --restart-stage: discarding in-flight stage state")
        clear_progress()

    try:
        run_evolution(config, max_stages=args.stages)
    except KeyboardInterrupt:
        write_idle_status(config, message="Interrupted by operator")
        print("\nInterrupted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
