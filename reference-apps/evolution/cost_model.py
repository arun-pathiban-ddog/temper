"""Stage 0 of the fitness cascade: a cheap, instant cost-model gate (ADR-0095).

Before spending a (slow) Stage-1 bench on a proposed mutation, the harness asks:
does this gene change *plausibly* move the lineage's FitnessGoal in the right
direction? This is a transparent heuristic over the known monotone effects of
each gene — not a measurement. Its job is only to reject obviously-wrong moves so
we don't waste a bench, and to rank candidate mutations.

Known monotone effects (the gene set):
  - linger_ms          ↑ => throughput ↑, p95_latency ↑   (the classic linger tradeoff)
  - max_inflight       ↑ => throughput ↑   (more replication pipelining)
  - append_batch_size  ↑ => throughput ↑   (fewer round-trips)
  - sync_on_rotation   false => latency ↓ (skips fsync) BUT lethal to durability

A FitnessGoal is {metric, direction}. We score a candidate by whether its gene
delta pushes `metric` in `direction`. A positive score means "worth benching."
"""

from __future__ import annotations

from dataclasses import dataclass

from genome import Genome

# Sign of each gene's effect on each metric. +1 means "raising the gene raises
# the metric". throughput and p95_latency are the two metrics the goals use.
_EFFECT = {
    # gene -> {metric -> sign of d(metric)/d(gene_up)}
    "linger_ms": {"throughput": +1, "p95_latency": +1},
    "max_inflight": {"throughput": +1, "p95_latency": 0},
    # A larger replication batch cap lets more entries queue per AppendEntries,
    # which raises commit (hence produce-ack) tail latency under load. So raising
    # it raises p95; lowering it trims the tail. A safe latency lever.
    "append_batch_size": {"throughput": +1, "p95_latency": +1},
    # sync_on_rotation is a bool; "raising" it (false->true) RESTORES fsync, which
    # raises latency. Lowering it (true->false) cuts latency but is lethal.
    "sync_on_rotation": {"throughput": 0, "p95_latency": +1},
}

# Direction multiplier: we want the metric to go this way.
_DIR_SIGN = {"minimize": -1, "maximize": +1}

METRICS = ("throughput", "p95_latency")
DIRECTIONS = ("minimize", "maximize")


@dataclass
class CostVerdict:
    """Stage-0 result for one candidate mutation."""

    gene: str
    old_value: object
    new_value: object
    score: float           # >0 == plausibly helps the goal; <=0 == reject/neutral
    lethal_risk: bool      # True if this candidate is the known-lethal move
    rationale: str

    def passes(self) -> bool:
        return self.score > 0.0


def _gene_delta_sign(old, new) -> int:
    """+1 if the gene went up, -1 if down, 0 if unchanged. Bools: True=1, False=0."""
    o = int(old) if not isinstance(old, bool) else (1 if old else 0)
    n = int(new) if not isinstance(new, bool) else (1 if new else 0)
    if n > o:
        return +1
    if n < o:
        return -1
    return 0


def score_candidate(
    current: Genome, gene: str, new_value, metric: str, direction: str
) -> CostVerdict:
    """Score whether changing `gene` to `new_value` helps {metric, direction}."""
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric!r}; metrics are {METRICS}")
    if direction not in DIRECTIONS:
        raise ValueError(f"unknown direction {direction!r}; directions are {DIRECTIONS}")

    old_value = getattr(current, gene)
    delta = _gene_delta_sign(old_value, new_value)
    effect = _EFFECT[gene].get(metric, 0)
    want = _DIR_SIGN[direction]

    # Does the gene move push the metric the way we want?
    #   metric_change_sign = delta * effect
    #   aligned if metric_change_sign has the same sign as `want`
    metric_change = delta * effect
    score = float(metric_change * want)  # +1 aligned, -1 opposed, 0 neutral

    lethal_risk = gene == "sync_on_rotation" and new_value is False

    if delta == 0:
        rationale = f"{gene} unchanged ({old_value}); no effect."
    elif effect == 0:
        rationale = (
            f"{gene} {'↑' if delta > 0 else '↓'} has no modeled effect on {metric}; "
            f"neutral."
        )
    elif score > 0:
        rationale = (
            f"{gene} {'↑' if delta > 0 else '↓'} ({old_value}→{new_value}) pushes "
            f"{metric} {'↑' if metric_change > 0 else '↓'}, which {direction}s it. "
            f"Worth benching."
        )
    else:
        rationale = (
            f"{gene} {'↑' if delta > 0 else '↓'} pushes {metric} the WRONG way for "
            f"a {direction} goal. Reject before benching."
        )
    if lethal_risk:
        rationale += (
            " WARNING: sync_on_rotation=false is the known-lethal durability "
            "shortcut; Stage 2 will cull it."
        )

    return CostVerdict(
        gene=gene,
        old_value=old_value,
        new_value=new_value,
        score=score,
        lethal_risk=lethal_risk,
        rationale=rationale,
    )
