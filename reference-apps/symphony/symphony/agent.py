"""Component 4: Coding-agent runner.

A swappable adapter interface so the orchestrator does not care which coding
agent makes the edit. Two adapters:

  * ``ClaudeAdapter`` (default) — invokes Claude Code headless via ``claude -p``.
  * ``CodexAdapter`` (stub)     — documents how it would speak the
                                  ``codex app-server`` stdio JSON-RPC protocol.

Selectable via ``SYMPHONY_AGENT`` env / ``--agent`` flag, default ``claude``.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import SymphonyConfig


@dataclass
class AgentResult:
    """Outcome of a coding-agent run."""

    ok: bool
    summary: str
    raw_stdout: str = ""
    raw_stderr: str = ""
    returncode: int = 0


class CodingAgent(Protocol):
    """The runner seam. ``cwd`` MUST be the isolated worktree path."""

    def run(self, prompt: str, cwd: Path) -> AgentResult:
        ...


class ClaudeAdapter:
    """Invoke Claude Code headless (`claude -p`) inside the worktree.

    Headless flags (verified against `claude --help`, v2.x):
      * ``-p/--print``            : non-interactive, print response and exit.
      * ``--permission-mode acceptEdits`` : allow file edits without prompting.
      * ``--allowedTools "Edit Read Write"`` : restrict to editing tools so the
        agent cannot run arbitrary shell, and never commits/pushes itself.
      * ``--output-format json``  : machine-readable result envelope.

    The cwd is set on the subprocess so Claude operates on the worktree only.
    """

    def __init__(self, config: SymphonyConfig) -> None:
        self.config = config
        # Invoke via `lapdog claude` when available (auto-traces the run), else
        # plain `claude`. Opt out with LAPDOG_TRACE=0.
        import os as _os
        import shutil as _shutil
        if _os.environ.get("LAPDOG_TRACE", "1") != "0" and _shutil.which("lapdog"):
            self.binary = ["lapdog", "claude"]
        else:
            self.binary = ["claude"]

    def run(self, prompt: str, cwd: Path) -> AgentResult:
        # Pre-assertion: never let the agent run anywhere but the worktree.
        assert Path(cwd).exists(), f"agent cwd does not exist: {cwd}"

        cmd = [
            *self.binary,
            "-p",
            prompt,
            "--permission-mode",
            "acceptEdits",
            "--allowedTools",
            "Edit Read Write",
            "--output-format",
            "json",
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=900,
            )
        except FileNotFoundError:
            return AgentResult(
                ok=False,
                summary="claude CLI not found on PATH",
                returncode=127,
            )
        except subprocess.TimeoutExpired:
            return AgentResult(ok=False, summary="claude run timed out (900s)", returncode=124)

        summary = proc.stdout.strip()
        # Claude --output-format json emits a result envelope; extract the text.
        try:
            payload = json.loads(proc.stdout)
            if isinstance(payload, dict):
                summary = str(payload.get("result", payload.get("response", summary)))
        except json.JSONDecodeError:
            pass

        return AgentResult(
            ok=proc.returncode == 0,
            summary=summary or "(no output)",
            raw_stdout=proc.stdout,
            raw_stderr=proc.stderr,
            returncode=proc.returncode,
        )


class CodexAdapter:
    """STUB: drive OpenAI Codex via the ``codex app-server`` stdio protocol.

    Not implemented for the hackathon. How it WOULD work:

      1. Spawn ``codex app-server`` as a child process. It speaks newline-
         delimited JSON-RPC 2.0 over stdin/stdout (the same transport MCP uses).
      2. Send an ``initialize`` request, then a ``newConversation`` / ``newSession``
         request scoped to ``cwd`` (the worktree) with an approval policy that
         auto-approves file edits (equivalent to Claude's ``acceptEdits``).
      3. Send the prompt as a ``sendUserTurn`` / ``sendUserMessage`` request and
         stream ``codex/event`` notifications (token deltas, tool calls, file
         patches) until a ``turnComplete`` / ``taskComplete`` event arrives.
      4. Read the applied file changes from the worktree (Codex edits in place,
         like Claude) and return an ``AgentResult``; the orchestrator then runs
         the same ``git add -A && git commit`` path.

    Because steps 2-3 require pinning to a specific ``codex app-server`` schema
    version (which changes across releases), this adapter intentionally raises
    rather than guessing the wire format.
    """

    def __init__(self, config: SymphonyConfig) -> None:
        self.config = config

    def run(self, prompt: str, cwd: Path) -> AgentResult:
        raise NotImplementedError(
            "CodexAdapter is a stub. It would spawn `codex app-server`, speak "
            "JSON-RPC over stdio (initialize -> newConversation(cwd) -> "
            "sendUserTurn(prompt) -> stream events until turnComplete), then "
            "read in-place edits from the worktree. Use --agent claude for now."
        )


class DryRunAgent:
    """A no-op 'agent' for the offline end-to-end demo.

    It does NOT call any model. Instead it makes the exact safe, deterministic
    edit the demo issue asks for: a clarifying comment near the ``linger_ms``
    field in the Helix batcher. This lets us prove worktree -> commit -> PR
    mechanics with zero network and zero risk.
    """

    MARKER = "// dark-factory: linger_ms is intentionally low"

    def __init__(self, config: SymphonyConfig) -> None:
        self.config = config

    def run(self, prompt: str, cwd: Path) -> AgentResult:
        target = Path(cwd) / "helix-server/src/service/batcher.rs"
        assert target.exists(), f"demo target file missing in worktree: {target}"
        text = target.read_text()
        if self.MARKER in text:
            return AgentResult(ok=True, summary="comment already present; no-op")

        anchor = "    pub linger_ms: u64,"
        assert anchor in text, "could not find linger_ms field to annotate"
        comment = (
            "    // dark-factory: linger_ms is intentionally low (1ms default).\n"
            "    // Clients already batch via linger.ms, so a large server-side\n"
            "    // linger adds latency without throughput; override per-workload\n"
            "    // with HELIX_BATCHER_LINGER_MS. (Added by Symphony dry-run demo.)\n"
        )
        target.write_text(text.replace(anchor, comment + anchor, 1))
        return AgentResult(ok=True, summary="added clarifying comment near linger_ms (dry-run)")


def make_agent(config: SymphonyConfig) -> CodingAgent:
    """Select the coding-agent adapter from config."""
    if config.dry_run_agent:
        return DryRunAgent(config)
    name = config.agent_name.lower()
    if name == "claude":
        return ClaudeAdapter(config)
    if name == "codex":
        return CodexAdapter(config)
    raise ValueError(f"unknown agent '{config.agent_name}' (expected 'claude' or 'codex')")
