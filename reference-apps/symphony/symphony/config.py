"""Configuration for the Symphony orchestrator.

Everything is overridable by environment variable so the orchestrator can run in
CI, in the demo, or against a live Temper server without code changes. Defaults
match the Dark Factory contract (``docs/dark-factory-BC-contract.md``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


# Contract defaults.
DEFAULT_TEMPER_URL = "http://127.0.0.1:3000"
DEFAULT_TENANT = "dark-factory"
DEFAULT_HELIX_REPO = "/Users/arun.parthiban/notdd/helix"
DEFAULT_BASE_BRANCH = "main"
DEFAULT_BRANCH_PREFIX = "darkfactory/"
# The OData entity set name for B's tracker (plural of ImprovementIssue).
DEFAULT_ENTITY_SET = "ImprovementIssues"
# CSDL namespace bound actions resolve under. OData allows the unqualified
# action too; we send the qualified form and fall back if the server 404s.
DEFAULT_ODATA_NAMESPACE = "Default"


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value else default


@dataclass(frozen=True)
class SymphonyConfig:
    """Resolved runtime configuration. Frozen — build once per pass."""

    # Temper data-plane.
    temper_url: str = field(default_factory=lambda: _env("TEMPER_URL", DEFAULT_TEMPER_URL))
    tenant: str = field(default_factory=lambda: _env("TEMPER_TENANT", DEFAULT_TENANT))
    entity_set: str = field(default_factory=lambda: _env("SYMPHONY_ENTITY_SET", DEFAULT_ENTITY_SET))
    odata_namespace: str = field(
        default_factory=lambda: _env("SYMPHONY_ODATA_NAMESPACE", DEFAULT_ODATA_NAMESPACE)
    )

    # Git / Helix.
    helix_repo: Path = field(
        default_factory=lambda: Path(_env("HELIX_REPO", DEFAULT_HELIX_REPO)).resolve()
    )
    base_branch: str = field(default_factory=lambda: _env("SYMPHONY_BASE_BRANCH", DEFAULT_BASE_BRANCH))
    branch_prefix: str = field(
        default_factory=lambda: _env("SYMPHONY_BRANCH_PREFIX", DEFAULT_BRANCH_PREFIX)
    )

    # Workspace root that ALL worktrees must live under (Symphony invariant 2).
    # Default: a sibling of the Helix repo so worktrees never nest inside it.
    workspace_root: Path = field(
        default_factory=lambda: Path(
            _env(
                "SYMPHONY_WORKSPACE_ROOT",
                str(Path(DEFAULT_HELIX_REPO).resolve().parent / "symphony-worktrees"),
            )
        ).resolve()
    )

    # Coding agent.
    agent_name: str = field(default_factory=lambda: _env("SYMPHONY_AGENT", "claude"))
    # When set, the agent does NOT actually edit; the orchestrator makes a
    # deterministic safe no-op edit itself. Used by the offline end-to-end demo.
    dry_run_agent: bool = field(
        default_factory=lambda: _env("SYMPHONY_DRY_RUN_AGENT", "0") in ("1", "true", "TRUE")
    )

    # Tracker selection. "temper" (default) or "fake".
    tracker_name: str = field(default_factory=lambda: _env("SYMPHONY_TRACKER", "temper"))

    def branch_for(self, issue_id: str) -> str:
        """Branch name for an issue, sanitized."""
        from .workspace import sanitize_issue_key

        return f"{self.branch_prefix}{sanitize_issue_key(issue_id)}"
