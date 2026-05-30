"""RESEARCH phase: form competing hypotheses and run an experiment to confirm one.

This is the "autoresearch" core. Given a :class:`~observer.observe.LatencyObservation`,
the researcher:

1. Forms 2-3 concrete, competing hypotheses for the elevated p95, each grounded in
   a real Helix source location (batcher linger, raft inflight pipelining, raft
   batch-size cap).
2. Runs an EXPERIMENT that reads the actual Helix source to measure the current
   value of each suspected parameter, and combines that with the observed
   telemetry (notably ``replication.lag``) to score each hypothesis.
3. Confirms exactly one hypothesis and rules out the others *with evidence*, not by
   hardcoding the answer. If the source changes (e.g. someone already raised
   ``linger_ms``), the conclusion changes with it.

The reasoning is deliberately transparent: every confirm/rule-out carries the
concrete evidence string that produced it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .observe import LatencyObservation


# Real Helix source locations (relative to the Helix repo root). The researcher
# reads these files at runtime -- the line numbers below are starting hints; the
# probe re-derives the actual line so it stays correct if the file shifts.
HELIX_REPO_DEFAULT = "/Users/arun.parthiban/notdd/helix"
BATCHER_PATH = "helix-server/src/service/batcher.rs"
RAFT_LIB_PATH = "helix-raft/src/lib.rs"


@dataclass
class SourceProbe:
    """A measurement read directly from Helix source -- the experiment's evidence."""

    name: str
    path: str
    line: int | None
    snippet: str
    value: int | None
    found: bool

    def cite(self) -> str:
        loc = f"{self.path}:{self.line}" if self.line else self.path
        if not self.found:
            return f"{self.name}: NOT FOUND in {self.path}"
        return f"{self.name} = {self.value} ({loc})  // `{self.snippet.strip()}`"


@dataclass
class Hypothesis:
    """A competing explanation for the elevated p95, grounded in real source."""

    id: str
    title: str
    target_file: str
    mechanism: str
    probe: SourceProbe | None = None
    score: float = 0.0
    verdict: str = "untested"  # "confirmed" | "ruled_out" | "untested"
    evidence: list[str] = field(default_factory=list)

    def line(self) -> str:
        mark = {"confirmed": "[CONFIRMED]", "ruled_out": "[ruled out]"}.get(
            self.verdict, "[untested]"
        )
        return f"  {mark} {self.id}: {self.title}"


@dataclass
class ResearchResult:
    """The output of RESEARCH: the confirmed hypothesis + the ruled-out competitors."""

    observation: LatencyObservation
    hypotheses: list[Hypothesis]
    confirmed: Hypothesis | None
    experiment_log: list[str]

    def summary(self) -> str:
        lines = ["[RESEARCH] Competing hypotheses for elevated produce p95:"]
        for h in self.hypotheses:
            lines.append(h.line())
            for ev in h.evidence:
                lines.append(f"      - {ev}")
        lines.append("")
        lines.append("[EXPERIMENT] source probes (read live from the Helix repo):")
        for entry in self.experiment_log:
            lines.append(f"  {entry}")
        lines.append("")
        if self.confirmed:
            lines.append(
                f"[CONCLUSION] Confirmed {self.confirmed.id}: {self.confirmed.title}"
            )
            lines.append(f"  target file: {self.confirmed.target_file}")
        else:
            lines.append("[CONCLUSION] No hypothesis confirmed by the experiment.")
        return "\n".join(lines)


def _probe_assignment(
    repo: str, rel_path: str, name: str, pattern: str
) -> SourceProbe:
    """Read a Helix source file and extract the integer value of a named param.

    ``pattern`` is a regex with one capture group for the integer value. The probe
    records the actual line number and the matched snippet -- this is the real
    measurement, not a constant baked into the observer.
    """
    abs_path = os.path.join(repo, rel_path)
    if not os.path.exists(abs_path):
        return SourceProbe(name, rel_path, None, "", None, found=False)
    with open(abs_path, "r", encoding="utf-8") as handle:
        for idx, raw in enumerate(handle, start=1):
            m = re.search(pattern, raw)
            if m:
                return SourceProbe(
                    name=name,
                    path=rel_path,
                    line=idx,
                    snippet=raw,
                    value=int(m.group(1)),
                    found=True,
                )
    return SourceProbe(name, rel_path, None, "", None, found=False)


def _build_hypotheses() -> list[Hypothesis]:
    """The three competing, source-grounded explanations."""
    return [
        Hypothesis(
            id="H1",
            title="Server-side batcher linger_ms too low -> tiny batches under burst",
            target_file=BATCHER_PATH,
            mechanism=(
                "With linger_ms ~= 1ms the batcher flushes almost immediately, so a "
                "burst of concurrent produce requests becomes many small Raft "
                "proposals instead of one large batch. Raft proposal overhead per "
                "batch then dominates and p95 spikes. Raising linger_ms trades a few "
                "ms of steady-state latency for far higher batching efficiency and a "
                "lower tail under load (the classic Kafka linger.ms tradeoff)."
            ),
        ),
        Hypothesis(
            id="H2",
            title="Raft MAX_INFLIGHT_APPEND_ENTRIES too low -> replication stalls",
            target_file=RAFT_LIB_PATH,
            mechanism=(
                "If the leader can only have a few AppendEntries in flight per "
                "follower, replication pipelining stalls under load and commit "
                "(hence produce ack) latency climbs. This would show up as "
                "replication.lag rising during the same bursts."
            ),
        ),
        Hypothesis(
            id="H3",
            title="Raft APPEND_ENTRIES_BATCH_SIZE_MAX caps replication batch size",
            target_file=RAFT_LIB_PATH,
            mechanism=(
                "A low cap on entries per AppendEntries forces many round-trips to "
                "ship a burst of entries, inflating commit latency. Like H2, this is "
                "a replication-path cause and would correlate with non-zero "
                "replication.lag."
            ),
        ),
    ]


def research_latency(
    observation: LatencyObservation,
    helix_repo: str = HELIX_REPO_DEFAULT,
) -> ResearchResult:
    """Run the experiment: probe Helix source, score hypotheses, confirm one.

    The experiment is real: it reads the current values straight out of the Helix
    source tree and combines them with the observed telemetry. The discriminating
    signal is ``replication.lag`` -- if it is ~0 during the p95 bursts, the
    replication-path hypotheses (H2/H3) are ruled out and the leader-side batcher
    (H1) is implicated.
    """
    log: list[str] = []
    hyps = _build_hypotheses()
    by_id = {h.id: h for h in hyps}

    # --- EXPERIMENT: probe the real source ---
    linger_probe = _probe_assignment(
        helix_repo,
        BATCHER_PATH,
        "batcher linger_ms default",
        r"\.unwrap_or\((\d+)\)",
    )
    inflight_probe = _probe_assignment(
        helix_repo,
        RAFT_LIB_PATH,
        "MAX_INFLIGHT_APPEND_ENTRIES",
        r"MAX_INFLIGHT_APPEND_ENTRIES:\s*u32\s*=\s*(\d+)",
    )
    batchsize_probe = _probe_assignment(
        helix_repo,
        RAFT_LIB_PATH,
        "APPEND_ENTRIES_BATCH_SIZE_MAX",
        r"APPEND_ENTRIES_BATCH_SIZE_MAX:\s*u32\s*=\s*(\d+)",
    )
    by_id["H1"].probe = linger_probe
    by_id["H2"].probe = inflight_probe
    by_id["H3"].probe = batchsize_probe
    for probe in (linger_probe, inflight_probe, batchsize_probe):
        log.append(probe.cite())

    lag_max = observation.replication_lag_max
    log.append(
        f"observed replication.lag max over window = {lag_max:.0f} "
        f"(0 => followers are NOT behind during the p95 bursts)"
    )
    log.append(
        f"observed p95 burst_ratio = {observation.burst_ratio:.1f}x "
        f"(peak {observation.p95_peak_ms:.1f}ms vs baseline "
        f"{observation.p95_baseline_ms:.2f}ms)"
    )

    # --- SCORING: combine the experiment with the telemetry ---
    # H2 / H3 are replication-path causes. They require replication.lag to climb
    # during bursts. If lag stayed at 0, the evidence rules them out regardless of
    # the configured constant.
    replication_implicated = lag_max > 0.0

    # H2
    h2 = by_id["H2"]
    if not replication_implicated:
        h2.verdict = "ruled_out"
        h2.score = 0.0
        h2.evidence.append(
            f"replication.lag stayed at {lag_max:.0f} through the bursts; a "
            f"replication-pipelining stall would have driven lag > 0. Ruled out."
        )
        if inflight_probe.found:
            h2.evidence.append(
                f"(configured {inflight_probe.cite()} -- low, but not the active "
                f"cause given zero lag.)"
            )
    else:
        h2.score = 0.6
        h2.evidence.append("replication.lag is non-zero during bursts -- plausible.")

    # H3
    h3 = by_id["H3"]
    if not replication_implicated:
        h3.verdict = "ruled_out"
        h3.score = 0.0
        h3.evidence.append(
            f"same discriminator as H2: replication.lag={lag_max:.0f} means the "
            f"replication batch cap is not the bottleneck right now. Ruled out."
        )
        if batchsize_probe.found:
            h3.evidence.append(f"(configured {batchsize_probe.cite()}.)")
    else:
        h3.score = 0.4
        h3.evidence.append("replication.lag non-zero -- batch cap could contribute.")

    # H1: leader-side batcher. Confirmed when (a) there IS a burst opportunity,
    # (b) replication is NOT the cause (lag ~0), and (c) linger_ms is small.
    h1 = by_id["H1"]
    LOW_LINGER_MS = 5  # at or below this, server-side batching is effectively off
    if linger_probe.found:
        h1.evidence.append(f"measured {linger_probe.cite()}.")
        linger_is_low = linger_probe.value is not None and linger_probe.value <= LOW_LINGER_MS
    else:
        h1.evidence.append("could not read batcher linger_ms from source.")
        linger_is_low = False

    if (
        observation.opportunity_detected
        and not replication_implicated
        and linger_is_low
    ):
        h1.verdict = "confirmed"
        h1.score = 0.95
        h1.evidence.append(
            f"p95 spikes {observation.burst_ratio:.0f}x under burst with "
            f"replication.lag=0 => the tail is on the LEADER/batcher path, not "
            f"replication. With linger_ms={linger_probe.value} the batcher flushes "
            f"near-instantly, so bursts fragment into many small Raft proposals. "
            f"CONFIRMED."
        )
    elif linger_is_low and observation.opportunity_detected:
        # Replication implicated too; H1 still plausible but not exclusive.
        h1.score = 0.5
        h1.evidence.append("linger low and burst present, but replication also implicated.")
    else:
        h1.verdict = "ruled_out"
        h1.evidence.append("no burst opportunity or linger already tuned -- ruled out.")

    confirmed = max(
        (h for h in hyps if h.verdict == "confirmed"),
        key=lambda h: h.score,
        default=None,
    )
    return ResearchResult(
        observation=observation,
        hypotheses=hyps,
        confirmed=confirmed,
        experiment_log=log,
    )
