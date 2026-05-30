"""Stage 2 of the fitness cascade: THE CULL (ADR-0095).

After a genome is applied to a lineage's worktree, Stage 2 runs Helix's WAL
durability DST against the mutated source. A genome that disables fsync
durability (`sync_on_rotation=false`, applied as the proven fsync-skip patch in
`wal.rs:sync()`) is REJECTED here with the real DST failure message:

    Durability violation: partition group-N index M was synced but not recovered

This is the proven cull from memory dark-factory-lethal-mutation-spike: the
SIMPLE crash test does NOT catch it (its SimulatedStorage persists immediately),
but the HIGH-FIDELITY shared_wal_dst durability test models crash-loss of
un-fsync'd data and catches it deterministically.

The cull is REAL: this actually compiles + runs `cargo test` on the mutated
worktree. A safe genome passes; the lethal one fails.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# The proven, fast, deterministic cull test (memory dark-factory-lethal-mutation-spike).
CULL_TEST = "test_dst_shared_wal_basic_durability"
CULL_PACKAGE = "helix-tests"

# Pull the durability-violation lines out of the test panic for the report.
_VIOLATION_RE = re.compile(r"Durability violation: [^\"\]]+")


@dataclass
class VerifyResult:
    """Outcome of the Stage-2 verification cull."""

    passed: bool
    test: str
    returncode: int
    failure_message: str  # the extracted durability-violation summary (empty if passed)
    raw_tail: str         # last chunk of cargo output for the record
    test_ran: bool = True  # False = the cull test didn't actually run (misconfig, NOT a pass)

    def summary(self) -> str:
        if not self.test_ran:
            return f"[Stage 2 CULL] ERROR — {self.failure_message}"
        if self.passed:
            return f"[Stage 2 CULL] PASS — {self.test} held durability."
        return (
            f"[Stage 2 CULL] FAIL — {self.test} CULLED this genome.\n"
            f"  {self.failure_message}"
        )


def verify_durability(
    repo: str | Path, timeout_s: int = 600, test: str = CULL_TEST
) -> VerifyResult:
    """Run the WAL durability DST against the (mutated) worktree source.

    Returns a VerifyResult. `passed=False` means the genome is culled — a lethal
    durability-breaking mutation was caught.
    """
    import os
    repo = Path(repo)

    # Ensure ~/.cargo/bin is on PATH so `cargo` is found even when invoked from
    # a Python subprocess that doesn't inherit the shell's Rust environment.
    env = os.environ.copy()
    cargo_bin = Path.home() / ".cargo" / "bin"
    if str(cargo_bin) not in env.get("PATH", ""):
        env["PATH"] = str(cargo_bin) + os.pathsep + env.get("PATH", "")

    # Force the nightly toolchain for the helix repo. The temper repo's
    # rust-toolchain.toml would otherwise override the default to 1.85.0,
    # which cannot compile helix's AWS SDK dependencies (requires >= 1.88).
    # nightly-2026-02-08 is installed and satisfies the >= 1.88 requirement.
    if "RUSTUP_TOOLCHAIN" not in env:
        env["RUSTUP_TOOLCHAIN"] = "nightly-2026-02-08"

    proc = subprocess.run(
        [str(cargo_bin / "cargo"), "test", "-p", CULL_PACKAGE, "--lib", test],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
        env=env,
    )
    out = proc.stdout + "\n" + proc.stderr

    # CRITICAL: the cull is only meaningful if the test ACTUALLY RAN. cargo
    # prints "test result: ok. 0 passed; 0 failed; ...; 1 filtered out" — exit 0
    # — when the filter matches ZERO tests (e.g. the test was renamed/moved).
    # Treating that as a pass would let a lethal genome silently survive the
    # cull. So require >=1 test to have run, and treat "filtered out" / zero-run
    # as an ERROR (not a pass), so the harness fails loud instead of shipping.
    m = re.search(r"test result:\s*(ok|FAILED)\.\s*(\d+)\s+passed;\s*(\d+)\s+failed", out)
    ran = int(m.group(2)) + int(m.group(3)) if m else 0
    test_actually_ran = ran >= 1
    passed = proc.returncode == 0 and m is not None and m.group(1) == "ok" and test_actually_ran

    failure_message = ""
    if not test_actually_ran:
        # Not a cull and not a pass — the verifier itself is broken/misconfigured.
        return VerifyResult(
            passed=False,
            test=test,
            returncode=proc.returncode,
            test_ran=False,
            failure_message=(
                f"VERIFY ERROR: cull test '{test}' matched {ran} tests — it did not run "
                f"(renamed/moved/wrong package?). Refusing to treat as pass. "
                f"returncode={proc.returncode}."
            ),
            raw_tail=out[-2000:],
        )
    if not passed:
        violations = _VIOLATION_RE.findall(out)
        if violations:
            shown = "; ".join(dict.fromkeys(violations))  # dedup, keep order
            failure_message = f'Durability violations: [{shown}]'
        else:
            # Fall back to the panic line if we can't parse violations.
            panic = re.search(r"panicked at [^\n]+\n([^\n]+)", out)
            failure_message = panic.group(1).strip() if panic else "DST test failed (see raw_tail)."

    return VerifyResult(
        passed=passed,
        test=test,
        returncode=proc.returncode,
        failure_message=failure_message,
        raw_tail=out[-2500:],
    )


if __name__ == "__main__":
    import sys

    repo = sys.argv[1] if len(sys.argv) > 1 else "/Users/arun.parthiban/notdd/helix"
    print(f"[verify] running {CULL_TEST} against {repo} ...")
    res = verify_durability(repo)
    print(res.summary())
    raise SystemExit(0 if res.passed else 1)
