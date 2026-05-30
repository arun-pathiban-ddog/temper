"""Shared `claude` invocation prefix for the agents.

All the LLM-driven agents (classifier, optimizer propose, optimizer-executor edit,
symphony editor) shell out to Claude Code. Wrapping the call as `lapdog claude ...`
auto-traces it (lapdog launches Claude with intercept/tracing). We centralize the
prefix here so every agent traces consistently — and so it degrades gracefully:

  - LAPDOG_TRACE=0            -> plain `claude` (opt out)
  - lapdog not on PATH        -> plain `claude` (e.g. the handoff machine)
  - otherwise (default)       -> `lapdog claude` (traced)

Use: `cmd = [*claude_argv(), "-p", "--model", ...]`. lapdog forwards all flags +
stdin and leaves stdout clean (verified), so JSON parsing is unaffected.
"""

from __future__ import annotations

import os
import shutil


def claude_argv() -> list[str]:
    """The argv prefix to invoke Claude Code (traced via lapdog when available)."""
    if os.environ.get("LAPDOG_TRACE", "1") != "0" and shutil.which("lapdog"):
        return ["lapdog", "claude"]
    return ["claude"]
