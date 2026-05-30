"""OBSERVE phase: read live Helix produce-latency metrics and detect tuning opportunities.

The hero metric is ``helix.produce.latency_ms.95percentile`` from the Helix
cluster on GKE (Datadog org ``gensim.datadoghq.com``, dashboard ``t8q-ntj-v83``).

Two metric sources are provided so the same detection logic runs in two modes:

* :class:`DatadogMetricSource` -- talks to the Datadog v1 query API directly using
  ``DD_API_KEY``/``DD_APP_KEY``. This is the production path (e.g. running inside a
  job with API keys mounted).

* :class:`SnapshotMetricSource` -- replays a JSON snapshot of timeseries that were
  pulled live via the Datadog MCP tool. This is the agent-driven path: the Claude
  agent fetches the real series through MCP, writes it to a snapshot, and the
  observer reasons over those real numbers. No fabricated data -- a snapshot is a
  byte-for-byte capture of a live MCP query response.

Both sources return the same shape, so detection is source-agnostic.
"""

from __future__ import annotations

import json
import os
import statistics
import urllib.request
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Protocol


# The hero metric and its companions. Tags ``{*}`` -> whole cluster.
PRODUCE_P95 = "helix.produce.latency_ms.95percentile"
PRODUCE_AVG = "helix.produce.latency_ms.avg"
PRODUCE_MEDIAN = "helix.produce.latency_ms.median"
PRODUCE_MAX = "helix.produce.latency_ms.max"
PRODUCE_COUNT = "helix.produce.latency_ms.count"
REPLICATION_LAG = "helix.replication.lag"

# Detection thresholds. These are budgets, not hard limits: a tuning opportunity
# exists when the p95 *peak* runs well above the *baseline* (steady-state) p95,
# which is the signature of a latency-vs-throughput tradeoff under load bursts.
BASELINE_P95_MS_NOMINAL = 5.0
BURST_RATIO_THRESHOLD = 3.0  # peak/baseline ratio that flags an opportunity
ABSOLUTE_BURST_MS = 50.0     # any p95 bucket above this is a clear burst


class MetricSource(Protocol):
    """A source of Datadog timeseries data for one metric query."""

    def query_series(self, query: str, from_iso: str, to_iso: str) -> list[float]:
        """Return the data points for ``query`` over the window, NaNs dropped."""
        ...


@dataclass
class SnapshotMetricSource:
    """Replays real timeseries captured from a live Datadog MCP query.

    The snapshot maps a Datadog query string to its list of point values. This is
    the agent-driven live path: every number here came from an actual MCP
    ``get_datadog_metric`` call against the Helix cluster.
    """

    series: dict[str, list[float]]
    captured_at: str = "unknown"
    window: str = "unknown"

    @classmethod
    def from_file(cls, path: str) -> "SnapshotMetricSource":
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return cls(
            series={k: [float(x) for x in v] for k, v in payload["series"].items()},
            captured_at=payload.get("captured_at", "unknown"),
            window=payload.get("window", "unknown"),
        )

    def query_series(self, query: str, from_iso: str, to_iso: str) -> list[float]:
        if query not in self.series:
            raise KeyError(
                f"snapshot has no series for query {query!r}; "
                f"available: {sorted(self.series)}"
            )
        return [v for v in self.series[query] if v == v]  # drop NaN


@dataclass
class DatadogMetricSource:
    """Queries the Datadog v1 timeseries API directly.

    Used when ``DD_API_KEY`` and ``DD_APP_KEY`` are present (the production /
    GKE-job path). Read-only: only issues GET ``/api/v1/query``.
    """

    api_key: str
    app_key: str
    site: str = "datadoghq.com"

    @classmethod
    def from_env(cls) -> "DatadogMetricSource | None":
        api_key = os.environ.get("DD_API_KEY") or os.environ.get("DATADOG_API_KEY")
        app_key = os.environ.get("DD_APP_KEY") or os.environ.get("DATADOG_APP_KEY")
        if not api_key or not app_key:
            return None
        site = os.environ.get("DD_SITE", "datadoghq.com")
        return cls(api_key=api_key, app_key=app_key, site=site)

    @staticmethod
    def _as_query(metric_or_query: str) -> str:
        """Wrap a bare metric name into a valid Datadog v1 query.

        The metric constants (e.g. ``helix.produce.latency_ms.95percentile``) are
        bare names — valid as snapshot keys but NOT valid query expressions: the
        v1 query API requires a scope and a space aggregator (``avg:<metric>{*}``).
        Pass through anything that already looks like a query (has ``:`` or ``{``).
        """
        q = metric_or_query.strip()
        if "{" in q or ":" in q:
            return q
        return f"avg:{q}{{*}}"

    def query_series(self, query: str, from_iso: str, to_iso: str) -> list[float]:
        from_ts = _iso_to_epoch(from_iso)
        to_ts = _iso_to_epoch(to_iso)
        query = self._as_query(query)
        params = urllib.parse.urlencode(
            {"query": query, "from": from_ts, "to": to_ts}
        )
        url = f"https://api.{self.site}/api/v1/query?{params}"
        req = urllib.request.Request(url, method="GET")
        req.add_header("DD-API-KEY", self.api_key)
        req.add_header("DD-APPLICATION-KEY", self.app_key)
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        points: list[float] = []
        for series in body.get("series", []):
            for _, value in series.get("pointlist", []):
                if value is not None:
                    points.append(float(value))
        return points


@dataclass
class LatencyObservation:
    """The result of the OBSERVE phase -- real numbers, no interpretation yet."""

    p95_baseline_ms: float
    p95_peak_ms: float
    p95_mean_ms: float
    avg_baseline_ms: float
    avg_peak_ms: float
    max_peak_ms: float
    total_produces: int
    replication_lag_max: float
    burst_ratio: float
    opportunity_detected: bool
    window: str
    captured_at: str
    raw: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        verdict = (
            "TUNING OPPORTUNITY DETECTED"
            if self.opportunity_detected
            else "no opportunity (latency steady)"
        )
        return (
            f"[OBSERVE] Helix produce latency over {self.window} "
            f"(captured {self.captured_at})\n"
            f"  p95:  baseline={self.p95_baseline_ms:.2f}ms  "
            f"peak={self.p95_peak_ms:.2f}ms  mean={self.p95_mean_ms:.2f}ms  "
            f"burst_ratio={self.burst_ratio:.1f}x\n"
            f"  avg:  baseline={self.avg_baseline_ms:.2f}ms  "
            f"peak={self.avg_peak_ms:.2f}ms\n"
            f"  max peak: {self.max_peak_ms:.1f}ms   "
            f"replication.lag max: {self.replication_lag_max:.0f}\n"
            f"  throughput: {self.total_produces:,} produces in window\n"
            f"  => {verdict}"
        )


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    frac = k - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def observe_produce_latency(
    source: MetricSource,
    from_iso: str = "now-1h",
    to_iso: str = "now",
    window_label: str = "last 1h",
    captured_at: str = "unknown",
) -> LatencyObservation:
    """Pull the live produce-latency series and decide if there's an opportunity.

    Detection logic: the *baseline* p95 is the low quantile of the per-bucket p95
    series (steady state); the *peak* is the max bucket. A latency-vs-throughput
    tuning opportunity exists when the peak runs >= ``BURST_RATIO_THRESHOLD`` over
    the baseline, or any bucket exceeds ``ABSOLUTE_BURST_MS``. The system is under
    real, bursty produce load, so the tail is what matters.
    """
    p95 = source.query_series(PRODUCE_P95, from_iso, to_iso)
    avg = source.query_series(PRODUCE_AVG, from_iso, to_iso)
    mx = source.query_series(PRODUCE_MAX, from_iso, to_iso)
    counts = source.query_series(PRODUCE_COUNT, from_iso, to_iso)
    try:
        lag = source.query_series(REPLICATION_LAG, from_iso, to_iso)
    except KeyError:
        lag = [0.0]

    if not p95:
        raise RuntimeError("no p95 produce-latency data returned -- is the cluster live?")

    # Baseline = 10th percentile of the p95 buckets (steady state, ignoring bursts).
    p95_baseline = _percentile(p95, 10.0)
    p95_peak = max(p95)
    p95_mean = statistics.fmean(p95)
    avg_baseline = _percentile(avg, 10.0) if avg else 0.0
    avg_peak = max(avg) if avg else 0.0
    max_peak = max(mx) if mx else 0.0
    total = int(round(sum(counts))) if counts else 0
    lag_max = max(lag) if lag else 0.0

    burst_ratio = (p95_peak / p95_baseline) if p95_baseline > 0 else 0.0
    opportunity = burst_ratio >= BURST_RATIO_THRESHOLD or p95_peak >= ABSOLUTE_BURST_MS

    return LatencyObservation(
        p95_baseline_ms=p95_baseline,
        p95_peak_ms=p95_peak,
        p95_mean_ms=p95_mean,
        avg_baseline_ms=avg_baseline,
        avg_peak_ms=avg_peak,
        max_peak_ms=max_peak,
        total_produces=total,
        replication_lag_max=lag_max,
        burst_ratio=burst_ratio,
        opportunity_detected=opportunity,
        window=window_label,
        captured_at=captured_at,
        raw={
            "p95": p95,
            "avg": avg,
            "max": mx,
            "count": counts,
            "replication_lag": lag,
        },
    )


def _iso_to_epoch(value: str) -> int:
    """Convert ISO8601 or a relative ``now-1h`` token to an epoch-second int."""
    import time

    if value == "now":
        return int(time.time())
    if value.startswith("now-"):
        token = value[4:]
        unit = token[-1]
        amount = int(token[:-1])
        scale = {"m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
        return int(time.time()) - amount * scale
    # Assume ISO8601.
    import datetime

    dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(dt.timestamp())
