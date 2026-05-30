"""Anthropic SDK agent for the live evolution loop.

Key improvements over the previous `claude -p` CLI wrapper:
  • Parallel tool execution — all tool_use blocks in a single response run
    concurrently via asyncio.gather.  Claude typically requests 4-8 file reads
    at once when exploring a codebase, so exploration is 4-8x faster.
  • No subprocess overhead — no 2-3s claude CLI startup per call.
  • Session persistence — conversation history saved to
    /tmp/evolution-sessions/<uuid>.json so refinement rounds resume correctly.

Interface is identical to the old ClaudeCliAgent:
    agent = make_research_agent(model, helix_repo)
    result = agent.run(prompt, label="research v0")
    result = agent.resume(result.session_id, prompt, label="research v0 r1")

Observer Agent (needs Datadog MCP) still uses `claude -p`; everything else
uses the SDK.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic as _anthropic

# ---------------------------------------------------------------------------
# Session storage
# ---------------------------------------------------------------------------

_SESSION_DIR = Path("/tmp/evolution-sessions")
_SESSION_DIR.mkdir(parents=True, exist_ok=True)


def _session_path(sid: str) -> Path:
    return _SESSION_DIR / f"{sid}.json"


def _save_session(sid: str, messages: list[dict]) -> None:
    _session_path(sid).write_text(json.dumps(messages, indent=2), encoding="utf-8")


def _load_session(sid: str) -> list[dict]:
    p = _session_path(sid)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------

_MODEL_MAP: dict[str, str] = {
    # Map config names → Anthropic API model IDs.
    # claude-sonnet-4-6 is confirmed available; 4-6 variants for opus/haiku
    # fall back to the latest stable 4-5 release.
    "claude-opus-4-6":   "claude-opus-4-5",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "claude-haiku-4-6":  "claude-haiku-4-5",
    "claude-opus-4-5":   "claude-opus-4-5",
    "claude-sonnet-4-5": "claude-sonnet-4-5",
    "claude-haiku-4-5":  "claude-haiku-4-5",
    "opus":   "claude-opus-4-5",
    "sonnet": "claude-sonnet-4-6",
    "haiku":  "claude-haiku-4-5",
}


def _resolve_model(name: str) -> str:
    return _MODEL_MAP.get(name, name)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class AgentResult:
    text: str
    session_id: str
    cost_usd: float = 0.0
    is_error: bool = False
    error_message: str = ""

    def ok(self) -> bool:
        return not self.is_error and bool(self.text)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

_TOOL_READ_FILE: dict = {
    "name": "read_file",
    "description": "Read the full contents of a file. Paths may be absolute or relative to cwd.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to read."},
        },
        "required": ["path"],
    },
}

_TOOL_WRITE_FILE: dict = {
    "name": "write_file",
    "description": "Write (overwrite) a file with new content.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path":    {"type": "string", "description": "File path to write."},
            "content": {"type": "string", "description": "Full file content."},
        },
        "required": ["path", "content"],
    },
}

_TOOL_STR_REPLACE: dict = {
    "name": "str_replace",
    "description": (
        "Replace the FIRST occurrence of old_str with new_str in a file. "
        "old_str must match the file exactly (whitespace included). "
        "Use read_file first to confirm the exact text."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path":    {"type": "string", "description": "File to edit."},
            "old_str": {"type": "string", "description": "Exact text to replace."},
            "new_str": {"type": "string", "description": "Replacement text."},
        },
        "required": ["path", "old_str", "new_str"],
    },
}

_TOOL_BASH: dict = {
    "name": "bash",
    "description": (
        "Run a shell command. stdout+stderr (truncated to 8000 chars) are returned. "
        "Timeout: 120s. PATH includes ~/.cargo/bin and /opt/homebrew/bin. "
        "RUSTUP_TOOLCHAIN=nightly-2026-02-08 is set automatically."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run."},
        },
        "required": ["command"],
    },
}

_TOOL_GLOB: dict = {
    "name": "glob_files",
    "description": "Find files matching a glob pattern. Returns up to 200 paths.",
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.rs'."},
            "root":    {"type": "string", "description": "Root directory (default: cwd)."},
        },
        "required": ["pattern"],
    },
}

_TOOL_GREP: dict = {
    "name": "grep_files",
    "description": "Search files with ripgrep. Returns matching lines (up to 200).",
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern":    {"type": "string", "description": "Regex pattern."},
            "path":       {"type": "string", "description": "Directory or file to search (default: cwd)."},
            "file_glob":  {"type": "string", "description": "Only search files matching this glob, e.g. '*.rs'."},
            "context":    {"type": "integer", "description": "Lines of context around matches (default 0)."},
        },
        "required": ["pattern"],
    },
}

# Tool sets per agent role.
_TOOLS_RESEARCH: list[dict] = [_TOOL_READ_FILE, _TOOL_BASH, _TOOL_GLOB, _TOOL_GREP]
_TOOLS_CODING:   list[dict] = [_TOOL_READ_FILE, _TOOL_WRITE_FILE, _TOOL_STR_REPLACE,
                                _TOOL_BASH, _TOOL_GLOB, _TOOL_GREP]
_TOOLS_TERSE:    list[dict] = []   # classifier / meta-rec: no tools needed


# ---------------------------------------------------------------------------
# Bash safety filters
# ---------------------------------------------------------------------------

_BASH_BLOCK_ALWAYS = [
    re.compile(r'\bgit\s+push\b'),
    re.compile(r'\bgit\s+commit\b'),
]
_BASH_BLOCK_READONLY = _BASH_BLOCK_ALWAYS + [
    re.compile(r'\bgit\s+(add|rm|mv|reset|clean)\b'),
    re.compile(r'brew\s+install\b'),           # research agent: no installs
]


def _bash_env(base: dict | None = None) -> dict:
    """Build subprocess env with cargo, homebrew, and correct Rust toolchain."""
    env = (base or os.environ).copy()
    path_prepend = ":".join([
        str(Path.home() / ".cargo" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ])
    existing = env.get("PATH", "/usr/bin:/bin")
    env["PATH"] = f"{path_prepend}:{existing}"
    env.setdefault("RUSTUP_TOOLCHAIN", "nightly-2026-02-08")
    return env


# ---------------------------------------------------------------------------
# Tool execution (async)
# ---------------------------------------------------------------------------

async def _exec_read_file(inp: dict, cwd: Path) -> str:
    path = Path(inp["path"])
    if not path.is_absolute():
        path = cwd / path
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"Error reading {path}: {exc}"


async def _exec_write_file(inp: dict, cwd: Path) -> str:
    path = Path(inp["path"])
    if not path.is_absolute():
        path = cwd / path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(inp["content"], encoding="utf-8")
        return f"Wrote {len(inp['content'])} bytes to {path}"
    except Exception as exc:
        return f"Error writing {path}: {exc}"


async def _exec_str_replace(inp: dict, cwd: Path) -> str:
    path = Path(inp["path"])
    if not path.is_absolute():
        path = cwd / path
    old_str = inp.get("old_str", "")
    new_str = inp.get("new_str", "")
    try:
        content = path.read_text(encoding="utf-8")
        if old_str not in content:
            # Give a useful diff hint
            snippet = content[max(0, content.find(old_str[:30])-50):][:200]
            return (f"Error: old_str not found verbatim in {path}.\n"
                    f"Nearby content: ...{snippet!r}...")
        path.write_text(content.replace(old_str, new_str, 1), encoding="utf-8")
        return f"Replaced in {path}"
    except Exception as exc:
        return f"Error editing {path}: {exc}"


async def _exec_bash(inp: dict, cwd: Path, blocked: list[re.Pattern]) -> str:
    command = inp.get("command", "")
    for pat in blocked:
        if pat.search(command):
            return f"Error: command blocked by safety policy ({pat.pattern!r}): {command[:80]}"
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(cwd),
            env=_bash_env(),
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            return "Error: command timed out after 120s"
        return stdout.decode("utf-8", errors="replace")[:8000]
    except Exception as exc:
        return f"Error running bash: {exc}"


async def _exec_glob(inp: dict, cwd: Path) -> str:
    import glob as _glob
    pattern = inp.get("pattern", "**/*")
    root = Path(inp.get("root", str(cwd)))
    if not root.is_absolute():
        root = cwd / root
    full_pattern = str(root / pattern)
    try:
        matches = sorted(_glob.glob(full_pattern, recursive=True))
        return "\n".join(matches[:200]) or "(no matches)"
    except Exception as exc:
        return f"Error: {exc}"


async def _exec_grep(inp: dict, cwd: Path) -> str:
    pattern = inp.get("pattern", "")
    search_path = inp.get("path", str(cwd))
    if not Path(search_path).is_absolute():
        search_path = str(cwd / search_path)
    file_glob = inp.get("file_glob", "")
    ctx = int(inp.get("context", 0))
    cmd_parts = ["rg", "--no-heading", "-n", f"-C{ctx}"]
    if file_glob:
        cmd_parts += [f"--glob={file_glob}"]
    cmd_parts += [pattern, search_path]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd_parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(cwd),
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            return "Error: grep timed out after 30s"
        lines = stdout.decode("utf-8", errors="replace").splitlines()
        return "\n".join(lines[:200]) or "(no matches)"
    except FileNotFoundError:
        # rg not found — fallback to grep
        try:
            cmd2 = ["grep", "-rn", pattern, search_path]
            proc2 = await asyncio.create_subprocess_exec(
                *cmd2,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
            )
            out, _ = await asyncio.wait_for(proc2.communicate(), timeout=30)
            lines = out.decode("utf-8", errors="replace").splitlines()
            return "\n".join(lines[:200]) or "(no matches)"
        except Exception as exc2:
            return f"Error: {exc2}"
    except Exception as exc:
        return f"Error: {exc}"


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _print_tool_start(name: str, inp: dict, prefix: str) -> None:
    if name == "bash":
        snippet = str(inp.get("command", ""))[:120].replace("\n", "; ")
        print(f"\n{prefix}⚙  bash: {snippet}", flush=True)
    elif name == "read_file":
        print(f"\n{prefix}📄 read: {inp.get('path', '')}", flush=True)
    elif name == "write_file":
        print(f"\n{prefix}📝 write: {inp.get('path', '')}", flush=True)
    elif name == "str_replace":
        print(f"\n{prefix}✏  edit: {inp.get('path', '')}", flush=True)
    elif name == "glob_files":
        print(f"\n{prefix}🔍 glob: {inp.get('pattern', '')}", flush=True)
    elif name == "grep_files":
        print(f"\n{prefix}🔎 grep: {inp.get('pattern', '')[:60]}", flush=True)
    else:
        print(f"\n{prefix}🔧 {name}", flush=True)


def _print_tool_result(name: str, result: str, prefix: str) -> None:
    if name in ("read_file", "write_file", "str_replace"):
        lines = result.splitlines()
        preview = f" ({len(lines)} lines)" if len(lines) > 1 else f" → {result[:60]}"
        print(f"{prefix}   ↳{preview}", flush=True)


# ---------------------------------------------------------------------------
# Core SDK agent
# ---------------------------------------------------------------------------

class SdkAgent:
    """Anthropic SDK agent with parallel tool execution and session persistence.

    Async core wrapped in sync run()/resume() methods so callers need no
    asyncio changes.
    """

    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        cwd: Path | None = None,
        system_prompt: str = "",
        tools: list[dict] | None = None,
        readonly_bash: bool = False,   # True → research agent (no writes/installs)
        persist_session: bool = True,
        timeout_s: int = 600,
        max_tokens: int = 8192,
    ):
        self.model = _resolve_model(model)
        self.cwd = Path(cwd) if cwd else Path.cwd()
        self.system_prompt = system_prompt
        self.tool_defs = tools if tools is not None else []
        self.bash_blocked = _BASH_BLOCK_READONLY if readonly_bash else _BASH_BLOCK_ALWAYS
        self.persist_session = persist_session
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens

    # ── API key ──────────────────────────────────────────────────────────────

    def _api_key(self) -> str:
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            # Try loading from .dd-env.json next to this file
            dd_env = Path(__file__).parent / ".." / "deploy-history" / ".dd-env.json"
            try:
                key = json.loads(dd_env.read_text()).get("ANTHROPIC_API_KEY", "")
            except Exception:
                pass
        return key

    # ── Tool dispatch ─────────────────────────────────────────────────────────

    async def _dispatch_tool(self, name: str, inp: dict) -> str:
        if name == "read_file":
            return await _exec_read_file(inp, self.cwd)
        if name == "write_file":
            return await _exec_write_file(inp, self.cwd)
        if name == "str_replace":
            return await _exec_str_replace(inp, self.cwd)
        if name == "bash":
            return await _exec_bash(inp, self.cwd, self.bash_blocked)
        if name == "glob_files":
            return await _exec_glob(inp, self.cwd)
        if name == "grep_files":
            return await _exec_grep(inp, self.cwd)
        return f"Unknown tool: {name}"

    # ── Async agent loop ──────────────────────────────────────────────────────

    async def _run_async(
        self,
        messages: list[dict],
        label: str = "",
    ) -> tuple[str, list[dict], float]:
        """Agent loop: stream → parallel tools → stream → … → end_turn.

        Returns (final_text, updated_messages, total_cost_usd).
        """
        api_key = self._api_key()
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")

        prefix = f"    [{label}] " if label else "    "
        total_cost = 0.0
        final_text = ""
        at_line_start = True

        kwargs: dict[str, Any] = dict(
            model=self.model,
            messages=messages,
            max_tokens=self.max_tokens,
        )
        if self.system_prompt:
            kwargs["system"] = self.system_prompt
        if self.tool_defs:
            kwargs["tools"] = self.tool_defs

        # Async context manager guarantees the httpx pool closes before the
        # event loop shuts down (otherwise we get "Event loop is closed" noise).
        async with _anthropic.AsyncAnthropic(api_key=api_key) as client:
          while True:
            text_chunks: list[str] = []
            tool_uses: list[Any] = []   # ToolUseBlock objects

            # ── Stream one turn ────────────────────────────────────────────
            try:
                async with client.messages.stream(**kwargs) as stream:
                    async for event in stream:
                        etype = getattr(event, "type", "")
                        if etype == "content_block_delta":
                            delta = getattr(event, "delta", None)
                            if delta and getattr(delta, "type", "") == "text_delta":
                                chunk = getattr(delta, "text", "")
                                if chunk:
                                    for j, ln in enumerate(chunk.split("\n")):
                                        if j > 0:
                                            print(f"\n{prefix}", end="", flush=True)
                                            at_line_start = False
                                        elif at_line_start:
                                            print(prefix, end="", flush=True)
                                            at_line_start = False
                                        print(ln, end="", flush=True)
                                    text_chunks.append(chunk)
                    final_msg = await stream.get_final_message()
            except _anthropic.APIStatusError as exc:
                raise RuntimeError(f"Anthropic API error: {exc.status_code} {exc.message}") from exc

            # ── Collect tool_use blocks ────────────────────────────────────
            for block in final_msg.content:
                if block.type == "tool_use":
                    tool_uses.append(block)

            # Accumulate cost
            if final_msg.usage:
                # Rough cost estimate (sonnet-4-6 pricing: $3/$15 per M tokens)
                inp_k = final_msg.usage.input_tokens / 1_000_000
                out_k = final_msg.usage.output_tokens / 1_000_000
                total_cost += inp_k * 3.0 + out_k * 15.0

            # Serialize content for history (SDK objects → dicts)
            assistant_content = _serialize_content(final_msg.content)
            messages = messages + [{"role": "assistant", "content": assistant_content}]

            if final_msg.stop_reason == "end_turn" or not tool_uses:
                final_text = "".join(text_chunks)
                if not final_text:
                    # Grab text from content blocks
                    for block in final_msg.content:
                        if block.type == "text":
                            final_text += block.text
                break

            # ── Parallel tool execution ────────────────────────────────────
            print(f"\n{prefix}[parallel: {len(tool_uses)} tool(s)]", flush=True)
            at_line_start = True

            for tu in tool_uses:
                _print_tool_start(tu.name, tu.input, prefix)

            results = await asyncio.gather(*[
                self._dispatch_tool(tu.name, tu.input) for tu in tool_uses
            ])

            for tu, res in zip(tool_uses, results):
                _print_tool_result(tu.name, res, prefix)

            messages = messages + [{"role": "user", "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": res,
                }
                for tu, res in zip(tool_uses, results)
            ]}]
            at_line_start = True

            # Update messages in kwargs for next turn
            kwargs["messages"] = messages

        print(f"\n{prefix}💰 cost≈${total_cost:.4f}", flush=True)
        print()
        return final_text, messages, total_cost

    # ── Sync public API ───────────────────────────────────────────────────────

    def run(self, prompt: str, label: str = "") -> AgentResult:
        """Start a new agent session."""
        messages: list[dict] = [{"role": "user", "content": prompt}]
        return self._invoke(messages, label)

    def resume(self, session_id: str, prompt: str, label: str = "") -> AgentResult:
        """Continue an existing session by appending a new user message."""
        messages = _load_session(session_id)
        if not messages:
            # No history found — start fresh (research agent restart case)
            messages = [{"role": "user", "content": prompt}]
        else:
            messages = messages + [{"role": "user", "content": prompt}]
        return self._invoke(messages, label, session_id=session_id)

    def _invoke(
        self,
        messages: list[dict],
        label: str,
        session_id: str | None = None,
    ) -> AgentResult:
        try:
            text, updated_messages, cost = asyncio.run(
                asyncio.wait_for(
                    self._run_async(messages, label=label),
                    timeout=self.timeout_s,
                )
            )
        except asyncio.TimeoutError:
            return AgentResult(text="", session_id=session_id or "",
                               is_error=True,
                               error_message=f"agent timed out after {self.timeout_s}s")
        except Exception as exc:
            return AgentResult(text="", session_id=session_id or "",
                               is_error=True, error_message=str(exc))

        sid = session_id or str(uuid.uuid4())
        if self.persist_session:
            _save_session(sid, updated_messages)

        return AgentResult(text=text, session_id=sid, cost_usd=cost)


def _serialize_content(content: list) -> list[dict]:
    """Convert SDK ContentBlock objects to plain dicts for JSON serialization."""
    out = []
    for block in content:
        btype = getattr(block, "type", "")
        if btype == "text":
            out.append({"type": "text", "text": block.text})
        elif btype == "tool_use":
            out.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })
        elif btype == "thinking":
            out.append({"type": "thinking", "thinking": getattr(block, "thinking", "")})
    return out


# ---------------------------------------------------------------------------
# ClaudeCliAgent — kept for Observer Agent (needs Datadog MCP via claude -p)
# ---------------------------------------------------------------------------

_DD_MCP_CONFIG = {
    "mcpServers": {
        "datadog": {
            "type": "http",
            "url": "https://mcp.datadoghq.com/api/unstable/mcp-server/mcp?toolsets=core,metrics",
        }
    }
}
_DD_MCP_PATH = Path("/tmp/evolution-dd-mcp.json")


def ensure_dd_mcp_config() -> Path:
    _DD_MCP_PATH.write_text(json.dumps(_DD_MCP_CONFIG, indent=2))
    return _DD_MCP_PATH


class ClaudeCliAgent:
    """Thin `claude -p` wrapper kept exclusively for the Observer Agent (MCP).

    All other agents use SdkAgent above.
    """

    CLAUDE_BIN: Path = Path.home() / ".local" / "bin" / "claude"

    def __init__(
        self,
        model: str = "haiku",
        system_prompt: str = "",
        allowed_tools: list[str] | None = None,
        mcp_config: Path | None = None,
        timeout_s: int = 120,
    ):
        self.model = model
        self.system_prompt = system_prompt
        self.allowed_tools = allowed_tools or []
        self.mcp_config = mcp_config
        self.timeout_s = timeout_s

    def _cmd(self) -> list[str]:
        cmd = [
            str(self.CLAUDE_BIN), "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--model", self.model,
        ]
        if self.system_prompt:
            cmd += ["--system-prompt", self.system_prompt]
        if self.allowed_tools:
            cmd += ["--allowedTools"] + self.allowed_tools
        if self.mcp_config and self.mcp_config.exists():
            cmd += ["--mcp-config", str(self.mcp_config)]
        cmd += ["--no-session-persistence"]
        return cmd

    def run(self, prompt: str, label: str = "") -> AgentResult:
        prefix = f"    [{label}] " if label else "    "
        try:
            proc = subprocess.Popen(
                self._cmd(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=os.environ.copy(),
            )
        except FileNotFoundError:
            return AgentResult(text="", session_id="", is_error=True,
                               error_message=f"claude binary not found at {self.CLAUDE_BIN}")
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except BrokenPipeError:
            pass

        import threading
        stderr_lines: list[str] = []
        threading.Thread(target=lambda: [stderr_lines.append(l) for l in proc.stderr],
                         daemon=True).start()

        accumulated: list[str] = []
        final_result = ""
        session_id = ""
        cost = 0.0
        is_error = False
        at_line_start = True

        try:
            for raw_line in proc.stdout:
                raw_line = raw_line.rstrip("\n")
                if not raw_line:
                    continue
                try:
                    event = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type", "")
                if etype == "assistant":
                    for block in event.get("message", {}).get("content", []):
                        btype = block.get("type", "")
                        if btype == "text":
                            chunk = block.get("text", "")
                            if chunk:
                                for j, ln in enumerate(chunk.split("\n")):
                                    if j > 0:
                                        print(f"\n{prefix}", end="", flush=True)
                                        at_line_start = False
                                    elif at_line_start:
                                        print(prefix, end="", flush=True)
                                        at_line_start = False
                                    print(ln, end="", flush=True)
                                accumulated.append(chunk)
                        elif btype == "tool_use":
                            name = block.get("name", "")
                            inp = block.get("input", {})
                            print(f"\n{prefix}🔧 {name}", flush=True)
                            at_line_start = True
                elif etype == "result":
                    final_result = event.get("result", "")
                    session_id = event.get("session_id", "")
                    cost = float(event.get("total_cost_usd", 0) or 0)
                    is_error = bool(event.get("is_error", False))
                    if cost:
                        print(f"\n{prefix}💰 cost=${cost:.4f}", flush=True)
        finally:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

        print()
        full_text = "".join(accumulated) or final_result
        if is_error:
            return AgentResult(text=full_text, session_id=session_id, cost_usd=cost,
                               is_error=True, error_message=full_text)
        return AgentResult(text=full_text, session_id=session_id, cost_usd=cost)

    def resume(self, session_id: str, prompt: str, label: str = "") -> AgentResult:
        # CLI sessions aren't used for observer; just start fresh
        return self.run(prompt, label=label)


# ---------------------------------------------------------------------------
# Agent factories (same signatures as before)
# ---------------------------------------------------------------------------

def make_research_agent(model: str, helix_repo: Path, timeout_s: int = 300) -> SdkAgent:
    """Research Agent: parallel file reads, read-only bash, produces Hypothesis JSON."""
    return SdkAgent(
        model=model,
        cwd=helix_repo,
        system_prompt=(
            "You are a distributed systems performance researcher with deep Rust expertise. "
            "You have access to the helix source tree via read_file, glob_files, grep_files, "
            "and bash tools. Explore the codebase — read multiple files in parallel — before "
            "proposing a hypothesis. Be precise about code mechanisms and file paths."
        ),
        tools=_TOOLS_RESEARCH,
        readonly_bash=True,
        persist_session=True,
        timeout_s=timeout_s,
        max_tokens=8192,
    )


def make_coding_agent(model: str, worktree: Path, helix_repo: Path,
                      timeout_s: int = 600) -> SdkAgent:
    """Coding Agent: reads + edits worktree files, verifies with cargo check."""
    return SdkAgent(
        model=model,
        cwd=worktree,
        system_prompt=(
            "You are a Rust engineer implementing a targeted performance change. "
            "Tools available: read_file, write_file, str_replace, bash, glob_files, grep_files. "
            "After making changes run: bash {\"command\": \"cargo check -p helix-server 2>&1 | head -60\"}. "
            "Do NOT commit, push, or run git add/commit. "
            "Edit only files in the current worktree directory. "
            "Keep changes minimal and focused on the hypothesis."
        ),
        tools=_TOOLS_CODING,
        readonly_bash=False,
        persist_session=False,
        timeout_s=timeout_s,
        max_tokens=8192,
    )


def make_observer_agent(model: str, timeout_s: int = 120) -> ClaudeCliAgent:
    """Observer Agent: uses claude -p with Datadog MCP.

    Prerequisite: the Datadog MCP server must be registered globally with
        claude mcp add --transport http --scope user datadog \
          "https://mcp.datadoghq.com/api/unstable/mcp-server/mcp?toolsets=core,metrics"
    and OAuth-authorized once via `claude` (interactive). Tokens are then
    cached and reused by all subsequent `claude -p` invocations.
    """
    return ClaudeCliAgent(
        model="haiku",   # always use haiku alias for CLI
        mcp_config=None,  # rely on global ~/.claude.json registration
        allowed_tools=["mcp__datadog__query_metrics",
                       "mcp__datadog__execute_ddsql",
                       "mcp__datadog__get_timeseries_data"],
        timeout_s=timeout_s,
        system_prompt=(
            "You are an observer agent for a directed-evolution system. "
            "Query Datadog metrics to determine which performance dimension "
            "(latency or throughput) needs improvement most. Reply with "
            "exactly one word: latency or throughput."
        ),
    )


def make_classifier_agent(model: str, timeout_s: int = 60) -> SdkAgent:
    """Failure Classifier: one-shot, no tools, cheap."""
    return SdkAgent(
        model=model,
        system_prompt=(
            "You are a terse failure classifier. "
            "Reply with exactly one word: hypothesis or code."
        ),
        tools=_TOOLS_TERSE,
        persist_session=False,
        timeout_s=timeout_s,
        max_tokens=64,
    )


def make_meta_rec_agent(model: str, timeout_s: int = 60) -> SdkAgent:
    """Meta-recommendation Agent: high-level batch direction hints, no tools."""
    return SdkAgent(
        model=model,
        system_prompt=(
            "You are a terse distributed systems advisor for a performance "
            "evolution system. Be specific about Helix code areas. "
            "2-3 sentences max."
        ),
        tools=_TOOLS_TERSE,
        persist_session=False,
        timeout_s=timeout_s,
        max_tokens=512,
    )
