"""DRIVE phase orchestration: create and drive an ImprovementIssue through Temper.

Per the BC contract, Agent-C2 CREATES and DRIVES the ``ImprovementIssue`` entity
(it does NOT own the spec -- Agent-B does). The sequence this driver runs:

    Observe(title, hypothesis, target_file)   # Observed -> Researching
    BeginPlanning()                            # Researching -> (planning)
    WritePlan(plan, acceptance_criteria)       # records the plan
    -- hand off --                             # human ApprovePlan (Cedar), then
                                               # C1 Symphony picks up StartWork

ApprovePlan is human-only (Cedar). When the driver hits that denial it surfaces
the ``decision_id`` and polls -- it never self-approves. After human approval the
issue is ``Planned`` and ready for Symphony (Agent-C1).

If Agent-B's ``ImprovementIssue`` spec is not yet deployed, the driver detects
that up front and reports the issue payload it *would* have created, so the
OBSERVE + RESEARCH work is never lost.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from .observe import LatencyObservation
from .research import ResearchResult
from .temper_client import TemperClient, TemperDenied, TemperUnavailable


IMPROVEMENT_ISSUE_TYPE = "ImprovementIssue"
IMPROVEMENT_ISSUE_SET = "ImprovementIssues"


@dataclass
class IssuePayload:
    """The fields the observer feeds into ImprovementIssue.Observe + WritePlan."""

    id: str
    title: str
    hypothesis: str
    target_file: str
    plan: str
    acceptance_criteria: str
    evidence: str  # JSON string

    def as_observe_params(self) -> dict[str, str]:
        return {
            "title": self.title,
            "hypothesis": self.hypothesis,
            "target_file": self.target_file,
        }

    def as_writeplan_params(self) -> dict[str, str]:
        return {"plan": self.plan, "acceptance_criteria": self.acceptance_criteria}


def build_issue_payload(
    observation: LatencyObservation, research: ResearchResult
) -> IssuePayload:
    """Turn the confirmed hypothesis into a concrete improvement issue."""
    confirmed = research.confirmed
    if confirmed is None:
        raise ValueError("cannot build an issue: research confirmed no hypothesis")

    probe = confirmed.probe
    current = probe.value if probe and probe.found else "?"
    title = (
        f"Raise Helix batcher linger_ms to cut produce p95 tail under burst"
        if confirmed.id == "H1"
        else f"Tune {confirmed.title}"
    )

    hypothesis = (
        f"{confirmed.title}. {confirmed.mechanism} "
        f"Observed: p95 peaks at {observation.p95_peak_ms:.0f}ms "
        f"({observation.burst_ratio:.0f}x over a {observation.p95_baseline_ms:.1f}ms "
        f"baseline) while replication.lag stays at "
        f"{observation.replication_lag_max:.0f}, isolating the tail to the "
        f"leader-side batcher. Current value: {current}."
    )

    if confirmed.id == "H1":
        plan = (
            "1. In helix-server/src/service/batcher.rs, raise the default "
            f"linger_ms from {current} to a small but non-trivial value (e.g. 5ms) "
            "via HELIX_BATCHER_LINGER_MS / BatcherConfig::default(). "
            "2. Keep the env override so it stays tunable. "
            "3. Run helix-bench (docker/bench.sh, 128 partitions) before/after to "
            "confirm the throughput gain and that steady-state p95 stays within "
            "budget. "
            "4. Confirm DST + TLA + cargo test pass on the branch (Agent-B CIRun). "
            "5. Roll out to the dark-factory GKE StatefulSet via the Deploy entity "
            "(human-approved) and watch helix.produce.latency_ms.95percentile."
        )
        acceptance_criteria = (
            "Under the same bursty load, produce p95 peak drops measurably "
            f"(target: well below the observed {observation.p95_peak_ms:.0f}ms "
            "burst) with no regression in steady-state p95 beyond the linger budget; "
            "throughput (helix.produce.latency_ms.count) holds or improves; "
            "replication.lag stays 0; all CI gates green."
        )
    else:
        plan = (
            f"Investigate and tune {confirmed.target_file} per the confirmed "
            f"mechanism; benchmark with helix-bench; gate via CIRun; deploy via the "
            f"Deploy entity."
        )
        acceptance_criteria = (
            "Produce p95 burst tail reduced with no replication regression; CI green."
        )

    evidence = json.dumps(
        {
            "observation": {
                "p95_baseline_ms": round(observation.p95_baseline_ms, 3),
                "p95_peak_ms": round(observation.p95_peak_ms, 3),
                "p95_mean_ms": round(observation.p95_mean_ms, 3),
                "burst_ratio": round(observation.burst_ratio, 2),
                "max_peak_ms": round(observation.max_peak_ms, 1),
                "replication_lag_max": observation.replication_lag_max,
                "total_produces": observation.total_produces,
                "window": observation.window,
                "captured_at": observation.captured_at,
            },
            "confirmed_hypothesis": confirmed.id,
            "experiment_log": research.experiment_log,
            "ruled_out": [
                {"id": h.id, "evidence": h.evidence}
                for h in research.hypotheses
                if h.verdict == "ruled_out"
            ],
        },
        indent=2,
    )

    return IssuePayload(
        id=f"imp-{uuid.uuid4().hex[:8]}",
        title=title,
        hypothesis=hypothesis,
        target_file=confirmed.target_file,
        plan=plan,
        acceptance_criteria=acceptance_criteria,
        evidence=evidence,
    )


@dataclass
class DriveResult:
    """Outcome of the DRIVE phase."""

    issue_id: str
    status: str  # "planned" | "pending_approval" | "spec_not_deployed" | "error"
    detail: str
    pending_decision: str | None = None


def drive_issue(
    client: TemperClient, payload: IssuePayload, poll_for_approval: bool = True
) -> DriveResult:
    """Create + drive the ImprovementIssue, surfacing Cedar denials honestly.

    Returns a :class:`DriveResult`. Never self-approves a Cedar decision.
    """
    # Guard: is Agent-B's spec live yet?
    if not client.has_entity_set(IMPROVEMENT_ISSUE_SET):
        return DriveResult(
            issue_id=payload.id,
            status="spec_not_deployed",
            detail=(
                f"{IMPROVEMENT_ISSUE_TYPE} spec is not deployed for tenant "
                f"{client.tenant!r} yet (Agent-B owns it). The observer prepared the "
                f"full issue payload below; it will create+drive it once the spec is "
                f"live.\n\n--- prepared ImprovementIssue ---\n"
                f"id={payload.id}\ntitle={payload.title}\n"
                f"target_file={payload.target_file}\n"
                f"hypothesis={payload.hypothesis}\n"
            ),
        )

    # 1. create the entity in its initial state
    try:
        client.create(IMPROVEMENT_ISSUE_SET, {"id": payload.id})
    except TemperUnavailable as exc:
        return DriveResult(payload.id, "error", f"create failed: {exc}")
    except TemperDenied as denied:
        return _handle_denial(client, payload, denied, "create", poll_for_approval)

    # 2. Observe(title, hypothesis, target_file)  Observed -> Researching
    step = self_drive_action(client, payload, "Observe", payload.as_observe_params())
    if step is not None:
        return step

    # 3. BeginPlanning()
    step = self_drive_action(client, payload, "BeginPlanning", {})
    if step is not None:
        return step

    # 4. WritePlan(plan, acceptance_criteria)
    step = self_drive_action(
        client, payload, "WritePlan", payload.as_writeplan_params()
    )
    if step is not None:
        return step

    # 5. Best-effort: attach the structured Evidence JSON via a PATCH on the data
    #    plane. The contract lists Evidence as a plain field; if PATCH is denied or
    #    unsupported the plan/criteria are already recorded, so we don't fail.
    try:
        client.patch(IMPROVEMENT_ISSUE_SET, payload.id, {"Evidence": payload.evidence})
    except (TemperDenied, TemperUnavailable):
        pass

    return DriveResult(
        issue_id=payload.id,
        status="planned",
        detail=(
            "ImprovementIssue created and driven through Observe -> BeginPlanning -> "
            "WritePlan. Plan is recorded and AWAITING HUMAN ApprovePlan (Cedar, "
            "human-only). After approval, Symphony (Agent-C1) runs StartWork."
        ),
    )


def self_drive_action(
    client: TemperClient, payload: IssuePayload, action: str, params: dict
) -> DriveResult | None:
    """Fire one action; return a terminal DriveResult on denial/error, else None."""
    try:
        client.action(IMPROVEMENT_ISSUE_SET, payload.id, action, params)
        return None
    except TemperDenied as denied:
        return _handle_denial(client, payload, denied, action, poll_for_approval=False)
    except TemperUnavailable as exc:
        return DriveResult(payload.id, "error", f"{action} failed: {exc}")


def _handle_denial(
    client: TemperClient,
    payload: IssuePayload,
    denied: TemperDenied,
    action: str,
    poll_for_approval: bool,
) -> DriveResult:
    detail = (
        f"Cedar denied {action} on ImprovementIssue {payload.id}. "
        f"This is expected for human-only actions (e.g. ApprovePlan). "
        f"A human must approve via the Observe UI -- the agent NEVER self-approves.\n"
        f"  decision_id: {denied.decision_id}\n"
        f"  message: {denied.message}"
    )
    if poll_for_approval and denied.decision_id:
        status = client.poll_decision(denied.decision_id)
        detail += f"\n  poll result: {status}"
    return DriveResult(
        issue_id=payload.id,
        status="pending_approval",
        detail=detail,
        pending_decision=denied.decision_id,
    )
