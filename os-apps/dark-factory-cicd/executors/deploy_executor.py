#!/usr/bin/env python3
"""Deploy executor — the Cedar-gated rollout capability of the Dark Factory.

Polls the Temper `dark-factory` tenant for Deploy entities in the `Rolling`
state (a deploy only reaches Rolling after a human `Approve` + `Apply`), runs
`kubectl set image` / `kubectl rollout status` against the Helix StatefulSet in
the `dark-factory` namespace, records the result, and lands the deploy in
`Live` / `RolledBack`.

SAFETY (per ADR-0093 and the contract):
  * Namespace is PINNED to `dark-factory`. The executor asserts it and never
    reads a namespace from entity data. It never touches another namespace.
  * Context is PINNED to gke_datadog-sandbox_us-west3_gs-us-west3.
  * DRY-RUN BY DEFAULT (`kubectl --dry-run=client`). A real rollout that mutates
    the live cluster fires ONLY when DARK_FACTORY_DEPLOY_FOR_REAL=1 is set in
    the executor's environment. Other hackathon tracks depend on Helix staying
    up, so the real path is off unless an operator explicitly flips the flag.

This is an OUT-OF-BAND runner: the spec stays pure state+Cedar; this executor is
the only component that touches the GKE cluster.

Usage:
  python3 deploy_executor.py               # poll loop (dry-run)
  python3 deploy_executor.py --once
  python3 deploy_executor.py --deploy-id <id>
  DARK_FACTORY_DEPLOY_FOR_REAL=1 python3 deploy_executor.py --deploy-id <id>  # REAL rollout

Identity: connects as the `deploy-service` agent_type (verified) which
deploy.cedar permits to Apply/RecordResult/MarkLive/Rollback. It CANNOT Approve
— approval must come from a human (the Cedar gate).
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

# PINNED deploy target — never derived from entity data.
KUBE_CONTEXT = "gke_datadog-sandbox_us-west3_gs-us-west3"
NAMESPACE = "dark-factory"
STATEFULSET = "helix"
CONTAINER = os.environ.get("DEPLOY_CONTAINER", "helix")
IMAGE_REPO = os.environ.get("DEPLOY_IMAGE_REPO", "us-west3-docker.pkg.dev/datadog-sandbox/dark-factory/helix")

# Real rollout guard. Off unless an operator sets the env var.
DEPLOY_FOR_REAL = os.environ.get("DARK_FACTORY_DEPLOY_FOR_REAL") == "1"
POLL_INTERVAL_S = int(os.environ.get("DEPLOY_POLL_INTERVAL_S", "10"))

DEPLOY_HEADERS = {
    "Content-Type": "application/json",
    "X-Tenant-Id": TENANT,
    "X-Temper-Principal-Kind": "agent",
    "X-Temper-Principal-Id": "deploy-service",
    "X-Temper-Agent-Type": "deploy-service",
    "X-Temper-Ctx-AgentTypeVerified": "true",
}


def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{URL}{path}", data=data, method=method, headers=DEPLOY_HEADERS
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


def list_rolling_deploys():
    status, body = _req("GET", f"/tdata/Deploys?$filter=Status eq 'Rolling'")
    if status != 200:
        print(f"[deploy] list failed HTTP {status}: {body}", file=sys.stderr)
        return []
    return body.get("value", body.get("entities", []))


def get_deploy(deploy_id):
    status, body = _req("GET", f"/tdata/Deploys('{deploy_id}')")
    return body if status == 200 else None


def _kubectl_base():
    return ["kubectl", "--context", KUBE_CONTEXT, "-n", NAMESPACE]


def run_rollout(image_tag):
    """Run kubectl set image + rollout status against the PINNED target.

    Returns (success: bool, dry_run: bool, cmd_str: str, output: str).
    """
    # Hard safety assertion: never operate outside dark-factory.
    assert NAMESPACE == "dark-factory", "refusing to deploy outside dark-factory namespace"

    image = f"{IMAGE_REPO}:{image_tag}"
    set_image = _kubectl_base() + [
        "set", "image", f"statefulset/{STATEFULSET}", f"{CONTAINER}={image}",
    ]
    rollout = _kubectl_base() + ["rollout", "status", f"statefulset/{STATEFULSET}", "--timeout=300s"]

    if not DEPLOY_FOR_REAL:
        # DRY-RUN: validate the mutation client-side without touching the cluster.
        dry_cmd = set_image + ["--dry-run=client", "-o", "yaml"]
        cmd_str = " ".join(dry_cmd)
        print(f"[deploy] DRY-RUN (set DARK_FACTORY_DEPLOY_FOR_REAL=1 for a real rollout):\n  {cmd_str}")
        print(f"[deploy] would then run: {' '.join(rollout)}")
        proc = subprocess.run(dry_cmd, capture_output=True, text=True)
        out = (proc.stdout + proc.stderr).strip()
        # Dry-run "success" means kubectl validated the command (exit 0). If the
        # cluster/context is unreachable in the demo box, we still treat the
        # dry-run as the executor having done its job and report the outcome.
        success = proc.returncode == 0
        return success, True, cmd_str, out[-4000:]

    # ===================== REAL ROLLOUT (guarded) =====================
    # This path mutates the live Helix StatefulSet. It runs ONLY because an
    # operator set DARK_FACTORY_DEPLOY_FOR_REAL=1.
    cmd_str = " ".join(set_image)
    print(f"[deploy] *** REAL ROLLOUT *** {cmd_str}")
    p1 = subprocess.run(set_image, capture_output=True, text=True)
    out = p1.stdout + p1.stderr
    if p1.returncode != 0:
        return False, False, cmd_str, out[-4000:]
    p2 = subprocess.run(rollout, capture_output=True, text=True)
    out += "\n" + p2.stdout + p2.stderr
    return p2.returncode == 0, False, cmd_str, out[-4000:]


def record_and_land(deploy_id, success, dry_run, cmd_str, output):
    result = ("dry-run-ok" if dry_run else "rolled-out") if success else "failed"
    s, b = _req(
        "POST", f"/tdata/Deploys('{deploy_id}')/Default.RecordResult",
        {"result": f"{result}: {output[:2000]}", "rollout_cmd": cmd_str, "dry_run": dry_run},
    )
    if s == 403:
        print(f"[deploy] RecordResult denied (decision pending): {b}", file=sys.stderr)
        return
    if s not in (200, 204):
        print(f"[deploy] RecordResult HTTP {s}: {b}", file=sys.stderr)
        return
    land = "MarkLive" if success else "Rollback"
    s, b = _req("POST", f"/tdata/Deploys('{deploy_id}')/Default.{land}", {})
    print(f"[deploy] deploy {deploy_id}: {result} -> {land} HTTP {s}")


def process_deploy(dep):
    deploy_id = dep.get("Id") or dep.get("id")
    image_tag = dep.get("ImageTag") or dep.get("image_tag") or "latest"
    # Defense-in-depth: if entity data carried a namespace, ignore anything but ours.
    ns = dep.get("Namespace") or dep.get("namespace") or NAMESPACE
    if ns != NAMESPACE:
        print(f"[deploy] REFUSING deploy {deploy_id}: namespace {ns!r} != {NAMESPACE!r}", file=sys.stderr)
        return
    print(f"[deploy] processing deploy {deploy_id} image_tag={image_tag} (dry_run={not DEPLOY_FOR_REAL})")
    success, dry_run, cmd_str, output = run_rollout(image_tag)
    record_and_land(deploy_id, success, dry_run, cmd_str, output)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--deploy-id")
    args = ap.parse_args()

    if args.deploy_id:
        dep = get_deploy(args.deploy_id)
        if not dep:
            print(f"[deploy] deploy {args.deploy_id} not found", file=sys.stderr)
            sys.exit(1)
        process_deploy(dep)
        return

    while True:
        for dep in list_rolling_deploys():
            process_deploy(dep)
        if args.once:
            return
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
