"""Fitness measurement via Datadog metrics for the live evolution loop.

Queries helix.produce.latency_ms.95percentile and helix.produce.throughput
for a specific cluster, identified by the `cluster:<tag>` DogStatsD tag.

The cluster DD tag is set by helix-server at startup:
    --cluster-id=helix-<cluster_name>
resulting in DogStatsD tag: `cluster:helix-<cluster_name>`
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from config import LiveEvolutionConfig


@dataclass
class FitnessResult:
    """Measured fitness for a single cluster."""

    cluster_tag: str
    latency_p95_ms: float | None       # helix.produce.latency_ms p95 (ms); None = no data
    throughput_rps: float | None       # helix.produce.throughput (msgs/sec); None = no data
    data_points: int = 0               # total metric points found (data quality indicator)
    error: str = ""

    @property
    def is_valid(self) -> bool:
        return self.latency_p95_ms is not None or self.throughput_rps is not None

    def pct_improvement_latency(self, baseline: "FitnessResult") -> float | None:
        """Negative = better (lower latency). Returns None if either is missing."""
        if self.latency_p95_ms is None or baseline.latency_p95_ms is None:
            return None
        if baseline.latency_p95_ms == 0:
            return None
        return (baseline.latency_p95_ms - self.latency_p95_ms) / baseline.latency_p95_ms * 100

    def pct_improvement_throughput(self, baseline: "FitnessResult") -> float | None:
        """Positive = better (higher throughput). Returns None if either is missing."""
        if self.throughput_rps is None or baseline.throughput_rps is None:
            return None
        if baseline.throughput_rps == 0:
            return None
        return (self.throughput_rps - baseline.throughput_rps) / baseline.throughput_rps * 100

    def summary(self) -> str:
        lat = f"{self.latency_p95_ms:.1f}ms" if self.latency_p95_ms else "N/A"
        thr = f"{self.throughput_rps:.0f}rps" if self.throughput_rps else "N/A"
        return f"p95_latency={lat} throughput={thr} points={self.data_points}"


class DatadogMetrics:
    """Thin Datadog Metrics API v1 client."""

    def __init__(self, api_key: str, app_key: str, site: str = "datadoghq.com"):
        self.api_key = api_key
        self.app_key = app_key
        self.site = site.rstrip("/")
        self._base = f"https://api.{self.site}/api/v1"

    def _get(self, path: str, params: dict) -> dict:
        qs = urllib.parse.urlencode(params)
        url = f"{self._base}{path}?{qs}"
        req = urllib.request.Request(url, headers={
            "DD-API-KEY": self.api_key,
            "DD-APPLICATION-KEY": self.app_key,
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            raise RuntimeError(f"DD API HTTP {exc.code}: {raw[:300]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"DD API unreachable: {exc.reason}") from exc

    def query(
        self,
        metric_query: str,
        from_ts: int,
        to_ts: int,
        aggregator: str = "avg",
    ) -> list[tuple[int, float]]:
        """Query a metric and return list of (timestamp, value) tuples."""
        result = self._get("/query", {
            "from": from_ts,
            "to": to_ts,
            "query": f"{aggregator}:{metric_query}",
        })
        series = result.get("series", [])
        if not series:
            return []
        points: list[tuple[int, float]] = []
        for s in series:
            for pt in (s.get("pointlist") or []):
                if len(pt) == 2 and pt[1] is not None:
                    points.append((int(pt[0]), float(pt[1])))
        return sorted(points, key=lambda x: x[0])


# ---------------------------------------------------------------------------
# High-level fitness measurement
# ---------------------------------------------------------------------------

def _dd_client(config: LiveEvolutionConfig) -> DatadogMetrics:
    creds = config.load_dd_credentials()
    api_key = creds.get("DD_API_KEY", "")
    app_key = creds.get("DD_APP_KEY", "")
    site = creds.get("DD_SITE", "datadoghq.com")
    if not api_key or not app_key:
        raise RuntimeError("DD_API_KEY / DD_APP_KEY not set — check .dd-env.json")
    return DatadogMetrics(api_key=api_key, app_key=app_key, site=site)


def measure_cluster(
    config: LiveEvolutionConfig,
    cluster_dd_tag: str,
    window_minutes: int = 3,
    delay_seconds: int = 30,
) -> FitnessResult:
    """Measure latency + throughput for a cluster over a trailing window.

    The `delay_seconds` gives the agent a moment after workload start before
    sampling, so we read steady-state rather than ramp-up noise.
    """
    if delay_seconds > 0:
        print(f"  [fitness] waiting {delay_seconds}s before sampling {cluster_dd_tag}…")
        time.sleep(delay_seconds)

    dd = _dd_client(config)
    to_ts = int(time.time())
    from_ts = to_ts - window_minutes * 60

    tag_filter = f"cluster:{cluster_dd_tag}"
    lat_query = f"{config.DD_LATENCY_METRIC}{{{tag_filter}}}"
    thr_query = f"{config.DD_THROUGHPUT_METRIC}{{{tag_filter}}}"

    latency_p95: float | None = None
    throughput: float | None = None
    total_points = 0
    errors: list[str] = []

    try:
        lat_points = dd.query(lat_query, from_ts, to_ts, aggregator="avg")
        if lat_points:
            # Use the max p95 over the window (conservative — avoids rewarding
            # quiet periods that haven't actually improved tail latency).
            latency_p95 = max(v for _, v in lat_points)
            total_points += len(lat_points)
    except Exception as exc:
        errors.append(f"latency query failed: {exc}")

    try:
        thr_points = dd.query(thr_query, from_ts, to_ts, aggregator="avg")
        if thr_points:
            # Use the mean throughput over the window.
            throughput = sum(v for _, v in thr_points) / len(thr_points)
            total_points += len(thr_points)
    except Exception as exc:
        errors.append(f"throughput query failed: {exc}")

    return FitnessResult(
        cluster_tag=cluster_dd_tag,
        latency_p95_ms=latency_p95,
        throughput_rps=throughput,
        data_points=total_points,
        error="; ".join(errors) if errors else "",
    )


def measure_all_clusters(
    config: LiveEvolutionConfig,
    cluster_names: list[str],
    window_minutes: int | None = None,
) -> dict[str, FitnessResult]:
    """Measure all clusters sequentially. Returns {cluster_name: FitnessResult}."""
    win = window_minutes or config.traffic_window_minutes
    return {
        name: measure_cluster(
            config, config.variant_cluster_dd_tag(name), window_minutes=win, delay_seconds=0
        )
        for name in cluster_names
    }


def measure_champion_baseline(config: LiveEvolutionConfig) -> FitnessResult:
    """Measure the champion cluster as baseline for improvement comparison."""
    tag = config.champion_cluster_dd_tag
    return measure_cluster(config, tag, window_minutes=config.traffic_window_minutes,
                           delay_seconds=0)


def compute_delta_pct(
    variant: FitnessResult,
    baseline: FitnessResult,
    direction: str,
    metric: str,
) -> float | None:
    """Compute the improvement delta (+ = better) for the given direction."""
    if metric == "p95_latency":
        return variant.pct_improvement_latency(baseline)
    if metric == "throughput":
        return variant.pct_improvement_throughput(baseline)
    return None


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    from config import LiveEvolutionConfig

    cfg = LiveEvolutionConfig()
    print(f"[fitness_live] measuring champion baseline ({cfg.champion_cluster_dd_tag}) …")
    r = measure_champion_baseline(cfg)
    print(f"  {r.summary()}")
    if r.error:
        print(f"  errors: {r.error}")
