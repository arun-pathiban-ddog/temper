"""Observer/Researcher autonomous loop entry point (Agent-C2, Dark Factory).

Runs the three phases end-to-end:

    OBSERVE  -> RESEARCH -> DRIVE

Usage::

    # one tick against the captured live snapshot (real telemetry, no DD keys needed)
    python -m observer.main --source snapshot --snapshot snapshots/helix_live_2026-05-24.json

    # one tick against the live Datadog API (requires DD_API_KEY / DD_APP_KEY)
    python -m observer.main --source datadog

    # observe + research only, skip the Temper DRIVE phase
    python -m observer.main --source snapshot --snapshot <file> --no-drive

The DRIVE phase auto-detects whether Agent-B's ImprovementIssue spec is deployed
and degrades gracefully if not.
"""

from __future__ import annotations

import argparse
import sys

from .observe import (
    DatadogMetricSource,
    SnapshotMetricSource,
    observe_produce_latency,
)
from .research import research_latency
from .driver import build_issue_payload, drive_issue
from .temper_client import TemperClient


def _build_source(args: argparse.Namespace):
    if args.source == "datadog":
        src = DatadogMetricSource.from_env()
        if src is None:
            print(
                "ERROR: --source datadog needs DD_API_KEY and DD_APP_KEY in the env.",
                file=sys.stderr,
            )
            sys.exit(2)
        return src, "live Datadog v1 query API"
    snap = SnapshotMetricSource.from_file(args.snapshot)
    return snap, f"snapshot {args.snapshot}"


def run_once(args: argparse.Namespace) -> int:
    source, source_label = _build_source(args)
    captured = getattr(source, "captured_at", "live")
    window = getattr(source, "window", "last 1h")

    print("=" * 78)
    print(f"OBSERVER/RESEARCHER TICK  (source: {source_label})")
    print("=" * 78)

    # --- OBSERVE ---
    observation = observe_produce_latency(
        source,
        from_iso=args.from_,
        to_iso=args.to,
        window_label=window,
        captured_at=captured,
    )
    print(observation.summary())
    print()

    if not observation.opportunity_detected:
        print("No tuning opportunity this tick. Nothing to research or drive.")
        return 0

    # --- RESEARCH ---
    research = research_latency(observation, helix_repo=args.helix_repo)
    print(research.summary())
    print()

    if research.confirmed is None:
        print("Research did not confirm a hypothesis. Not creating an issue.")
        return 0

    # --- DRIVE ---
    if args.no_drive:
        payload = build_issue_payload(observation, research)
        print("[DRIVE] --no-drive set; prepared issue payload (not submitted):")
        print(f"  id:           {payload.id}")
        print(f"  title:        {payload.title}")
        print(f"  target_file:  {payload.target_file}")
        print(f"  hypothesis:   {payload.hypothesis}")
        print(f"  plan:         {payload.plan}")
        print(f"  acceptance:   {payload.acceptance_criteria}")
        return 0

    payload = build_issue_payload(observation, research)
    client = TemperClient(
        base_url=args.temper_url,
        tenant=args.tenant,
        session_id=args.session_id,
    )
    print(f"[DRIVE] driving ImprovementIssue {payload.id} via Temper "
          f"({args.temper_url}, tenant={args.tenant})")
    result = drive_issue(client, payload, poll_for_approval=not args.no_poll)
    print(f"[DRIVE] status: {result.status}")
    print(result.detail)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dark Factory Observer/Researcher (C2)")
    parser.add_argument(
        "--source", choices=["snapshot", "datadog"], default="snapshot"
    )
    parser.add_argument(
        "--snapshot",
        default="snapshots/helix_live_2026-05-24.json",
        help="path to a live-captured metric snapshot (for --source snapshot)",
    )
    parser.add_argument("--from", dest="from_", default="now-1h")
    parser.add_argument("--to", default="now")
    parser.add_argument(
        "--helix-repo", default="/Users/arun.parthiban/notdd/helix"
    )
    parser.add_argument("--temper-url", default="http://127.0.0.1:3000")
    parser.add_argument("--tenant", default="dark-factory")
    parser.add_argument("--session-id", default=None)
    parser.add_argument(
        "--no-drive", action="store_true", help="run OBSERVE+RESEARCH only"
    )
    parser.add_argument(
        "--no-poll",
        action="store_true",
        help="do not block polling for human approval after a Cedar denial",
    )
    args = parser.parse_args(argv)
    return run_once(args)


if __name__ == "__main__":
    raise SystemExit(main())
