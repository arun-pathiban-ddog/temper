"""Unit tests for the pure / isolated Symphony components.

No network, no Helix repo, no model. Run: python -m pytest reference-apps/symphony
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony.agent import CodexAdapter, make_agent
from symphony.config import SymphonyConfig
from symphony.prompt import build_prompt
from symphony.tracker import DEMO_ISSUE, FakeTracker, Issue, make_tracker
from symphony.workspace import sanitize_issue_key


# ---- invariant 3: issue key sanitization --------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("DF-123", "DF-123"),
        ("PROJ/abc 9", "PROJ_abc_9"),
        ("a.b_c-d", "a.b_c-d"),
        ("../etc/passwd", "___etc_passwd"),
        ("héllo!", "h_llo_"),
        ("with spaces & symbols#", "with_spaces___symbols_"),
    ],
)
def test_sanitize_issue_key(raw, expected):
    assert sanitize_issue_key(raw) == expected


def test_sanitize_rejects_empty():
    with pytest.raises(AssertionError):
        sanitize_issue_key("")


# ---- tracker ------------------------------------------------------------

def test_fake_tracker_claims_once():
    t = FakeTracker()
    issue = t.claim_issue()
    assert issue is not None
    assert issue.status == "Implementing"
    assert issue.target_file.endswith("batcher.rs")
    # Second claim returns nothing (single demo issue).
    assert t.claim_issue() is None


def test_fake_tracker_attach_pr():
    t = FakeTracker()
    issue = t.claim_issue()
    res = t.attach_pr(issue, "https://example/pr/1")
    assert res["ok"] is True
    assert issue.pr_url == "https://example/pr/1"
    assert t.attached == [(issue.id, "https://example/pr/1")]


def test_issue_from_odata_handles_pascal_and_snake():
    row = {"Id": "X", "Status": "Implementing", "target_file": "a.rs", "Title": "t"}
    issue = Issue.from_odata(row)
    assert issue.id == "X"
    assert issue.status == "Implementing"
    assert issue.target_file == "a.rs"
    assert issue.title == "t"


def test_make_tracker_selects_fake():
    cfg = SymphonyConfig(tracker_name="fake")
    from symphony.tracker import FakeTracker as FT

    assert isinstance(make_tracker(cfg), FT)


# ---- prompt -------------------------------------------------------------

def test_build_prompt_includes_issue_fields():
    prompt = build_prompt(DEMO_ISSUE)
    assert DEMO_ISSUE.title in prompt
    assert DEMO_ISSUE.target_file in prompt
    assert "Plan" in prompt
    assert "Edit only" in prompt
    # Acceptance criteria carried through.
    assert "Only a comment is added" in prompt


# ---- agent --------------------------------------------------------------

def test_codex_adapter_is_stub():
    cfg = SymphonyConfig()
    with pytest.raises(NotImplementedError):
        CodexAdapter(cfg).run("prompt", os.getcwd())


def test_make_agent_default_is_claude():
    cfg = SymphonyConfig(agent_name="claude", dry_run_agent=False)
    from symphony.agent import ClaudeAdapter

    assert isinstance(make_agent(cfg), ClaudeAdapter)


def test_make_agent_dry_run_overrides():
    cfg = SymphonyConfig(agent_name="claude", dry_run_agent=True)
    from symphony.agent import DryRunAgent

    assert isinstance(make_agent(cfg), DryRunAgent)
