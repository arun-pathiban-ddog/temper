"""End-to-end orchestrator test against a throwaway git repo.

Proves the worktree -> agent-edit -> commit -> (simulated) PR -> AttachPr flow
without touching the real Helix repo, any network, or any model. The "agent"
here is a tiny in-test stub that edits a file in the worktree.
"""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony.agent import AgentResult
from symphony.config import SymphonyConfig
from symphony.orchestrator import Orchestrator
from symphony.tracker import FakeTracker, Issue
from symphony.workspace import WorkspaceManager


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


@pytest.fixture
def fake_repo(tmp_path):
    """A minimal git repo with one source file and a 'main' branch, no remote."""
    repo = tmp_path / "fakerepo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    src = repo / "src"
    src.mkdir()
    (src / "lib.rs").write_text("// original\npub fn f() {}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


class _EditAgent:
    """Test stub agent: appends a comment to src/lib.rs in the worktree."""

    def run(self, prompt, cwd):
        from pathlib import Path

        target = Path(cwd) / "src/lib.rs"
        target.write_text(target.read_text() + "// edited by test agent\n")
        return AgentResult(ok=True, summary="appended a comment")


def test_e2e_worktree_commit_simulated_pr(fake_repo, tmp_path):
    cfg = SymphonyConfig(
        helix_repo=fake_repo,
        workspace_root=tmp_path / "worktrees",
        base_branch="main",
        tracker_name="fake",
    )
    issue = Issue(
        id="PROJ/E2E 1",
        title="add a comment",
        status="Implementing",
        target_file="src/lib.rs",
        plan="append a comment",
        acceptance_criteria="only a comment added",
    )
    orch = Orchestrator(
        cfg,
        tracker=FakeTracker(issue),
        workspace_mgr=WorkspaceManager(cfg),
        agent=_EditAgent(),
    )
    result = orch.run_once()

    # Picked up and worked.
    assert result.picked_up
    assert result.issue_id == "PROJ/E2E 1"

    # Invariant 3: branch/worktree key sanitized.
    assert result.branch == "darkfactory/PROJ_E2E_1"
    assert result.worktree_path.endswith("PROJ_E2E_1")

    # Invariant 2: worktree under the configured root.
    assert str(tmp_path / "worktrees") in result.worktree_path

    # A real commit exists on the branch.
    assert result.commit_sha
    log = subprocess.run(
        ["git", "log", "--oneline", result.branch],
        cwd=str(fake_repo),
        capture_output=True,
        text=True,
    )
    assert result.commit_sha[:7] in log.stdout

    # No remote -> PR is simulated and clearly marked.
    assert result.pr_simulated
    assert result.pr_url.startswith("SIMULATED://")

    # Handoff: AttachPr recorded on the (fake) tracker.
    assert result.attach_result["ok"] is True
    assert issue.pr_url == result.pr_url

    # main branch is untouched (still the init commit only).
    main_log = subprocess.run(
        ["git", "log", "--oneline", "main"], cwd=str(fake_repo), capture_output=True, text=True
    )
    assert len(main_log.stdout.strip().splitlines()) == 1


def test_no_change_agent_aborts(fake_repo, tmp_path):
    cfg = SymphonyConfig(helix_repo=fake_repo, workspace_root=tmp_path / "wt", base_branch="main")

    class _NoOp:
        def run(self, prompt, cwd):
            return AgentResult(ok=True, summary="did nothing")

    issue = Issue(id="N1", title="noop", status="Implementing", target_file="src/lib.rs")
    orch = Orchestrator(cfg, tracker=FakeTracker(issue), workspace_mgr=WorkspaceManager(cfg), agent=_NoOp())
    result = orch.run_once(cleanup=True)
    assert result.picked_up
    assert result.commit_sha == ""  # nothing to commit -> aborted


def test_containment_invariant_rejects_escape(fake_repo, tmp_path):
    cfg = SymphonyConfig(helix_repo=fake_repo, workspace_root=tmp_path / "wt", base_branch="main")
    mgr = WorkspaceManager(cfg)
    # A key that resolves outside the root would trip invariant 2; sanitization
    # turns slashes into underscores so traversal can't escape. Verify the
    # sanitized path stays under root.
    ws = mgr.create("../../escape")
    assert str(ws.path).startswith(str((tmp_path / "wt").resolve()))
    mgr.remove(ws, delete_branch=True)
