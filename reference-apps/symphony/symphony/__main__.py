"""CLI entrypoint for the Symphony orchestrator.

Examples:
    # Offline end-to-end demo (no network, no model): fake tracker + dry-run agent
    python -m symphony --tracker fake --dry-run-agent

    # Real: read an Implementing ImprovementIssue from Temper, run Claude Code
    python -m symphony --tracker temper --agent claude

    # Keep the worktree around for inspection (default) or clean it up
    python -m symphony --tracker fake --dry-run-agent --cleanup
"""

from __future__ import annotations

import argparse
import os
import sys

from .config import SymphonyConfig
from .orchestrator import Orchestrator


def _apply_overrides(args: argparse.Namespace) -> None:
    """Translate CLI flags into the env vars SymphonyConfig reads."""
    if args.tracker:
        os.environ["SYMPHONY_TRACKER"] = args.tracker
    if args.agent:
        os.environ["SYMPHONY_AGENT"] = args.agent
    if args.dry_run_agent:
        os.environ["SYMPHONY_DRY_RUN_AGENT"] = "1"
    if args.helix_repo:
        os.environ["HELIX_REPO"] = args.helix_repo
    if args.workspace_root:
        os.environ["SYMPHONY_WORKSPACE_ROOT"] = args.workspace_root
    if args.temper_url:
        os.environ["TEMPER_URL"] = args.temper_url
    if args.tenant:
        os.environ["TEMPER_TENANT"] = args.tenant


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="symphony", description="Dark Factory orchestrator for Helix")
    p.add_argument("--tracker", choices=["temper", "fake"], help="issue source (default: temper)")
    p.add_argument("--agent", choices=["claude", "codex"], help="coding agent (default: claude)")
    p.add_argument(
        "--dry-run-agent",
        action="store_true",
        help="skip the model; make the safe demo edit deterministically",
    )
    p.add_argument("--helix-repo", help="path to the Helix git repo")
    p.add_argument("--workspace-root", help="root dir all worktrees live under")
    p.add_argument("--temper-url", help="Temper server base url")
    p.add_argument("--tenant", help="Temper tenant id")
    p.add_argument("--cleanup", action="store_true", help="remove the worktree after the pass")
    args = p.parse_args(argv)

    _apply_overrides(args)
    config = SymphonyConfig()

    print(f"[symphony] tenant={config.tenant} helix={config.helix_repo}")
    print(f"[symphony] tracker={config.tracker_name} agent={config.agent_name} dry_run={config.dry_run_agent}")
    print(f"[symphony] workspace_root={config.workspace_root}")

    orch = Orchestrator(config)
    result = orch.run_once(cleanup=args.cleanup)

    print("\n========== PASS RESULT ==========")
    print(f"picked_up    : {result.picked_up}")
    print(f"issue_id     : {result.issue_id}")
    print(f"branch       : {result.branch}")
    print(f"worktree     : {result.worktree_path}")
    print(f"commit_sha   : {result.commit_sha}")
    print(f"pr_url       : {result.pr_url}")
    print(f"pr_simulated : {result.pr_simulated}")
    print(f"attach       : {result.attach_result}")
    print("=================================")

    if not result.picked_up:
        return 0
    return 0 if result.commit_sha else 1


if __name__ == "__main__":
    sys.exit(main())
