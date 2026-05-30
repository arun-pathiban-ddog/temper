"""Symphony-on-Temper — a minimal Dark Factory orchestrator for Helix.

Turns an ``ImprovementIssue`` (Agent-B's Temper tracker) in state ``Implementing``
into a real code change: isolated git worktree -> coding agent -> commit -> PR ->
PR url written back onto the issue via ``AttachPr``.

Five components (see ADR-0093 in the temper repo):
  1. tracker      — read candidate work / write PR url back (Temper or fake)
  2. workspace    — isolated git worktree with Symphony's 3 invariants
  3. prompt       — render the coding-agent prompt from the issue
  4. agent        — swappable coding-agent runner (Claude default, Codex stub)
  5. orchestrator — one pass tying it all together
"""

from .config import SymphonyConfig
from .tracker import Issue, Tracker, FakeTracker, TemperTracker, make_tracker
from .workspace import Workspace, WorkspaceManager, sanitize_issue_key
from .prompt import build_prompt
from .agent import CodingAgent, AgentResult, ClaudeAdapter, CodexAdapter, make_agent
from .orchestrator import Orchestrator, PassResult

__all__ = [
    "SymphonyConfig",
    "Issue",
    "Tracker",
    "FakeTracker",
    "TemperTracker",
    "make_tracker",
    "Workspace",
    "WorkspaceManager",
    "sanitize_issue_key",
    "build_prompt",
    "CodingAgent",
    "AgentResult",
    "ClaudeAdapter",
    "CodexAdapter",
    "make_agent",
    "Orchestrator",
    "PassResult",
]
