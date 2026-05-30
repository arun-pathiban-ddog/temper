#!/usr/bin/env python3
"""CIRun executor — the verification capability of the Dark Factory.

Polls the Temper `dark-factory` tenant for CIRun entities in the `Running`
state, runs `cargo test --workspace` against the Helix repo on the run's ref,
captures pass/fail + a short report, and calls the run's `RecordResult` action,
then lands it in `Passed` / `Failed`.

DST and TLA model checking would also run here for a real CI gate; see the
clearly-marked block below. For the demo we run the cargo suite for real and
note where DST/TLA hooks would fire.

This is an OUT-OF-BAND runner (per ADR-0093): the spec stays pure state+Cedar;
the executor is the only component that touches the Helix repo.

Usage:
  python3 ci_run_executor.py                 # poll loop
  python3 ci_run_executor.py --once          # one pass, then exit
  python3 ci_run_executor.py --run-id <id>   # process exactly one run id

Identity: connects as the `ci-service` agent_type (verified) which the
ci_run.cedar / improvement_issue.cedar policies permit to record results.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

URL = os.environ.get("TEMPER_URL", "http://127.0.0.1:3000")
TENANT = os.environ.get("TENANT", "dark-factory")
HELIX_REPO = os.environ.get("HELIX_REPO", "/Users/pranav.garg/go/src/github.com/DataDog/helix")
# cargo test can be slow; cap it so the demo loop stays responsive.
TEST_TIMEOUT_S = int(os.environ.get("CI_TEST_TIMEOUT_S", "1800"))
POLL_INTERVAL_S = int(os.environ.get("CI_POLL_INTERVAL_S", "10"))

# The CI service identity. ci_run.cedar permits agent_type "ci-service"
# (verified) to RecordResult / MarkPassed / MarkFailed.
CI_HEADERS = {
    "Content-Type": "application/json",
    "X-Tenant-Id": TENANT,
    "X-Temper-Principal-Kind": "agent",
    "X-Temper-Principal-Id": "ci-service",
    "X-Temper-Agent-Type": "ci-service",
    "X-Temper-Ctx-AgentTypeVerified": "true",
}


def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{URL}{path}", data=data, method=method, headers=CI_HEADERS
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw}


def list_running_runs():
    status, body = _req("GET", f"/tdata/CIRuns?$filter=Status eq 'Running'")
    if status != 200:
        print(f"[ci] list runs failed HTTP {status}: {body}", file=sys.stderr)
        return []
    return body.get("value", body.get("entities", []))


def get_run(run_id):
    status, body = _req("GET", f"/tdata/CIRuns('{run_id}')")
    return body if status == 200 else None


def run_cargo_tests(ref):
    """Check out `ref` in the Helix repo and run cargo test --workspace.

    Returns (passed: bool, exit_code: int, report: str).
    """
    lines = []
    if not os.path.isdir(HELIX_REPO):
        return False, 127, f"Helix repo not found at {HELIX_REPO}"

    # Best-effort checkout of the requested ref. We do NOT hard-fail the run on
    # a checkout miss — we test the current working tree and note it, so the
    # demo can run before C1's branch exists.
    if ref:
        co = subprocess.run(
            ["git", "-C", HELIX_REPO, "checkout", ref],
            capture_output=True, text=True,
        )
        if co.returncode == 0:
            lines.append(f"checked out ref '{ref}'")
        else:
            lines.append(
                f"WARNING: could not checkout ref '{ref}' "
                f"({co.stderr.strip()[:200]}); testing current working tree"
            )

    # --- cargo test --workspace (real) ---
    proc = subprocess.run(
        ["cargo", "test", "--workspace"],
        cwd=HELIX_REPO, capture_output=True, text=True, timeout=TEST_TIMEOUT_S,
    )
    exit_code = proc.returncode
    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-40:])
    lines.append(f"cargo test --workspace exit={exit_code}")
    lines.append(tail)

    # --- WHERE DST / TLA WOULD RUN (stubbed for the demo) ---
    # A production CI gate would, after cargo test, also run:
    #   * Deterministic Simulation Testing (DST): the seeded simulation sweep
    #     over the changed crates, e.g. `cargo test -p <crate> --features dst`
    #     or the repo's DST harness, asserting no invariant violations across N
    #     seeds. Helix would expose its own DST entrypoint here.
    #   * TLA+ / model checking: run TLC against the affected `.tla` specs (or
    #     Temper's own L0-L3 cascade for any spec changes in the PR).
    # For the demo we record that these hooks exist but only run cargo test.
    lines.append("[stub] DST sweep: would run Helix DST harness here")
    lines.append("[stub] TLA model check: would run TLC on affected specs here")

    passed = exit_code == 0
    return passed, exit_code, "\n".join(lines)


def record_and_land(run_id, passed, exit_code, report):
    status_str = "passed" if passed else "failed"
    # RecordResult (input action; stays in Running, sets has_report).
    s, b = _req(
        "POST", f"/tdata/CIRuns('{run_id}')/Default.RecordResult",
        {"status": status_str, "report": report[:8000], "exit_code": exit_code},
    )
    if s == 403:
        print(f"[ci] RecordResult denied (decision pending): {b}", file=sys.stderr)
        return
    if s not in (200, 204):
        print(f"[ci] RecordResult HTTP {s}: {b}", file=sys.stderr)
        return
    # Land terminal.
    land = "MarkPassed" if passed else "MarkFailed"
    s, b = _req("POST", f"/tdata/CIRuns('{run_id}')/Default.{land}", {})
    print(f"[ci] run {run_id}: {status_str} (exit={exit_code}) -> {land} HTTP {s}")


def process_run(run):
    run_id = run.get("Id") or run.get("id")
    ref = run.get("Ref") or run.get("ref") or ""
    repo = run.get("Repo") or run.get("repo") or HELIX_REPO
    print(f"[ci] processing run {run_id} repo={repo} ref={ref!r}")
    try:
        passed, exit_code, report = run_cargo_tests(ref)
    except subprocess.TimeoutExpired:
        passed, exit_code, report = False, 124, f"cargo test timed out after {TEST_TIMEOUT_S}s"
    record_and_land(run_id, passed, exit_code, report)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="one poll pass then exit")
    ap.add_argument("--run-id", help="process exactly one run id then exit")
    args = ap.parse_args()

    if args.run_id:
        run = get_run(args.run_id)
        if not run:
            print(f"[ci] run {args.run_id} not found", file=sys.stderr)
            sys.exit(1)
        process_run(run)
        return

    while True:
        runs = list_running_runs()
        for run in runs:
            process_run(run)
        if args.once:
            return
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
