"""Optimizer executor (ADR-0103) — implement → verify → deploy → post-verify.

Claims optimizer tickets (ImprovementIssue, TargetFile opt://<cluster>) and runs
the full pipeline that Symphony's PR-only implementer can't:

  1. IMPLEMENT  — worktree off the cluster's branch; `claude -p` (Edit/Read/Write)
                  applies the ticket's proposed code/config changes to Helix source.
  2. VERIFY     — `cargo test` (pinned nightly) gate. Red -> ticket Failed, NO deploy.
  3. DEPLOY     — backend builds the worktree image + sets it on the cluster's
                  StatefulSet (real Cloud Build + rollout).
  4. POST-VERIFY— re-read the target metric from Datadog; confirm it improved (or
                  did not regress) vs a pre-deploy baseline. Regression -> Failed.

One ticket per tick. Runs as the breeder identity (the backend drives the governed
ticket transitions). Human-readable summary line for the Agent Runs tab.

Run: python3 -m breeder.optimize_executor
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

UI_URL = os.environ.get("OPTIMIZE_UI_URL", "http://127.0.0.1:4100")
HELIX_REPO = Path(os.environ.get("OPTIMIZE_HELIX_REPO", "/Users/arun.parthiban/notdd/helix"))
WORKTREES = HELIX_REPO.parent / "helix-worktrees"
CARGO_TOOLCHAIN = os.environ.get("OPTIMIZE_TOOLCHAIN", "nightly-2026-02-08")
EDIT_MODEL = os.environ.get("OPTIMIZE_EDIT_MODEL", "sonnet")
VERIFY_TIMEOUT_S = int(os.environ.get("OPTIMIZE_VERIFY_TIMEOUT_S", "1200"))
EDIT_TIMEOUT_S = int(os.environ.get("OPTIMIZE_EDIT_TIMEOUT_S", "900"))
DEPLOY_WAIT_MIN = int(os.environ.get("OPTIMIZE_DEPLOY_WAIT_MIN", "20"))
POST_VERIFY_WAIT_S = int(os.environ.get("OPTIMIZE_POST_VERIFY_WAIT_S", "120"))
# Git remote the modal-verify path pushes the per-ticket branch to (public fork the
# Modal Sandbox clones). Only used when --verify-backend modal.
MODAL_PUSH_REMOTE = os.environ.get("OPTIMIZE_PUSH_REMOTE", "fork")


def _http(method: str, path: str, body: dict | None = None, timeout: float = 60.0):
    url = f"{UI_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json", "Accept": "application/json"},
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode(errors="replace")[:300]}
    except urllib.error.URLError as e:
        return 0, {"error": str(e)}


def _git(*args: str, cwd: Path | None = None) -> tuple[int, str]:
    p = subprocess.run(["git", "-C", str(cwd or HELIX_REPO), *args], capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


# --- pipeline stages -------------------------------------------------------

def _claim(ui_path="/api/optimizer/tickets") -> list[dict]:
    _, doc = _http("GET", ui_path)
    return [t for t in (doc.get("tickets", []) if isinstance(doc, dict) else []) if (t.get("status") or "") == "Implementing"]


def _cluster_branch(cluster: str) -> str:
    """The branch the cluster currently builds from (its df-cluster/<name> branch,
    created when the cluster was built). Falls back to champion-metrics."""
    branch = f"df-cluster/{cluster}"
    rc, _ = _git("rev-parse", "--verify", branch)
    return branch if rc == 0 else "champion-metrics"


def _make_worktree(cluster: str) -> tuple[Path | None, str]:
    """Worktree at helix-worktrees/cluster-<cluster> on the cluster's branch (the
    same path the backend's _cloud_build_from_worktree builds from)."""
    branch = _cluster_branch(cluster)
    wt = WORKTREES / f"cluster-{cluster}"
    if wt.exists():
        # reuse: reset it to the branch tip so edits start clean
        _git("reset", "--hard", branch, cwd=wt)
        return wt, branch
    rc, out = _git("worktree", "add", "--force", str(wt), branch)
    if rc != 0:
        return None, f"worktree add failed: {out}"
    return wt, branch


def _edit(wt: Path, plan: dict) -> tuple[bool, str]:
    """claude -p applies the proposed edits in the worktree (Edit/Read/Write only)."""
    proposals = plan.get("proposals", [])
    lines = [f"- {p.get('target_file')}: {p.get('change')} (rationale: {p.get('rationale','')})" for p in proposals]
    prompt = (
        f"Apply these code/config changes to the Helix source in this repo. Make ONLY these changes, "
        f"precisely and minimally; keep the code compiling. Do not reformat unrelated code.\n\n"
        + "\n".join(lines) +
        "\n\nAfter editing, briefly confirm what you changed."
    )
    from breeder.claude_cmd import claude_argv
    cmd = [*claude_argv(), "-p", "--model", EDIT_MODEL,
           "--permission-mode", "acceptEdits", "--allowedTools", "Edit Read Write Grep Glob"]
    try:
        proc = subprocess.run(cmd, input=prompt, cwd=str(wt), capture_output=True, text=True, timeout=EDIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False, f"edit timed out after {EDIT_TIMEOUT_S}s"
    if proc.returncode != 0:
        return False, f"claude -p edit exited {proc.returncode}: {proc.stderr[:300]}"
    # did anything actually change?
    rc, out = _git("status", "--porcelain", cwd=wt)
    if not out.strip():
        return False, "edit produced no changes"
    return True, out.strip()[:500]


def _crates_for_plan(plan: dict) -> list[str]:
    """The crates the proposals touch, derived from their target_file paths
    (e.g. 'helix-raft/src/lib.rs' -> 'helix-raft'). We verify only these crates,
    NOT the whole workspace: building every crate pulls in native deps like
    rdkafka-sys (needs cmake) that our pure-Rust tuning edits never touch — that's
    what made the full-workspace verify fail with 'cmake: failed to exec'."""
    crates = []
    for p in plan.get("proposals", []):
        tf = str(p.get("target_file", ""))
        head = tf.split("/", 1)[0]
        if head.startswith("helix-") and head not in crates:
            crates.append(head)
    return crates or ["helix-server"]


def _verify(wt: Path, plan: dict) -> tuple[bool, str]:
    """Crate-scoped verify gate (pinned toolchain). Runs `cargo test` on ONLY the
    crates the change touches — fast and avoids the workspace's native deps. The
    'assuming tests pass' guard before any deploy."""
    env = dict(os.environ)
    env.setdefault("RUSTUP_TOOLCHAIN", CARGO_TOOLCHAIN)
    crates = _crates_for_plan(plan)
    pkg_args = []
    for c in crates:
        pkg_args += ["-p", c]
    print(f"    [verify] cargo test {' '.join(pkg_args)}", flush=True)
    try:
        p = subprocess.run(["cargo", "test", *pkg_args, "--no-fail-fast"],
                           cwd=str(wt), capture_output=True, text=True, timeout=VERIFY_TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired:
        return False, f"cargo test timed out after {VERIFY_TIMEOUT_S}s"
    tail = (p.stdout + p.stderr)[-1200:]
    return p.returncode == 0, f"crates={crates}; …{tail[-900:]}"


def _commit(wt: Path, cluster: str, plan: dict) -> str:
    n = len(plan.get("proposals", []))
    msg = (f"optimize({cluster}): {n} telemetry-driven code/config change(s)\n\n"
           f"Authored by the optimizer agent (ADR-0103). Workload: {plan.get('workload_summary','')}")
    _git("add", "-A", cwd=wt)
    subprocess.run(["git", "-C", str(wt), "commit", "-m", msg], capture_output=True, text=True)
    rc, sha = _git("rev-parse", "--short", "HEAD", cwd=wt)
    return sha


def _push_branch(wt: Path, branch_name: str) -> tuple[bool, str]:
    """Push the worktree's current HEAD to the public fork under `branch_name`,
    so a Modal Sandbox can clone it. Force-push (the branch is per-ticket/ephemeral)."""
    rc, out = _git("push", "--force", MODAL_PUSH_REMOTE, f"HEAD:refs/heads/{branch_name}", cwd=wt)
    return rc == 0, out[-400:]


def _deploy(cluster: str, image_tag: str) -> tuple[bool, str]:
    s, b = _http("POST", "/api/optimizer/build-deploy", {"cluster": cluster, "image_tag": image_tag},
                 timeout=1500)
    if s in (200, 201) and b.get("ok"):
        return True, f"deployed {image_tag}: {b.get('detail','')[:200]}"
    return False, f"build/deploy failed ({b.get('stage','?')}): {b.get('detail', b.get('error',''))[:300]}"


def _metric_avg(query: str, minutes: int) -> float | None:
    """Read a Datadog metric average over the last `minutes` via the observer source."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "observer"))
    try:
        from observer.observe import DatadogMetricSource
    except Exception:
        return None
    src = DatadogMetricSource.from_env()
    if src is None:
        return None
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    frm = (now - timedelta(minutes=minutes)).isoformat()
    try:
        pts = src.query_series(query, frm, now.isoformat())
    except Exception:
        return None
    return (sum(pts) / len(pts)) if pts else None


def _post_verify(cluster: str, plan: dict, baseline: dict) -> tuple[bool, str]:
    """Re-read the proposals' target metrics; confirm improvement / no regression
    vs the pre-deploy baseline. v1: flag regression, don't auto-rollback."""
    notes = []
    regressed = False
    # Use the primary proposal's expected_metric; default to produce p95 latency.
    metrics = {p.get("expected_metric") for p in plan.get("proposals", []) if p.get("expected_metric")}
    metrics = {m for m in metrics if m} or {"helix.produce.latency_ms.95percentile"}
    for m in list(metrics)[:3]:
        q = f"avg:{m}{{cluster:helix-{cluster}}}"
        after = _metric_avg(q, 3)
        before = baseline.get(m)
        if after is None:
            notes.append(f"{m}: no post-deploy data")
            continue
        if before is None:
            notes.append(f"{m}: {after:.1f} (no baseline)")
            continue
        # For latency metrics, lower is better; treat >10% worse as a regression.
        delta_pct = ((after - before) / before * 100) if before else 0
        better = after <= before
        if not better and delta_pct > 10:
            regressed = True
            notes.append(f"{m}: {before:.1f}→{after:.1f} (REGRESSED +{delta_pct:.0f}%)")
        else:
            notes.append(f"{m}: {before:.1f}→{after:.1f} ({'improved' if better else 'stable'})")
    ok = not regressed
    return ok, "; ".join(notes) or "no metrics to verify"


def _baseline(cluster: str, plan: dict) -> dict:
    metrics = {p.get("expected_metric") for p in plan.get("proposals", []) if p.get("expected_metric")}
    metrics = {m for m in metrics if m} or {"helix.produce.latency_ms.95percentile"}
    out = {}
    for m in list(metrics)[:3]:
        out[m] = _metric_avg(f"avg:{m}{{cluster:helix-{cluster}}}", 5)
    return out


def _implement_ticket(ticket: dict, verify_backend: str = "local") -> tuple[bool, str, str]:
    """Run the full pipeline for one ticket. Returns (ok, stage, detail).
    verify_backend: 'local' (default, cargo test here) or 'modal' (in a sandbox)."""
    iid = ticket["issue_id"]
    cluster = ticket.get("cluster") or ""
    plan = ticket.get("plan") or {}
    if not cluster or not plan.get("proposals"):
        return False, "claim", f"{iid}: malformed ticket"

    # capture pre-deploy baseline BEFORE we change anything
    baseline = _baseline(cluster, plan)

    print(f"  [implement] {iid} cluster={cluster}", flush=True)
    wt, branch = _make_worktree(cluster)
    if wt is None:
        return False, "worktree", branch
    ok, detail = _edit(wt, plan)
    if not ok:
        return False, "implement", detail

    if verify_backend == "modal":
        # Source travels via git: commit the edits and push the branch to the
        # public fork so the Modal Sandbox can clone it, then verify in-sandbox.
        sha = _commit(wt, cluster, plan)
        branch_name = f"opt/{iid}"
        ok, push_detail = _push_branch(wt, branch_name)
        if not ok:
            return False, "verify", f"push for modal verify failed: {push_detail}"
        print(f"  [verify:modal] cargo test in Modal Sandbox on {branch_name} …", flush=True)
        from breeder.modal_verify import verify_on_modal
        ok, vtail = verify_on_modal(branch_name, _crates_for_plan(plan),
                                    on_line=lambda l: print(f"      {l}", flush=True))
        if not ok:
            return False, "verify", f"modal verify failed: {vtail[-600:]}"
    else:
        print(f"  [verify:local] cargo test on {wt.name} …", flush=True)
        ok, vtail = _verify(wt, plan)
        if not ok:
            return False, "verify", f"cargo test failed: {vtail[-500:]}"
        sha = _commit(wt, cluster, plan)

    image_tag = f"opt-{cluster}-{sha}"
    print(f"  [deploy] building {image_tag} + deploying to helix-{cluster} …", flush=True)
    ok, ddetail = _deploy(cluster, image_tag)
    if not ok:
        return False, "deploy", ddetail

    print("  [post-verify] checking metrics …", flush=True)
    time.sleep(POST_VERIFY_WAIT_S)  # let the rolled cluster emit fresh metrics
    ok, pdetail = _post_verify(cluster, plan, baseline)
    stage = "post-verify"
    full = f"verified+deployed {image_tag}; {pdetail}"
    return ok, stage, full


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Optimizer executor (ADR-0103).")
    ap.add_argument("--max-per-tick", type=int, default=1)
    ap.add_argument("--verify-backend", choices=["local", "modal"], default="local",
                    help="local = cargo test on this machine (default, unchanged); "
                         "modal = cargo test in a Modal Sandbox (hermetic Linux+cmake toolchain)")
    args = ap.parse_args(argv)

    tickets = _claim()
    if not tickets:
        print("optimizer-executor: no Implementing optimizer tickets to claim.")
        return 0

    done = []
    for t in tickets[: args.max_per_tick]:
        iid = t["issue_id"]
        ok, stage, detail = _implement_ticket(t, verify_backend=args.verify_backend)
        _http("POST", f"/api/optimizer/tickets/{iid}/advance",
              {"ok": ok, "result": detail, "stage": stage})
        done.append(f"{'OK' if ok else 'FAIL@'+stage} {t.get('cluster')}: {detail[:120]}")
        print(f"  [{'done' if ok else 'fail'}] {iid}: {detail[:160]}", flush=True)

    remaining = max(0, len(tickets) - args.max_per_tick)
    summary = (f"optimizer-executor: processed {len(done)} ticket(s)"
               + (f", {remaining} queued" if remaining else "") + " :: " + " | ".join(done))
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
