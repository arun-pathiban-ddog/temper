"""Tests for the OBSERVE + RESEARCH phases (no Temper dependency).

These exercise the autoresearch logic against:
  * the real captured live snapshot (burst present, replication.lag == 0),
  * a synthetic steady-state series (no opportunity),
  * a synthetic replication-stall series (lag > 0 -> different hypothesis wins).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from observer.observe import (  # noqa: E402
    SnapshotMetricSource,
    observe_produce_latency,
    PRODUCE_P95,
    PRODUCE_AVG,
    PRODUCE_MEDIAN,
    PRODUCE_MAX,
    PRODUCE_COUNT,
    REPLICATION_LAG,
)
from observer.research import research_latency  # noqa: E402

HELIX_REPO = "/Users/arun.parthiban/notdd/helix"
SNAPSHOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "snapshots",
    "helix_live_2026-05-24.json",
)


def _steady_source() -> SnapshotMetricSource:
    n = 30
    return SnapshotMetricSource(
        series={
            PRODUCE_P95: [5.0] * n,
            PRODUCE_AVG: [3.7] * n,
            PRODUCE_MEDIAN: [3.6] * n,
            PRODUCE_MAX: [9.0] * n,
            PRODUCE_COUNT: [2000.0] * n,
            REPLICATION_LAG: [0.0] * n,
        }
    )


def _replication_stall_source() -> SnapshotMetricSource:
    # bursty p95 AND non-zero replication lag during the bursts
    p95 = [5.0] * 10 + [120.0, 200.0, 90.0] + [5.0] * 10
    lag = [0.0] * 10 + [800.0, 1200.0, 600.0] + [0.0] * 10
    n = len(p95)
    return SnapshotMetricSource(
        series={
            PRODUCE_P95: p95,
            PRODUCE_AVG: [4.0] * n,
            PRODUCE_MEDIAN: [3.6] * n,
            PRODUCE_MAX: [400.0] * n,
            PRODUCE_COUNT: [2000.0] * n,
            REPLICATION_LAG: lag,
        }
    )


def test_live_snapshot_detects_opportunity():
    src = SnapshotMetricSource.from_file(SNAPSHOT)
    obs = observe_produce_latency(src, window_label=src.window, captured_at=src.captured_at)
    assert obs.opportunity_detected is True
    # baseline is steady ~5ms; peak is a real burst > 200ms
    assert obs.p95_baseline_ms < 6.0
    assert obs.p95_peak_ms > 100.0
    assert obs.burst_ratio > 10.0
    assert obs.replication_lag_max == 0.0
    assert obs.total_produces > 100000  # sustained real throughput


def test_live_snapshot_confirms_batcher_hypothesis():
    src = SnapshotMetricSource.from_file(SNAPSHOT)
    obs = observe_produce_latency(src)
    res = research_latency(obs, helix_repo=HELIX_REPO)
    assert res.confirmed is not None, "expected a confirmed hypothesis"
    assert res.confirmed.id == "H1", "batcher linger should be confirmed"
    # the two replication-path hypotheses must be ruled out by lag==0
    ruled = {h.id for h in res.hypotheses if h.verdict == "ruled_out"}
    assert {"H2", "H3"}.issubset(ruled)
    # the confirmed hypothesis must carry a real source probe value
    assert res.confirmed.probe is not None and res.confirmed.probe.found
    assert res.confirmed.probe.value is not None


def test_steady_state_no_opportunity():
    obs = observe_produce_latency(_steady_source())
    assert obs.opportunity_detected is False
    assert obs.burst_ratio < 3.0


def test_replication_stall_rules_out_batcher():
    obs = observe_produce_latency(_replication_stall_source())
    assert obs.opportunity_detected is True
    assert obs.replication_lag_max > 0.0
    res = research_latency(obs, helix_repo=HELIX_REPO)
    # With non-zero lag, the leader-side batcher (H1) must NOT be the exclusive
    # confirmed cause -- the discriminator flips. H2/H3 are no longer ruled out.
    ruled = {h.id for h in res.hypotheses if h.verdict == "ruled_out"}
    assert "H2" not in ruled
    assert "H3" not in ruled
    # H1 should not be confirmed when replication is implicated
    assert not (res.confirmed and res.confirmed.id == "H1")


def test_probes_read_real_source():
    obs = observe_produce_latency(SnapshotMetricSource.from_file(SNAPSHOT))
    res = research_latency(obs, helix_repo=HELIX_REPO)
    probes = {h.id: h.probe for h in res.hypotheses}
    # batcher linger default
    assert probes["H1"].found and probes["H1"].value == 1
    # raft constants
    assert probes["H2"].found and probes["H2"].value == 5
    assert probes["H3"].found and probes["H3"].value == 1000


if __name__ == "__main__":
    import traceback

    tests = [
        test_live_snapshot_detects_opportunity,
        test_live_snapshot_confirms_batcher_hypothesis,
        test_steady_state_no_opportunity,
        test_replication_stall_rules_out_batcher,
        test_probes_read_real_source,
    ]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
