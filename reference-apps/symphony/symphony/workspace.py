"""Component 2: Workspace manager.

Creates an isolated git worktree on the Helix repo for an issue, on a fresh
branch ``darkfactory/<issue-id>`` based on the configured base branch.

Enforces Symphony's three workspace invariants:
  1. ``cwd == workspace_path``  — the agent runs *inside* the worktree.
  2. workspace path is contained under the configured ``workspace_root``.
  3. the issue key is sanitized: ``[^A-Za-z0-9._-]`` -> ``_``.

Never touches ``main`` and never deletes the Helix working tree or pre-existing
branches: it only adds/removes its own ``darkfactory/*`` worktrees.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import SymphonyConfig

# Symphony invariant 3: sanitize the issue key.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def sanitize_issue_key(key: str) -> str:
    """Map any issue key to a filesystem/branch-safe token.

    Symphony invariant: ``[^A-Za-z0-9._-]`` -> ``_``. We additionally make the
    result a valid git ref component (no leading dot, no ``..`` sequence) so the
    derived ``darkfactory/<key>`` branch name is always creatable. Empty input
    is rejected (fail fast).
    """
    assert key, "issue key must be non-empty"  # pre-assertion
    sanitized = _UNSAFE.sub("_", key)
    # git refuses ref components that contain ".." or begin with "." — neutralize
    # them after the contract's character class has been applied.
    sanitized = sanitized.replace("..", "__")
    sanitized = sanitized.lstrip(".")
    if not sanitized:
        sanitized = "issue"
    assert sanitized, "sanitized key collapsed to empty"  # post-assertion
    return sanitized


@dataclass
class Workspace:
    """A live worktree: where it lives, the branch, and the source repo."""

    issue_key: str
    path: Path
    branch: str
    repo: Path


class WorkspaceManager:
    """Owns worktree lifecycle for one Helix repo under one workspace root."""

    def __init__(self, config: SymphonyConfig) -> None:
        self.config = config
        self.repo = config.helix_repo
        self.root = config.workspace_root
        # Pre-assertions: the things we depend on must be true up front.
        assert self.repo.exists(), f"Helix repo does not exist: {self.repo}"
        assert (self.repo / ".git").exists(), f"not a git repo: {self.repo}"

    # ---- git plumbing -----------------------------------------------------

    def _git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.repo),
            capture_output=True,
            text=True,
        )

    def _branch_exists(self, branch: str) -> bool:
        r = self._git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        return r.returncode == 0

    def _worktree_registered(self, path: Path) -> bool:
        r = self._git("worktree", "list", "--porcelain")
        return any(line == f"worktree {path}" for line in r.stdout.splitlines())

    # ---- public API -------------------------------------------------------

    def create(self, issue_key: str) -> Workspace:
        """Create (or recreate) a clean worktree for ``issue_key``.

        Returns a ``Workspace`` whose ``path`` is the only place the agent may
        write. Invariant 2 (containment) is asserted before any FS mutation.
        """
        # Accept either a bare issue id or an already-prefixed branch name; strip
        # a leading branch prefix so we don't end up with darkfactory/darkfactory_X.
        if issue_key.startswith(self.config.branch_prefix):
            issue_key = issue_key[len(self.config.branch_prefix) :]
        key = sanitize_issue_key(issue_key)  # invariant 3
        branch = f"{self.config.branch_prefix}{key}"
        self.root.mkdir(parents=True, exist_ok=True)
        path = (self.root / key).resolve()

        # Invariant 2: workspace path must be contained under the configured
        # root. Reject path traversal before touching the filesystem.
        assert self._contained(path, self.root), (
            f"workspace path {path} escapes configured root {self.root}"
        )
        # And the worktree must NOT be inside the Helix repo itself (git refuses
        # nested worktrees, and it would corrupt the source tree).
        assert not self._contained(path, self.repo), (
            f"workspace path {path} must not be inside the Helix repo {self.repo}"
        )

        # If a stale worktree/branch from a previous run exists, remove just our
        # own darkfactory artifacts (never anything else).
        self._cleanup_existing(path, branch)

        # Base the branch on the configured base branch's committed tip so we do
        # NOT inherit the uncommitted working-tree changes on the source repo.
        base_ref = self.config.base_branch
        if not self._branch_exists(base_ref):
            # Fall back to current HEAD if base branch missing locally.
            base_ref = "HEAD"

        r = self._git("worktree", "add", "-b", branch, str(path), base_ref)
        assert r.returncode == 0, f"git worktree add failed: {r.stderr.strip()}"

        ws = Workspace(issue_key=key, path=path, branch=branch, repo=self.repo)
        # Post-assertions.
        assert path.exists(), f"worktree path was not created: {path}"
        assert self._worktree_registered(path), "worktree not registered with git"
        return ws

    def assert_cwd(self, workspace: Workspace, cwd: str | Path) -> None:
        """Invariant 1: the agent's cwd must equal the workspace path."""
        resolved = Path(cwd).resolve()
        assert resolved == workspace.path, (
            f"cwd invariant violated: cwd={resolved} != workspace={workspace.path}"
        )

    def remove(self, workspace: Workspace, *, delete_branch: bool = False) -> None:
        """Remove our worktree (and optionally our branch). Best-effort.

        Only removes the darkfactory worktree/branch — never the Helix repo,
        never ``main``, never any non-darkfactory branch.
        """
        assert workspace.branch.startswith(self.config.branch_prefix), (
            "refusing to remove a non-darkfactory branch"
        )
        self._git("worktree", "remove", "--force", str(workspace.path))
        if delete_branch and self._branch_exists(workspace.branch):
            self._git("branch", "-D", workspace.branch)

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    def _contained(path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False

    def _cleanup_existing(self, path: Path, branch: str) -> None:
        if self._worktree_registered(path) or path.exists():
            self._git("worktree", "remove", "--force", str(path))
        # Prune dangling worktree admin entries.
        self._git("worktree", "prune")
        if self._branch_exists(branch):
            assert branch.startswith(self.config.branch_prefix), (
                "refusing to delete a non-darkfactory branch"
            )
            self._git("branch", "-D", branch)
        if path.exists():
            # Should be gone after worktree remove; guard against orphan dirs.
            import shutil

            shutil.rmtree(path, ignore_errors=True)


def run_in_cwd(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run a subprocess with cwd set, asserting the cwd exists first."""
    assert Path(cwd).exists(), f"cwd does not exist: {cwd}"
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True)
