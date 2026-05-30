"""Modal-Sandbox verify backend for the optimizer-executor (opt-in, ADR-0103).

The default verify backend is LOCAL (`cargo test` on this machine). This module is
the OPTIONAL `--verify-backend modal` path: it runs the crate-scoped `cargo test`
inside a Modal Sandbox — a hermetic Linux container with the full Rust + cmake
toolchain. This fixes "works-on-my-machine" gaps (e.g. the missing local cmake that
broke the native rdkafka build) and lets many verifies run in parallel.

Flow (Option A — source travels via git):
  1. the executor commits the claude edits to the cluster branch and pushes it to
     the public fork (arun-pathiban-ddog/helix),
  2. a Modal Sandbox clones that branch over https (no auth — public repo),
  3. runs `cargo test -p <crate> ...` with the pinned nightly toolchain,
  4. returns (ok, tail-of-output) — the same shape the local _verify returns.

A cold sandbox has no cargo `target/` cache, so a build is minutes (it compiles
deps from scratch). The win is correctness + isolation + parallelism, not latency.
"""

from __future__ import annotations

import os

FORK_HTTPS = os.environ.get("MODAL_VERIFY_REPO", "https://github.com/arun-pathiban-ddog/helix.git")
TOOLCHAIN = os.environ.get("OPTIMIZE_TOOLCHAIN", "nightly-2026-02-08")
SANDBOX_TIMEOUT_S = int(os.environ.get("MODAL_VERIFY_TIMEOUT_S", "1800"))  # 30 min cap
_APP_NAME = "df-optimizer-verify"


def _build_image():
    """Linux image with the Rust toolchain + cmake + the native build deps Helix
    needs (rdkafka-sys -> cmake/clang). Built once, cached by Modal."""
    import modal
    return (
        modal.Image.debian_slim()
        .apt_install("curl", "git", "build-essential", "cmake", "clang", "pkg-config", "libssl-dev")
        .run_commands(
            # rustup + the pinned nightly the cargo tests use
            "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y "
            f"--default-toolchain {TOOLCHAIN} --profile minimal",
        )
        .env({"PATH": "/root/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
              "RUSTUP_TOOLCHAIN": TOOLCHAIN})
    )


def verify_on_modal(branch: str, crates: list[str], on_line=None) -> tuple[bool, str]:
    """Run `cargo test -p <crate> ...` for `branch` inside a Modal Sandbox.

    Returns (ok, output_tail). Requires the branch to already be pushed to the
    public fork. `on_line` (optional) receives streamed log lines.
    """
    try:
        import modal
    except ImportError:
        return False, "modal SDK not installed (pip install modal)"

    pkg_args = " ".join(f"-p {c}" for c in (crates or ["helix-server"]))
    script = (
        "set -e; "
        f"git clone --depth 1 --branch {branch} {FORK_HTTPS} /helix 2>&1; "
        "cd /helix; "
        f"echo '=== cargo test {pkg_args} (toolchain {TOOLCHAIN}) ==='; "
        f"cargo test {pkg_args} --no-fail-fast"
    )

    def _emit(line: str):
        if on_line:
            on_line(line)

    try:
        app = modal.App.lookup(_APP_NAME, create_if_missing=True)
        image = _build_image()
        _emit("[modal] creating sandbox (building/pulling image if needed)…")
        with modal.enable_output():
            sb = modal.Sandbox.create(app=app, image=image, timeout=SANDBOX_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — surface any Modal/setup failure as a verify failure
        return False, f"modal sandbox create failed: {exc}"

    out_lines: list[str] = []
    try:
        p = sb.exec("bash", "-lc", script)
        for line in p.stdout:
            ln = line.rstrip("\n")
            out_lines.append(ln)
            _emit(ln)
        p.wait()
        rc = p.returncode
    except Exception as exc:  # noqa: BLE001
        rc, out_lines = 1, out_lines + [f"[modal] exec error: {exc}"]
    finally:
        try:
            sb.terminate()
            sb.detach()
        except Exception:  # noqa: BLE001
            pass

    tail = "\n".join(out_lines)[-1500:]
    return rc == 0, f"[modal sandbox] crates={crates}; …{tail[-1100:]}"
