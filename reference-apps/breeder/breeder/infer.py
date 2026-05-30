"""Telemetry-only workload-niche inference (ADR-0099).

Reads Datadog metrics tagged by `queue:` / `role:` and classifies each queue into
a workload niche, then maps the niche to a candidate gene + FitnessGoal
direction. NO Temper access — the breeder cannot read the workload spec (Cedar
forbids it); everything here comes from telemetry.

Reuses observer.observe.MetricSource (the Datadog v1 query API or a captured
snapshot) so the live and offline paths share one abstraction.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Reuse the observer's read-only Datadog source abstraction (Datadog v1 API /
# snapshot). This is the ONLY external data dependency — no Temper import.
_OBSERVER = Path(__file__).resolve().parents[2] / "observer"
sys.path.insert(0, str(_OBSERVER))
from observer.observe import DatadogMetricSource, SnapshotMetricSource  # noqa: E402


# Niche -> the concrete Helix genome that optimizes it, plus the fitness metric /
# direction the breeder proposes. `genome` is a dict of real Helix tuning knobs
# (genome.py: linger_ms / max_inflight / append_batch_size / sync_on_rotation)
# relative to the baseline (linger_ms=1, max_inflight=5, append_batch_size=1000,
# sync_on_rotation=true). The speciation agent commits these to df-niche/<niche>
# and builds the niche-optimized image. Classification is from TELEMETRY ONLY.
NICHE_PLAYBOOK = {
    "high-throughput-batch": {
        "genome": {"linger_ms": 50, "append_batch_size": 4000},
        "gene": "linger_ms",
        "metric": "throughput",
        "direction": "maximize",
        "rationale": "high sustained produce rate, latency-tolerant -> coalesce appends "
                     "(raise linger_ms + larger append batches) to maximize throughput",
    },
    "bursty": {
        "genome": {"linger_ms": 20, "max_inflight": 8},
        "gene": "linger_ms",
        "metric": "p95_latency",
        "direction": "minimize",
        "rationale": "spiky produce load (high burst ratio) -> moderate linger to absorb "
                     "bursts + more in-flight appends so spikes don't queue at the tail",
    },
    "low-latency": {
        "genome": {"linger_ms": 0, "append_batch_size": 64},
        "gene": "append_batch_size",
        "metric": "p95_latency",
        "direction": "minimize",
        "rationale": "steady, latency-sensitive traffic -> no linger, tiny append batches "
                     "to minimize p95 produce latency",
    },
    "steady": {
        "genome": {},  # baseline genome — isolation alone is the win; no retune
        "gene": "linger_ms",
        "metric": "cost_efficiency",
        "direction": "maximize",
        "rationale": "modest steady load -> isolate onto its own cluster at baseline tuning; "
                     "no retune needed (cost efficiency)",
    },
}


# Time-series signal thresholds (telemetry-derived, explainable). A workload is
# "bursty" when its produce-rate is highly variable (rate CV) or its tail latency
# spikes above its steady-state baseline (burst_ratio). Produce latency in Helix
# is often sub-millisecond and unmeasurable live, so RATE magnitude + RATE CV are
# the primary discriminators; latency signals refine when present.
BURST_RATIO_THRESHOLD = 2.5      # p95_peak / p95_baseline above this = bursty (when latency is measurable)
RATE_CV_THRESHOLD = 0.5          # coeff. of variation of produce-rate above this = bursty
HIGH_THROUGHPUT_RATE = 200.0     # msgs/sec sustained = high-throughput regime
MID_THROUGHPUT_RATE = 40.0       # msgs/sec above this (but below high) = steady service traffic
LOW_LATENCY_P95_MS = 12.0        # steady p95 produce latency at/below this = latency-sensitive
# A burst_ratio computed on a signal whose peak is below this is treated as noise
# (e.g. sub-millisecond produce latency that's mostly 0.0). Units are the series'
# own units; 1.0 suppresses latency quantization noise without affecting rate
# series (whose peaks are in the tens-to-hundreds).
NEGLIGIBLE_MAGNITUDE = 1.0


@dataclass
class QueueTelemetry:
    queue: str
    produce_rate: float          # msgs/sec produced (mean over window)
    produce_latency_ms: float    # local produce()-call latency (mean over window)
    consume_rate: float          # msgs/sec consumed
    fanout: float                # consumers per producer (>=1 means fan-out)
    # Time-series signals — what separates bursty from steady from batch.
    p95_latency_ms: float = 0.0  # p95 of the produce-latency buckets
    burst_ratio: float = 0.0     # p95_peak / p95_baseline of latency buckets
    rate_cv: float = 0.0         # stddev/mean of the produce-rate buckets
    lag_slope: float = 0.0       # per-bucket slope of consumer lag (growth = backlog)


@dataclass
class NicheInference:
    queue: str
    niche: str
    confidence: str              # "high" | "medium" | "low" (heuristic strength)
    gene: str
    metric: str
    direction: str
    genome: dict                 # concrete Helix tuning knobs for this niche
    motivation: str              # human-readable, telemetry-derived (no spec)
    telemetry: dict


def _series_stats(pts: list[float]) -> dict:
    """Summarize a metric series into the signals classification keys off.

    All telemetry-derived: mean, p50/p95, peak (p95-of-buckets max proxy),
    baseline (10th pct steady state), coefficient of variation, burst ratio.
    """
    if not pts:
        return {"mean": 0.0, "p50": 0.0, "p95": 0.0, "peak": 0.0,
                "baseline": 0.0, "cv": 0.0, "burst_ratio": 0.0}
    n = len(pts)
    mean = sum(pts) / n
    var = sum((x - mean) ** 2 for x in pts) / n
    stddev = var ** 0.5
    cv = (stddev / mean) if mean > 0 else 0.0
    p50 = _percentile(pts, 50)
    p95 = _percentile(pts, 95)
    peak = max(pts)
    # Baseline = steady-state floor (10th pct), but floored at a fraction of the
    # mean so a single near-zero trough (or a trickle workload that dips to 0)
    # doesn't make burst_ratio explode. Burstiness is peak-vs-typical, not
    # peak-vs-zero.
    baseline = max(_percentile(pts, 10), 0.25 * mean) if mean > 0 else 0.0
    # A burst_ratio on a near-zero-magnitude signal is just quantization noise
    # (e.g. sub-millisecond produce latency that's mostly 0.0 with rare 0.1
    # blips). Only report a burst_ratio when the signal has real magnitude.
    burst_ratio = (peak / baseline) if (baseline > 0 and peak >= NEGLIGIBLE_MAGNITUDE) else 0.0
    return {"mean": mean, "p50": p50, "p95": p95, "peak": peak,
            "baseline": baseline, "cv": cv, "burst_ratio": burst_ratio}


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile (mirrors observer.observe._percentile)."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    rank = (pct / 100.0) * (len(s) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(s) - 1)
    frac = rank - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def _slope(pts: list[float]) -> float:
    """Least-squares slope over evenly-spaced buckets (lag growth detector)."""
    n = len(pts)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(pts) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    return sum((xs[i] - mx) * (pts[i] - my) for i in range(n)) / denom


def _q(metric: str, queue: str, agg: str = "sum", rate: bool = False) -> str:
    """Build a Datadog query scoped to one queue by its telemetry tag."""
    expr = f"{agg}:{metric}{{queue:{queue}}}"
    return expr + (".as_rate()" if rate else "")


def discover_queues(src) -> list[str]:
    """Discover queue names FROM TELEMETRY (the queue: tag), never from Temper.

    The Datadog v1 query API doesn't list tag values directly here, so we read a
    broad series and rely on the snapshot/source exposing per-queue series. For
    the snapshot source we read the queue list it captured; for live we accept a
    --queues hint (still telemetry-sourced, e.g. from a tag-values query upstream).
    """
    qs = getattr(src, "queues", None)
    if qs:
        return list(qs)
    return []


def classify(t: QueueTelemetry) -> NicheInference:
    """Pure heuristic classifier over telemetry time-series — explainable, no spec.

    Order matters: burstiness is checked before the steady/latency split because a
    bursty low-latency workload still wants the burst-absorbing genome.
    """
    bursty = t.burst_ratio >= BURST_RATIO_THRESHOLD or t.rate_cv >= RATE_CV_THRESHOLD
    # "Have latency" only when the p95 is above the noise floor. Sub-millisecond
    # produce latency (mostly 0.0 with rare 0.1 blips) is not a usable signal, so
    # we don't let it force a low-latency classification — fall back to rate.
    have_latency = t.p95_latency_ms >= NEGLIGIBLE_MAGNITUDE

    if bursty:
        # Spiky load — variance dominates regardless of magnitude.
        niche = "bursty"
        conf = "high" if t.rate_cv >= RATE_CV_THRESHOLD * 1.5 else "medium"
    elif t.produce_rate >= HIGH_THROUGHPUT_RATE and (not have_latency or t.p95_latency_ms > LOW_LATENCY_P95_MS):
        # High sustained, low-variance rate, latency-tolerant -> batch regime.
        niche, conf = "high-throughput-batch", "high"
    elif t.produce_rate >= MID_THROUGHPUT_RATE:
        # Steady, mid-to-high rate that isn't bursty or batch-scale. This is
        # latency-sensitive service traffic (a tight measured p95 confirms it).
        niche = "low-latency"
        conf = "high" if (have_latency and t.p95_latency_ms <= LOW_LATENCY_P95_MS) else "medium"
    else:
        # Low rate, low variance -> steady trickle.
        niche, conf = "steady", "medium"

    play = NICHE_PLAYBOOK[niche]
    motivation = (
        f"telemetry: {t.produce_rate:.0f} msg/s produced, p95 produce-latency "
        f"{t.p95_latency_ms:.0f}ms, burst-ratio {t.burst_ratio:.1f}x, rate CV "
        f"{t.rate_cv:.2f}, lag-slope {t.lag_slope:+.1f}/bucket -> niche '{niche}'. "
        f"{play['rationale']}."
    )
    return NicheInference(
        queue=t.queue, niche=niche, confidence=conf,
        gene=play["gene"], metric=play["metric"], direction=play["direction"],
        genome=dict(play["genome"]), motivation=motivation, telemetry=asdict(t),
    )


def read_queue_telemetry(src, queue: str, from_iso: str, to_iso: str) -> QueueTelemetry:
    def series(series_query: str) -> list[float]:
        try:
            return src.query_series(series_query, from_iso, to_iso) or []
        except KeyError:
            return []

    def first_nonempty(*queries: str) -> list[float]:
        """First query that returns data. DogStatsD HISTOGRAMS land in Datadog as
        sub-metrics (.avg/.95percentile/.max/...), so the raw name returns nothing
        live — try the sub-metric, then fall back to the raw name (snapshots use
        the raw name)."""
        for qy in queries:
            pts = series(qy)
            if pts:
                return pts
        return []

    def mean(pts: list[float], default: float = 0.0) -> float:
        return (sum(pts) / len(pts)) if pts else default

    rate_pts = series(_q("workload.producer.sent", queue, "sum", rate=True))
    # send_latency_ms is a histogram -> query its .avg / .95percentile sub-metrics
    # live; fall back to the raw name for snapshot fixtures.
    lat_avg_pts = first_nonempty(
        _q("workload.producer.send_latency_ms.avg", queue, "avg"),
        _q("workload.producer.send_latency_ms", queue, "avg"),
    )
    lat_p95_pts = first_nonempty(
        _q("workload.producer.send_latency_ms.95percentile", queue, "avg"),
        _q("workload.producer.send_latency_ms", queue, "avg"),
    )
    consume_pts = series(_q("workload.consumer.consumed", queue, "sum", rate=True))
    lag_pts = first_nonempty(
        _q("workload.consumer.lag", queue, "avg"),
        _q("workload.consumer.lag.avg", queue, "avg"),
    )

    rate_stats = _series_stats(rate_pts)
    lat_avg_stats = _series_stats(lat_avg_pts)
    lat_p95_stats = _series_stats(lat_p95_pts)

    produce_rate = rate_stats["mean"]
    produce_lat = lat_avg_stats["mean"]
    consume_rate = mean(consume_pts)
    # fan-out: distinct consumer count / producer count, from the per-queue fanout
    # metric when present, else derived from the consume/produce rate ratio.
    fanout = mean(series(_q("workload.consumer.fanout", queue, "max"))) or (
        2.0 if consume_rate > produce_rate * 1.5 else 1.0
    )
    # Burstiness signal. The per-bucket RATE burst_ratio (peak/baseline) is noisy
    # live: a DogStatsD count rolled up to a 1-min rate has sawtooth flush
    # artifacts that inflate peak/baseline even for steady producers. So we DON'T
    # trip on the rate burst_ratio; we rely on the rate coefficient-of-variation
    # (rate_cv, robust to flush noise) for throughput burstiness, and keep the
    # LATENCY burst_ratio (a genuine tail-latency spike is meaningful) as the
    # explicit burst_ratio signal.
    burst_ratio = lat_p95_stats["burst_ratio"]
    return QueueTelemetry(
        queue=queue,
        produce_rate=produce_rate,
        produce_latency_ms=produce_lat,
        consume_rate=consume_rate,
        fanout=fanout,
        p95_latency_ms=lat_p95_stats["p95"] or lat_avg_stats["p95"],
        burst_ratio=burst_ratio,
        rate_cv=rate_stats["cv"],
        lag_slope=_slope(lag_pts),
    )


def infer_all(src, queues: list[str], window_min: int = 30) -> list[NicheInference]:
    now = datetime.now(timezone.utc)
    to_iso = now.isoformat()
    from_iso = (now - timedelta(minutes=window_min)).isoformat()
    out = []
    for q in queues:
        t = read_queue_telemetry(src, q, from_iso, to_iso)
        out.append(classify(t))
    return out


def _build_source(args):
    if args.source == "snapshot":
        src = SnapshotMetricSource.from_file(args.snapshot)
        # snapshot may carry the queue list it captured
        try:
            payload = json.load(open(args.snapshot))
            setattr(src, "queues", payload.get("queues", []))
        except (OSError, ValueError):
            pass
        return src
    src = DatadogMetricSource.from_env()
    if src is None:
        print("ERROR: --source datadog needs DD_API_KEY and DD_APP_KEY in env.", file=sys.stderr)
        sys.exit(2)
    setattr(src, "queues", [q for q in (args.queues or "").split(",") if q])
    return src


def main(argv=None):
    ap = argparse.ArgumentParser(description="Telemetry-only workload-niche inference (breeder).")
    ap.add_argument("--source", choices=["snapshot", "datadog"], default="snapshot")
    ap.add_argument("--snapshot", default=str(Path(__file__).resolve().parents[1] / "snapshots" / "niches.json"))
    ap.add_argument("--queues", default="", help="comma queue list (live mode; telemetry-sourced)")
    ap.add_argument("--window", default="30m")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args(argv)

    window_min = int(args.window.rstrip("m")) if args.window.endswith("m") else 30
    src = _build_source(args)
    queues = discover_queues(src)
    if not queues:
        print("No queues discoverable from telemetry "
              "(snapshot has no 'queues' / no --queues hint).", file=sys.stderr)
        return 1
    results = infer_all(src, queues, window_min)

    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
    else:
        print("Telemetry-inferred workload niches (NO Temper read):\n")
        for r in results:
            print(f"  queue={r.queue:14s} niche={r.niche:32s} conf={r.confidence}")
            print(f"      -> gene {r.gene} / {r.metric} {r.direction}")
            print(f"      {r.motivation}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
