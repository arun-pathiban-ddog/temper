"""Tests for the DRIVE phase orchestration (no live server needed).

Uses a fake TemperClient to exercise: payload construction, the spec-not-deployed
graceful path, the happy path, and Cedar-denial surfacing (no self-approval).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from observer.observe import SnapshotMetricSource, observe_produce_latency  # noqa: E402
from observer.research import research_latency  # noqa: E402
from observer.driver import build_issue_payload, drive_issue  # noqa: E402
from observer.temper_client import TemperDenied  # noqa: E402

HELIX_REPO = "/Users/arun.parthiban/notdd/helix"
SNAPSHOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "snapshots",
    "helix_live_2026-05-24.json",
)


def _confirmed_research():
    obs = observe_produce_latency(SnapshotMetricSource.from_file(SNAPSHOT))
    res = research_latency(obs, helix_repo=HELIX_REPO)
    return obs, res


class FakeClient:
    """Records calls; configurable to deny a given action or hide the entity set."""

    def __init__(self, entity_set_exists=True, deny_action=None):
        self._exists = entity_set_exists
        self._deny = deny_action
        self.calls = []
        self.tenant = "dark-factory"

    def has_entity_set(self, entity_set):
        return self._exists

    def create(self, entity_set, fields):
        self.calls.append(("create", entity_set, fields))
        return {"id": fields.get("id")}

    def action(self, entity_set, entity_id, action_name, params):
        self.calls.append(("action", action_name, params))
        if action_name == self._deny:
            raise TemperDenied(
                decision_id="PD-test-1234", message="no matching permit policy"
            )
        return {"ok": True}

    def patch(self, entity_set, entity_id, fields):
        self.calls.append(("patch", fields))
        return {"ok": True}

    def poll_decision(self, decision_id, timeout_s=120.0, interval_s=3.0):
        return "no_read_access"


def test_payload_is_grounded():
    obs, res = _confirmed_research()
    payload = build_issue_payload(obs, res)
    assert payload.target_file.endswith("batcher.rs")
    assert "linger_ms" in payload.title
    assert "254" in payload.hypothesis or "p95" in payload.hypothesis
    # evidence carries the ruled-out competitors
    assert "ruled_out" in payload.evidence
    assert "H1" in payload.evidence
    assert payload.plan and payload.acceptance_criteria


def test_spec_not_deployed_degrades_gracefully():
    obs, res = _confirmed_research()
    payload = build_issue_payload(obs, res)
    client = FakeClient(entity_set_exists=False)
    result = drive_issue(client, payload, poll_for_approval=False)
    assert result.status == "spec_not_deployed"
    assert payload.title in result.detail
    # nothing was created
    assert client.calls == []


def test_happy_path_drives_three_actions():
    obs, res = _confirmed_research()
    payload = build_issue_payload(obs, res)
    client = FakeClient(entity_set_exists=True)
    result = drive_issue(client, payload, poll_for_approval=False)
    assert result.status == "planned"
    fired = [c[1] for c in client.calls if c[0] == "action"]
    assert fired == ["Observe", "BeginPlanning", "WritePlan"]
    # evidence patch attempted
    assert any(c[0] == "patch" for c in client.calls)


def test_cedar_denial_surfaces_without_self_approval():
    obs, res = _confirmed_research()
    payload = build_issue_payload(obs, res)
    client = FakeClient(entity_set_exists=True, deny_action="Observe")
    result = drive_issue(client, payload, poll_for_approval=True)
    assert result.status == "pending_approval"
    assert result.pending_decision == "PD-test-1234"
    # never reached BeginPlanning/WritePlan
    fired = [c[1] for c in client.calls if c[0] == "action"]
    assert fired == ["Observe"]


if __name__ == "__main__":
    import traceback

    tests = [
        test_payload_is_grounded,
        test_spec_not_deployed_degrades_gracefully,
        test_happy_path_drives_three_actions,
        test_cedar_denial_surfaces_without_self_approval,
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
