"""Proposal pipeline for the live evolution loop (ADR-0095 live extension).

Three-agent pipeline per variant, all backed by `claude -p` (Claude Code CLI):

  1. Research Agent  — uses Read+Bash tools to browse helix codebase, produces
     a well-reasoned Hypothesis JSON written to /tmp/hypothesis-sN-vM.json.
     Session is persisted so hypothesis refinement can resume the same context.

  2. Coding Agent    — implements the Hypothesis by directly editing files in
     the variant's worktree (no JSON edit patches). After it finishes we run
     `git diff --name-only` to see what changed. No more hallucinated old-strings.
     Fresh session each attempt (worktree is reset between attempts).

  3. Failure Classifier — after a fitness failure, one-shot claude -p call
     that returns "hypothesis" or "code". Routes the variant to the right agent
     for refinement. Resuming the Research Agent's session works naturally for
     hypothesis refinement.

Support agents (no tools, no session persistence):
  - Meta-rec:    batch direction hint every N variants
  - Spec updater: direct Anthropic API call (reads diff, writes one file)

Model assignments (all configurable in config.py):
  Research:   model_research_agent    (default: claude-opus-4-6)
  Coding:     model_coding_agent      (default: claude-opus-4-6)
  Classifier: model_failure_classifier (default: claude-haiku-4-6)
  Meta-rec:   model_meta_rec          (default: claude-haiku-4-6)
  Spec upd:   model_spec_updater      (default: claude-sonnet-4-6)
"""

from __future__ import annotations

import json
import subprocess
import textwrap
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent import (
    AgentResult,
    ClaudeCliAgent,
    make_classifier_agent,
    make_coding_agent,
    make_meta_rec_agent,
    make_research_agent,
)
from config import LiveEvolutionConfig


# ---------------------------------------------------------------------------
# Core data types
# ---------------------------------------------------------------------------

@dataclass
class Hypothesis:
    """A research-agent theory about why a particular code change will help."""
    title: str
    reasoning: str
    expected_mechanism: str
    suggested_files: list[str]
    confidence: str                  # "high" | "medium" | "low"
    variant_num: int = 0
    refinement_num: int = 0
    research_session_id: str = ""    # claude session ID for refinement
    raw_response: str = ""

    def summary(self) -> str:
        return (f"[v{self.variant_num} r{self.refinement_num}] "
                f"{self.title} (confidence={self.confidence})")


@dataclass
class ProposedMutation:
    """A coding-agent implementation of a Hypothesis.

    The coding agent edits worktree files directly — `edits` is always empty.
    `coding_session_id` is stored so `fix_code_for_hypothesis` can resume it.
    """
    change_description: str
    rationale: str
    mutation_type: str               # "code" | "control_plane" | "skip"
    edits: list[dict] = field(default_factory=list)   # always [] (agent edits directly)
    control_plane: list[dict] = field(default_factory=list)
    hypothesis: Hypothesis | None = None
    coding_session_id: str = ""      # claude session ID (coding agent)
    files_changed: list[str] = field(default_factory=list)  # from git diff
    raw_response: str = ""


@dataclass
class TextFeedback:
    """Structured feedback from a prior variant attempt."""
    variant_num: int
    outcome: str                 # "dst_fail" | "fitness_fail" | "fitness_win" | "no_data"
    message: str
    attempt_num: int = 0
    failure_type: str = ""       # "hypothesis" | "code" | ""

    def as_prompt_fragment(self) -> str:
        round_label = f" (round {self.attempt_num})" if self.attempt_num else ""
        attempt_label = f"v{self.variant_num}{round_label}"
        if self.outcome == "dst_fail":
            return (
                f"{attempt_label} was CULLED by the WAL durability DST:\n"
                f"  {self.message}\n"
                f"Do not propose mutations that skip or weaken fsync on the WAL sync() path."
            )
        if self.outcome == "fitness_fail":
            fault = f" (fault: {self.failure_type})" if self.failure_type else ""
            return f"{attempt_label} FAILED fitness{fault}:\n  {self.message}\n"
        if self.outcome == "fitness_win":
            return f"{attempt_label} SURVIVED: {self.message}\nBuild on this direction."
        return f"{attempt_label}: {self.message}"


@dataclass
class StageHistoryEntry:
    stage_num: int
    variant_num: int
    description: str
    outcome: str
    delta_pct: float | None
    hypothesis_title: str = ""
    refinement_num: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stage_history_summary(history: list[StageHistoryEntry]) -> str:
    if not history:
        return "No previous attempts this stage."
    lines = ["Previous attempts (most recent first):"]
    for e in reversed(history[-15:]):
        delta = f" Δ={e.delta_pct:+.1f}%" if e.delta_pct is not None else ""
        hyp = f" | hyp: {e.hypothesis_title}" if e.hypothesis_title else ""
        r = f" r{e.refinement_num}" if e.refinement_num else ""
        lines.append(
            f"  v{e.variant_num}{r}: [{e.outcome.upper()}]{delta}{hyp} — {e.description}"
        )
    return "\n".join(lines)


def _read_hypothesis_file(path: Path) -> dict:
    """Read a JSON hypothesis file written by the research agent."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 1. Research Agent — produces Hypothesis
# ---------------------------------------------------------------------------

def research_hypothesis(
    config: LiveEvolutionConfig,
    lineage: str,
    metric: str,
    direction: str,
    stage_history: list[StageHistoryEntry],
    telemetry_summary: str,
    meta_hint: str,
    variant_num: int,
    previous_hypotheses: list[Hypothesis] | None = None,
    text_feedback: TextFeedback | None = None,
) -> Hypothesis:
    """Research Agent: produce a well-reasoned hypothesis via claude -p.

    The agent has Read+Bash access to the helix codebase. It browses the repo,
    reads relevant files, and writes its hypothesis to a JSON temp file. The
    session ID is preserved so `refine_hypothesis` can resume the same context.
    """
    guide = config.evolution_guide()
    history_block = _stage_history_summary(stage_history)
    direction_word = "reduce" if direction == "minimize" else "increase"
    target_label = {
        "p95_latency": "produce latency p95 (helix.produce.latency_ms.95percentile)",
        "throughput":  "produce throughput (helix.produce.throughput)",
    }.get(metric, metric)

    prev_hyp_block = ""
    if previous_hypotheses:
        lines = ["Previously tested hypotheses this stage (do NOT repeat):"]
        for h in previous_hypotheses:
            lines.append(f"  - {h.title}: {h.expected_mechanism[:150]}")
        prev_hyp_block = "\n".join(lines)

    feedback_block = (f"\n## Feedback from prior attempt\n{text_feedback.as_prompt_fragment()}"
                      if text_feedback else "")
    meta_block = f"\n## Meta-recommendation\n{meta_hint}" if meta_hint else ""

    hyp_file = Path(f"/tmp/hypothesis-s{stage_history[0].stage_num if stage_history else 0}-v{variant_num}.json")

    prompt = textwrap.dedent(f"""\
        You are the Research Agent for Helix directed-evolution.
        Goal: {direction_word} {target_label} for the {lineage} lineage.
        Variant: {variant_num}

        ## Live telemetry
        {telemetry_summary}
        {meta_block}
        ## Stage history
        {history_block}

        {prev_hyp_block}
        {feedback_block}
        ## Evolution spec
        {guide}

        ## Your task
        1. Browse the helix codebase (use Read/Bash to explore relevant files).
        2. Reason about which unexplored mechanism could {direction_word} {metric}.
        3. Form a specific, testable hypothesis with a named code mechanism.
        4. Write your hypothesis as JSON to: {hyp_file}

        JSON format:
        {{
          "title": "<≤10-word label>",
          "reasoning": "<chain-of-thought>",
          "expected_mechanism": "<specific code path/parameter, how and why>",
          "suggested_files": ["<path/relative/to/helix/root>"],
          "confidence": "high|medium|low"
        }}

        After writing the file, confirm with: HYPOTHESIS WRITTEN
    """)

    agent = make_research_agent(config.model_research_agent, config.helix_repo,
                                timeout_s=300)
    print(f"  [research] v{variant_num}: browsing helix codebase…")
    result = agent.run(prompt, label=f"research v{variant_num}")

    if result.is_error:
        print(f"  [research] error v{variant_num}: {result.error_message[:120]}")
        return Hypothesis(
            title="(research agent error)", reasoning=result.error_message[:200],
            expected_mechanism="", suggested_files=[], confidence="low",
            variant_num=variant_num, research_session_id=result.session_id,
        )

    parsed = _read_hypothesis_file(hyp_file)
    if not parsed:
        # Try to extract JSON from the text response as fallback.
        try:
            b, e = result.text.find("{"), result.text.rfind("}") + 1
            if b >= 0 and e > b:
                parsed = json.loads(result.text[b:e])
        except Exception:
            pass

    if not parsed:
        print(f"  [research] v{variant_num}: could not parse hypothesis JSON (cost=${result.cost_usd:.3f})")
        return Hypothesis(
            title="(parse failed)", reasoning=result.text[:300],
            expected_mechanism="", suggested_files=[], confidence="low",
            variant_num=variant_num, research_session_id=result.session_id,
        )

    hyp = Hypothesis(
        title=str(parsed.get("title", "untitled")),
        reasoning=str(parsed.get("reasoning", "")),
        expected_mechanism=str(parsed.get("expected_mechanism", "")),
        suggested_files=parsed.get("suggested_files", []),
        confidence=str(parsed.get("confidence", "low")),
        variant_num=variant_num,
        research_session_id=result.session_id,
        raw_response=result.text,
    )
    print(f"  [research] v{variant_num}: {hyp.title} (confidence={hyp.confidence}, "
          f"cost=${result.cost_usd:.3f})")
    return hyp


def refine_hypothesis(
    config: LiveEvolutionConfig,
    original: Hypothesis,
    failure_feedback: TextFeedback,
    stage_history: list[StageHistoryEntry],
    telemetry_summary: str,
) -> Hypothesis:
    """Research Agent: resume the session to revise a failing hypothesis."""
    prompt = textwrap.dedent(f"""\
        The hypothesis you produced failed in testing.

        ## Original hypothesis
        Title: {original.title}
        Reasoning: {original.reasoning}
        Expected mechanism: {original.expected_mechanism}

        ## Failure feedback
        {failure_feedback.as_prompt_fragment()}

        ## Telemetry
        {telemetry_summary}

        Produce a REVISED hypothesis. If the original mechanism was fundamentally wrong,
        pivot to a different approach. Write to the same JSON file as before, or to:
        /tmp/hypothesis-refined-v{original.variant_num}-r{original.refinement_num + 1}.json

        JSON format: same as before. After writing: HYPOTHESIS WRITTEN
    """)

    agent = make_research_agent(config.model_research_agent, config.helix_repo,
                                timeout_s=240)

    lbl = f"research v{original.variant_num} r{original.refinement_num+1}"
    if original.research_session_id:
        print(f"  [research] refine {lbl}: resuming session {original.research_session_id[:8]}…")
        result = agent.resume(original.research_session_id, prompt, label=lbl)
    else:
        print(f"  [research] refine {lbl}: fresh session (no prior session_id)")
        result = agent.run(prompt, label=lbl)

    hyp_file = Path(f"/tmp/hypothesis-refined-v{original.variant_num}-r{original.refinement_num + 1}.json")
    parsed = _read_hypothesis_file(hyp_file)
    if not parsed:
        try:
            b, e = result.text.find("{"), result.text.rfind("}") + 1
            if b >= 0 and e > b:
                parsed = json.loads(result.text[b:e])
        except Exception:
            pass

    if not parsed or result.is_error:
        return Hypothesis(
            title=f"(revised {original.title})", reasoning=result.text[:300],
            expected_mechanism=original.expected_mechanism,
            suggested_files=original.suggested_files, confidence="low",
            variant_num=original.variant_num,
            refinement_num=original.refinement_num + 1,
            research_session_id=result.session_id,
        )

    return Hypothesis(
        title=str(parsed.get("title", original.title)),
        reasoning=str(parsed.get("reasoning", "")),
        expected_mechanism=str(parsed.get("expected_mechanism", "")),
        suggested_files=parsed.get("suggested_files", original.suggested_files),
        confidence=str(parsed.get("confidence", "low")),
        variant_num=original.variant_num,
        refinement_num=original.refinement_num + 1,
        research_session_id=result.session_id,
        raw_response=result.text,
    )


# ---------------------------------------------------------------------------
# 2. Coding Agent — implements Hypothesis by editing files directly
# ---------------------------------------------------------------------------

def implement_hypothesis(
    config: LiveEvolutionConfig,
    hypothesis: Hypothesis,
    wt_path: Path,
    stage_history: list[StageHistoryEntry],
    text_feedback: TextFeedback | None,
    previous_mutations: list[str] | None = None,
) -> ProposedMutation:
    """Coding Agent: edit worktree files to implement the Hypothesis.

    The agent runs in the worktree directory with Read/Edit/Write/Bash tools.
    It reads actual source files (no hallucination), makes targeted changes,
    and optionally runs `cargo check` to verify compilation.
    After the call, the caller uses `git diff` to see what changed.
    """
    guide = config.evolution_guide()
    already_tried = ", ".join(previous_mutations or []) or "none"

    feedback_block = ""
    if text_feedback and text_feedback.failure_type == "code":
        feedback_block = (
            f"\n## Previous code attempt failed (fix the implementation)\n"
            f"{text_feedback.as_prompt_fragment()}\n"
            f"The hypothesis is still valid. Fix the implementation."
        )

    prompt = textwrap.dedent(f"""\
        You are the Coding Agent for Helix directed-evolution.
        Implement the following hypothesis by editing files in this worktree.

        ## Hypothesis
        Title: {hypothesis.title}
        Confidence: {hypothesis.confidence}
        Reasoning: {hypothesis.reasoning}
        Expected mechanism: {hypothesis.expected_mechanism}
        Focus files: {', '.join(hypothesis.suggested_files) or 'see guide'}

        ## Already tried this stage (do NOT repeat these approaches)
        {already_tried}
        {feedback_block}
        ## Constraints (from Evolution Guide)
        {guide[:2000]}

        ## Instructions
        1. Read the relevant files using the Read tool.
        2. Make ONE targeted, minimal change that best tests the hypothesis.
        3. Run `cargo check -p helix-server 2>&1 | head -60` to verify compilation.
        4. If it does not compile, fix the errors.
        5. Do NOT commit, push, or run git operations.
        6. When done, output a single line: CHANGE COMPLETE: <one-line description>
           If you cannot find a safe change to make, output: NO CHANGE: <reason>
    """)

    agent = make_coding_agent(config.model_coding_agent, wt_path, config.helix_repo,
                              timeout_s=600)
    print(f"  [coding] v{hypothesis.variant_num}: implementing '{hypothesis.title}'…")
    result = agent.run(prompt, label=f"coding v{hypothesis.variant_num}")

    if result.is_error:
        print(f"  [coding] error v{hypothesis.variant_num}: {result.error_message[:120]}")
        return ProposedMutation(
            change_description=f"(agent error: {result.error_message[:80]})",
            rationale="", mutation_type="skip", hypothesis=hypothesis,
            coding_session_id=result.session_id,
        )

    # Parse the final line to get the description.
    last_line = result.text.strip().splitlines()[-1] if result.text.strip() else ""
    if last_line.startswith("NO CHANGE:"):
        reason = last_line[len("NO CHANGE:"):].strip()
        print(f"  [coding] v{hypothesis.variant_num}: agent chose no change: {reason}")
        return ProposedMutation(
            change_description=f"skip: {reason}", rationale=reason,
            mutation_type="skip", hypothesis=hypothesis,
            coding_session_id=result.session_id,
        )

    description = (
        last_line.replace("CHANGE COMPLETE:", "").strip()
        if "CHANGE COMPLETE:" in last_line
        else hypothesis.title
    )

    # Discover which files were changed (git diff in the worktree).
    files_changed = _git_diff_files(wt_path)
    if not files_changed:
        print(f"  [coding] v{hypothesis.variant_num}: agent made no file changes")
        return ProposedMutation(
            change_description="skip: no files modified",
            rationale=result.text[-200:], mutation_type="skip",
            hypothesis=hypothesis, coding_session_id=result.session_id,
        )

    print(f"  [coding] v{hypothesis.variant_num}: {description} "
          f"(files: {', '.join(files_changed)}, cost=${result.cost_usd:.3f})")
    return ProposedMutation(
        change_description=description,
        rationale=result.text[-500:],
        mutation_type="code",
        files_changed=files_changed,
        hypothesis=hypothesis,
        coding_session_id=result.session_id,
        raw_response=result.text,
    )


def fix_code_for_hypothesis(
    config: LiveEvolutionConfig,
    hypothesis: Hypothesis,
    prior_mutation: ProposedMutation,
    failure_feedback: TextFeedback,
    stage_history: list[StageHistoryEntry],
    wt_path: Path,
) -> ProposedMutation:
    """Coding Agent: fresh session to fix an implementation (right theory, wrong code)."""
    prompt = textwrap.dedent(f"""\
        A prior code implementation of a hypothesis failed. Fix it.

        ## Hypothesis (STILL VALID)
        Title: {hypothesis.title}
        Expected mechanism: {hypothesis.expected_mechanism}

        ## Prior attempt changed these files
        {', '.join(prior_mutation.files_changed) or 'unknown'}

        ## Failure feedback
        {failure_feedback.as_prompt_fragment()}

        ## Constraints
        {config.evolution_guide()[:1500]}

        Read the relevant files, then fix the implementation.
        Run `cargo check -p helix-server 2>&1 | head -40` to verify.
        When done, output: CHANGE COMPLETE: <description>
        If no safe fix exists: NO CHANGE: <reason>
    """)

    agent = make_coding_agent(config.model_coding_agent, wt_path, config.helix_repo,
                              timeout_s=600)
    print(f"  [coding-fix] v{hypothesis.variant_num}: fixing '{hypothesis.title}'…")
    result = agent.run(prompt, label=f"coding-fix v{hypothesis.variant_num}")

    if result.is_error:
        return ProposedMutation(
            change_description=f"(fix error: {result.error_message[:80]})",
            rationale="", mutation_type="skip", hypothesis=hypothesis,
            coding_session_id=result.session_id,
        )

    last_line = result.text.strip().splitlines()[-1] if result.text.strip() else ""
    if "NO CHANGE:" in last_line:
        return ProposedMutation(
            change_description=f"skip: {last_line[len('NO CHANGE:'):].strip()}",
            rationale="", mutation_type="skip", hypothesis=hypothesis,
            coding_session_id=result.session_id,
        )

    description = (last_line.replace("CHANGE COMPLETE:", "").strip()
                   if "CHANGE COMPLETE:" in last_line else hypothesis.title)
    files_changed = _git_diff_files(wt_path)
    return ProposedMutation(
        change_description=description, rationale=result.text[-300:],
        mutation_type="code" if files_changed else "skip",
        files_changed=files_changed, hypothesis=hypothesis,
        coding_session_id=result.session_id,
    )


def _git_diff_files(wt_path: Path) -> list[str]:
    """Return list of files changed vs HEAD in the worktree."""
    r = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=wt_path, capture_output=True, text=True, timeout=15, check=False,
    )
    return [f.strip() for f in r.stdout.splitlines() if f.strip()]


# ---------------------------------------------------------------------------
# 3. Failure Classifier
# ---------------------------------------------------------------------------

def classify_failure(
    config: LiveEvolutionConfig,
    hypothesis: Hypothesis,
    mutation: ProposedMutation,
    delta_pct: float | None,
    fitness_summary: str,
) -> str:
    """One-shot claude -p call to classify failure as 'hypothesis' or 'code'."""
    delta_str = f"{delta_pct:+.1f}%" if delta_pct is not None else "no data"
    files = ", ".join(mutation.files_changed) or "(unknown)"

    prompt = textwrap.dedent(f"""\
        A Helix performance variant failed. Classify the root cause.

        Hypothesis: {hypothesis.title}
        Expected mechanism: {hypothesis.expected_mechanism}
        Files changed: {files}
        Measurement: delta={delta_str} — {fitness_summary}

        Reply with exactly one word:
        - "hypothesis" if the theory itself was wrong (wrong mechanism or direction)
        - "code" if the theory is plausible but implementation was incorrect or too small
    """)

    agent = make_classifier_agent(config.model_failure_classifier)
    result = agent.run(prompt, label=f"classifier v{hypothesis.variant_num}")
    if result.is_error:
        print(f"  [classifier] warning: {result.error_message[:80]} — defaulting to 'code'")
        return "code"
    answer = result.text.strip().lower()
    return "hypothesis" if "hypothesis" in answer else "code"


# ---------------------------------------------------------------------------
# 4. Meta-recommendation
# ---------------------------------------------------------------------------

def meta_recommendation(
    config: LiveEvolutionConfig,
    lineage: str,
    metric: str,
    direction: str,
    stage_history: list[StageHistoryEntry],
) -> str:
    """High-level batch direction hint from a cheap/fast claude -p call."""
    if not stage_history:
        return ""
    history_block = _stage_history_summary(stage_history)
    direction_word = "reduce" if direction == "minimize" else "increase"

    prompt = textwrap.dedent(f"""\
        Meta-advisor for directed-evolution of Helix {metric}.
        Goal: {direction_word} {metric} ({lineage} lineage).

        What was tried:
        {history_block}

        Suggest in 2-3 sentences a high-level direction for the next batch.
        Be specific about Helix code areas (crate, file, parameter).
    """)

    agent = make_meta_rec_agent(config.model_meta_rec)
    result = agent.run(prompt, label="meta-rec")
    if result.is_error:
        print(f"  [meta-rec] warning: {result.error_message[:80]}")
        return ""
    return result.text.strip()


# ---------------------------------------------------------------------------
# 5. Batch proposal
# ---------------------------------------------------------------------------

def propose_batch(
    config: LiveEvolutionConfig,
    lineage: str,
    metric: str,
    direction: str,
    stage_history: list[StageHistoryEntry],
    feedback_list: list[TextFeedback],
    batch_start_variant: int,
    telemetry_summary: str = "",
    worktree_map: dict[int, Path] | None = None,
) -> tuple[list[tuple[Hypothesis, ProposedMutation]], str]:
    """Propose a batch: Research → Coding for each variant.

    Returns ([(hypothesis, mutation)], meta_hint).
    The coding agent edits files directly in worktree_map[variant_num].
    If worktree_map is None, no coding step is done (hypothesis-only mode).
    """
    n = config.batch_size
    meta_hint = ""

    if (batch_start_variant // n) % config.meta_rec_interval == 0 and batch_start_variant > 0:
        meta_hint = meta_recommendation(config, lineage, metric, direction, stage_history)
        if meta_hint:
            print(f"  [meta-rec] {meta_hint[:200]}")

    already_tried_hyp: list[Hypothesis] = []
    already_tried_desc: list[str] = []
    pairs: list[tuple[Hypothesis, ProposedMutation]] = []

    for i in range(n):
        variant_num = batch_start_variant + i
        feedback = feedback_list[i] if i < len(feedback_list) else None

        hyp = research_hypothesis(
            config, lineage, metric, direction,
            stage_history, telemetry_summary, meta_hint,
            variant_num,
            previous_hypotheses=already_tried_hyp,
            text_feedback=feedback,
        )

        wt = worktree_map.get(variant_num) if worktree_map else None
        if wt and wt.exists():
            mut = implement_hypothesis(
                config, hyp, wt, stage_history, feedback,
                previous_mutations=already_tried_desc,
            )
        else:
            # Worktree not ready yet — return a placeholder; live_loop creates
            # the worktree and calls implement_hypothesis directly.
            mut = ProposedMutation(
                change_description=hyp.title, rationale=hyp.reasoning,
                mutation_type="pending", hypothesis=hyp,
            )

        pairs.append((hyp, mut))
        already_tried_hyp.append(hyp)
        already_tried_desc.append(mut.change_description)

    return pairs, meta_hint


# ---------------------------------------------------------------------------
# 6. Spec updater (stays as direct Anthropic API — reads diff, writes one file)
# ---------------------------------------------------------------------------

def _anthropic_request(
    api_key: str,
    messages: list[dict],
    system: str,
    max_tokens: int = 2048,
    model: str = "claude-sonnet-4-6",
) -> str:
    import urllib.request, urllib.error
    url = "https://api.anthropic.com/v1/messages"
    payload = {"model": model, "max_tokens": max_tokens, "system": system,
               "messages": messages}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        raise RuntimeError(f"Anthropic HTTP {exc.code}: {raw[:300]}") from exc
    for block in result.get("content", []):
        if block.get("type") == "text":
            return block["text"]
    return ""


def update_evolution_spec(
    config: LiveEvolutionConfig,
    wt_path: Path,
    change_description: str,
    delta_pct: float | None,
    stage_num: int,
    lineage: str,
    metric: str,
) -> bool:
    """Direct Anthropic API call to update EVOLUTION_GUIDE.md after champion promotion."""
    creds = config.load_dd_credentials()
    api_key = creds.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("  [spec-updater] no ANTHROPIC_API_KEY — skipping")
        return False

    guide_path = config.helix_repo / config.evolution_guide_path
    if not guide_path.exists():
        return False

    current_spec = guide_path.read_text(encoding="utf-8")
    diff_result = subprocess.run(
        ["git", "diff", "HEAD~1", "HEAD", "--", "*.rs", "*.toml"],
        cwd=wt_path, capture_output=True, text=True, timeout=30, check=False,
    )
    git_diff = diff_result.stdout[:3000] if diff_result.stdout else "(no diff)"
    delta_str = f"{delta_pct:+.1f}%" if delta_pct is not None else "unknown"

    user_msg = textwrap.dedent(f"""\
        Champion promoted to champion-metrics-pranav-clone:
        Stage {stage_num} | {lineage} ({metric}) | {delta_str} | {change_description}

        Git diff:
        ```diff
        {git_diff}
        ```

        Current EVOLUTION_GUIDE.md:
        {current_spec}

        Update: 1) baseline values in performance levers if changed,
        2) append to "## Champion History": `- Stage {stage_num} ({lineage}): {change_description} → {delta_str}`,
        3) document any new lever.
        Keep mutation format, WAL invariant, INVARIANT file list unchanged.
        Return ONLY the raw updated file content.
    """)

    print(f"  [spec-updater] updating EVOLUTION_GUIDE.md after stage {stage_num}…")
    try:
        updated = _anthropic_request(
            api_key, [{"role": "user", "content": user_msg}],
            "Return only the raw file content, no fences.",
            max_tokens=4096, model=config.model_spec_updater,
        )
    except Exception as exc:
        print(f"  [spec-updater] error: {exc}")
        return False

    if not updated or len(updated) < 200:
        return False
    if updated.strip().startswith("```"):
        lines = updated.strip().splitlines()
        updated = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

    guide_path.write_text(updated, encoding="utf-8")
    r = subprocess.run(["git", "add", str(guide_path)], cwd=config.helix_repo,
                       capture_output=True, text=True, timeout=30, check=False)
    if r.returncode != 0:
        return False
    r = subprocess.run(
        ["git", "commit", "--no-verify", "-m",
         f"spec: update EVOLUTION_GUIDE.md after stage {stage_num} champion ({delta_str})"],
        cwd=config.helix_repo, capture_output=True, text=True, timeout=30, check=False,
    )
    if r.returncode != 0:
        return False
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                         cwd=config.helix_repo, capture_output=True, text=True,
                         timeout=10).stdout.strip()
    print(f"  [spec-updater] committed {sha}")
    return True
