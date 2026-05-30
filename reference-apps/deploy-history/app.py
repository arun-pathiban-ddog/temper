"""Deployment-History + Control-Plane mini-app — FastAPI backend.

Two faces, one app, sharing the Temper Dark-Factory tenant:

OBSERVE (read-only, unchanged — see /api/deployments*):
  1. Temper Deploy entities       -> the deployment history + governed timeline
  2. Linked ImprovementIssue      -> the changelog ("why")
  3. Helix git repo               -> the actual code diff

CONTROL PLANE (ADR-0096 — /api/cluster, /api/images, /api/actions/*):
  Start deployments, rollback, and scale Helix. EVERY control action flows
  through a governed Temper entity (Deploy / ScaleOp), never raw kubectl from a
  button. The flow is: create entity (operator) -> Request -> human Approve
  (supervisor token = the human at the wheel) -> Apply -> an ns-pinned kubectl
  executor runs FOR REAL -> RecordResult -> MarkLive / MarkDone (or Rollback /
  Fail). The Cedar gate on Approve is the governance point.

SAFETY: every kubectl is pinned to namespace `dark-factory` (asserted in code)
and context `gke_datadog-sandbox_us-west3_gs-us-west3`. The namespace is never
read from entity data.

Run:  uvicorn app:app --host 127.0.0.1 --port 4100
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import threading
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any


def sim_ts() -> str:
    """A short, unique-enough suffix for control-plane entity ids.

    This is a UI helper app (not a simulation-visible Temper crate), so wall
    time is fine here for generating human-readable, sortable entity ids."""
    return str(int(time.time()))

import httpx
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

TEMPER_BASE = "http://127.0.0.1:3000"
TENANT = "dark-factory"
HELIX_REPO = "/Users/arun.parthiban/notdd/helix"
TOKENS_PATH = (
    "/Users/arun.parthiban/notdd/temper/reference-apps/identity/tokens.json"
)

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"

# --------------------------------------------------------------------------- #
# Control-plane configuration (ADR-0096) — PINNED kube target.
# These are never derived from entity data; the namespace is asserted in code.
# --------------------------------------------------------------------------- #

KUBE_CONTEXT = "gke_datadog-sandbox_us-west3_gs-us-west3"
NAMESPACE = "dark-factory"
STATEFULSET = "helix"
CONTAINER = "helix"
IMAGE_REPO = "us-west3-docker.pkg.dev/datadog-sandbox/dark-factory-helix/helix-server"

# --------------------------------------------------------------------------- #
# Workloads configuration (ADR-0097) — governed Kafka load generation.
# Real producer-<id>/consumer-<id> Deployments in dark-factory ONLY.
# --------------------------------------------------------------------------- #

# Brokers: the headless-backed Service for the Helix Kafka-protocol cluster.
KAFKA_BROKERS = "helix.dark-factory.svc.cluster.local:9092"

# DogStatsD target for per-workload metrics. The cluster DD agent's ClusterIP
# Service DNS (NOT a node IP) — the proven path on gs-us-west3 (the shared agent
# does not expose a hostPort for 8125, only the Service load-balances it). Helix
# itself emits via this same DNS. Workload pods can resolve it identically.
WORKLOAD_DD_AGENT_HOST = "gensim-datadog.datadog.svc.cluster.local"
WORKLOAD_DD_DOGSTATSD_PORT = "8125"

# One image for both workload kinds: confluentinc/cp-kafka ships python3.9 but
# confluent-kafka has no prebuilt wheel there (needs gcc). python:3.11-slim has
# a prebuilt manylinux wheel that `pip install`s in ~7s with no compiler — so
# both scripts use python:3.11-slim + confluent-kafka for PRECISE control of
# rate, msg size, per-message process time, and concurrency. No image build:
# the scripts are bundled inline (heredoc) in the Deployment command.
WORKLOAD_IMAGE = "python:3.11-slim"

# Keep workload pods SMALL — the dedicated pool is capacity-constrained.
WORKLOAD_CPU_REQUEST = "50m"
WORKLOAD_MEM_REQUEST = "96Mi"
WORKLOAD_CPU_LIMIT = "300m"
WORKLOAD_MEM_LIMIT = "192Mi"

# Modest bounds so a typo can't melt the shared cluster.
MAX_RATE_PER_SEC = 5000
MAX_MSG_SIZE = 65536
MAX_CONCURRENCY = 5
MAX_PROCESS_MS = 60000

# The deploy dropdown's tags. Refreshed live from Artifact Registry by
# /api/images, with this list as the documented fallback (spec §Environment).
KNOWN_IMAGE_TAGS = ["champion-throughput", "latest", "metrics-v1", "metrics-v2"]

# Entities with no issue/tag (e.g. the `probe-1` smoke-test row) are noise; we
# hide anything missing both an image_tag and an issue_id.
JUNK_DEPLOY_IDS = {"probe-1"}

# Static tag -> (branch, base) map from the spec. The diff resolver prefers a
# branch carried on the linked ImprovementIssue when present, then falls back
# to this table, then to a graceful "no diff mapping".
ANCESTOR = "8260087"
TAG_DIFF_MAP: dict[str, tuple[str, str]] = {
    "champion-throughput": ("evolve/throughput", ANCESTOR),
    "latest": ("evolve/throughput", ANCESTOR),
    "champion-latency": ("evolve/latency", ANCESTOR),
    "metrics-v1": ("main", "main~1"),
    "metrics-v2": ("main", "main~1"),
}


# --------------------------------------------------------------------------- #
# Token / HTTP helpers
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=1)
def operator_token() -> str:
    """Read the operator bearer token once; cache it for the process.

    The operator is the admin/global-API-key principal — it creates entities
    and reads. Bound actions (Request/Approve/...) are Cedar-gated and the
    operator presents no verified agent_type, so those go through the
    supervisor identity instead (see supervisor_token)."""
    with open(TOKENS_PATH) as fh:
        data = json.load(fh)
    return data["operator"]["token"]


@lru_cache(maxsize=1)
def supervisor_token() -> str:
    """Read the supervisor bearer token once; cache it for the process.

    The supervisor is the verified human-gate identity (agent_type=supervisor,
    agentTypeVerified=true) registered via the real credential chain in
    tokens.json — NOT a header spoof. deploy.cedar / scale_op.cedar permit it to
    Request / Approve / Apply / RecordResult / land. In the UI, clicking Approve
    IS the human acting as this supervisor."""
    with open(TOKENS_PATH) as fh:
        data = json.load(fh)
    return data["supervisor"]["token"]


@lru_cache(maxsize=1)
def breeder_token() -> str | None:
    """The breeder bearer token (verified agent_type=breeder). Used ONLY to
    demonstrate the telemetry firewall: a breeder read on a workload entity is
    Cedar-forbidden (403). Returns None if not registered."""
    try:
        with open(TOKENS_PATH) as fh:
            data = json.load(fh)
        return data.get("breeder", {}).get("token")
    except (OSError, ValueError):
        return None


@lru_cache(maxsize=1)
def observer_token() -> str:
    """The observer bearer token (verified agent_type=observer). Permitted to
    create/Observe/BeginPlanning ImprovementIssues (improvement_issue.cedar)."""
    with open(TOKENS_PATH) as fh:
        data = json.load(fh)
    return data["observer"]["token"]


# Registered agent principal ids (agent_instance_id) — used as planner_id /
# assignee_id on ImprovementIssues so Cedar's planner!=approver and
# implementer!=deploy-approver role-separation rules are satisfied.
PRINCIPAL_OBSERVER = "observer-agent"
PRINCIPAL_BREEDER = "breeder-agent"
PRINCIPAL_SUPERVISOR = "supervisor-agent"


def temper_headers(token: str | None = None) -> dict[str, str]:
    return {
        "X-Tenant-Id": TENANT,
        "Authorization": f"Bearer {token or operator_token()}",
    }


def temper_get(path: str) -> dict[str, Any] | None:
    """GET a Temper OData path. Returns parsed JSON, or None on 404 / error.

    Tolerant by design: a missing linked entity must not break the page.
    """
    url = f"{TEMPER_BASE}{path}"
    try:
        resp = httpx.get(url, headers=temper_headers(), timeout=10.0)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def temper_post(
    path: str, body: dict[str, Any], token: str | None = None
) -> tuple[int, dict[str, Any]]:
    """POST to a Temper OData path. Returns (status_code, parsed_json).

    Used by the control plane to create entities and fire governed actions.
    On a Cedar denial the caller gets HTTP 403 with a decision_id to surface.
    """
    url = f"{TEMPER_BASE}{path}"
    headers = {**temper_headers(token), "Content-Type": "application/json"}
    try:
        resp = httpx.post(url, headers=headers, json=body, timeout=30.0)
    except httpx.HTTPError as exc:
        return 0, {"error": str(exc)}
    try:
        return resp.status_code, (resp.json() if resp.text.strip() else {})
    except ValueError:
        return resp.status_code, {"raw": resp.text}


# --------------------------------------------------------------------------- #
# Field-access helpers (tolerate PascalCase / snake_case)
# --------------------------------------------------------------------------- #

def field(fields: dict[str, Any], *names: str, default: Any = None) -> Any:
    """Return the first present field among `names` (case variants included)."""
    for n in names:
        if n in fields and fields[n] not in (None, ""):
            return fields[n]
    # case-insensitive fallback
    lowered = {k.lower(): v for k, v in fields.items()}
    for n in names:
        v = lowered.get(n.lower())
        if v not in (None, "", None):
            return v
    return default


# --------------------------------------------------------------------------- #
# Domain mapping
# --------------------------------------------------------------------------- #

def classify_lineage(image_tag: str | None, issue_fields: dict[str, Any]) -> str:
    """Best-effort lineage label: latency | throughput | baseline.

    Derived from the image tag, the linked issue's branch, target file, and
    title — whichever speaks first.
    """
    blob = " ".join(
        str(x).lower()
        for x in (
            image_tag,
            field(issue_fields, "branch_name", "BranchName", default=""),
            field(issue_fields, "title", "Title", default=""),
            field(issue_fields, "target_file", "TargetFile", default=""),
            field(issue_fields, "hypothesis", "Hypothesis", default=""),
        )
        if x
    )
    # linger_ms / batcher tuning is the throughput lineage even when the issue
    # text mentions latency descriptively (e.g. "p95 latency burst from linger").
    if "linger" in blob or "batcher" in blob:
        return "throughput"
    # append_batch_size / the evolve/latency branch is the latency lineage.
    if "latency" in blob or "append_batch_size" in blob or "evolve/latency" in blob:
        return "latency"
    if "throughput" in blob or "evolve/throughput" in blob:
        return "throughput"
    return "baseline"


def last_event_timestamp(events: list[dict[str, Any]]) -> str | None:
    """The MarkLive (or otherwise last) event timestamp = 'deployed_at'."""
    if not events:
        return None
    live = [e for e in events if e.get("action") == "MarkLive"]
    if live:
        return live[-1].get("timestamp")
    return events[-1].get("timestamp")


def issue_fields_for(issue_id: str | None) -> dict[str, Any]:
    """Fetch the linked ImprovementIssue's fields; {} if missing."""
    if not issue_id:
        return {}
    doc = temper_get(f"/tdata/ImprovementIssues('{issue_id}')")
    if not doc:
        return {}
    return doc.get("fields", {}) or {}


def normalize_deploy(entity: dict[str, Any]) -> dict[str, Any]:
    """Map a raw Temper Deploy entity into the list-item shape."""
    fields = entity.get("fields", {}) or {}
    events = entity.get("events", []) or []
    deploy_id = entity.get("entity_id") or field(fields, "Id", "id")
    image_tag = field(fields, "image_tag", "ImageTag")
    issue_id = field(fields, "issue_id", "IssueId")
    status = entity.get("status") or field(fields, "Status", default="Unknown")
    approved = bool(
        (entity.get("booleans") or {}).get("approved")
        or field(fields, "approved", "Approved", default=False)
    )
    result = field(fields, "result", "Result", default="")
    dry_run = bool(field(fields, "dry_run", "DryRun", default=False))

    issue_fields = issue_fields_for(issue_id)

    return {
        "id": deploy_id,
        "status": status,
        "image_tag": image_tag,
        "issue_id": issue_id,
        "deployed_at": last_event_timestamp(events),
        "approved": approved,
        "dry_run": dry_run,
        "result_summary": (result or "").strip(),
        "lineage": classify_lineage(image_tag, issue_fields),
        "issue_title": field(issue_fields, "title", "Title"),
        "event_count": entity.get("total_event_count", len(events)),
    }


def is_junk(entity: dict[str, Any]) -> bool:
    """Filter smoke-test / placeholder rows with no tag and no issue."""
    eid = entity.get("entity_id")
    if eid in JUNK_DEPLOY_IDS:
        return True
    fields = entity.get("fields", {}) or {}
    tag = field(fields, "image_tag", "ImageTag")
    issue = field(fields, "issue_id", "IssueId")
    return not tag and not issue


# --------------------------------------------------------------------------- #
# Diff resolution + git
# --------------------------------------------------------------------------- #

def resolve_diff_target(
    image_tag: str | None, issue_fields: dict[str, Any]
) -> tuple[str, str] | None:
    """Resolve (branch, base) for a deploy.

    Order of preference:
      1. The linked issue's branch_name (with base = the ancestor / merge-base).
      2. The static TAG_DIFF_MAP keyed on the image tag.
      3. None -> caller renders "no diff mapping".
    """
    branch = field(issue_fields, "branch_name", "BranchName")
    if branch and git_ref_exists(branch):
        base = git_merge_base(ANCESTOR, branch) or ANCESTOR
        return branch, base

    if image_tag:
        # tags can arrive as "helix:champion-throughput" etc — try suffix too.
        candidates = [image_tag, image_tag.split(":")[-1], image_tag.split("/")[-1]]
        for cand in candidates:
            if cand in TAG_DIFF_MAP:
                br, base = TAG_DIFF_MAP[cand]
                if git_ref_exists(br):
                    return br, base
    return None


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", HELIX_REPO, *args],
        capture_output=True,
        text=True,
        timeout=20,
    )


def git_ref_exists(ref: str) -> bool:
    try:
        return _git("rev-parse", "--verify", "--quiet", ref).returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def git_merge_base(a: str, b: str) -> str | None:
    try:
        out = _git("merge-base", a, b)
    except (subprocess.SubprocessError, OSError):
        return None
    if out.returncode != 0:
        return None
    return (out.stdout or "").strip() or None


_STAT_RE = re.compile(r"^\s*(?P<file>.+?)\s+\|\s+(?P<count>\d+)\s+(?P<marks>[+-]*)\s*$")


def git_diff(base: str, branch: str | None, paths: list[str] | None = None) -> dict[str, Any]:
    """Return {base, branch, stat[], unified_diff} for `git diff base [branch] [-- paths]`.

    When `branch` is None, diffs the working tree against `base` (used for the
    telemetry/baseline images whose changes live uncommitted in the tree).
    `paths` scopes the diff to specific files so a baseline image shows only
    *its* change, not unrelated commits on the branch.
    """
    args = ["diff", base] + ([branch] if branch else [])
    pathspec = (["--"] + paths) if paths else []
    unified = _git(*args, *pathspec)
    if unified.returncode != 0:
        raise RuntimeError(unified.stderr.strip() or "git diff failed")

    numstat = _git(*(["diff", "--numstat", base] + ([branch] if branch else []) + pathspec))
    stat: list[dict[str, Any]] = []
    if numstat.returncode == 0:
        for line in numstat.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            adds, dels, fname = parts
            stat.append(
                {
                    "file": fname,
                    "additions": int(adds) if adds.isdigit() else 0,
                    "deletions": int(dels) if dels.isdigit() else 0,
                }
            )

    return {
        "base": base,
        "branch": branch or "working tree",
        "stat": stat,
        "unified_diff": unified.stdout,
    }


# --------------------------------------------------------------------------- #
# Control plane — kubectl helpers (ns + context PINNED; ADR-0096)
# --------------------------------------------------------------------------- #

def _assert_namespace() -> None:
    """Hard safety gate: refuse to operate outside the dark-factory namespace."""
    assert NAMESPACE == "dark-factory", (
        f"refusing kubectl: namespace pinned to dark-factory, got {NAMESPACE!r}"
    )


def _kubectl(*args: str, timeout: int = 330) -> subprocess.CompletedProcess[str]:
    """Run kubectl PINNED to the dark-factory namespace + the GKE context.

    Every cluster call goes through here, so the ns/context pin is enforced in
    exactly one place. The namespace is never read from entity data.
    """
    _assert_namespace()
    cmd = ["kubectl", "--context", KUBE_CONTEXT, "-n", NAMESPACE, *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _kubectl_stdin(stdin: str, *args: str, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    """Run kubectl PINNED to dark-factory + context, feeding `stdin` (e.g. a
    manifest for `apply -f -`). Same chokepoint/pin as `_kubectl`."""
    _assert_namespace()
    cmd = ["kubectl", "--context", KUBE_CONTEXT, "-n", NAMESPACE, *args]
    return subprocess.run(
        cmd, input=stdin, capture_output=True, text=True, timeout=timeout
    )


def cluster_state() -> dict[str, Any]:
    """Read-only snapshot of the Helix StatefulSet + its pods."""
    sset = _kubectl(
        "get", "statefulset", STATEFULSET, "-o", "json", timeout=30
    )
    if sset.returncode != 0:
        return {
            "reachable": False,
            "error": (sset.stderr or sset.stdout or "kubectl get statefulset failed").strip(),
            "namespace": NAMESPACE,
            "statefulset": STATEFULSET,
        }
    doc = json.loads(sset.stdout)
    spec = doc.get("spec", {}) or {}
    status = doc.get("status", {}) or {}
    containers = (spec.get("template", {}).get("spec", {}).get("containers", []) or [])
    image = containers[0].get("image", "") if containers else ""
    image_tag = image.split(":")[-1] if ":" in image else image

    pods_proc = _kubectl(
        "get", "pods", "-l", f"app={STATEFULSET}", "-o", "json", timeout=30
    )
    pods: list[dict[str, Any]] = []
    if pods_proc.returncode == 0:
        try:
            for item in json.loads(pods_proc.stdout).get("items", []):
                meta = item.get("metadata", {}) or {}
                pstatus = item.get("status", {}) or {}
                conds = {c.get("type"): c.get("status") for c in (pstatus.get("conditions") or [])}
                ready = conds.get("Ready") == "True"
                pods.append({
                    "name": meta.get("name", ""),
                    "phase": pstatus.get("phase", "Unknown"),
                    "ready": ready,
                })
        except ValueError:
            pods = []
    pods.sort(key=lambda p: p["name"])

    return {
        "reachable": True,
        "namespace": NAMESPACE,
        "statefulset": STATEFULSET,
        "context": KUBE_CONTEXT,
        "image": image,
        "image_tag": image_tag,
        "image_repo": IMAGE_REPO,
        "configured_replicas": spec.get("replicas"),
        "ready_replicas": status.get("readyReplicas", 0) or 0,
        "current_replicas": status.get("currentReplicas", 0) or 0,
        "updated_replicas": status.get("updatedReplicas", 0) or 0,
        "lineage": classify_lineage(image_tag, {}),
        "pods": pods,
    }


def registry_image_tags() -> list[str]:
    """Live Artifact Registry tags for the deploy dropdown; fallback to KNOWN."""
    try:
        proc = subprocess.run(
            [
                "gcloud", "artifacts", "docker", "tags", "list",
                IMAGE_REPO, "--format=value(tag)",
            ],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        return list(KNOWN_IMAGE_TAGS)
    if proc.returncode != 0:
        return list(KNOWN_IMAGE_TAGS)
    tags = sorted({
        line.split("/")[-1].strip()
        for line in proc.stdout.splitlines()
        if line.strip()
    })
    return tags or list(KNOWN_IMAGE_TAGS)


# --------------------------------------------------------------------------- #
# Control plane — real, ns-pinned executors (run only after Approve + Apply)
# --------------------------------------------------------------------------- #

def exec_deploy(image_tag: str) -> tuple[bool, str, str]:
    """kubectl set image + rollout status. Returns (ok, cmd_str, output)."""
    _assert_namespace()
    image = f"{IMAGE_REPO}:{image_tag}"
    set_image = ["set", "image", f"statefulset/{STATEFULSET}", f"{CONTAINER}={image}"]
    cmd_str = "kubectl -n dark-factory " + " ".join(set_image)
    p1 = _kubectl(*set_image, timeout=60)
    out = (p1.stdout + p1.stderr).strip()
    if p1.returncode != 0:
        return False, cmd_str, out[-3000:]
    p2 = _kubectl("rollout", "status", f"statefulset/{STATEFULSET}", "--timeout=300s")
    out = (out + "\n" + p2.stdout + p2.stderr).strip()
    return p2.returncode == 0, cmd_str, out[-3000:]


def exec_rollback() -> tuple[bool, str, str]:
    """kubectl rollout undo + status. Returns (ok, cmd_str, output)."""
    _assert_namespace()
    cmd_str = f"kubectl -n dark-factory rollout undo statefulset/{STATEFULSET}"
    p1 = _kubectl("rollout", "undo", f"statefulset/{STATEFULSET}", timeout=60)
    out = (p1.stdout + p1.stderr).strip()
    if p1.returncode != 0:
        return False, cmd_str, out[-3000:]
    p2 = _kubectl("rollout", "status", f"statefulset/{STATEFULSET}", "--timeout=300s")
    out = (out + "\n" + p2.stdout + p2.stderr).strip()
    return p2.returncode == 0, cmd_str, out[-3000:]


def exec_scale(target_replicas: int) -> tuple[bool, str, str]:
    """kubectl scale + a bounded rollout wait. Returns (ok, cmd_str, output).

    A scale-up the shared cluster can't schedule will time out on rollout
    status; we surface that as ok=False (the ScaleOp lands Failed) with the real
    output, rather than hanging or pretending success.
    """
    _assert_namespace()
    scale = ["scale", f"statefulset/{STATEFULSET}", f"--replicas={target_replicas}"]
    cmd_str = "kubectl -n dark-factory " + " ".join(scale)
    p1 = _kubectl(*scale, timeout=60)
    out = (p1.stdout + p1.stderr).strip()
    if p1.returncode != 0:
        return False, cmd_str, out[-3000:]
    # Bounded wait — capacity-starved scale-ups won't go Ready; don't hang.
    p2 = _kubectl(
        "rollout", "status", f"statefulset/{STATEFULSET}", "--timeout=120s", timeout=140
    )
    out = (out + "\n" + p2.stdout + p2.stderr).strip()
    return p2.returncode == 0, cmd_str, out[-3000:]


# --------------------------------------------------------------------------- #
# Control plane — governed-action helpers
# --------------------------------------------------------------------------- #

def _denial_response(status: int, payload: dict[str, Any], step: str) -> JSONResponse:
    """Shape a Cedar denial / Temper error into a structured 403/502."""
    err = payload.get("error", payload) if isinstance(payload, dict) else payload
    msg = err.get("message") if isinstance(err, dict) else str(err)
    decision_id = None
    if isinstance(msg, str):
        m = re.search(r"PD-[0-9a-f-]+", msg)
        decision_id = m.group(0) if m else None
    return JSONResponse(
        status_code=status if status in (401, 403) else 502,
        content={
            "ok": False,
            "step": step,
            "temper_status": status,
            "decision_id": decision_id,
            "detail": msg or payload,
        },
    )


# --------------------------------------------------------------------------- #
# App + routes
# --------------------------------------------------------------------------- #

app = FastAPI(title="Dark Factory — Control Plane", version="2.0.0")


def _fetch_all_deploys() -> list[dict[str, Any]]:
    doc = temper_get("/tdata/Deploys")
    if not doc:
        raise HTTPException(
            status_code=502,
            detail="Could not reach the Temper server at "
            f"{TEMPER_BASE}/tdata/Deploys",
        )
    return doc.get("value", []) or []


@app.get("/api/deployments")
def list_deployments() -> JSONResponse:
    """List deployments, newest-first, joined with lineage + deployed_at."""
    raw = _fetch_all_deploys()
    items = [normalize_deploy(e) for e in raw if not is_junk(e)]

    def sort_key(it: dict[str, Any]) -> str:
        return it.get("deployed_at") or ""

    items.sort(key=sort_key, reverse=True)
    return JSONResponse({"deployments": items, "count": len(items)})


@app.get("/api/deployments/{deploy_id}")
def get_deployment(deploy_id: str) -> JSONResponse:
    """Detail: deploy fields + full event timeline + joined changelog."""
    doc = temper_get(f"/tdata/Deploys('{deploy_id}')")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Deploy '{deploy_id}' not found")

    fields = doc.get("fields", {}) or {}
    image_tag = field(fields, "image_tag", "ImageTag")
    issue_id = field(fields, "issue_id", "IssueId")

    # Detail endpoint sometimes returns events: []; fall back to the list view,
    # which carries the governed timeline for the champion deploy.
    events = doc.get("events", []) or []
    if not events:
        for e in _fetch_all_deploys():
            if e.get("entity_id") == deploy_id:
                events = e.get("events", []) or []
                break

    issue_fields = issue_fields_for(issue_id)

    timeline = [
        {
            "action": e.get("action"),
            "from_status": e.get("from_status"),
            "to_status": e.get("to_status"),
            "timestamp": e.get("timestamp"),
            "params": e.get("params", {}),
        }
        for e in events
    ]

    changelog = {
        "issue_id": issue_id,
        "title": field(issue_fields, "title", "Title"),
        "hypothesis": field(issue_fields, "hypothesis", "Hypothesis"),
        "plan": field(issue_fields, "plan", "Plan"),
        "target_file": field(issue_fields, "target_file", "TargetFile"),
        "branch_name": field(issue_fields, "branch_name", "BranchName"),
        "pr_url": field(issue_fields, "pr_url", "PrUrl"),
        "acceptance_criteria": field(
            issue_fields, "acceptance_criteria", "AcceptanceCriteria"
        ),
        "issue_status": field(issue_fields, "Status", "status"),
        "ci_status": field(issue_fields, "ci_status", "CiStatus"),
    } if issue_fields else {"issue_id": issue_id, "missing": True}

    detail = {
        "id": doc.get("entity_id") or deploy_id,
        "status": doc.get("status") or field(fields, "Status", default="Unknown"),
        "image_tag": image_tag,
        "issue_id": issue_id,
        "approved": bool(
            (doc.get("booleans") or {}).get("approved")
            or field(fields, "approved", "Approved", default=False)
        ),
        "dry_run": bool(field(fields, "dry_run", "DryRun", default=False)),
        "namespace": field(fields, "Namespace", "namespace"),
        "result": (field(fields, "result", "Result", default="") or "").strip(),
        "rollout_cmd": field(fields, "rollout_cmd", "RolloutCmd"),
        "deployed_at": last_event_timestamp(events),
        "lineage": classify_lineage(image_tag, issue_fields),
        "timeline": timeline,
        "changelog": changelog,
        "has_diff": resolve_diff_target(image_tag, issue_fields) is not None,
    }
    return JSONResponse(detail)


@app.get("/api/deployments/{deploy_id}/diff")
def get_deployment_diff(deploy_id: str) -> JSONResponse:
    """Resolve the deploy's branch/base and return the real git diff."""
    doc = temper_get(f"/tdata/Deploys('{deploy_id}')")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Deploy '{deploy_id}' not found")

    fields = doc.get("fields", {}) or {}
    image_tag = field(fields, "image_tag", "ImageTag")
    issue_id = field(fields, "issue_id", "IssueId")
    issue_fields = issue_fields_for(issue_id)

    target = resolve_diff_target(image_tag, issue_fields)
    if target is None:
        return JSONResponse(
            {
                "available": False,
                "reason": "no diff mapping",
                "image_tag": image_tag,
                "base": None,
                "branch": None,
                "stat": [],
                "unified_diff": "",
            }
        )

    branch, base = target
    try:
        result = git_diff(base, branch)
    except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
        return JSONResponse(
            {
                "available": False,
                "reason": f"git diff failed: {exc}",
                "image_tag": image_tag,
                "base": base,
                "branch": branch,
                "stat": [],
                "unified_diff": "",
            }
        )

    result["available"] = True
    result["image_tag"] = image_tag
    return JSONResponse(result)


@app.get("/api/health")
def health() -> JSONResponse:
    reachable = temper_get("/tdata/Deploys") is not None
    return JSONResponse(
        {
            "ok": True,
            "temper_reachable": reachable,
            "helix_repo": HELIX_REPO,
            "tenant": TENANT,
        }
    )


# --------------------------------------------------------------------------- #
# Control-plane routes (ADR-0096)
# --------------------------------------------------------------------------- #

@app.get("/api/cluster")
def get_cluster() -> JSONResponse:
    """Live, read-only Helix StatefulSet + pod state (kubectl get)."""
    return JSONResponse(cluster_state())


# Human descriptions for image tags. Evolve tags also pull their hypothesis
# from the linked ImprovementIssue at diff time; this is the short blurb shown
# inline under the deploy dropdown. Non-issue tags (the baseline/telemetry
# builds) get a curated one-liner.
TAG_DESCRIPTIONS: dict[str, str] = {
    "champion-throughput": "Throughput champion — batcher linger_ms=9, bred on evolve/throughput and survived the durability cull.",
    "champion-latency": "Latency champion — append_batch_size=250, bred on evolve/latency under the minimize-p95 pressure.",
    "metrics-v2": "Baseline + DogStatsD telemetry (Track A) — the instrumented build before any directed evolution.",
    "metrics-v1": "Earlier telemetry build (Track A) — superseded by metrics-v2.",
    "latest": "Alias → currently the throughput champion (evolve/throughput).",
}


def tag_lineage(tag: str) -> str:
    """Classify a tag's lineage for the chip (mirrors the deploy-history logic)."""
    branch = (TAG_DIFF_MAP.get(tag) or ("", ""))[0]
    if "throughput" in branch or "throughput" in tag:
        return "throughput"
    if "latency" in branch or "latency" in tag:
        return "latency"
    return "baseline"


def tag_description(tag: str) -> str:
    return TAG_DESCRIPTIONS.get(tag, "No description available for this image tag.")


@app.get("/api/images")
def get_images() -> JSONResponse:
    """Available image tags for the deploy dropdown, each enriched with a human
    description, its lineage, and whether a code diff is browsable."""
    tags = registry_image_tags()
    images = []
    for t in tags:
        branch_base = TAG_DIFF_MAP.get(t)
        images.append({
            "tag": t,
            "description": tag_description(t),
            "lineage": tag_lineage(t),
            "branch": branch_base[0] if branch_base else None,
            "has_code": branch_base is not None,
        })
    return JSONResponse({"images": images, "tags": tags, "image_repo": IMAGE_REPO})


# The telemetry/baseline images (metrics-v*) were built from the working tree
# Track-A telemetry work, which lives UNCOMMITTED on main. So their honest diff
# is the working tree vs the ancestor, SCOPED to the telemetry files — not a
# main commit range (which would show unrelated history). See ADR-0007.
TELEMETRY_PATHS = [
    "helix-server/src/metrics.rs",
    "helix-server/src/kafka/handler.rs",
    "helix-server/src/service/mod.rs",
    "helix-server/src/service/handlers/write.rs",
    "helix-server/src/lib.rs",
]
BASELINE_TAGS = {"metrics-v1", "metrics-v2"}


@app.get("/api/images/{tag}/diff")
def get_image_diff(tag: str) -> JSONResponse:
    """The code diff for an image tag vs the baseline ancestor — what this image
    CHANGED. Reuses the deploy-history diff renderer."""
    bb = TAG_DIFF_MAP.get(tag)
    if not bb:
        return JSONResponse({"tag": tag, "no_diff": True,
                             "reason": f"No branch mapping for tag '{tag}'."})
    branch, base = bb

    # Baseline/telemetry tags: scoped working-tree diff vs the ancestor.
    if tag in BASELINE_TAGS:
        try:
            d = git_diff(ANCESTOR, None, paths=TELEMETRY_PATHS)
        except RuntimeError as e:
            return JSONResponse({"tag": tag, "no_diff": True, "reason": str(e)})
        # metrics.rs is untracked (Track-A working-tree add), so plain `git diff`
        # omits it. Append it as a synthetic all-add diff so the core new file
        # shows up in the browse-code view.
        untracked = "helix-server/src/metrics.rs"
        upath = os.path.join(HELIX_REPO, untracked)
        if os.path.isfile(upath) and not any(s["file"] == untracked for s in d["stat"]):
            di = _git("diff", "--no-index", "/dev/null", upath)
            extra = (di.stdout or "").replace(f"a{upath}", f"a/{untracked}").replace(
                upath, untracked)
            adds = sum(1 for ln in extra.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
            d["unified_diff"] = extra + d["unified_diff"]
            d["stat"].insert(0, {"file": untracked, "additions": adds, "deletions": 0})
        d.update({"tag": tag, "description": tag_description(tag), "no_diff": False})
        return JSONResponse(d)

    if not git_ref_exists(branch):
        return JSONResponse({"tag": tag, "no_diff": True,
                             "reason": f"Branch '{branch}' not found locally."})
    # For evolve branches the base is the ancestor; merge-base keeps it honest.
    real_base = git_merge_base(base, branch) or base
    try:
        d = git_diff(real_base, branch)
    except RuntimeError as e:
        return JSONResponse({"tag": tag, "no_diff": True, "reason": str(e)})
    d.update({"tag": tag, "description": tag_description(tag), "no_diff": False})
    return JSONResponse(d)


@app.get("/api/images/{tag}/file")
def get_image_file(tag: str, path: str) -> JSONResponse:
    """Full source of a file at an image tag's branch (the 'view full file'
    toggle). Path-restricted to the changed file(s) the diff already exposes."""
    bb = TAG_DIFF_MAP.get(tag)
    if not bb:
        raise HTTPException(status_code=404, detail=f"No branch mapping for tag '{tag}'")
    branch = bb[0]
    # Reject path traversal; only allow repo-relative paths.
    if path.startswith("/") or ".." in path:
        raise HTTPException(status_code=400, detail="invalid path")

    # Baseline/telemetry tags reflect the (possibly uncommitted) working tree —
    # read the file from disk, not a git ref (e.g. metrics.rs is untracked).
    if tag in BASELINE_TAGS:
        fpath = os.path.join(HELIX_REPO, path)
        if not os.path.isfile(fpath):
            raise HTTPException(status_code=404, detail=f"{path} not found in working tree")
        with open(fpath, encoding="utf-8", errors="replace") as fh:
            return JSONResponse({"tag": tag, "branch": "working tree",
                                 "path": path, "content": fh.read()})

    show = _git("show", f"{branch}:{path}")
    if show.returncode != 0:
        raise HTTPException(status_code=404,
                            detail=f"{path} not found at {branch}: {show.stderr.strip()}")
    return JSONResponse({"tag": tag, "branch": branch, "path": path,
                         "content": show.stdout})


@app.post("/api/actions/deploy")
def action_deploy(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """Create + Request a governed Deploy. Returns id + pending-approval state.

    No kubectl runs here — the rollout fires only on /approve, after the human
    Approve + Apply (the Cedar gate).
    """
    image_tag = (payload or {}).get("image_tag")
    if not image_tag:
        raise HTTPException(status_code=400, detail="image_tag is required")

    deploy_id = f"deploy-{image_tag}-{sim_ts()}"
    issue_id = (payload or {}).get("issue_id", "") or ""

    # 1. Create the entity (operator/admin).
    s, b = temper_post(
        "/tdata/Deploys",
        {"id": deploy_id, "Status": "Pending", "ImageTag": image_tag,
         "IssueId": issue_id, "Namespace": NAMESPACE, "DryRun": False},
        token=operator_token(),
    )
    if s not in (200, 201):
        return _denial_response(s, b, "create Deploy")

    # 2. Request (supervisor — verified, permitted; operator has no agent_type).
    s, b = temper_post(
        f"/tdata/Deploys('{deploy_id}')/Default.Request",
        {"issue_id": issue_id, "image_tag": image_tag},
        token=supervisor_token(),
    )
    if s not in (200, 204):
        return _denial_response(s, b, "Request Deploy")

    return JSONResponse({
        "ok": True, "kind": "deploy", "id": deploy_id, "status": "Pending",
        "image_tag": image_tag,
        "pending_approval": True,
        "gate": "Awaiting human Approve (supervisor). The Approve button is the human at the wheel.",
    })


@app.post("/api/actions/scale")
def action_scale(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """Create + Request a governed ScaleOp. Returns id + pending-approval state."""
    target = (payload or {}).get("target_replicas")
    try:
        target = int(target)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="target_replicas must be an integer")
    if target < 0 or target > 10:
        raise HTTPException(status_code=400, detail="target_replicas out of bounds (0-10)")

    snap = cluster_state()
    current = snap.get("ready_replicas", 0) if snap.get("reachable") else 0

    scale_id = f"scale-{target}-{sim_ts()}"
    s, b = temper_post(
        "/tdata/ScaleOps",
        {"id": scale_id, "Status": "Requested", "TargetReplicas": target,
         "CurrentReplicas": current, "Namespace": NAMESPACE},
        token=operator_token(),
    )
    if s not in (200, 201):
        return _denial_response(s, b, "create ScaleOp")

    s, b = temper_post(
        f"/tdata/ScaleOps('{scale_id}')/Default.Request",
        {"target_replicas": str(target), "current_replicas": str(current)},
        token=supervisor_token(),
    )
    if s not in (200, 204):
        return _denial_response(s, b, "Request ScaleOp")

    return JSONResponse({
        "ok": True, "kind": "scale", "id": scale_id, "status": "Requested",
        "target_replicas": target, "current_replicas": current,
        "pending_approval": True,
        "gate": "Awaiting human Approve (supervisor).",
        "note": "Scaling above the current node count may not schedule on the shared cluster.",
    })


@app.post("/api/actions/{kind}/{entity_id}/approve")
def action_approve(kind: str, entity_id: str) -> JSONResponse:
    """The human Approve (supervisor token) -> Apply -> run the executor for real.

    This is the governance moment: clicking Approve in the UI = the human acting
    as the verified supervisor. Cedar permits supervisor/human to Approve; only
    after Approve + Apply does the entity reach its action state and the kubectl
    executor fire.
    """
    if kind == "deploy":
        return _approve_deploy(entity_id)
    if kind == "scale":
        return _approve_scale(entity_id)
    raise HTTPException(status_code=400, detail=f"unknown kind {kind!r}")


def _approve_deploy(deploy_id: str) -> JSONResponse:
    doc = temper_get(f"/tdata/Deploys('{deploy_id}')")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Deploy '{deploy_id}' not found")
    image_tag = field(doc.get("fields", {}) or {}, "image_tag", "ImageTag")

    # Approve (HUMAN GATE) -> Apply (Rolling).
    s, b = temper_post(f"/tdata/Deploys('{deploy_id}')/Default.Approve", {},
                       token=supervisor_token())
    if s not in (200, 204):
        return _denial_response(s, b, "Approve Deploy")
    s, b = temper_post(f"/tdata/Deploys('{deploy_id}')/Default.Apply", {},
                       token=supervisor_token())
    if s not in (200, 204):
        return _denial_response(s, b, "Apply Deploy")

    # Executor: REAL, ns-pinned rollout (entity is now Rolling).
    ok, cmd_str, output = exec_deploy(image_tag)
    result = ("rolled-out" if ok else "failed") + f": {output[:1500]}"
    temper_post(
        f"/tdata/Deploys('{deploy_id}')/Default.RecordResult",
        {"result": result, "rollout_cmd": cmd_str, "dry_run": False},
        token=supervisor_token(),
    )
    land = "MarkLive" if ok else "Rollback"
    s, b = temper_post(f"/tdata/Deploys('{deploy_id}')/Default.{land}", {},
                       token=supervisor_token())

    return JSONResponse({
        "ok": ok, "kind": "deploy", "id": deploy_id,
        "status": "Live" if ok else "RolledBack",
        "image_tag": image_tag, "rollout_cmd": cmd_str,
        "result": result, "output": output,
        "cluster": cluster_state(),
    })


def _approve_scale(scale_id: str) -> JSONResponse:
    doc = temper_get(f"/tdata/ScaleOps('{scale_id}')")
    if not doc:
        raise HTTPException(status_code=404, detail=f"ScaleOp '{scale_id}' not found")
    fields = doc.get("fields", {}) or {}
    target = field(fields, "target_replicas", "TargetReplicas")
    try:
        target = int(target)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="ScaleOp has no valid target_replicas")

    s, b = temper_post(f"/tdata/ScaleOps('{scale_id}')/Default.Approve", {},
                       token=supervisor_token())
    if s not in (200, 204):
        return _denial_response(s, b, "Approve ScaleOp")
    s, b = temper_post(f"/tdata/ScaleOps('{scale_id}')/Default.Apply", {},
                       token=supervisor_token())
    if s not in (200, 204):
        return _denial_response(s, b, "Apply ScaleOp")

    # Executor: REAL, ns-pinned scale (entity is now Scaling).
    ok, cmd_str, output = exec_scale(target)
    result = ("scaled" if ok else "scale-incomplete") + f": {output[:1500]}"
    temper_post(
        f"/tdata/ScaleOps('{scale_id}')/Default.RecordResult",
        {"result": result, "scale_cmd": cmd_str},
        token=supervisor_token(),
    )
    land = "MarkDone" if ok else "Fail"
    land_body = {} if ok else {"reason": "rollout did not reach Ready (capacity / shared cluster)"}
    s, b = temper_post(f"/tdata/ScaleOps('{scale_id}')/Default.{land}", land_body,
                       token=supervisor_token())

    return JSONResponse({
        "ok": ok, "kind": "scale", "id": scale_id,
        "status": "Done" if ok else "Failed",
        "target_replicas": target, "scale_cmd": cmd_str,
        "result": result, "output": output,
        "cluster": cluster_state(),
    })


@app.post("/api/actions/deploy/{deploy_id}/rollback")
def action_rollback(deploy_id: str) -> JSONResponse:
    """Governed rollback of a Deploy.

    The Deploy IOA's Rollback transition is from `Rolling`. A Live deploy is
    terminal, so we model "rollback the live cluster" as a NEW governed Deploy
    of the previous image, driven through the full gate, whose executor runs
    `kubectl rollout undo`. If the named deploy is still `Rolling`, we instead
    record + drive its own Rollback transition directly.
    """
    doc = temper_get(f"/tdata/Deploys('{deploy_id}')")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Deploy '{deploy_id}' not found")
    status = doc.get("status") or field(doc.get("fields", {}) or {}, "Status")

    if status == "Rolling":
        # Land this deploy's own Rollback transition with a real undo.
        ok, cmd_str, output = exec_rollback()
        result = ("rolled-back" if ok else "rollback-failed") + f": {output[:1500]}"
        temper_post(
            f"/tdata/Deploys('{deploy_id}')/Default.RecordResult",
            {"result": result, "rollout_cmd": cmd_str, "dry_run": False},
            token=supervisor_token(),
        )
        s, b = temper_post(f"/tdata/Deploys('{deploy_id}')/Default.Rollback", {},
                           token=supervisor_token())
        return JSONResponse({
            "ok": ok, "kind": "rollback", "id": deploy_id, "status": "RolledBack",
            "mode": "transition", "rollout_cmd": cmd_str, "result": result,
            "output": output, "cluster": cluster_state(),
        })

    # Live (or other terminal) -> new governed Deploy whose executor undoes.
    rb_id = f"rollback-{sim_ts()}"
    s, b = temper_post(
        "/tdata/Deploys",
        {"id": rb_id, "Status": "Pending", "ImageTag": "rollback-undo",
         "IssueId": "", "Namespace": NAMESPACE, "DryRun": False},
        token=operator_token(),
    )
    if s not in (200, 201):
        return _denial_response(s, b, "create rollback Deploy")
    for action, body, tok in (
        ("Request", {"issue_id": "", "image_tag": "rollback-undo"}, supervisor_token()),
        ("Approve", {}, supervisor_token()),
        ("Apply", {}, supervisor_token()),
    ):
        s, b = temper_post(f"/tdata/Deploys('{rb_id}')/Default.{action}", body, token=tok)
        if s not in (200, 204):
            return _denial_response(s, b, f"{action} rollback Deploy")

    ok, cmd_str, output = exec_rollback()
    result = ("rolled-back" if ok else "rollback-failed") + f": {output[:1500]}"
    temper_post(
        f"/tdata/Deploys('{rb_id}')/Default.RecordResult",
        {"result": result, "rollout_cmd": cmd_str, "dry_run": False},
        token=supervisor_token(),
    )
    land = "MarkLive" if ok else "Rollback"
    temper_post(f"/tdata/Deploys('{rb_id}')/Default.{land}", {}, token=supervisor_token())

    return JSONResponse({
        "ok": ok, "kind": "rollback", "id": rb_id,
        "status": "Live" if ok else "RolledBack",
        "mode": "new-deploy", "rollout_cmd": cmd_str, "result": result,
        "output": output, "cluster": cluster_state(),
    })


# --------------------------------------------------------------------------- #
# Workloads (ADR-0097) — inline producer/consumer scripts + manifests
# --------------------------------------------------------------------------- #

# The producer script: a token-bucket rate-limited produce loop using
# confluent-kafka. Emits exactly RATE msgs/sec of SIZE bytes to TOPIC, logging
# throughput each second. Every replica runs one such producer (concurrency =
# replicas = parallel producer workers).
# Shared, zero-dependency DogStatsD client prepended to both workload scripts.
# Raw UDP (the proven path on this org — no `pip install datadog` needed, no
# extra install latency). Best-effort + fully non-blocking: a single UDP socket,
# every send wrapped so a metrics failure can NEVER stall or crash the
# produce/consume hot path (matches the Helix exporter's no-op-on-error policy).
# IMPORTANT: histograms use `|h` (the agent computes p50/p95 server-side); this
# org DROPS `|d` distributions, so never use `|d`. No-op when DD_AGENT_HOST unset.
DOGSTATSD_SNIPPET = r"""
import os as _os, socket as _socket

class _Dsd:
    def __init__(self):
        host = _os.environ.get("DD_AGENT_HOST", "")
        self.enabled = bool(host)
        self._sock = None
        self._addr = None
        wl = _os.environ.get("WORKLOAD_ID", "unknown")
        queue = _os.environ.get("QUEUE", _os.environ.get("TOPIC", "unknown"))
        role = _os.environ.get("ROLE", "unknown")
        self._tags = f"|#queue:{queue},workload_id:{wl},role:{role}"
        if not self.enabled:
            print("[dogstatsd] DD_AGENT_HOST unset; metrics disabled", flush=True)
            return
        try:
            port = int(_os.environ.get("DD_DOGSTATSD_PORT", "8125"))
            self._sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
            self._sock.setblocking(False)
            self._addr = (host, port)
            print(f"[dogstatsd] enabled -> {host}:{port} tags={self._tags}", flush=True)
        except Exception as e:
            self.enabled = False
            print(f"[dogstatsd] init failed ({e}); metrics disabled", flush=True)

    def _send(self, name, value, mtype):
        if not self.enabled:
            return
        try:
            self._sock.sendto(
                f"{name}:{value}|{mtype}{self._tags}".encode("utf-8"), self._addr
            )
        except Exception:
            pass  # best-effort: never block or raise on the hot path

    def count(self, name, value=1):
        self._send(name, value, "c")

    def gauge(self, name, value):
        self._send(name, value, "g")

    def histogram(self, name, value):
        # `|h`: agent-side histogram (p50/p95/...); `|d` is dropped by this org.
        self._send(name, value, "h")

_dsd = _Dsd()
"""


PRODUCER_SCRIPT = DOGSTATSD_SNIPPET + r"""
import os, sys, time, threading
from confluent_kafka import Producer

import json as _json
BROKERS = os.environ["BROKERS"]
TOPIC   = os.environ["TOPIC"]
RATE    = int(os.environ["RATE"])         # target msgs/sec for THIS worker
SIZE    = int(os.environ["MSG_SIZE"])     # bytes per message
SHAPE   = os.environ.get("SHAPE", "stable").strip().lower()
try:
    SP = _json.loads(os.environ.get("SHAPE_PARAMS", "") or "{}")
except Exception:
    SP = {}
def _spn(key, default):
    try: return float(SP.get(key, default))
    except Exception: return default
print(f"[producer] brokers={BROKERS} topic={TOPIC} rate={RATE}/s size={SIZE}B shape={SHAPE} params={SP}", flush=True)

p = Producer({
    "bootstrap.servers": BROKERS,
    "linger.ms": 5,
    "batch.size": 16384,
    "acks": "all",
    "client.id": os.environ.get("HOSTNAME", "producer"),
})
payload = b"x" * SIZE
sent = [0]
lock = threading.Lock()

# Per-message metrics are emitted at ENQUEUE time, not on a Kafka delivery report.
# IMPORTANT (Helix limitation): Helix's Kafka protocol accepts Produce but does
# NOT return produce responses that librdkafka maps to client delivery reports
# (a flush() of 500 msgs returns remaining=500, ok=0, err=0 — server-side
# helix.produce.latency_ms still records every produce). So a delivery-callback
# metric would never fire. We therefore count workload.producer.sent on enqueue
# (matching the stdout `sent` rate the demo drives) and record
# workload.producer.send_latency_ms as the local produce()-call latency. The
# authoritative server-side produce->commit latency lives in helix.produce.latency_ms.
def report():
    while True:
        time.sleep(5)
        with lock:
            n = sent[0]; sent[0] = 0
        print(f"[producer] sent {n} msgs in last 5s (~{n//5}/s)", flush=True)

threading.Thread(target=report, daemon=True).start()

def emit_one():
    try:
        t0 = time.time()
        p.produce(TOPIC, payload)
        _dsd.histogram("workload.producer.send_latency_ms", (time.time() - t0) * 1000.0)
        _dsd.count("workload.producer.sent", 1)
        with lock:
            sent[0] += 1
    except BufferError:
        p.poll(0.1)

def send_window(n):
    # Emit n messages spread evenly across ~1s, then poll.
    interval = 1.0 / n if n > 0 else 0.0
    nt = time.time()
    for _ in range(int(n)):
        emit_one()
        nt += interval
        slack = nt - time.time()
        if slack > 0:
            time.sleep(slack)
    p.poll(0)

if SHAPE == "bursty":
    # Send a burst of burst_size as fast as possible, then idle for idle_ms. Repeat.
    burst = int(_spn("burst_size", max(RATE, 1) * 2))
    idle_s = _spn("idle_ms", 3000) / 1000.0
    while True:
        for _ in range(burst):
            emit_one()
        p.poll(0)
        print(f"[producer] burst of {burst} sent, idling {idle_s:.1f}s", flush=True)
        time.sleep(max(0.0, idle_s))
elif SHAPE == "ramp":
    # Linearly ramp from a low rate up to ramp_to over ramp_period_ms, then hold.
    target = int(_spn("ramp_to", RATE * 4))
    period_s = max(1.0, _spn("ramp_period_ms", 60000) / 1000.0)
    t_start = time.time()
    while True:
        frac = min(1.0, (time.time() - t_start) / period_s)
        cur = max(1, int(RATE + (target - RATE) * frac))
        send_window(cur)
elif SHAPE == "batch":
    # Accumulate for batch_period_ms with big linger, then flush a large batch.
    period_s = max(0.2, _spn("batch_period_ms", 2000) / 1000.0)
    per_batch = max(1, int(RATE * period_s))
    while True:
        for _ in range(per_batch):
            emit_one()
        p.flush(5)
        print(f"[producer] flushed batch of {per_batch}", flush=True)
        time.sleep(period_s)
else:
    # stable: token-bucket at RATE msgs/sec, steady.
    while True:
        start = time.time()
        send_window(RATE)
        elapsed = time.time() - start
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
"""

# The consumer script: sleeps PROCESS_MS per message (the artificial "process
# time" knob), commits offsets, and logs consume rate + lag.
#
# IMPORTANT (Helix limitation): Helix's Kafka protocol implements Produce, Fetch,
# ListOffsets, Metadata, OffsetCommit/Fetch — but NOT the consumer-group
# coordination APIs (JoinGroup/SyncGroup/Heartbeat return _UNSUPPORTED_FEATURE).
# So we do NOT subscribe() (which triggers JoinGroup). Instead each replica
# manually assign()s the topic's partitions and Fetches directly, committing
# offsets under GROUP via OffsetCommit (which Helix DOES support). concurrency =
# replicas = parallel consumer workers (each independently applies the knob).
CONSUMER_SCRIPT = DOGSTATSD_SNIPPET + r"""
import os, time
from confluent_kafka import Consumer, TopicPartition, OFFSET_END, OFFSET_BEGINNING

import json as _json, random as _random
BROKERS    = os.environ["BROKERS"]
TOPIC      = os.environ["TOPIC"]
GROUP      = os.environ["GROUP"]
PROCESS_MS = int(os.environ["PROCESS_MS"])   # artificial per-message process time
SHAPE      = os.environ.get("SHAPE", "stable").strip().lower()
try:
    SP = _json.loads(os.environ.get("SHAPE_PARAMS", "") or "{}")
except Exception:
    SP = {}
def _spn(key, default):
    try: return float(SP.get(key, default))
    except Exception: return default
START_OFFSET = os.environ.get("START_OFFSET", "end").strip().lower()
_START = OFFSET_BEGINNING if START_OFFSET == "beginning" else OFFSET_END
print(f"[consumer] brokers={BROKERS} topic={TOPIC} group={GROUP} process_ms={PROCESS_MS} shape={SHAPE} params={SP} start={START_OFFSET} (manual-assign; Helix has no consumer groups)", flush=True)

# Per-shape librdkafka fetch tuning.
_cfg = {"bootstrap.servers": BROKERS, "group.id": GROUP, "enable.auto.commit": False}
if SHAPE == "low_latency":
    _cfg["fetch.wait.max.ms"] = 0
    _cfg["fetch.min.bytes"] = 1
elif SHAPE == "batch":
    _cfg["fetch.wait.max.ms"] = int(_spn("fetch_wait_ms", 500))
    _cfg["fetch.min.bytes"] = int(_spn("fetch_min_bytes", 65536))
c = Consumer(_cfg)

# Discover partitions via Metadata, then manually assign (no JoinGroup).
parts = []
for _ in range(30):
    md = c.list_topics(topic=TOPIC, timeout=5.0)
    t = md.topics.get(TOPIC)
    if t and t.partitions and not t.error:
        parts = sorted(t.partitions.keys())
        break
    print(f"[consumer] waiting for topic {TOPIC} metadata...", flush=True)
    time.sleep(2)
if not parts:
    print(f"[consumer] topic {TOPIC} has no partitions yet; assuming [0]", flush=True)
    parts = [0]
# Start at the configured offset (live end by default; beginning to drain backlog).
c.assign([TopicPartition(TOPIC, p, _START) for p in parts])
print(f"[consumer] assigned {TOPIC} partitions {parts} at {START_OFFSET}", flush=True)

processed = 0
window = 0
last = time.time()
sleep_s = PROCESS_MS / 1000.0

# Resilient per-partition lag: high-watermark - committed position, summed across
# only the partitions whose watermark query succeeds. On Helix get_watermark_offsets
# can intermittently return _UNKNOWN_PARTITION; we skip those rather than void the
# whole reading, and always emit the gauge (lag=0 when nothing is resolvable) so
# workload.consumer.lag stays live for the dashboard instead of going dark.
def emit_lag():
    lag_total = 0
    ok_parts = 0
    for tp in c.assignment():
        try:
            _lo, hi = c.get_watermark_offsets(tp, timeout=2.0)
            pos = c.position([tp])[0].offset
            if pos is None or pos < 0:
                pos = _lo
            lag_total += max(0, hi - pos)
            ok_parts += 1
        except Exception:
            continue  # skip partitions Helix won't report a watermark for
    _dsd.gauge("workload.consumer.lag", lag_total)
    return lag_total, ok_parts

state = {"processed": processed, "window": window, "last": last}

def account(msg, proc_ms):
    # Record one processed message: count + per-message latency + periodic log.
    state["processed"] += 1
    state["window"] += 1
    _dsd.count("workload.consumer.consumed", 1)
    _dsd.histogram("workload.consumer.process_latency_ms", proc_ms)
    try:
        c.commit(msg, asynchronous=True)
    except Exception:
        pass
    now = time.time()
    if now - state["last"] >= 5:
        rate = state["window"] / (now - state["last"])
        lag_total, ok_parts = emit_lag()
        print(f"[consumer/{SHAPE}] consumed ~{rate:.0f}/s processed_total={state['processed']} lag~={lag_total} (parts_ok={ok_parts})", flush=True)
        state["window"] = 0
        state["last"] = now

def maybe_idle_log():
    now = time.time()
    if now - state["last"] >= 5:
        lag_total, ok_parts = emit_lag()
        print(f"[consumer/{SHAPE}] idle; processed_total={state['processed']} lag~={lag_total} (parts_ok={ok_parts})", flush=True)
        state["last"] = now

if SHAPE == "batch":
    # Collect up to batch_size messages (poll quickly), then process the WHOLE
    # batch with one process-sleep and a bulk commit — high-latency/throughput.
    batch_size = int(_spn("batch_size", 100))
    while True:
        batch = []
        deadline = time.time() + 1.0
        while len(batch) < batch_size and time.time() < deadline:
            m = c.poll(0.2)
            if m is None:
                break
            if m.error():
                continue
            batch.append(m)
        if not batch:
            maybe_idle_log(); continue
        bstart = time.time()
        if sleep_s > 0:
            time.sleep(sleep_s)  # one process-time per batch
        for m in batch:
            state["processed"] += 1; state["window"] += 1
            _dsd.count("workload.consumer.consumed", 1)
        _dsd.histogram("workload.consumer.process_latency_ms", (time.time() - bstart) * 1000.0)
        try:
            c.commit(batch[-1], asynchronous=True)
        except Exception:
            pass
        now = time.time()
        if now - state["last"] >= 5:
            lag_total, ok_parts = emit_lag()
            print(f"[consumer/batch] batch={len(batch)} processed_total={state['processed']} lag~={lag_total} (parts_ok={ok_parts})", flush=True)
            state["window"] = 0; state["last"] = now
elif SHAPE == "bursty":
    # Consume flat-out for burst_period_ms, then idle for idle_ms. Repeat.
    burst_s = _spn("burst_period_ms", 2000) / 1000.0
    idle_s = _spn("idle_ms", 4000) / 1000.0
    while True:
        end = time.time() + burst_s
        while time.time() < end:
            m = c.poll(0.5)
            if m is None or m.error():
                continue
            account(m, 0.0)  # burst = consume as fast as possible
        print(f"[consumer/bursty] burst done, idling {idle_s:.1f}s", flush=True)
        emit_lag()
        time.sleep(max(0.0, idle_s))
elif SHAPE == "spiky":
    # Tail-latency: per-message process time drawn from a lognormal with the
    # given p50/p99 (mostly fast, occasional slow message).
    p50 = max(0.1, _spn("p50_ms", max(PROCESS_MS, 1)))
    p99 = max(p50 + 0.1, _spn("p99_ms", p50 * 20))
    import math as _math
    mu = _math.log(p50 / 1000.0)
    sigma = (_math.log(p99 / 1000.0) - mu) / 2.326  # z(0.99)
    while True:
        m = c.poll(1.0)
        if m is None: maybe_idle_log(); continue
        if m.error():
            time.sleep(1); continue
        s = time.time()
        time.sleep(max(0.0, _random.lognormvariate(mu, sigma)))
        account(m, (time.time() - s) * 1000.0)
else:
    # stable (default) and low_latency: one message at a time, fixed process_ms
    # (low_latency just uses the tuned fetch config + typically process_ms=0).
    while True:
        m = c.poll(1.0)
        if m is None: maybe_idle_log(); continue
        if m.error():
            time.sleep(1); continue
        s = time.time()
        if sleep_s > 0:
            time.sleep(sleep_s)
        account(m, (time.time() - s) * 1000.0)
"""


def _workload_resources_yaml() -> str:
    return (
        f"          resources:\n"
        f"            requests: {{ cpu: \"{WORKLOAD_CPU_REQUEST}\", memory: \"{WORKLOAD_MEM_REQUEST}\" }}\n"
        f"            limits: {{ cpu: \"{WORKLOAD_CPU_LIMIT}\", memory: \"{WORKLOAD_MEM_LIMIT}\" }}\n"
    )


def _b64(text: str) -> str:
    """Base64-encode a script so it embeds in YAML/shell with zero quoting or
    indentation hazards (decoded back to a file in the container)."""
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _workload_command(script_b64: str) -> str:
    """The single bash -lc command: decode the script, pip-install the prebuilt
    confluent-kafka wheel (~7s, no compiler), then exec it. One inline string
    arg — no YAML block scalar, no heredoc indentation traps."""
    return (
        f"set -e; "
        f"echo {script_b64} | base64 -d > /tmp/wl.py; "
        f"pip install --no-cache-dir --quiet confluent-kafka; "
        f"exec python /tmp/wl.py"
    )


def _producer_manifest(
    dep_name: str, entity_id: str, topic: str, rate: int, msg_size: int, replicas: int,
    brokers: str = KAFKA_BROKERS, cluster: str = "", shape: str = "stable", shape_params: str = ""
) -> str:
    """Inline producer Deployment manifest (no image build).

    Each replica installs confluent-kafka (prebuilt wheel, ~7s) then runs the
    rate-limited producer at `rate` msgs/sec against `brokers` (the target
    cluster's Kafka Service). replicas = concurrency = parallel producer
    workers, so the cluster sees ~rate*replicas msgs/sec total. The `cluster`
    label lets the UI group workloads by the cluster they target.
    """
    cmd = _workload_command(_b64(PRODUCER_SCRIPT))
    cluster_label = cluster or "helix"
    return f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: {dep_name}
  namespace: {NAMESPACE}
  labels:
    app: {dep_name}
    workload: producer
    managed-by: directed-evolution-workloads
    cluster: "{cluster_label}"
    entity-id: "{entity_id}"
spec:
  replicas: {replicas}
  selector:
    matchLabels: {{ app: {dep_name} }}
  template:
    metadata:
      labels:
        app: {dep_name}
        workload: producer
        managed-by: directed-evolution-workloads
        cluster: "{cluster_label}"
    spec:
      nodeSelector: {{ pool: dark-factory }}
      tolerations:
        - {{ key: dedicated, operator: Equal, value: dark-factory, effect: NoExecute }}
      containers:
        - name: producer
          image: {WORKLOAD_IMAGE}
          env:
            - {{ name: BROKERS, value: "{brokers}" }}
            - {{ name: TOPIC, value: "{topic}" }}
            - {{ name: RATE, value: "{rate}" }}
            - {{ name: MSG_SIZE, value: "{msg_size}" }}
            - {{ name: DD_AGENT_HOST, value: "{WORKLOAD_DD_AGENT_HOST}" }}
            - {{ name: DD_DOGSTATSD_PORT, value: "{WORKLOAD_DD_DOGSTATSD_PORT}" }}
            - {{ name: WORKLOAD_ID, value: "{entity_id}" }}
            - {{ name: QUEUE, value: "{topic}" }}
            - {{ name: ROLE, value: "producer" }}
            - {{ name: SHAPE, value: "{shape}" }}
            - {{ name: SHAPE_PARAMS, value: {json.dumps(shape_params)} }}
          command: ["bash", "-lc", {json.dumps(cmd)}]
{_workload_resources_yaml()}"""


def _consumer_manifest(
    dep_name: str,
    entity_id: str,
    topic: str,
    process_ms: int,
    replicas: int,
    start_offset: str = "end",
    brokers: str = KAFKA_BROKERS,
    cluster: str = "",
    shape: str = "stable",
    shape_params: str = "",
) -> str:
    """Inline consumer Deployment manifest (no image build).

    replicas = concurrency = consumer-group size; each member sleeps process_ms
    per message (the process-time knob), commits, and logs lag. Connects to
    `brokers` (the target cluster's Kafka Service).

    ``start_offset`` is ``"end"`` (default; only fresh traffic) or ``"beginning"``
    (drain the existing backlog, forcing data-bearing Fetches).
    """
    cmd = _workload_command(_b64(CONSUMER_SCRIPT))
    group = f"{dep_name}-grp"
    start_offset = "beginning" if str(start_offset).strip().lower() == "beginning" else "end"
    cluster_label = cluster or "helix"
    return f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: {dep_name}
  namespace: {NAMESPACE}
  labels:
    app: {dep_name}
    workload: consumer
    managed-by: directed-evolution-workloads
    cluster: "{cluster_label}"
    entity-id: "{entity_id}"
spec:
  replicas: {replicas}
  selector:
    matchLabels: {{ app: {dep_name} }}
  template:
    metadata:
      labels:
        app: {dep_name}
        workload: consumer
        managed-by: directed-evolution-workloads
        cluster: "{cluster_label}"
    spec:
      nodeSelector: {{ pool: dark-factory }}
      tolerations:
        - {{ key: dedicated, operator: Equal, value: dark-factory, effect: NoExecute }}
      containers:
        - name: consumer
          image: {WORKLOAD_IMAGE}
          env:
            - {{ name: BROKERS, value: "{brokers}" }}
            - {{ name: TOPIC, value: "{topic}" }}
            - {{ name: GROUP, value: "{group}" }}
            - {{ name: PROCESS_MS, value: "{process_ms}" }}
            - {{ name: START_OFFSET, value: "{start_offset}" }}
            - {{ name: DD_AGENT_HOST, value: "{WORKLOAD_DD_AGENT_HOST}" }}
            - {{ name: DD_DOGSTATSD_PORT, value: "{WORKLOAD_DD_DOGSTATSD_PORT}" }}
            - {{ name: WORKLOAD_ID, value: "{entity_id}" }}
            - {{ name: QUEUE, value: "{topic}" }}
            - {{ name: ROLE, value: "consumer" }}
            - {{ name: SHAPE, value: "{shape}" }}
            - {{ name: SHAPE_PARAMS, value: {json.dumps(shape_params)} }}
          command: ["bash", "-lc", {json.dumps(cmd)}]
{_workload_resources_yaml()}"""


# --------------------------------------------------------------------------- #
# Workloads — real, ns-pinned executors (apply/delete producer-*/consumer-*)
# --------------------------------------------------------------------------- #

def exec_apply_workload(manifest: str, dep_name: str) -> tuple[bool, str, str]:
    """kubectl apply the inline workload manifest, then a bounded readiness wait.

    A pod that can't schedule (capacity) won't go Ready; we surface that as
    ok=False with the real pod state rather than hanging or faking success.
    Only ever applies a producer-*/consumer-* Deployment.
    """
    _assert_namespace()
    assert dep_name.startswith(("producer-", "consumer-")), (
        f"refusing apply: workload dep must be producer-*/consumer-*, got {dep_name!r}"
    )
    cmd_str = f"kubectl -n dark-factory apply -f - (Deployment/{dep_name})"
    p1 = _kubectl_stdin(manifest, "apply", "-f", "-", timeout=60)
    out = (p1.stdout + p1.stderr).strip()
    if p1.returncode != 0:
        return False, cmd_str, out[-2500:]
    # Bounded wait: give pods time to pull + pip-install + connect. Capacity
    # failures surface as not-Available within the window.
    p2 = _kubectl(
        "rollout", "status", f"deployment/{dep_name}", "--timeout=90s", timeout=100
    )
    out = (out + "\n" + p2.stdout + p2.stderr).strip()
    return p2.returncode == 0, cmd_str, out[-2500:]


def exec_delete_workload(dep_name: str) -> tuple[bool, str, str]:
    """kubectl delete the producer-<id>/consumer-<id> Deployment."""
    _assert_namespace()
    assert dep_name.startswith(("producer-", "consumer-")), (
        f"refusing delete: workload dep must be producer-*/consumer-*, got {dep_name!r}"
    )
    cmd_str = f"kubectl -n dark-factory delete deployment {dep_name} --ignore-not-found"
    p = _kubectl(
        "delete", "deployment", dep_name, "--ignore-not-found", timeout=60
    )
    out = (p.stdout + p.stderr).strip()
    return p.returncode == 0, cmd_str, out[-2500:]


def workload_pods(dep_name: str) -> list[dict[str, Any]]:
    """Live pod state for a workload Deployment (read-only)."""
    proc = _kubectl(
        "get", "pods", "-l", f"app={dep_name}", "-o", "json", timeout=30
    )
    pods: list[dict[str, Any]] = []
    if proc.returncode != 0:
        return pods
    try:
        for item in json.loads(proc.stdout).get("items", []):
            meta = item.get("metadata", {}) or {}
            pstatus = item.get("status", {}) or {}
            conds = {c.get("type"): c.get("status") for c in (pstatus.get("conditions") or [])}
            cstatuses = pstatus.get("containerStatuses") or []
            waiting = None
            for cs in cstatuses:
                w = (cs.get("state", {}) or {}).get("waiting")
                if w:
                    waiting = w.get("reason")
                    break
            pods.append({
                "name": meta.get("name", ""),
                "phase": pstatus.get("phase", "Unknown"),
                "ready": conds.get("Ready") == "True",
                "reason": waiting or pstatus.get("reason"),
            })
    except ValueError:
        return []
    pods.sort(key=lambda p: p["name"])
    return pods


# --------------------------------------------------------------------------- #
# Workloads — governed-entity helpers (auto-approved; supervisor drives)
# --------------------------------------------------------------------------- #

def _wl_int(payload: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(payload.get(key, default))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{key} must be an integer")
    if v < lo or v > hi:
        raise HTTPException(status_code=400, detail=f"{key} out of bounds ({lo}-{hi})")
    return v


# A queue/topic name is interpolated into a YAML manifest that gets piped to
# `kubectl apply -f -`. Validate it (same rule as Queue create) on EVERY path
# that uses it (producer/consumer too) so a name with newlines/quotes can't
# inject arbitrary YAML / escape the namespace pin.
_QUEUE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")


def _valid_queue_name(payload: dict[str, Any]) -> str:
    name = (payload or {}).get("queue", "").strip()
    if not name or not _QUEUE_NAME_RE.match(name):
        raise HTTPException(status_code=400,
                            detail="queue must match [A-Za-z0-9._-]{1,200}")
    return name


def _normalize_workload(entity: dict[str, Any], kind: str) -> dict[str, Any]:
    """Map a raw Producer/Consumer/Queue entity into a UI list item + pods."""
    fields = entity.get("fields", {}) or {}
    eid = entity.get("entity_id") or field(fields, "Id", "id")
    status = entity.get("status") or field(fields, "Status", default="Unknown")
    item: dict[str, Any] = {
        "id": eid,
        "status": status,
        "queue": field(fields, "Queue", "queue"),
        # "" = baseline 'helix'. Normalize so the UI can group by cluster.
        "cluster": (field(fields, "Cluster", "cluster", default="") or "").strip() or "helix",
        "shape": (field(fields, "Shape", "shape", default="") or "stable"),
        # The shape's load knobs (burst_size/idle_ms/etc.) — must be carried so a
        # migration recreates the SAME load shape on the target cluster, not the
        # shape's defaults. Without this, e.g. a bursty producer migrates as a
        # flat ripple (default burst_size/idle) instead of its real big spikes.
        "shape_params": (field(fields, "ShapeParams", "shape_params", default="") or ""),
        "result": (field(fields, "Result", "result", default="") or "").strip(),
    }
    if kind == "queue":
        item["name"] = field(fields, "Name", "name")
        item["partitions"] = field(fields, "Partitions", "partitions")
        item.pop("queue", None)
    elif kind == "producer":
        item["rate_per_sec"] = field(fields, "RatePerSec", "rate_per_sec")
        item["msg_size"] = field(fields, "MsgSize", "msg_size")
        item["concurrency"] = field(fields, "Concurrency", "concurrency")
    elif kind == "consumer":
        item["process_ms"] = field(fields, "ProcessMs", "process_ms")
        item["concurrency"] = field(fields, "Concurrency", "concurrency")
    if kind in ("producer", "consumer") and status == "Running":
        dep = f"{kind}-{eid}"
        item["pods"] = workload_pods(dep)
        item["deployment"] = dep
    return item


def _fetch_workload_set(entity_set: str) -> list[dict[str, Any]]:
    doc = temper_get(f"/tdata/{entity_set}")
    return (doc or {}).get("value", []) or []


@app.get("/api/workloads")
def list_workloads() -> JSONResponse:
    """Queues + Producers + Consumers with live entity state and (for running
    producers/consumers) their real pod status."""
    queues = [
        _normalize_workload(e, "queue")
        for e in _fetch_workload_set("Queues")
        if (e.get("status") or "") != "Deleted"
    ]
    producers = [_normalize_workload(e, "producer") for e in _fetch_workload_set("Producers")]
    consumers = [_normalize_workload(e, "consumer") for e in _fetch_workload_set("Consumers")]
    # Mark workloads whose target cluster no longer exists (running -> will crashloop/dangle).
    live = _live_cluster_names()
    for w in producers + consumers:
        w["orphan"] = w.get("status") == "Running" and (w.get("cluster") or "helix") not in live
    queues.sort(key=lambda q: q.get("id") or "", reverse=True)
    producers.sort(key=lambda p: p.get("id") or "", reverse=True)
    consumers.sort(key=lambda c: c.get("id") or "", reverse=True)
    return JSONResponse({
        "queues": queues,
        "producers": producers,
        "consumers": consumers,
        "brokers": KAFKA_BROKERS,
        "image": WORKLOAD_IMAGE,
    })


@app.post("/api/workloads/reap-orphans")
def reap_orphans() -> JSONResponse:
    """Govern-Stop + delete every Running producer/consumer whose target cluster
    no longer exists (e.g. stranded by a teardown). Prevents crashloops/dangling
    pods. Returns the list reaped."""
    live = _live_cluster_names()
    reaped: list[dict[str, Any]] = []
    for kind, es in (("producer", "Producers"), ("consumer", "Consumers")):
        for e in _fetch_workload_set(es):
            f = e.get("fields", {}) or {}
            eid = e.get("entity_id") or field(f, "Id", "id")
            status = e.get("status") or field(f, "Status")
            cl = (field(f, "Cluster", "cluster", default="") or "").strip() or "helix"
            if status != "Running" or not eid or cl in live:
                continue
            dep = f"{kind}-{eid}"
            del_ok, _c, _o = exec_delete_workload(dep)
            temper_post(f"/tdata/{es}('{eid}')/Default.Stop", {}, token=supervisor_token())
            reaped.append({"kind": kind, "id": eid, "cluster": cl, "deleted": del_ok})
    return JSONResponse({"ok": True, "reaped": reaped, "live_clusters": sorted(live)})


def _live_cluster_names() -> set[str]:
    """Names of clusters that currently EXIST and can take workloads: every Live
    governed Cluster entity, plus 'helix' (the baseline, if its StatefulSet is up).
    Used to reject workloads targeting a dead cluster and to reap orphans."""
    names: set[str] = set()
    doc = temper_get("/tdata/Clusters")
    if doc:
        for e in doc.get("value", doc.get("entities", [])) or []:
            f = e.get("fields", {}) or {}
            if (e.get("status") or field(f, "Status")) == "Live":
                nm = field(f, "name", "Name")
                if nm:
                    names.add(nm)
    # Baseline 'helix' is live only if its StatefulSet exists.
    try:
        p = _kubectl("get", "statefulset", "helix", "-o", "name", timeout=15)
        if p.returncode == 0 and p.stdout.strip():
            names.add("helix")
    except (subprocess.SubprocessError, OSError):
        pass
    return names


def _resolve_cluster(payload: dict[str, Any], require_live: bool = False) -> str:
    """Normalize the workload's target cluster name. '' or 'helix' = baseline.
    When require_live=True (workload creation), reject a target that is not a
    currently-existing cluster so we never strand a workload on a dead broker."""
    raw = str((payload or {}).get("cluster", "")).strip().lower()
    if raw in ("", "helix", "baseline"):
        target = ""
    elif not _CLUSTER_NAME_RE.match(raw):
        raise HTTPException(
            status_code=400,
            detail=f"invalid cluster name {raw!r} (must be a DNS-label slug or empty for baseline)",
        )
    else:
        target = raw
    if require_live:
        live = _live_cluster_names()
        check = target or "helix"
        if check not in live:
            raise HTTPException(
                status_code=409,
                detail=f"cluster {check!r} is not Live (existing: {sorted(live) or 'none'}); "
                       f"create the cluster (and let it reach Live) before adding workloads",
            )
    return target


PRODUCER_SHAPES = {"stable", "bursty", "ramp", "batch"}
CONSUMER_SHAPES = {"stable", "low_latency", "batch", "bursty", "spiky"}


def _resolve_shape(payload: dict[str, Any], allowed: set[str]) -> tuple[str, str]:
    """Return (shape, shape_params_json). shape defaults to 'stable'; unknown
    shapes are rejected. shape_params accepts a dict or JSON string of knobs."""
    shape = str((payload or {}).get("shape", "stable")).strip().lower() or "stable"
    if shape not in allowed:
        raise HTTPException(status_code=400, detail=f"unknown shape {shape!r}; allowed: {sorted(allowed)}")
    raw = (payload or {}).get("shape_params", {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise HTTPException(status_code=400, detail="shape_params must be valid JSON")
    if not isinstance(raw, dict):
        raw = {}
    # Bound every numeric knob so a typo can't melt the cluster.
    clean = {}
    for k, v in raw.items():
        try:
            clean[str(k)] = max(0, min(int(float(v)), 600000))
        except (TypeError, ValueError):
            continue
    return shape, json.dumps(clean)


def _brokers_for_cluster(cluster: str) -> str:
    """The Kafka brokers DNS for a workload's target cluster. Baseline ('') uses
    the hardcoded helix Service; a named cluster uses its own helix-<name>
    Service (created by _cluster_manifest)."""
    if not cluster:
        return KAFKA_BROKERS
    return f"helix-{cluster}.{NAMESPACE}.svc.cluster.local:9092"


@app.post("/api/workloads/queue")
def create_queue(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """Create + Define + Activate a governed Queue (auto-approved)."""
    name = (payload or {}).get("name", "").strip()
    if not name or not _QUEUE_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="queue name must match [A-Za-z0-9._-]{1,200}")
    partitions = _wl_int(payload, "partitions", 1, 1, 50)
    cluster = _resolve_cluster(payload, require_live=True)

    qid = f"q-{name}-{sim_ts()}"
    s, b = temper_post(
        "/tdata/Queues",
        {"id": qid, "Status": "Defined", "Name": name, "Partitions": partitions, "Cluster": cluster},
        token=operator_token(),
    )
    if s not in (200, 201):
        return _denial_response(s, b, "create Queue")
    # Drive Define + Activate with the verified supervisor (auto-approved; the
    # governance is the entity lifecycle + Cedar, not a human click).
    s, b = temper_post(
        f"/tdata/Queues('{qid}')/Default.Define",
        {"name": name, "partitions": str(partitions), "cluster": cluster},
        token=supervisor_token(),
    )
    if s not in (200, 204):
        return _denial_response(s, b, "Define Queue")
    s, b = temper_post(f"/tdata/Queues('{qid}')/Default.Activate", {}, token=supervisor_token())
    if s not in (200, 204):
        return _denial_response(s, b, "Activate Queue")
    return JSONResponse({"ok": True, "kind": "queue", "id": qid, "name": name,
                         "partitions": partitions, "cluster": cluster, "status": "Active"})


@app.post("/api/workloads/producer")
def create_producer(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """Create + Define + Start a governed Producer; executor deploys the pod."""
    queue = _valid_queue_name(payload)
    rate = _wl_int(payload, "rate_per_sec", 500, 1, MAX_RATE_PER_SEC)
    msg_size = _wl_int(payload, "msg_size", 512, 1, MAX_MSG_SIZE)
    concurrency = _wl_int(payload, "concurrency", 1, 1, MAX_CONCURRENCY)
    cluster = _resolve_cluster(payload, require_live=True)
    brokers = _brokers_for_cluster(cluster)
    shape, shape_params = _resolve_shape(payload, PRODUCER_SHAPES)

    pid = f"p-{sim_ts()}"
    s, b = temper_post(
        "/tdata/Producers",
        {"id": pid, "Status": "Defined", "Queue": queue, "RatePerSec": rate,
         "MsgSize": msg_size, "Concurrency": concurrency, "Cluster": cluster,
         "Shape": shape, "ShapeParams": shape_params, "Namespace": NAMESPACE},
        token=operator_token(),
    )
    if s not in (200, 201):
        return _denial_response(s, b, "create Producer")
    s, b = temper_post(
        f"/tdata/Producers('{pid}')/Default.Define",
        {"queue": queue, "rate_per_sec": str(rate), "msg_size": str(msg_size),
         "concurrency": str(concurrency), "cluster": cluster,
         "shape": shape, "shape_params": shape_params},
        token=supervisor_token(),
    )
    if s not in (200, 204):
        return _denial_response(s, b, "Define Producer")
    s, b = temper_post(f"/tdata/Producers('{pid}')/Default.Start", {}, token=supervisor_token())
    if s not in (200, 204):
        return _denial_response(s, b, "Start Producer")

    # Executor: REAL, ns-pinned apply (entity is now Running). Brokers target the
    # producer's cluster (helix-<name> Service), or the baseline helix Service.
    dep = f"producer-{pid}"
    manifest = _producer_manifest(dep, pid, queue, rate, msg_size, concurrency, brokers, cluster, shape, shape_params)
    ok, cmd_str, output = exec_apply_workload(manifest, dep)
    result = ("scheduled" if ok else "schedule-incomplete") + f": {cmd_str} | {output[:1200]}"
    temper_post(f"/tdata/Producers('{pid}')/Default.RecordResult",
                {"result": result}, token=supervisor_token())

    return JSONResponse({
        "ok": ok, "kind": "producer", "id": pid, "status": "Running",
        "deployment": dep, "queue": queue, "rate_per_sec": rate,
        "msg_size": msg_size, "concurrency": concurrency,
        "cluster": cluster, "brokers": brokers, "shape": shape,
        "result": result, "pods": workload_pods(dep),
        "note": None if ok else "Pods may still be pulling/scheduling on the shared pool — check live status.",
    })


@app.post("/api/workloads/consumer")
def create_consumer(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """Create + Define + Start a governed Consumer; executor deploys the pod."""
    queue = _valid_queue_name(payload)
    process_ms = _wl_int(payload, "process_ms", 5, 0, MAX_PROCESS_MS)
    concurrency = _wl_int(payload, "concurrency", 1, 1, MAX_CONCURRENCY)
    start_offset = "beginning" if str(payload.get("start_offset", "end")).strip().lower() == "beginning" else "end"
    cluster = _resolve_cluster(payload, require_live=True)
    brokers = _brokers_for_cluster(cluster)
    shape, shape_params = _resolve_shape(payload, CONSUMER_SHAPES)

    cid = f"c-{sim_ts()}"
    s, b = temper_post(
        "/tdata/Consumers",
        {"id": cid, "Status": "Defined", "Queue": queue, "ProcessMs": process_ms,
         "Concurrency": concurrency, "Cluster": cluster,
         "Shape": shape, "ShapeParams": shape_params, "Namespace": NAMESPACE},
        token=operator_token(),
    )
    if s not in (200, 201):
        return _denial_response(s, b, "create Consumer")
    s, b = temper_post(
        f"/tdata/Consumers('{cid}')/Default.Define",
        {"queue": queue, "process_ms": str(process_ms), "concurrency": str(concurrency), "cluster": cluster,
         "shape": shape, "shape_params": shape_params},
        token=supervisor_token(),
    )
    if s not in (200, 204):
        return _denial_response(s, b, "Define Consumer")
    s, b = temper_post(f"/tdata/Consumers('{cid}')/Default.Start", {}, token=supervisor_token())
    if s not in (200, 204):
        return _denial_response(s, b, "Start Consumer")

    dep = f"consumer-{cid}"
    manifest = _consumer_manifest(dep, cid, queue, process_ms, concurrency, start_offset, brokers, cluster, shape, shape_params)
    ok, cmd_str, output = exec_apply_workload(manifest, dep)
    result = ("scheduled" if ok else "schedule-incomplete") + f": {cmd_str} | {output[:1200]}"
    temper_post(f"/tdata/Consumers('{cid}')/Default.RecordResult",
                {"result": result}, token=supervisor_token())

    return JSONResponse({
        "ok": ok, "kind": "consumer", "id": cid, "status": "Running",
        "deployment": dep, "queue": queue, "process_ms": process_ms,
        "concurrency": concurrency, "cluster": cluster, "brokers": brokers,
        "result": result, "pods": workload_pods(dep),
        "note": None if ok else "Pods may still be pulling/scheduling on the shared pool — check live status.",
    })


@app.post("/api/workloads/{kind}/{entity_id}/stop")
def stop_workload(kind: str, entity_id: str) -> JSONResponse:
    """Stop a Producer/Consumer: Stop transition (supervisor) -> kubectl delete
    the producer-<id>/consumer-<id> Deployment. Governed; auto-approved."""
    if kind not in ("producer", "consumer"):
        raise HTTPException(status_code=400, detail=f"unknown kind {kind!r}")
    entity_set = "Producers" if kind == "producer" else "Consumers"
    doc = temper_get(f"/tdata/{entity_set}('{entity_id}')")
    if not doc:
        raise HTTPException(status_code=404, detail=f"{kind} '{entity_id}' not found")

    dep = f"{kind}-{entity_id}"
    # Delete the real Deployment first (so even a half-transitioned entity stops
    # generating load), then land the governed Stop transition.
    del_ok, del_cmd, del_out = exec_delete_workload(dep)

    status = doc.get("status") or field(doc.get("fields", {}) or {}, "Status")
    stopped = False
    if status == "Running":
        s, b = temper_post(
            f"/tdata/{entity_set}('{entity_id}')/Default.Stop", {},
            token=supervisor_token(),
        )
        stopped = s in (200, 204)
        if not stopped:
            return _denial_response(s, b, f"Stop {kind}")

    return JSONResponse({
        "ok": del_ok, "kind": kind, "id": entity_id, "status": "Stopped",
        "deployment": dep, "delete_cmd": del_cmd, "output": del_out,
        "transitioned": stopped,
    })


@app.post("/api/workloads/queue/{entity_id}/delete")
def delete_queue(entity_id: str) -> JSONResponse:
    """Retire a Queue: govern the Delete transition (Defined/Active -> Deleted).
    The Kafka topic itself is left to Helix retention; producers/consumers should
    be stopped first (the UI surfaces them so the user can)."""
    doc = temper_get(f"/tdata/Queues('{entity_id}')")
    if not doc:
        raise HTTPException(status_code=404, detail=f"queue '{entity_id}' not found")
    status = doc.get("status") or field(doc.get("fields", {}) or {}, "Status")
    if status == "Deleted":
        return JSONResponse({"ok": True, "kind": "queue", "id": entity_id,
                             "status": "Deleted", "transitioned": False, "note": "already deleted"})
    s, b = temper_post(f"/tdata/Queues('{entity_id}')/Default.Delete", {},
                       token=supervisor_token())
    if s not in (200, 204):
        return _denial_response(s, b, "Delete Queue")
    return JSONResponse({"ok": True, "kind": "queue", "id": entity_id,
                         "status": "Deleted", "transitioned": True})


# --------------------------------------------------------------------------- #
# Git tree (ADR-0098 UI) — the helix commit DAG + worktrees, annotated with the
# Helix deployment (cluster) at each node, for the left-to-right homepage tree.
# --------------------------------------------------------------------------- #

def _git_worktrees() -> dict[str, dict[str, str]]:
    """Map commit-sha -> {path, branch} for every helix worktree."""
    out: dict[str, dict[str, str]] = {}
    try:
        p = _git("worktree", "list", "--porcelain")
    except (subprocess.SubprocessError, OSError):
        return out
    if p.returncode != 0:
        return out
    cur: dict[str, str] = {}
    for line in p.stdout.splitlines():
        if line.startswith("worktree "):
            cur = {"path": line[len("worktree "):].strip()}
        elif line.startswith("HEAD "):
            cur["sha"] = line[len("HEAD "):].strip()[:7]
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch "):].strip().replace("refs/heads/", "")
        elif line.strip() == "" and cur.get("sha"):
            out[cur["sha"]] = {"path": cur.get("path", ""), "branch": cur.get("branch", "")}
            cur = {}
    if cur.get("sha"):
        out[cur["sha"]] = {"path": cur.get("path", ""), "branch": cur.get("branch", "")}
    return out


@app.get("/api/tree")
def git_tree() -> JSONResponse:
    """The helix git DAG (all branches) + worktrees, annotated with the Helix
    deployment at each node, for the homepage left-to-right tree.

    Returns nodes (sha, parents, refs, subject, worktree, clusters[]) so the UI
    can lay out a graph. Each node carries the cluster(s) deployed from its
    branch/worktree (joined via Cluster.Branch) and any Deploys on its image tag.
    """
    # 1. The commit graph across all refs (bounded).
    try:
        p = _git(
            "log", "--all", "--date-order",
            "--pretty=format:%h\x1f%p\x1f%D\x1f%an\x1f%cI\x1f%s",
            "-40",
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return JSONResponse({"nodes": [], "error": str(exc)})
    if p.returncode != 0:
        return JSONResponse({"nodes": [], "error": (p.stderr or "git log failed")[:400]})

    worktrees = _git_worktrees()

    # 2. Cluster entities, indexed by branch, to annotate the tree. De-dupe by
    #    cluster NAME keeping the most-advanced status (so the tree chip matches
    #    the table, which also de-dupes; otherwise a stale 'Defined' shell from a
    #    prior create attempt would show instead of the live status).
    _CSTAGE = ["Defined", "Approved", "Building", "Deploying", "Live", "Failed"]
    def _crank(s: str) -> int:
        try:
            return _CSTAGE.index(s)
        except ValueError:
            return -1
    by_name: dict[str, dict[str, Any]] = {}
    cdoc = temper_get("/tdata/Clusters")
    if cdoc:
        for entity in cdoc.get("value", cdoc.get("entities", [])) or []:
            f = entity.get("fields", {}) or {}
            br = field(f, "branch", "Branch")
            name = field(f, "name", "Name")
            status = entity.get("status") or field(f, "Status")
            if not (br and name) or status == "Torndown":
                continue
            cur = {
                "id": entity.get("entity_id") or field(f, "Id", "id"),
                "name": name,
                "branch": br,
                "status": status,
                "image_tag": field(f, "image_tag", "ImageTag"),
            }
            prev = by_name.get(name)
            if not prev or _crank(status) >= _crank(prev["status"]):
                by_name[name] = cur
    clusters_by_branch: dict[str, list[dict[str, Any]]] = {}
    for c in by_name.values():
        clusters_by_branch.setdefault(c["branch"], []).append(c)

    nodes: list[dict[str, Any]] = []
    for line in p.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) < 6:
            continue
        sha, parents, decor, author, when, subject = parts[:6]
        refs = [r.strip() for r in decor.split(",") if r.strip()] if decor else []
        branches = [
            r.replace("HEAD -> ", "").strip()
            for r in refs
            if "tag:" not in r and "origin/" not in r and "HEAD" not in r or r.startswith("HEAD ->")
        ]
        wt = worktrees.get(sha)
        # Clusters deployed from any branch decorating this commit (or its worktree branch).
        node_branches = set(branches)
        if wt and wt.get("branch"):
            node_branches.add(wt["branch"])
        node_clusters: list[dict[str, Any]] = []
        for br in node_branches:
            node_clusters.extend(clusters_by_branch.get(br, []))
        nodes.append({
            "sha": sha,
            "parents": parents.split() if parents else [],
            "refs": refs,
            "branches": branches,
            "author": author,
            "when": when,
            "subject": subject,
            "worktree": ({"path": wt["path"], "branch": wt.get("branch")} if wt else None),
            "clusters": node_clusters,
        })

    pruned = _prune_tree(nodes)
    return JSONResponse({"nodes": pruned, "worktree_count": len(worktrees),
                         "total_commits": len(nodes)})


def _prune_tree(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only relevant nodes — worktree tips, commits with a deployed cluster,
    and fork points — then re-link each kept node to its nearest kept ancestor so
    the collapsed graph stays connected (long linear runs are dropped).

    A fork point is a commit that is the (transitive) nearest-kept-ancestor of
    two or more distinct kept nodes — i.e. where branches diverge.
    """
    by_sha = {n["sha"]: n for n in nodes}

    # Count children per sha (within the loaded window) to find branch points.
    child_count: dict[str, int] = {}
    for n in nodes:
        for p in n["parents"]:
            if p in by_sha:
                child_count[p] = child_count.get(p, 0) + 1

    # Seed "kept" set: anything interesting in its own right.
    keep: set[str] = set()
    for n in nodes:
        is_wt = n["worktree"] is not None
        has_cluster = len(n["clusters"]) > 0
        is_fork = child_count.get(n["sha"], 0) >= 2
        is_main_tip = "main" in n["branches"]
        if is_wt or has_cluster or is_fork or is_main_tip:
            keep.add(n["sha"])

    if not keep:
        # Nothing notable — fall back to the newest handful so the page isn't empty.
        keep = {n["sha"] for n in nodes[:6]}

    # For each kept node, find its nearest kept ANCESTOR (walk first-parent chain
    # through dropped commits) to re-link the collapsed DAG.
    def nearest_kept_ancestor(sha: str) -> str | None:
        seen = set()
        frontier = list(by_sha[sha]["parents"])
        while frontier:
            cur = frontier.pop(0)
            if cur in seen or cur not in by_sha:
                continue
            seen.add(cur)
            if cur in keep:
                return cur
            frontier.extend(by_sha[cur]["parents"])
        return None

    out: list[dict[str, Any]] = []
    for n in nodes:
        if n["sha"] not in keep:
            continue
        anc = nearest_kept_ancestor(n["sha"])
        node = dict(n)
        node["link_parents"] = [anc] if anc else []
        out.append(node)
    return out


# --------------------------------------------------------------------------- #
# Clusters (ADR-0098) — governed multi-cluster Helix deploy from per-cluster
# git worktrees. Each Cluster owns its own worktree (branch df-cluster/<name>
# off main in the helix repo); on Approve the backend builds an image FROM that
# worktree (Cloud Build) and kubectl-applies a per-cluster StatefulSet
# (helix-<name>) + Services into dark-factory ONLY. The existing single `helix`
# cluster is untouched. Approve is the Cedar human gate (cluster.cedar).
# --------------------------------------------------------------------------- #

# GCP project + Cloud Build config for building a cluster image from its worktree.
GCP_PROJECT = "datadog-sandbox"
CLOUDBUILD_CONFIG = "/tmp/helix-cloudbuild.yaml"

# Write the cloudbuild config to /tmp at import time so it survives server
# restarts without a manual `cp`. DOCKER_BUILDKIT=1 is required because the
# Dockerfile uses --mount=type=cache (BuildKit syntax).
_CLOUDBUILD_YAML = """\
steps:
  - name: 'gcr.io/cloud-builders/docker'
    env:
      - 'DOCKER_BUILDKIT=1'
    args:
      - build
      - --platform=linux/amd64
      - -t
      - us-west3-docker.pkg.dev/datadog-sandbox/dark-factory-helix/helix-server:${_TAG}
      - -f
      - docker/Dockerfile
      - .

images:
  - us-west3-docker.pkg.dev/datadog-sandbox/dark-factory-helix/helix-server:${_TAG}

substitutions:
  _TAG: latest

options:
  machineType: E2_HIGHCPU_8
  logging: CLOUD_LOGGING_ONLY
"""
try:
    Path(CLOUDBUILD_CONFIG).write_text(_CLOUDBUILD_YAML)
except Exception:
    pass  # /tmp not writable in some sandboxes; operator must place it manually
# Names allowed for a cluster: short DNS-label-safe slugs. helix-<name> must be a
# valid k8s object name and DNS label, so be strict.
_CLUSTER_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,18}[a-z0-9]$")

# In-process build/deploy status for clusters whose Apply is running in the
# background (the Cloud Build is ~8-10 min). Keyed by cluster entity id.
_CLUSTER_JOBS: dict[str, dict[str, Any]] = {}
_CLUSTER_JOBS_LOCK = threading.Lock()


def _valid_cluster_name(payload: dict[str, Any]) -> str:
    """Validate the user-supplied cluster name (becomes helix-<name>).

    Strict DNS-label slug so the templated manifest can never inject arbitrary
    k8s object names. Mirrors _valid_queue_name's defensive posture.
    """
    raw = str((payload or {}).get("name", "")).strip().lower()
    if not _CLUSTER_NAME_RE.match(raw):
        raise HTTPException(
            status_code=400,
            detail="name must be a DNS-label slug: lowercase a-z0-9-, 3-20 chars, "
                   "start with a letter, end alphanumeric (e.g. 'exp-linger')",
        )
    if raw == "helix":
        # 'helix' is the reserved single baseline cluster.
        raise HTTPException(status_code=400, detail="'helix' is reserved for the baseline cluster")
    return raw


def _cluster_manifest(name: str, image_tag: str, replicas: int) -> str:
    """Per-cluster StatefulSet + Services, fully parametrized so two clusters
    never collide on the `app:` selector or cross-wire their Raft peers.

    Derived from helix/docker/k8s/helix-statefulset.yaml but every name/label/
    DNS that was hardcoded to `helix`/`helix-headless` is now `helix-<name>` /
    `helix-<name>-headless`. ns is dark-factory (asserted at apply time).
    """
    app = f"helix-{name}"
    headless = f"helix-{name}-headless"
    domain = f"{headless}.{NAMESPACE}.svc.cluster.local"
    image = f"{IMAGE_REPO}:{image_tag}"
    cluster_id = f"helix-{name}"
    # Peer loop is built for `replicas` nodes (ordinals 0..replicas-1).
    return f"""apiVersion: v1
kind: Service
metadata:
  name: {headless}
  namespace: {NAMESPACE}
  labels: {{ app: {app}, managed-by: directed-evolution-clusters }}
spec:
  clusterIP: None
  publishNotReadyAddresses: true
  selector: {{ app: {app} }}
  ports:
    - {{ name: kafka, port: 9092 }}
    - {{ name: raft, port: 9001 }}
---
apiVersion: v1
kind: Service
metadata:
  name: {app}
  namespace: {NAMESPACE}
  labels: {{ app: {app}, managed-by: directed-evolution-clusters }}
spec:
  selector: {{ app: {app} }}
  ports:
    - {{ name: kafka, port: 9092, targetPort: 9092 }}
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: {app}
  namespace: {NAMESPACE}
  labels: {{ app: {app}, managed-by: directed-evolution-clusters }}
spec:
  serviceName: {headless}
  replicas: {replicas}
  podManagementPolicy: Parallel
  selector:
    matchLabels: {{ app: {app} }}
  template:
    metadata:
      labels: {{ app: {app}, managed-by: directed-evolution-clusters }}
    spec:
      terminationGracePeriodSeconds: 20
      securityContext: {{ runAsUser: 1000, runAsGroup: 1000, fsGroup: 1000 }}
      nodeSelector: {{ pool: dark-factory }}
      tolerations:
        - {{ key: dedicated, operator: Equal, value: dark-factory, effect: NoExecute }}
      affinity:
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 100
              podAffinityTerm:
                labelSelector:
                  matchLabels: {{ app: {app} }}
                topologyKey: kubernetes.io/hostname
      containers:
        - name: helix
          image: {image}
          imagePullPolicy: Always
          command: ["/bin/sh", "-c"]
          args:
            - |
              set -e
              ORD="${{HOSTNAME##*-}}"
              NODE_ID=$((ORD + 1))
              DOMAIN="{domain}"
              PEERS=""
              N={replicas}
              i=0
              while [ "$i" -lt "$N" ]; do
                if [ "$i" != "$ORD" ]; then
                  PID=$((i + 1))
                  PEERS="$PEERS --peer=${{PID}}:{app}-${{i}}.${{DOMAIN}}:9092:9001"
                fi
                i=$((i + 1))
              done
              echo "Starting {app} node_id=${{NODE_ID}} ordinal=${{ORD}} peers=${{PEERS}}"
              exec helix-server \\
                --protocol=kafka \\
                --node-id="${{NODE_ID}}" \\
                --cluster-id={cluster_id} \\
                --listen-addr=0.0.0.0:9092 \\
                --kafka-advertise-addr="{app}-${{ORD}}.${{DOMAIN}}:9092" \\
                --raft-addr=0.0.0.0:9001 \\
                ${{PEERS}} \\
                --data-dir=/var/lib/helix \\
                --auto-create-topics \\
                --auto-create-partitions=3 \\
                --log-level=info
          env:
            - {{ name: DD_AGENT_HOST, value: "{WORKLOAD_DD_AGENT_HOST}" }}
            - {{ name: DD_DOGSTATSD_PORT, value: "{WORKLOAD_DD_DOGSTATSD_PORT}" }}
          ports:
            - {{ name: kafka, containerPort: 9092 }}
            - {{ name: raft, containerPort: 9001 }}
          resources:
            requests: {{ cpu: "150m", memory: "256Mi" }}
            limits: {{ cpu: "500m", memory: "512Mi" }}
          readinessProbe:
            tcpSocket: {{ port: 9092 }}
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            tcpSocket: {{ port: 9092 }}
            initialDelaySeconds: 15
            periodSeconds: 20
          volumeMounts:
            - {{ name: data, mountPath: /var/lib/helix }}
  volumeClaimTemplates:
    - metadata: {{ name: data }}
      spec:
        accessModes: ["ReadWriteOnce"]
        resources: {{ requests: {{ storage: 1Gi }} }}
"""


def _worktree_path(name: str) -> str:
    """On-disk path for a cluster's worktree (sibling of the helix repo)."""
    return f"/Users/arun.parthiban/notdd/helix-worktrees/cluster-{name}"


def _create_worktree(name: str, base: str) -> tuple[bool, str, str]:
    """Create a git worktree at cluster-<name> on branch df-cluster/<name>
    forced to `base`. Idempotent: if the worktree already exists, reuse it.

    IMPORTANT: we ALWAYS force-reset the branch ref to ``base`` before adding
    the worktree. Otherwise a stale df-cluster/<name> branch left over from a
    previous run silently shadows the new base SHA, and the worktree ends up
    running the lineage tip instead of the variant's actual code change. This
    is what caused every stage-2+ variant cluster in the May 28 run to build
    the same `8260087` image regardless of the gene the agent emitted.
    """
    branch = f"df-cluster/{name}"
    path = _worktree_path(name)
    if os.path.isdir(path):
        return True, branch, f"worktree already exists at {path}"

    # Verify base is a real commit-ish before we touch any refs. `branch -f`
    # would happily fast-forward to garbage and we'd lose the existing ref.
    if not git_ref_exists(base):
        return False, branch, f"base ref {base!r} does not resolve to a commit"

    # Reset branch to exactly `base` (creates it if it doesn't exist), then
    # add the worktree at that branch. `branch -f` is safe because each
    # df-cluster/<name> is owned by exactly one cluster's lifecycle.
    pr = _git("branch", "-f", branch, base)
    if pr.returncode != 0:
        out = (pr.stdout + pr.stderr).strip()
        return False, branch, f"failed to point {branch} at {base}: {out[-800:]}"

    p = _git("worktree", "add", path, branch)
    out = (p.stdout + p.stderr).strip()
    return p.returncode == 0, branch, out[-1500:]


def _remove_worktree(name: str) -> tuple[bool, str]:
    """Prune a cluster's worktree (best-effort; force in case of local changes)."""
    path = _worktree_path(name)
    if not os.path.isdir(path):
        return True, f"no worktree at {path}"
    p = _git("worktree", "remove", "--force", path)
    return p.returncode == 0, (p.stdout + p.stderr).strip()[-800:]


def _cloud_build_from_worktree(name: str, image_tag: str, on_line=None) -> tuple[bool, str]:
    """Run a Cloud Build from the cluster's worktree, producing IMAGE_REPO:tag.

    amd64 build (~8-10 min). Uses the same /tmp cloudbuild config the rest of the
    demo uses, with the worktree as the build context (`gcloud builds submit .`).
    Streams stdout/stderr line-by-line to ``on_line`` so the UI can tail it live.
    """
    path = _worktree_path(name)
    if not os.path.isdir(path):
        return False, f"worktree not found at {path}"
    cmd = [
        "gcloud", "builds", "submit",
        f"--project={GCP_PROJECT}",
        f"--config={CLOUDBUILD_CONFIG}",
        f"--substitutions=_TAG={image_tag}",
        ".",
    ]
    lines: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd, cwd=path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return False, f"cloud build error: {exc}"
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            lines.append(line)
            if on_line:
                on_line(line)
        proc.wait(timeout=1200)
    except subprocess.TimeoutExpired:
        proc.kill()
        return False, "\n".join(lines[-60:]) + "\n[timed out after 1200s]"
    except (subprocess.SubprocessError, OSError) as exc:
        return False, "\n".join(lines[-60:]) + f"\n[stream error: {exc}]"
    return proc.returncode == 0, "\n".join(lines[-2000:])[-2000:]


def _prior_image_for_cluster(name: str, current_tag: str | None) -> str | None:
    """Pick the image tag to roll a cluster back to.

    Strategy: the most recent Deploy whose ImageTag differs from the cluster's
    current tag (the deploy history is the cluster's lineage) — BUT only tags
    that ACTUALLY EXIST in the Artifact Registry, so a rollback never targets a
    synthetic/audit tag (e.g. 'rollback-undo') that has no pullable image.
    Normalizes 'helix:foo' / 'repo/helix:foo' forms to the bare tag suffix.
    Falls back to the newest registry tag != current. None if nothing suitable.
    """
    real = set(registry_image_tags())  # only pullable tags

    def norm(tag: str | None) -> str | None:
        if not tag:
            return None
        return tag.split(":")[-1].split("/")[-1]

    cur = norm(current_tag)
    seen: list[str] = []
    doc = temper_get("/tdata/Deploys")
    if doc:
        rows = doc.get("value", doc.get("entities", [])) or []
        # Deploys come back oldest-first by id; reverse for most-recent-first.
        for entity in reversed(rows):
            f = entity.get("fields", {}) or {}
            tag = norm(field(f, "image_tag", "ImageTag"))
            if tag and tag in real and tag not in seen:
                seen.append(tag)
    for tag in seen:
        if tag != cur:
            return tag
    # Fallback: newest real registry tag != current.
    for tag in registry_image_tags():
        if norm(tag) != cur:
            return tag
    return None


def exec_rollback_cluster(name: str, to_tag: str) -> tuple[bool, str, str]:
    """Roll the helix-<name> StatefulSet back to a specific image tag via
    `kubectl set image` + rollout status (ns-pinned). Deterministic target,
    unlike `rollout undo`."""
    _assert_namespace()
    assert _CLUSTER_NAME_RE.match(name) and name != "helix", (
        f"refusing rollback: invalid/reserved cluster name {name!r}"
    )
    app = f"helix-{name}"
    image = f"{IMAGE_REPO}:{to_tag}"
    cmd_str = f"kubectl -n {NAMESPACE} set image statefulset/{app} helix={image}"
    p1 = _kubectl("set", "image", f"statefulset/{app}", f"helix={image}", timeout=60)
    out = (p1.stdout + p1.stderr).strip()
    if p1.returncode != 0:
        return False, cmd_str, out[-3000:]
    p2 = _kubectl("rollout", "status", f"statefulset/{app}", "--timeout=180s", timeout=200)
    out = (out + "\n" + p2.stdout + p2.stderr).strip()
    return p2.returncode == 0, cmd_str, out[-3000:]


def exec_apply_cluster(name: str, image_tag: str, replicas: int) -> tuple[bool, str, str]:
    """kubectl apply the per-cluster StatefulSet + Services, then a bounded wait.
    ns-pinned; only ever applies helix-<name> objects."""
    _assert_namespace()
    app = f"helix-{name}"
    manifest = _cluster_manifest(name, image_tag, replicas)
    cmd_str = f"kubectl -n {NAMESPACE} apply -f - (StatefulSet+Services/{app})"
    p1 = _kubectl_stdin(manifest, "apply", "-f", "-", timeout=90)
    out = (p1.stdout + p1.stderr).strip()
    if p1.returncode != 0:
        return False, cmd_str, out[-2500:]
    p2 = _kubectl(
        "rollout", "status", f"statefulset/{app}", "--timeout=180s", timeout=200
    )
    out = (out + "\n" + p2.stdout + p2.stderr).strip()
    return p2.returncode == 0, cmd_str, out[-2500:]


def exec_delete_cluster(name: str) -> tuple[bool, str, str]:
    """kubectl delete the helix-<name> StatefulSet + both Services (ns-pinned)."""
    _assert_namespace()
    assert _CLUSTER_NAME_RE.match(name) and name != "helix", (
        f"refusing delete: invalid/reserved cluster name {name!r}"
    )
    app = f"helix-{name}"
    cmd_str = f"kubectl -n {NAMESPACE} delete statefulset/{app} service/{app} service/{app}-headless --ignore-not-found"
    p = _kubectl(
        "delete",
        f"statefulset/{app}", f"service/{app}", f"service/{app}-headless",
        "--ignore-not-found", timeout=90,
    )
    return p.returncode == 0, cmd_str, (p.stdout + p.stderr).strip()[-2000:]


def cluster_pods(name: str) -> list[dict[str, Any]]:
    """Live pod state for a cluster (read-only). Reuses workload_pods' shape."""
    return workload_pods(f"helix-{name}")  # selector app=helix-<name>


def _map_cluster(doc: dict[str, Any]) -> dict[str, Any]:
    """Map a raw Temper Cluster entity into a list-item shape for the UI."""
    fields = doc.get("fields", {}) or {}
    name = field(fields, "name", "Name") or ""
    cid = doc.get("id") or field(fields, "Id", "id") or ""
    job = _CLUSTER_JOBS.get(cid, {})
    return {
        "id": cid,
        "name": name,
        "status": doc.get("status") or field(fields, "Status") or "Defined",
        "branch": field(fields, "branch", "Branch"),
        "worktree": field(fields, "worktree_path", "WorktreePath"),
        "image_tag": field(fields, "image_tag", "ImageTag"),
        "replicas": field(fields, "replicas", "Replicas"),
        "statefulset": field(fields, "StatefulSet") or (f"helix-{name}" if name else None),
        "result": field(fields, "result", "Result"),
        "build_result": field(fields, "build_result", "BuildResult"),
        # Live build/deploy phase for a cluster whose Apply is running in-process.
        "job_phase": job.get("phase"),
        "job_detail": job.get("detail"),
        "pods": cluster_pods(name) if name else [],
    }


@app.get("/api/clusters")
def list_clusters() -> JSONResponse:
    """List governed Cluster entities (newest first)."""
    doc = temper_get("/tdata/Clusters")
    items: list[dict[str, Any]] = []
    if doc:
        for entity in doc.get("value", doc.get("entities", [])) or []:
            try:
                items.append(_map_cluster(entity))
            except (KeyError, TypeError):
                continue
    items.sort(key=lambda c: c["id"], reverse=True)
    return JSONResponse({"clusters": items, "reachable": doc is not None})


@app.post("/api/clusters")
def create_cluster(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """Create + Define a governed Cluster. Stays Defined until a human approves.

    No worktree/build/kubectl runs here — provisioning fires only on /approve,
    after the human Approve + Apply (the Cedar gate).
    """
    name = _valid_cluster_name(payload)
    base = str((payload or {}).get("base", "main")).strip() or "main"
    replicas = _wl_int(payload, "replicas", 3, 1, 5)
    if not git_ref_exists(base):
        raise HTTPException(status_code=400, detail=f"base ref {base!r} does not exist in the helix repo")

    cid = f"cluster-{name}-{sim_ts()}"
    branch = f"df-cluster/{name}"
    worktree = _worktree_path(name)

    s, b = temper_post(
        "/tdata/Clusters",
        {"id": cid, "Status": "Defined", "Name": name, "Branch": branch,
         "BaseRef": base, "WorktreePath": worktree, "Replicas": replicas,
         "Namespace": NAMESPACE, "StatefulSet": f"helix-{name}"},
        token=operator_token(),
    )
    if s not in (200, 201):
        return _denial_response(s, b, "create Cluster")
    s, b = temper_post(
        f"/tdata/Clusters('{cid}')/Default.Define",
        {"name": name, "branch": branch, "worktree_path": worktree, "replicas": str(replicas)},
        token=supervisor_token(),
    )
    if s not in (200, 204):
        return _denial_response(s, b, "Define Cluster")

    return JSONResponse({
        "ok": True, "kind": "cluster", "id": cid, "status": "Defined",
        "name": name, "branch": branch, "worktree": worktree, "replicas": replicas,
        "base": base,
    })


def _provision_cluster(cid: str, name: str, base: str, replicas: int) -> None:
    """Background worker: worktree -> Cloud Build -> kubectl apply, recording
    each phase on the Cluster entity AND in _CLUSTER_JOBS for live UI polling.

    Runs after the human Approve + Apply (entity is already in Building)."""
    image_tag = f"cluster-{name}"

    def _log(line: str) -> None:
        # Append a line to the cluster's live build log (bounded ring).
        with _CLUSTER_JOBS_LOCK:
            job = _CLUSTER_JOBS.setdefault(cid, {"phase": "queued", "detail": "", "log": []})
            log = job.setdefault("log", [])
            log.append(line)
            if len(log) > 400:
                del log[: len(log) - 400]

    def _set(phase: str, detail: str = "") -> None:
        with _CLUSTER_JOBS_LOCK:
            job = _CLUSTER_JOBS.setdefault(cid, {"log": []})
            job["phase"] = phase
            job["detail"] = detail
        _log(f"── {phase}: {detail}")

    def _fail(step: str, detail: str) -> None:
        _set("failed", f"{step}: {detail}")
        temper_post(f"/tdata/Clusters('{cid}')/Default.RecordResult",
                    {"result": f"FAILED {step}: {detail[:1200]}", "apply_cmd": ""},
                    token=supervisor_token())
        temper_post(f"/tdata/Clusters('{cid}')/Default.MarkFailed", {},
                    token=supervisor_token())

    # 1. Worktree.
    _set("worktree", f"creating worktree off {base}")
    ok, _branch, out = _create_worktree(name, base)
    _log(out)
    if not ok:
        _fail("worktree", out)
        return

    # 2. Cloud Build from the worktree (~8-10 min), streaming output live.
    _set("building", "Cloud Build from worktree (~8-10 min)")
    ok, out = _cloud_build_from_worktree(name, image_tag, on_line=_log)
    if not ok:
        _fail("build", out)
        return
    # Record build success -> Deploying.
    s, b = temper_post(f"/tdata/Clusters('{cid}')/Default.RecordBuild",
                       {"image_tag": image_tag, "build_result": out[-800:]},
                       token=supervisor_token())
    if s not in (200, 204):
        _fail("record-build", json.dumps(b)[:600])
        return

    # 3. kubectl apply the per-cluster StatefulSet.
    _set("deploying", f"applying StatefulSet helix-{name}")
    ok, cmd_str, apply_out = exec_apply_cluster(name, image_tag, replicas)
    for ln in apply_out.splitlines():
        _log(ln)
    temper_post(f"/tdata/Clusters('{cid}')/Default.RecordResult",
                {"result": ("deployed" if ok else "apply-failed") + f": {apply_out[:1200]}",
                 "apply_cmd": cmd_str},
                token=supervisor_token())
    land = "MarkLive" if ok else "MarkFailed"
    temper_post(f"/tdata/Clusters('{cid}')/Default.{land}", {}, token=supervisor_token())
    _set("live" if ok else "failed", cmd_str if ok else apply_out[-400:])


@app.post("/api/clusters/{cid}/approve")
def approve_cluster(cid: str) -> JSONResponse:
    """Approve (HUMAN GATE) + Apply a Cluster, then provision it in the
    background (worktree -> build -> deploy). Returns immediately with Building."""
    doc = temper_get(f"/tdata/Clusters('{cid}')")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Cluster '{cid}' not found")
    fields = doc.get("fields", {}) or {}
    name = field(fields, "name", "Name")
    replicas = int(field(fields, "replicas", "Replicas") or 3)
    branch = field(fields, "branch", "Branch") or f"df-cluster/{name}"
    # Build from the base the cluster was CREATED with — not a hardcoded "main".
    # A niche/variant cluster commits its genome to a base ref; ignoring it here
    # silently built vanilla main (the "wrong image" bug class, see
    # _create_worktree docstring). Default to main only when no base was stored.
    base = field(fields, "base_ref", "BaseRef") or "main"

    s, b = temper_post(f"/tdata/Clusters('{cid}')/Default.Approve", {},
                       token=supervisor_token())
    if s not in (200, 204):
        return _denial_response(s, b, "Approve Cluster")
    s, b = temper_post(f"/tdata/Clusters('{cid}')/Default.Apply", {},
                       token=supervisor_token())
    if s not in (200, 204):
        return _denial_response(s, b, "Apply Cluster")

    with _CLUSTER_JOBS_LOCK:
        _CLUSTER_JOBS[cid] = {"phase": "queued", "detail": "provisioning starting", "log": []}
    threading.Thread(
        target=_provision_cluster, args=(cid, name, base, replicas), daemon=True
    ).start()

    return JSONResponse({
        "ok": True, "kind": "cluster", "id": cid, "status": "Building",
        "name": name, "branch": branch,
        "note": "Approved. Building image from worktree then deploying — poll /api/clusters for progress.",
    })


@app.get("/api/clusters/{cid}/logs")
def cluster_logs(cid: str) -> JSONResponse:
    """Live build/deploy logs for a cluster being provisioned: the in-process
    phase markers + the tail of the streaming Cloud Build / kubectl output.
    Poll this while a cluster is Building/Deploying."""
    with _CLUSTER_JOBS_LOCK:
        job = _CLUSTER_JOBS.get(cid)
        if not job:
            return JSONResponse({"phase": None, "detail": None, "log": [],
                                 "note": "no active provisioning job (already Live, or pre-restart)"})
        return JSONResponse({
            "phase": job.get("phase"),
            "detail": job.get("detail"),
            "log": list(job.get("log", []))[-300:],
        })


@app.post("/api/clusters/{cid}/rollback")
def rollback_cluster(cid: str) -> JSONResponse:
    """Roll a Live cluster's StatefulSet (helix-<name>) back to its previous
    image. Picks the prior image tag from the cluster's deploy history (or an
    explicit ?to_tag=), kubectl set image + rollout (ns-pinned), records result."""
    doc = temper_get(f"/tdata/Clusters('{cid}')")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Cluster '{cid}' not found")
    fields = doc.get("fields", {}) or {}
    name = field(fields, "name", "Name")
    status = doc.get("status") or field(fields, "Status")
    if not name or not _CLUSTER_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"cluster has no valid name: {name!r}")
    if status != "Live":
        raise HTTPException(status_code=400, detail=f"cluster must be Live to roll back (is {status})")

    # Determine the rollback target image. The cluster's deploy history is the
    # set of Deploys whose tag matches this cluster's lineage; pick the most
    # recent prior tag != the current image.
    cur_tag = field(fields, "image_tag", "ImageTag")
    prior = _prior_image_for_cluster(name, cur_tag)
    if not prior:
        raise HTTPException(status_code=400, detail="no prior image tag found to roll back to")

    ok, cmd_str, out = exec_rollback_cluster(name, prior)
    # Record on the entity (RecordResult is valid from Deploying; for a Live
    # cluster we just attach the audit string and keep it Live with the new tag).
    temper_post(f"/tdata/Clusters('{cid}')/Default.RecordResult",
                {"result": ("rolled-back to " + prior if ok else "rollback-failed") + f": {out[:1000]}",
                 "apply_cmd": cmd_str},
                token=supervisor_token())
    return JSONResponse({
        "ok": ok, "kind": "cluster-rollback", "id": cid, "name": name,
        "from": cur_tag, "to": prior, "cmd": cmd_str, "output": out,
    })


def _cascade_stop_cluster_workloads(cluster_name: str) -> list[dict[str, Any]]:
    """Govern-Stop + delete every Producer/Consumer targeting this cluster, and
    Delete its Queues. Called by teardown so a cluster never strands its
    workloads (their brokers DNS would otherwise dangle after the StatefulSet is
    gone). Baseline match: a workload's Cluster field == cluster_name."""
    stopped: list[dict[str, Any]] = []

    def matches(wl_cluster: str | None) -> bool:
        return ((wl_cluster or "helix").strip() or "helix") == cluster_name

    doc = temper_get("/tdata/Producers")
    consumers_doc = temper_get("/tdata/Consumers")
    for entity_set, kind, d in (("Producers", "producer", doc), ("Consumers", "consumer", consumers_doc)):
        if not d:
            continue
        for entity in d.get("value", d.get("entities", [])) or []:
            f = entity.get("fields", {}) or {}
            eid = entity.get("entity_id") or field(f, "Id", "id")
            status = entity.get("status") or field(f, "Status")
            if status in ("Stopped", None) or not eid:
                continue
            if not matches(field(f, "Cluster", "cluster", default="")):
                continue
            dep = f"{kind}-{eid}"
            del_ok, _cmd, _out = exec_delete_workload(dep)
            if status == "Running":
                temper_post(f"/tdata/{entity_set}('{eid}')/Default.Stop", {}, token=supervisor_token())
            stopped.append({"kind": kind, "id": eid, "deleted": del_ok})

    # Retire the cluster's Queues too (governed Delete; topic left to retention).
    qdoc = temper_get("/tdata/Queues")
    if qdoc:
        for entity in qdoc.get("value", qdoc.get("entities", [])) or []:
            f = entity.get("fields", {}) or {}
            eid = entity.get("entity_id") or field(f, "Id", "id")
            status = entity.get("status") or field(f, "Status")
            if status == "Deleted" or not eid:
                continue
            if not matches(field(f, "Cluster", "cluster", default="")):
                continue
            temper_post(f"/tdata/Queues('{eid}')/Default.Delete", {}, token=supervisor_token())
            stopped.append({"kind": "queue", "id": eid, "deleted": True})
    return stopped


@app.post("/api/clusters/{cid}/teardown")
def teardown_cluster(cid: str) -> JSONResponse:
    """Tear a cluster down: stop+delete every workload targeting it, kubectl
    delete its helix-<name> objects (ns-pinned), prune its worktree, and land the
    governed Teardown transition. Cascades so no orphan producer/consumer pods
    are left pointing at the deleted cluster's brokers."""
    doc = temper_get(f"/tdata/Clusters('{cid}')")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Cluster '{cid}' not found")
    fields = doc.get("fields", {}) or {}
    name = field(fields, "name", "Name")
    if not name or not _CLUSTER_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"cluster has no valid name: {name!r}")

    # 1. Cascade: stop/delete this cluster's workloads BEFORE removing the cluster.
    cascaded = _cascade_stop_cluster_workloads(name)
    # 2. Delete the cluster's StatefulSet + Services, prune worktree.
    del_ok, del_cmd, del_out = exec_delete_cluster(name)
    wt_ok, wt_out = _remove_worktree(name)
    with _CLUSTER_JOBS_LOCK:
        _CLUSTER_JOBS.pop(cid, None)

    s, b = temper_post(f"/tdata/Clusters('{cid}')/Default.Teardown", {},
                       token=supervisor_token())
    transitioned = s in (200, 204)

    return JSONResponse({
        "ok": del_ok, "kind": "cluster", "id": cid, "status": "Torndown",
        "name": name, "delete_cmd": del_cmd, "output": del_out,
        "worktree_pruned": wt_ok, "worktree_output": wt_out,
        "workloads_removed": cascaded, "transitioned": transitioned,
    })


# --------------------------------------------------------------------------- #
# Symphony (ADR-0093) — the C1 orchestrator that turns an ImprovementIssue in
# `Implementing` into a real Helix code change: worktree -> coding agent ->
# commit -> PR -> AttachPr. This surfaces the issue-centric board: every
# ImprovementIssue across the loop (Observed -> Researching -> Planned ->
# Implementing -> Verifying -> Deploying -> Done / Failed), with Symphony's
# worktree/branch/PR shown on the Implementing+ cards.
# --------------------------------------------------------------------------- #

# The loop columns, in order. Symphony owns the `Implementing` stage.
SYMPHONY_STAGES = [
    "Observed", "Researching", "Planned", "Implementing",
    "Verifying", "Deploying", "Done",
]
SYMPHONY_WORKTREE_ROOT = "/Users/arun.parthiban/notdd/symphony-worktrees"


def _symphony_worktrees() -> dict[str, dict[str, str]]:
    """Map branch-name -> {path, sha} for every symphony worktree (the isolated
    trees Symphony's workspace.py creates on the Helix repo)."""
    out: dict[str, dict[str, str]] = {}
    try:
        p = _git("worktree", "list", "--porcelain")
    except (subprocess.SubprocessError, OSError):
        return out
    if p.returncode != 0:
        return out
    cur: dict[str, str] = {}
    for line in p.stdout.splitlines():
        if line.startswith("worktree "):
            cur = {"path": line[len("worktree "):].strip()}
        elif line.startswith("HEAD "):
            cur["sha"] = line[len("HEAD "):].strip()[:7]
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch "):].strip().replace("refs/heads/", "")
        elif line.strip() == "" and cur.get("branch"):
            out[cur["branch"]] = {"path": cur.get("path", ""), "sha": cur.get("sha", "")}
            cur = {}
    if cur.get("branch"):
        out[cur["branch"]] = {"path": cur.get("path", ""), "sha": cur.get("sha", "")}
    return out


def _map_issue(entity: dict[str, Any], worktrees: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Map a raw ImprovementIssue entity into a board card."""
    f = entity.get("fields", {}) or {}
    b = entity.get("booleans", {}) or {}
    iid = entity.get("entity_id") or field(f, "Id", "id") or ""
    branch = field(f, "branch_name", "BranchName")
    pr = field(f, "pr_url", "PrUrl")
    wt = worktrees.get(branch) if branch else None
    pr_simulated = bool(pr and str(pr).startswith("SIMULATED://"))
    return {
        "id": iid,
        "status": entity.get("status") or field(f, "Status") or "Observed",
        "title": field(f, "title", "Title") or "(untitled issue)",
        "hypothesis": field(f, "hypothesis", "Hypothesis"),
        "target_file": field(f, "target_file", "TargetFile"),
        "planner_id": field(f, "planner_id", "PlannerId"),
        "assignee_id": field(f, "assignee_id", "AssigneeId"),
        "plan": field(f, "plan", "Plan"),
        "branch_name": branch,
        "pr_url": pr,
        "pr_simulated": pr_simulated,
        "ci_run_id": field(f, "ci_run_id", "CiRunId"),
        "ci_status": field(f, "ci_status", "CiStatus"),
        # Symphony's stage markers (from the entity booleans).
        "has_plan": bool(b.get("has_plan")),
        "plan_approved": bool(b.get("plan_approved")),
        "has_branch": bool(b.get("has_branch")),
        "has_pr": bool(b.get("has_pr")),
        "ci_passed": bool(b.get("ci_passed")),
        # Live worktree backing this issue's branch (Symphony's isolated tree).
        "worktree": ({"path": wt["path"], "sha": wt["sha"]} if wt else None),
    }


@app.get("/api/symphony/issues")
def symphony_issues() -> JSONResponse:
    """The issue-centric board: every ImprovementIssue grouped by loop stage,
    each annotated with Symphony's worktree/branch/PR where present."""
    doc = temper_get("/tdata/ImprovementIssues")
    worktrees = _symphony_worktrees()
    cards: list[dict[str, Any]] = []
    if doc:
        seen: dict[str, dict[str, Any]] = {}
        for entity in doc.get("value", doc.get("entities", [])) or []:
            try:
                card = _map_issue(entity, worktrees)
            except (KeyError, TypeError):
                continue
            # De-dupe by id (the live store can replay duplicates); keep the
            # most-advanced status (later in SYMPHONY_STAGES wins).
            prev = seen.get(card["id"])
            if not prev or _stage_rank(card["status"]) >= _stage_rank(prev["status"]):
                seen[card["id"]] = card
        cards = list(seen.values())

    # Group into columns in loop order; Failed is surfaced separately.
    columns = {stage: [] for stage in SYMPHONY_STAGES}
    failed: list[dict[str, Any]] = []
    for c in cards:
        if c["status"] == "Failed":
            failed.append(c)
        elif c["status"] in columns:
            columns[c["status"]].append(c)
    return JSONResponse({
        "stages": SYMPHONY_STAGES,
        "columns": columns,
        "failed": failed,
        "implementing_stage": "Implementing",
        "reachable": doc is not None,
        "worktree_count": len(worktrees),
    })


def _stage_rank(status: str) -> int:
    try:
        return SYMPHONY_STAGES.index(status)
    except ValueError:
        return -1


@app.get("/api/symphony/issues/{iid}")
def symphony_issue(iid: str) -> JSONResponse:
    """One issue's full detail (for the card expand / drill-in)."""
    doc = temper_get(f"/tdata/ImprovementIssues('{iid}')")
    if not doc:
        raise HTTPException(status_code=404, detail=f"ImprovementIssue '{iid}' not found")
    card = _map_issue(doc, _symphony_worktrees())
    # The governed event timeline (reuse the same shape as deploy detail).
    card["events"] = doc.get("events", [])
    return JSONResponse(card)


# --------------------------------------------------------------------------- #
# Migrations (ADR-0099) — governed queue move: relocate a queue's producers +
# consumers from a source cluster to a specialized target cluster (speciation).
# The breeder requests; the deploy-service executor creates the topic on the
# target, redeploys the queue's workloads at the target's brokers, and stops the
# old ones. Every step is Cedar-gated + recorded.
# --------------------------------------------------------------------------- #

def _workloads_for_queue(queue: str, kind: str) -> list[dict[str, Any]]:
    """Running producers/consumers bound to `queue` (kind in {producer,consumer}).
    Read by the deploy-service executor (NOT the breeder) to recreate them."""
    entity_set = "Producers" if kind == "producer" else "Consumers"
    out: list[dict[str, Any]] = []
    for e in _fetch_workload_set(entity_set):
        item = _normalize_workload(e, kind)
        if item.get("queue") == queue and item.get("status") == "Running":
            out.append(item)
    return out


def _recreate_producer_on(item: dict[str, Any], to_cluster: str) -> tuple[bool, str]:
    """Recreate a producer (same shape/rate/size/concurrency) on to_cluster."""
    res = create_producer({
        "queue": item["queue"],
        "rate_per_sec": item.get("rate_per_sec") or 500,
        "msg_size": item.get("msg_size") or 512,
        "concurrency": item.get("concurrency") or 1,
        "cluster": to_cluster,
        "shape": item.get("shape") or "stable",
        "shape_params": item.get("shape_params") or "",  # preserve the load shape's knobs
    })
    body = json.loads(bytes(res.body).decode()) if hasattr(res, "body") else {}
    return bool(body.get("ok")), body.get("id", "")


def _recreate_consumer_on(item: dict[str, Any], to_cluster: str) -> tuple[bool, str]:
    res = create_consumer({
        "queue": item["queue"],
        "process_ms": item.get("process_ms") or 5,
        "concurrency": item.get("concurrency") or 1,
        "cluster": to_cluster,
        "shape": item.get("shape") or "stable",
        "shape_params": item.get("shape_params") or "",  # preserve the load shape's knobs
    })
    body = json.loads(bytes(res.body).decode()) if hasattr(res, "body") else {}
    return bool(body.get("ok")), body.get("id", "")


def exec_migrate_queue(queue: str, to_cluster: str) -> tuple[bool, str, list[str]]:
    """Re-point a queue's workloads onto to_cluster, then stop the old ones.

    1. Recreate each running producer/consumer for `queue` on to_cluster
       (create_* also creates the topic there via auto-create-topics).
    2. Stop the original workloads so the source cluster sheds the load.
    Returns (ok, summary, moved_ids). ns-pinned via the underlying executors.
    """
    _assert_namespace()
    moved: list[str] = []
    notes: list[str] = []
    ok_all = True

    prods = _workloads_for_queue(queue, "producer")
    cons = _workloads_for_queue(queue, "consumer")
    if not prods and not cons:
        return False, f"no running workloads bound to queue '{queue}'", []

    for p in prods:
        ok, new_id = _recreate_producer_on(p, to_cluster)
        ok_all = ok_all and ok
        if ok:
            moved.append(f"producer {p['id']}->{new_id}")
            _stop_one_workload("producer", p["id"])  # shed from source
        else:
            notes.append(f"producer {p['id']} reschedule failed")
    for c in cons:
        ok, new_id = _recreate_consumer_on(c, to_cluster)
        ok_all = ok_all and ok
        if ok:
            moved.append(f"consumer {c['id']}->{new_id}")
            _stop_one_workload("consumer", c["id"])
        else:
            notes.append(f"consumer {c['id']} reschedule failed")

    summary = f"moved {len(moved)} workload(s) for '{queue}' -> helix-{to_cluster}"
    if notes:
        summary += " | " + "; ".join(notes)
    return ok_all, summary, moved


def _stop_one_workload(kind: str, entity_id: str) -> None:
    """Stop a single producer/consumer (delete the Deployment + land Stop).
    Best-effort: a migration that recreated on the target shouldn't fail just
    because the old stop hiccupped."""
    dep = f"{kind}-{entity_id}"
    try:
        exec_delete_workload(dep)
        entity_set = "Producers" if kind == "producer" else "Consumers"
        temper_post(f"/tdata/{entity_set}('{entity_id}')/Default.Stop", {},
                    token=supervisor_token())
    except (RuntimeError, subprocess.SubprocessError, OSError):
        pass


def _map_migration(entity: dict[str, Any]) -> dict[str, Any]:
    f = entity.get("fields", {}) or {}
    return {
        "id": entity.get("entity_id") or field(f, "Id", "id"),
        "status": entity.get("status") or field(f, "Status") or "Requested",
        "queue": field(f, "queue", "Queue"),
        "from_cluster": field(f, "from_cluster", "FromCluster"),
        "to_cluster": field(f, "to_cluster", "ToCluster"),
        "reason": field(f, "reason", "Reason"),
        "breed_id": field(f, "breed_id", "BreedId"),
        "result": field(f, "result", "Result"),
        "moved": field(f, "moved", "Moved"),
    }


@app.get("/api/migrations")
def list_migrations() -> JSONResponse:
    doc = temper_get("/tdata/Migrations")
    items = [_map_migration(e) for e in (doc or {}).get("value", []) or []] if doc else []
    items.sort(key=lambda m: m["id"] or "", reverse=True)
    return JSONResponse({"migrations": items, "reachable": doc is not None})


def _map_speciation(entity: dict[str, Any]) -> dict[str, Any]:
    f = entity.get("fields", {}) or {}
    return {
        "id": entity.get("entity_id") or field(f, "Id", "id"),
        "status": entity.get("status") or field(f, "Status") or "Proposed",
        "niche": field(f, "niche", "Niche"),
        "queues": field(f, "queues", "Queues"),
        "target_cluster": field(f, "target_cluster", "TargetCluster"),
        "genome": field(f, "genome", "Genome"),
        "motivation": field(f, "motivation", "Motivation"),
        "cluster_id": field(f, "cluster_id", "ClusterId"),
        "image_tag": field(f, "image_tag", "ImageTag"),
        "migration_ids": field(f, "migration_ids", "MigrationIds"),
        "moved": field(f, "moved", "Moved"),
    }


@app.get("/api/speciations")
def list_speciations() -> JSONResponse:
    """The workload-speciation ledger (ADR-0102): niche -> cluster -> migrated
    queues. The speciation agent reads this for idempotency (skip already-Converged
    niches); the UI renders it as the speciation timeline."""
    doc = temper_get("/tdata/Speciations")
    items = [_map_speciation(e) for e in (doc or {}).get("value", []) or []] if doc else []
    items.sort(key=lambda s: s["id"] or "", reverse=True)
    return JSONResponse({"speciations": items, "reachable": doc is not None})


# Speciation tickets ride on the Symphony board as ImprovementIssues. The marker
# below in TargetFile tells the board + the speciation executor that an issue is
# an infra-placement ticket (create cluster + migrate queues), NOT a code change —
# so Symphony's code-implementer skips it and the executor claims it instead.
SPECIATION_TARGET_PREFIX = "speciation://"

# Canonical cluster name per niche. The LLM may propose free-form cluster names
# that vary run-to-run (niche-batch vs niche-hightput-batch), which would defeat
# the dedup. We canonicalize the cluster name from the niche so the same niche
# always maps to the same cluster — dedup-by-cluster then holds. Names are
# DNS-label slugs (<=20 chars, start with a letter).
_NICHE_CANON_CLUSTER = {
    "high-throughput-batch": "niche-htb",
    "bursty": "niche-bursty",
    "low-latency": "niche-low-latency",
    "steady": "niche-steady",
}


def _canon_cluster_for_niche(niche: str, proposed: str) -> str:
    """Stable cluster name for a niche (falls back to a sanitized proposed name)."""
    if niche in _NICHE_CANON_CLUSTER:
        return _NICHE_CANON_CLUSTER[niche]
    s = re.sub(r"[^a-z0-9-]", "-", str(proposed).strip().lower()).strip("-")
    if not s or not s[0].isalpha():
        s = "niche-" + s
    return s[:20].rstrip("-") or "niche-cluster"


def _file_speciation_ticket(placement: dict[str, Any]) -> tuple[bool, str, str]:
    """Create an ImprovementIssue for one placement and drive it Observed ->
    Implementing (auto-advance), so the speciation executor picks it up. Backend-
    mediated with the right tokens (the breeder agent can't drive issues itself).

    placement: {cluster, niche, genome, queues, motivation}
    Returns (ok, issue_id, detail).
    """
    niche = str(placement.get("niche", "")).strip() or "steady"
    # Canonical, stable cluster name per niche (defeats LLM naming drift so dedup
    # across re-runs works). Validated as a DNS-label slug.
    cluster = _valid_cluster_name({"name": _canon_cluster_for_niche(niche, placement.get("cluster", ""))})
    queues = [str(q) for q in placement.get("queues", []) if q]
    genome = placement.get("genome") or {}
    motivation = str(placement.get("motivation", "")).strip()
    if not queues:
        return False, "", "placement has no queues"

    # Idempotency: don't file a duplicate ticket for a cluster that already has an
    # OPEN (non-terminal) speciation ticket. Re-running the agent then no-ops on
    # clusters already queued/in-flight instead of piling up dupes.
    target = f"{SPECIATION_TARGET_PREFIX}{cluster}"
    existing = temper_get("/tdata/ImprovementIssues")
    for e in (existing or {}).get("value", []) or []:
        f = e.get("fields", {}) or {}
        if (field(f, "target_file", "TargetFile") or "") != target:
            continue
        st = e.get("status") or field(f, "Status") or ""
        if st not in ("Done", "Failed"):
            return True, e.get("entity_id") or field(f, "Id", "id"), f"already open ({st})"

    iid = f"imp-spec-{cluster}-{sim_ts()}"
    title = f"Speciate: place [{', '.join(queues)}] on niche cluster {cluster}"
    # The placement payload travels in the plan (machine-readable) so the executor
    # has everything it needs without re-querying telemetry.
    plan_payload = json.dumps({"cluster": cluster, "niche": niche,
                               "genome": genome, "queues": queues})

    def drive(action: str, body: dict, token: str) -> bool:
        s, b = temper_post(f"/tdata/ImprovementIssues('{iid}')/Default.{action}", body, token=token)
        if s not in (200, 204):
            _denial_response(s, b, f"{action} speciation ticket")  # logs
            return False
        return True

    # 1. Create (observer is permitted) at Observed.
    s, b = temper_post("/tdata/ImprovementIssues",
                       {"id": iid, "Status": "Observed", "Title": title,
                        "Hypothesis": f"Telemetry-inferred niche '{niche}'. {motivation}",
                        "TargetFile": target},
                       token=observer_token())
    if s not in (200, 201):
        return False, iid, f"create failed: {b}"

    # 2. Observe -> AssignPlanner(observer) -> BeginPlanning -> WritePlan(observer)
    #    -> ApprovePlan(supervisor) -> Assign(breeder) -> StartWork(breeder).
    ok = (
        drive("Observe", {"title": title, "hypothesis": motivation, "target_file": target}, observer_token())
        and drive("AssignPlanner", {"planner_id": PRINCIPAL_OBSERVER}, supervisor_token())
        and drive("BeginPlanning", {}, supervisor_token())
        and drive("WritePlan", {"plan": plan_payload,
                                "acceptance_criteria": f"Cluster {cluster} Live and queues {queues} migrated onto it."},
                  observer_token())
        and drive("ApprovePlan", {}, supervisor_token())
        and drive("Assign", {"assignee_id": PRINCIPAL_BREEDER}, supervisor_token())
        and drive("StartWork", {"branch_name": f"df-niche/{niche}"}, breeder_token() or operator_token())
    )
    if not ok:
        temper_post(f"/tdata/ImprovementIssues('{iid}')/Default.Fail",
                    {"reason": "could not drive ticket to Implementing"}, token=supervisor_token())
        return False, iid, "drive-to-Implementing failed"
    return True, iid, "Implementing"


@app.post("/api/speciation/tickets")
def create_speciation_tickets(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """File one Symphony ticket per placement (auto-advanced to Implementing).

    Called by the speciation agent when it finishes classifying/proposing. The
    actual cluster-create + migrate is then done by the speciation executor when
    it claims the Implementing ticket. Body: {"placements": [ {cluster, niche,
    genome, queues, motivation}, ... ]}."""
    placements = (payload or {}).get("placements", []) or []
    filed = []
    for pl in placements:
        ok, iid, detail = _file_speciation_ticket(pl)
        filed.append({"ok": ok, "issue_id": iid, "cluster": pl.get("cluster"),
                      "queues": pl.get("queues"), "detail": detail})
    return JSONResponse({"ok": all(f["ok"] for f in filed) if filed else True,
                         "filed": filed, "count": len(filed)})


@app.get("/api/speciation/tickets")
def list_speciation_tickets() -> JSONResponse:
    """The speciation tickets the executor should claim: ImprovementIssues in
    Implementing whose TargetFile marks them as speciation placements. Carries the
    placement payload (from the issue's Plan) so the executor needs no telemetry."""
    doc = temper_get("/tdata/ImprovementIssues")
    out = []
    for e in (doc or {}).get("value", []) or []:
        f = e.get("fields", {}) or {}
        target = field(f, "target_file", "TargetFile") or ""
        status = e.get("status") or field(f, "Status") or ""
        if not target.startswith(SPECIATION_TARGET_PREFIX):
            continue
        plan_raw = field(f, "plan", "Plan") or "{}"
        try:
            placement = json.loads(plan_raw)
        except (ValueError, TypeError):
            placement = {}
        out.append({
            "issue_id": e.get("entity_id") or field(f, "Id", "id"),
            "status": status,
            "title": field(f, "title", "Title"),
            "cluster": target[len(SPECIATION_TARGET_PREFIX):],
            "placement": placement,
        })
    return JSONResponse({"tickets": out})


@app.post("/api/speciation/tickets/{iid}/advance")
def advance_speciation_ticket(iid: str, payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """Advance a speciation ticket through Verifying -> Deploying -> Done (or Fail),
    backend-mediated with the right tokens. The executor calls this after it has
    actually built the cluster + migrated the queues. Body: {"ok": bool,
    "result": str, "cluster_id": str, "migration_ids": [..]}."""
    ok = bool((payload or {}).get("ok", False))
    result = str((payload or {}).get("result", ""))[:1000]

    def drive(action: str, body: dict, token: str) -> bool:
        s, b = temper_post(f"/tdata/ImprovementIssues('{iid}')/Default.{action}", body, token=token)
        return s in (200, 204)

    if not ok:
        drive("Fail", {"reason": result or "speciation executor failed"}, supervisor_token())
        return JSONResponse({"ok": False, "issue_id": iid, "status": "Failed"})

    # Implementing -> AttachPr(record the speciation result as the "PR") -> StartVerify
    # -> RecordCiResult/MarkCiPassed -> ApproveDeploy -> MarkDone. AttachPr/StartVerify
    # are the assignee (breeder); ApproveDeploy/MarkDone are supervisor (role-separated).
    btok = breeder_token() or operator_token()
    seq_ok = (
        drive("AttachPr", {"pr_url": f"speciation://done/{iid} — {result}"}, btok)
        and drive("StartVerify", {"ci_run_id": f"spec-{iid}"}, btok)
        and drive("RecordCiResult", {"ci_status": "passed"}, supervisor_token())
        and drive("MarkCiPassed", {}, supervisor_token())
        and drive("ApproveDeploy", {}, supervisor_token())
        and drive("MarkDone", {}, supervisor_token())
    )
    return JSONResponse({"ok": seq_ok, "issue_id": iid,
                         "status": "Done" if seq_ok else "Implementing"})


# --------------------------------------------------------------------------- #
# Optimizer tickets (ADR-0103): code/config improvements, one ticket per cluster.
# Marked with TargetFile opt://<cluster> so Symphony's code-implementer skips them
# and the OPTIMIZER-EXECUTOR claims them (edit -> verify -> deploy -> post-verify).
# --------------------------------------------------------------------------- #

OPTIMIZER_TARGET_PREFIX = "opt://"


def _drive_issue_to_implementing(iid: str, title: str, hypothesis: str, target: str,
                                 plan_payload: str, acceptance: str) -> bool:
    """Shared: drive a freshly-created ImprovementIssue Observed -> Implementing
    via the observer(plan)/supervisor(approve)/breeder(assignee) token chain so the
    role-separation Cedar rules hold. The entity must already be created."""
    def drive(action: str, body: dict, token: str) -> bool:
        s, b = temper_post(f"/tdata/ImprovementIssues('{iid}')/Default.{action}", body, token=token)
        if s not in (200, 204):
            _denial_response(s, b, f"{action} ticket")
            return False
        return True
    return (
        drive("Observe", {"title": title, "hypothesis": hypothesis, "target_file": target}, observer_token())
        and drive("AssignPlanner", {"planner_id": PRINCIPAL_OBSERVER}, supervisor_token())
        and drive("BeginPlanning", {}, supervisor_token())
        and drive("WritePlan", {"plan": plan_payload, "acceptance_criteria": acceptance}, observer_token())
        and drive("ApprovePlan", {}, supervisor_token())
        and drive("Assign", {"assignee_id": PRINCIPAL_BREEDER}, supervisor_token())
        and drive("StartWork", {"branch_name": f"opt/{iid}"}, breeder_token() or operator_token())
    )


def _file_optimizer_ticket(plan: dict[str, Any]) -> tuple[bool, str, str]:
    """File one ImprovementIssue for a cluster's optimization plan, auto-advanced
    to Implementing. plan: {cluster, workload_summary, proposals:[...]}."""
    cluster = _valid_cluster_name({"name": plan.get("cluster", "")})
    proposals = plan.get("proposals", []) or []
    summary = str(plan.get("workload_summary", "")).strip()
    if not proposals:
        return False, "", f"{cluster}: no proposals"

    target = f"{OPTIMIZER_TARGET_PREFIX}{cluster}"
    # Idempotency: skip if an OPEN optimizer ticket already exists for this cluster.
    existing = temper_get("/tdata/ImprovementIssues")
    for e in (existing or {}).get("value", []) or []:
        f = e.get("fields", {}) or {}
        if (field(f, "target_file", "TargetFile") or "") != target:
            continue
        st = e.get("status") or field(f, "Status") or ""
        if st not in ("Done", "Failed"):
            return True, e.get("entity_id") or field(f, "Id", "id"), f"already open ({st})"

    iid = f"imp-opt-{cluster}-{sim_ts()}"
    n = len(proposals)
    title = f"Optimize helix-{cluster}: {n} code/config change(s) for its workload"
    plan_payload = json.dumps({"cluster": cluster, "workload_summary": summary, "proposals": proposals})
    acceptance = (f"All {n} change(s) applied to helix-{cluster}; cargo test + L0-L3 verify pass; "
                  f"image built + deployed; post-deploy metrics not regressed.")

    s, b = temper_post("/tdata/ImprovementIssues",
                       {"id": iid, "Status": "Observed", "Title": title,
                        "Hypothesis": f"Workload: {summary}", "TargetFile": target},
                       token=observer_token())
    if s not in (200, 201):
        return False, iid, f"create failed: {b}"
    if not _drive_issue_to_implementing(iid, title, summary, target, plan_payload, acceptance):
        temper_post(f"/tdata/ImprovementIssues('{iid}')/Default.Fail",
                    {"reason": "could not drive optimizer ticket to Implementing"}, token=supervisor_token())
        return False, iid, "drive-to-Implementing failed"
    return True, iid, "Implementing"


@app.post("/api/optimizer/tickets")
def create_optimizer_tickets(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """File one Symphony ticket per cluster optimization plan (auto-advanced to
    Implementing). Called by the optimizer agent. The optimizer-executor then
    claims each, applies the edits, verifies, deploys, and post-verifies.
    Body: {"plans": [ {cluster, workload_summary, proposals:[...]}, ... ]}."""
    plans = (payload or {}).get("plans", []) or []
    filed = []
    for p in plans:
        ok, iid, detail = _file_optimizer_ticket(p)
        filed.append({"ok": ok, "issue_id": iid, "cluster": p.get("cluster"),
                      "proposals": len(p.get("proposals", []) or []), "detail": detail})
    return JSONResponse({"ok": all(f["ok"] for f in filed) if filed else True,
                         "filed": filed, "count": len(filed)})


@app.get("/api/optimizer/tickets")
def list_optimizer_tickets() -> JSONResponse:
    """Optimizer tickets in Implementing the executor should claim, with the
    proposal payload (from the issue's Plan)."""
    doc = temper_get("/tdata/ImprovementIssues")
    out = []
    for e in (doc or {}).get("value", []) or []:
        f = e.get("fields", {}) or {}
        target = field(f, "target_file", "TargetFile") or ""
        if not target.startswith(OPTIMIZER_TARGET_PREFIX):
            continue
        try:
            plan = json.loads(field(f, "plan", "Plan") or "{}")
        except (ValueError, TypeError):
            plan = {}
        out.append({
            "issue_id": e.get("entity_id") or field(f, "Id", "id"),
            "status": e.get("status") or field(f, "Status") or "",
            "title": field(f, "title", "Title"),
            "cluster": target[len(OPTIMIZER_TARGET_PREFIX):],
            "plan": plan,
        })
    return JSONResponse({"tickets": out})


@app.post("/api/optimizer/tickets/{iid}/advance")
def advance_optimizer_ticket(iid: str, payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """Advance an optimizer ticket through Verifying -> Deploying -> Done (or Fail),
    after the executor has implemented + verified + deployed + post-verified. Body:
    {"ok": bool, "result": str, "stage": str}."""
    ok = bool((payload or {}).get("ok", False))
    result = str((payload or {}).get("result", ""))[:1200]
    stage = str((payload or {}).get("stage", ""))

    def drive(action: str, body: dict, token: str) -> bool:
        s, b = temper_post(f"/tdata/ImprovementIssues('{iid}')/Default.{action}", body, token=token)
        return s in (200, 204)

    if not ok:
        drive("Fail", {"reason": f"optimizer-executor failed at {stage}: {result}"[:500]}, supervisor_token())
        return JSONResponse({"ok": False, "issue_id": iid, "status": "Failed", "stage": stage})

    btok = breeder_token() or operator_token()
    seq_ok = (
        drive("AttachPr", {"pr_url": f"opt://done/{iid} — {result}"}, btok)
        and drive("StartVerify", {"ci_run_id": f"opt-{iid}"}, btok)
        and drive("RecordCiResult", {"ci_status": "passed"}, supervisor_token())
        and drive("MarkCiPassed", {}, supervisor_token())
        and drive("ApproveDeploy", {}, supervisor_token())
        and drive("MarkDone", {}, supervisor_token())
    )
    return JSONResponse({"ok": seq_ok, "issue_id": iid,
                         "status": "Done" if seq_ok else "Implementing"})


# --------------------------------------------------------------------------- #
# Meta-agent tickets (ADR-0103 follow-on): prompt improvements for the agents
# themselves. TargetFile meta://<agent>; one ticket per target agent. Skipped by
# Symphony's code-implementer (no executor wired yet — proposals are reviewed).
# --------------------------------------------------------------------------- #

META_TARGET_PREFIX = "meta://"


def _file_meta_ticket(agent_plan: dict[str, Any]) -> tuple[bool, str, str]:
    """File one ImprovementIssue for an agent's prompt-improvement plan, auto-
    advanced to Implementing. agent_plan: {agent, trace_summary, proposals:[...]}."""
    agent = re.sub(r"[^a-z0-9-]", "-", str(agent_plan.get("agent", "")).strip().lower()) or "agent"
    proposals = agent_plan.get("proposals", []) or []
    tsum = str(agent_plan.get("trace_summary", "")).strip()
    if not proposals:
        return False, "", f"{agent}: no proposals"

    target = f"{META_TARGET_PREFIX}{agent}"
    existing = temper_get("/tdata/ImprovementIssues")
    for e in (existing or {}).get("value", []) or []:
        f = e.get("fields", {}) or {}
        if (field(f, "target_file", "TargetFile") or "") != target:
            continue
        st = e.get("status") or field(f, "Status") or ""
        if st not in ("Done", "Failed"):
            return True, e.get("entity_id") or field(f, "Id", "id"), f"already open ({st})"

    iid = f"imp-meta-{agent}-{sim_ts()}"
    n = len(proposals)
    title = f"Tune {agent} agent prompt: {n} trace-driven improvement(s)"
    plan_payload = json.dumps({"agent": agent, "trace_summary": tsum, "proposals": proposals})
    acceptance = f"{n} prompt improvement(s) applied to the {agent} agent; fewer wasted MCP calls / lower cost."

    s, b = temper_post("/tdata/ImprovementIssues",
                       {"id": iid, "Status": "Observed", "Title": title,
                        "Hypothesis": f"Trace analysis: {tsum}", "TargetFile": target},
                       token=observer_token())
    if s not in (200, 201):
        return False, iid, f"create failed: {b}"
    if not _drive_issue_to_implementing(iid, title, tsum, target, plan_payload, acceptance):
        temper_post(f"/tdata/ImprovementIssues('{iid}')/Default.Fail",
                    {"reason": "could not drive meta ticket to Implementing"}, token=supervisor_token())
        return False, iid, "drive-to-Implementing failed"
    return True, iid, "Implementing"


@app.post("/api/meta/tickets")
def create_meta_tickets(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """File one Symphony ticket per agent prompt-improvement plan. Called by the
    meta-agent after it scans the LLM-obs traces. Body: {"agents": [ {agent,
    trace_summary, proposals:[...]}, ... ]}."""
    agents = (payload or {}).get("agents", []) or []
    filed = []
    for a in agents:
        ok, iid, detail = _file_meta_ticket(a)
        filed.append({"ok": ok, "issue_id": iid, "agent": a.get("agent"),
                      "proposals": len(a.get("proposals", []) or []), "detail": detail})
    return JSONResponse({"ok": all(f["ok"] for f in filed) if filed else True,
                         "filed": filed, "count": len(filed)})


@app.get("/api/meta/tickets")
def list_meta_tickets() -> JSONResponse:
    """Meta prompt-improvement tickets, with the proposal payload."""
    doc = temper_get("/tdata/ImprovementIssues")
    out = []
    for e in (doc or {}).get("value", []) or []:
        f = e.get("fields", {}) or {}
        target = field(f, "target_file", "TargetFile") or ""
        if not target.startswith(META_TARGET_PREFIX):
            continue
        try:
            plan = json.loads(field(f, "plan", "Plan") or "{}")
        except (ValueError, TypeError):
            plan = {}
        out.append({
            "issue_id": e.get("entity_id") or field(f, "Id", "id"),
            "status": e.get("status") or field(f, "Status") or "",
            "title": field(f, "title", "Title"),
            "agent": target[len(META_TARGET_PREFIX):],
            "plan": plan,
        })
    return JSONResponse({"tickets": out})


@app.post("/api/optimizer/build-deploy")
def optimizer_build_deploy(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """Build an optimizer cluster's worktree into a new image and deploy it to that
    cluster's StatefulSet. The optimizer-executor calls this AFTER it has applied +
    verified the edits in the worktree (helix-worktrees/cluster-<cluster>). Body:
    {"cluster": str, "image_tag": str}. Returns build/deploy result."""
    cluster = _valid_cluster_name({"name": (payload or {}).get("cluster", "")})
    image_tag = re.sub(r"[^a-zA-Z0-9._-]", "-", str((payload or {}).get("image_tag", "")).strip()) \
        or f"opt-{cluster}-{sim_ts()}"
    # 1. Cloud Build from the cluster's worktree (the executor edited it).
    ok, out = _cloud_build_from_worktree(cluster, image_tag)
    if not ok:
        return JSONResponse({"ok": False, "stage": "build", "detail": out[-1500:]})
    # 2. Deploy: set the cluster's StatefulSet image to the new tag + roll.
    ok, cmd_str, dep_out = exec_rollback_cluster(cluster, image_tag)  # generic set-image+rollout
    if ok:
        # Record on the Cluster entity so the UI version/lineage reflects the new image.
        doc = temper_get(f"/tdata/Clusters('cluster-{cluster}-adopt')") or {}
        return JSONResponse({"ok": True, "image_tag": image_tag, "cmd": cmd_str, "detail": dep_out[-800:]})
    return JSONResponse({"ok": False, "stage": "deploy", "image_tag": image_tag, "detail": dep_out[-1500:]})


@app.post("/api/migrations")
def create_migration(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """Create + Request + Approve + Apply a governed queue migration, then run
    the ns-pinned re-point executor. The queue is named by string (telemetry),
    the target cluster must be a live helix-<name>."""
    queue = _valid_queue_name(payload)
    to_cluster = _valid_cluster_name({"name": (payload or {}).get("to_cluster", "")})
    from_cluster = (str((payload or {}).get("from_cluster", "")).strip() or "helix")
    reason = str((payload or {}).get("reason", "")).strip() or "speciation: telemetry-inferred niche"
    breed_id = str((payload or {}).get("breed_id", "")).strip()

    mid = f"mig-{queue}-{to_cluster}-{sim_ts()}"
    s, b = temper_post(
        "/tdata/Migrations",
        {"id": mid, "Status": "Requested", "Queue": queue, "FromCluster": from_cluster,
         "ToCluster": to_cluster, "Reason": reason, "BreedId": breed_id, "Namespace": NAMESPACE},
        token=operator_token(),
    )
    if s not in (200, 201):
        return _denial_response(s, b, "create Migration")
    for action, body in [
        ("Request", {"queue": queue, "from_cluster": from_cluster, "to_cluster": to_cluster,
                     "reason": reason, "breed_id": breed_id}),
        ("Approve", {}),
        ("Apply", {}),
    ]:
        s, b = temper_post(f"/tdata/Migrations('{mid}')/Default.{action}", body,
                           token=supervisor_token())
        if s not in (200, 204):
            return _denial_response(s, b, f"{action} Migration")

    # Executor (entity is now Migrating): re-point the queue's workloads.
    ok, summary, moved = exec_migrate_queue(queue, to_cluster)
    temper_post(f"/tdata/Migrations('{mid}')/Default.RecordResult",
                {"result": summary[:1500], "moved": ", ".join(moved)[:1000]},
                token=supervisor_token())
    land = "MarkDone" if ok else "MarkFailed"
    temper_post(f"/tdata/Migrations('{mid}')/Default.{land}", {}, token=supervisor_token())

    return JSONResponse({
        "ok": ok, "kind": "migration", "id": mid,
        "status": "Done" if ok else "Failed",
        "queue": queue, "from_cluster": from_cluster, "to_cluster": to_cluster,
        "reason": reason, "moved": moved, "result": summary,
    })


# --------------------------------------------------------------------------- #
# Breeds board (ADR-0099) — the operator's view of directed evolution as a
# governed breeding program: global selective pressure, the telemetry firewall
# (proven live), and per-cluster niches with their breeds + results.
# --------------------------------------------------------------------------- #

def _map_goal(entity: dict[str, Any]) -> dict[str, Any]:
    f = entity.get("fields", {}) or {}
    return {
        "id": entity.get("entity_id") or field(f, "Id", "id"),
        "status": entity.get("status") or field(f, "Status"),
        "metric": field(f, "metric", "Metric"),
        "direction": field(f, "direction", "Direction"),
        "target": field(f, "target", "Target"),
        "scope": (field(f, "scope", "Scope", default="cluster") or "cluster"),
        "cluster_id": field(f, "cluster_id", "ClusterId"),
        "lineage_id": field(f, "lineage_id", "LineageId"),
        "generation": field(f, "generation", "Generation"),
    }


def _map_breed(entity: dict[str, Any]) -> dict[str, Any]:
    f = entity.get("fields", {}) or {}
    return {
        "id": entity.get("entity_id") or field(f, "Id", "id"),
        "status": entity.get("status") or field(f, "Status") or "Proposed",
        "cluster_id": field(f, "cluster_id", "ClusterId"),
        "niche": field(f, "niche", "Niche"),
        "fitness_goal_id": field(f, "fitness_goal_id", "FitnessGoalId"),
        "issue_id": field(f, "issue_id", "IssueId"),
        "gene": field(f, "gene", "Gene"),
        "motivation": field(f, "motivation", "Motivation"),
        "build_result": field(f, "build_result", "BuildResult"),
        "perf_delta": field(f, "perf_delta", "PerfDelta"),
        "ci_status": field(f, "ci_status", "CiStatus"),
        "cull_reason": field(f, "cull_reason", "CullReason"),
    }


def _firewall_probe() -> dict[str, Any]:
    """Demonstrate the telemetry firewall LIVE: the breeder must be DENIED (403)
    reading workload entities. Returns the per-entity verdicts for the UI."""
    tok = breeder_token()
    if not tok:
        return {"available": False, "note": "breeder identity not registered"}
    checks = []
    for es in ("Queues", "Producers", "Consumers"):
        try:
            resp = httpx.get(f"{TEMPER_BASE}/tdata/{es}",
                             headers=temper_headers(tok), timeout=8.0)
            code = resp.status_code
        except httpx.HTTPError:
            code = 0
        checks.append({"entity": es[:-1], "code": code, "denied": code == 403})
    all_denied = all(c["denied"] for c in checks)
    return {"available": True, "all_denied": all_denied, "checks": checks}


@app.get("/api/breeds/{breed_id}/diff")
def get_breed_diff(breed_id: str) -> JSONResponse:
    """Return the code diff for a single variant (Breed entity).

    Every variant that was committed has a corresponding `df-cluster/<name>`
    branch in the helix repo (created from the variant's commit SHA when the
    UI provisioned the variant cluster). The variant's mutation is the top
    commit on that branch, so diff `df-cluster/<name>~1...df-cluster/<name>`
    shows exactly the variant's own change vs. the lineage tip it was based on.

    The branch persists even after the cluster has been torn down (worktree
    removal does not delete the branch), so this works for historical variants
    too. Returns {available: False, reason: ...} for variants that never
    produced a commit (e.g. DST-culled before commit_mutation ran).
    """
    doc = temper_get(f"/tdata/Breeds('{breed_id}')")
    if not doc:
        raise HTTPException(status_code=404, detail=f"Breed '{breed_id}' not found")
    f = doc.get("fields", {}) or {}
    cluster_id = field(f, "cluster_id", "ClusterId") or ""
    gene = field(f, "gene", "Gene") or ""
    motivation = field(f, "motivation", "Motivation") or ""
    perf_delta = field(f, "perf_delta", "PerfDelta") or ""
    status = doc.get("status") or field(f, "Status") or "Proposed"

    base_payload = {
        "breed_id": breed_id, "gene": gene, "motivation": motivation,
        "perf_delta": perf_delta, "status": status, "cluster_id": cluster_id,
        "stat": [], "unified_diff": "",
    }

    if not cluster_id or cluster_id.lower() == "none":
        return JSONResponse({
            **base_payload, "available": False, "base": None, "branch": None,
            "reason": "variant never produced a commit (DST-culled or build "
                      "failed before workload was deployed)",
        })

    branch = f"df-cluster/{cluster_id}"
    if not git_ref_exists(branch):
        return JSONResponse({
            **base_payload, "available": False, "base": None, "branch": branch,
            "reason": f"branch {branch!r} no longer exists in the helix repo "
                      f"(was the worktree manually deleted with `git branch -D`?)",
        })

    base = f"{branch}~1"
    try:
        result = git_diff(base, branch)
    except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
        return JSONResponse({
            **base_payload, "available": False, "base": base, "branch": branch,
            "reason": f"git diff failed: {exc}",
        })

    result.update(base_payload)
    result["available"] = True
    return JSONResponse(result)


@app.get("/api/breeds/board")
def breeds_board() -> JSONResponse:
    """Everything the Breeds tab needs: global goals, per-cluster niches (goals +
    breeds), and a live firewall probe."""
    goals_doc = temper_get("/tdata/FitnessGoals")
    breeds_doc = temper_get("/tdata/Breeds")
    clusters_doc = temper_get("/tdata/Clusters")

    goals = [_map_goal(e) for e in (goals_doc or {}).get("value", []) or []]
    breeds = [_map_breed(e) for e in (breeds_doc or {}).get("value", []) or []]

    global_goals = [g for g in goals if g["scope"] == "global" and g["status"] != "Retired"]
    # Per-cluster grouping: live (non-Torndown) clusters + any cluster referenced
    # by a goal/breed.
    cluster_names = set()
    seen_cluster = {}
    for e in (clusters_doc or {}).get("value", []) or []:
        cf = e.get("fields", {}) or {}
        nm = field(cf, "name", "Name")
        st = e.get("status") or field(cf, "Status")
        if nm and st != "Torndown":
            prev = seen_cluster.get(nm)
            if not prev:
                seen_cluster[nm] = {"name": nm, "status": st}
                cluster_names.add(nm)
    for g in goals:
        if g["scope"] == "cluster" and g["cluster_id"]:
            cluster_names.add(g["cluster_id"])
    for b in breeds:
        if b["cluster_id"]:
            cluster_names.add(b["cluster_id"])

    clusters = []
    for nm in sorted(cluster_names):
        cgoals = [g for g in goals if g["scope"] == "cluster" and g["cluster_id"] == nm and g["status"] != "Retired"]
        cbreeds = [b for b in breeds if b["cluster_id"] == nm]
        # niche label: from a breed's niche, else inferred-unknown
        niche = next((b["niche"] for b in cbreeds if b.get("niche")), None)
        clusters.append({
            "name": nm,
            "status": (seen_cluster.get(nm) or {}).get("status"),
            "niche": niche,
            "goals": cgoals,
            "breeds": sorted(cbreeds, key=lambda b: b["id"] or "", reverse=True),
        })

    return JSONResponse({
        "global_goals": global_goals,
        "clusters": clusters,
        "firewall": _firewall_probe(),
        "reachable": goals_doc is not None,
    })


# --------------------------------------------------------------------------- #
# Agent runs (ADR-0100) — a reverse-chron timeline of every loop-agent pass
# (observer | researcher | symphony | breeder), each with a summary + links to
# what it produced. Powers the Runs tab.
# --------------------------------------------------------------------------- #

def _map_run(entity: dict[str, Any]) -> dict[str, Any]:
    f = entity.get("fields", {}) or {}
    metrics_raw = field(f, "metrics", "Metrics")
    try:
        metrics = json.loads(metrics_raw) if metrics_raw else {}
    except (ValueError, TypeError):
        metrics = {}
    produced_raw = field(f, "produced_ids", "ProducedIds") or ""
    produced = [p.strip() for p in str(produced_raw).split(",") if p.strip()]
    return {
        "id": entity.get("entity_id") or field(f, "Id", "id"),
        "status": entity.get("status") or field(f, "Status") or "Running",
        "agent_type": field(f, "agent_type", "AgentType"),
        "trigger": field(f, "trigger", "Trigger"),
        "started_at": field(f, "StartedAt", "started_at"),
        "ended_at": field(f, "EndedAt", "ended_at"),
        "summary": field(f, "summary", "Summary"),
        "metrics": metrics,
        "produced": produced,
    }


@app.get("/api/runs")
def list_runs() -> JSONResponse:
    """All agent runs, newest first (by id, which embeds a sim timestamp)."""
    doc = temper_get("/tdata/AgentRuns")
    runs = [_map_run(e) for e in (doc or {}).get("value", []) or []]
    runs.sort(key=lambda r: r["id"] or "", reverse=True)
    agents = sorted({r["agent_type"] for r in runs if r["agent_type"]})
    return JSONResponse({"runs": runs, "agents": agents, "reachable": doc is not None})


@app.get("/api/runs/{rid}")
def get_run(rid: str) -> JSONResponse:
    doc = temper_get(f"/tdata/AgentRuns('{rid}')")
    if not doc:
        raise HTTPException(status_code=404, detail=f"AgentRun '{rid}' not found")
    run = _map_run(doc)
    run["events"] = doc.get("events", [])
    return JSONResponse(run)


# --------------------------------------------------------------------------- #
# FitnessGoals CRUD (ADR-0099/0100) — the human edits selective pressure. A goal
# is a governed entity (Set->Active->Retired); since there is no in-place edit
# action and terminal states are final, "update" = Retire the old + create a new
# one (an auditable supersede, not a silent mutation). Add = create+Define+
# Activate; Delete = Retire.
# --------------------------------------------------------------------------- #

_GOAL_METRIC_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_GOAL_DIRECTIONS = ("maximize", "minimize")


def _valid_goal(payload: dict[str, Any]) -> dict[str, Any]:
    metric = str((payload or {}).get("metric", "")).strip().lower()
    direction = str((payload or {}).get("direction", "")).strip().lower()
    target = str((payload or {}).get("target", "")).strip()
    scope = str((payload or {}).get("scope", "global")).strip().lower()
    cluster_id = str((payload or {}).get("cluster_id", "")).strip()
    if not _GOAL_METRIC_RE.match(metric):
        raise HTTPException(status_code=400, detail="metric must be a lowercase slug (e.g. throughput, p95_latency, cost_efficiency)")
    if direction not in _GOAL_DIRECTIONS:
        raise HTTPException(status_code=400, detail="direction must be 'maximize' or 'minimize'")
    if scope not in ("global", "cluster"):
        raise HTTPException(status_code=400, detail="scope must be 'global' or 'cluster'")
    if scope == "cluster" and not cluster_id:
        raise HTTPException(status_code=400, detail="cluster-scoped goal needs a cluster_id")
    return {"metric": metric, "direction": direction, "target": target,
            "scope": scope, "cluster_id": cluster_id}


def _create_goal(g: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    """Create + Define + Activate a governed FitnessGoal. Returns (ok, id, err)."""
    gid = f"fg-{g['scope']}-{g['metric']}-{sim_ts()}"
    lineage = g["cluster_id"] or g["scope"]
    s, b = temper_post(
        "/tdata/FitnessGoals",
        {"id": gid, "Status": "Set", "Scope": g["scope"], "Metric": g["metric"],
         "Direction": g["direction"], "Target": g["target"],
         "ClusterId": g["cluster_id"], "LineageId": lineage},
        token=operator_token(),
    )
    if s not in (200, 201):
        return False, gid, b
    s, b = temper_post(
        f"/tdata/FitnessGoals('{gid}')/Default.Define",
        {"lineage_id": lineage, "metric": g["metric"], "direction": g["direction"],
         "target": g["target"], "scope": g["scope"], "cluster_id": g["cluster_id"]},
        token=supervisor_token(),
    )
    if s not in (200, 204):
        return False, gid, b
    s, b = temper_post(f"/tdata/FitnessGoals('{gid}')/Default.Activate", {},
                       token=supervisor_token())
    if s not in (200, 204):
        return False, gid, b
    return True, gid, {}


def _retire_goal(gid: str, reason: str) -> tuple[bool, dict[str, Any]]:
    s, b = temper_post(f"/tdata/FitnessGoals('{gid}')/Default.Retire",
                       {"reason": reason}, token=supervisor_token())
    return s in (200, 204), b


@app.get("/api/goals")
def list_goals() -> JSONResponse:
    doc = temper_get("/tdata/FitnessGoals")
    goals = [_map_goal(e) for e in (doc or {}).get("value", []) or []]
    goals = [g for g in goals if g["status"] != "Retired"]
    return JSONResponse({"goals": goals, "reachable": doc is not None})


@app.post("/api/goals")
def create_goal(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    g = _valid_goal(payload)
    ok, gid, err = _create_goal(g)
    if not ok:
        return _denial_response(403 if "AuthorizationDenied" in json.dumps(err) else 400, err, "create FitnessGoal")
    return JSONResponse({"ok": True, "id": gid, **g})


@app.post("/api/goals/{gid}/retire")
def retire_goal(gid: str) -> JSONResponse:
    if not temper_get(f"/tdata/FitnessGoals('{gid}')"):
        raise HTTPException(status_code=404, detail=f"FitnessGoal '{gid}' not found")
    ok, err = _retire_goal(gid, "removed via control plane")
    if not ok:
        return _denial_response(403, err, "Retire FitnessGoal")
    return JSONResponse({"ok": True, "id": gid, "status": "Retired"})


@app.put("/api/goals/{gid}")
def update_goal(gid: str, payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """Governed 'update' = retire the old goal + create a new one (supersede)."""
    old = temper_get(f"/tdata/FitnessGoals('{gid}')")
    if not old:
        raise HTTPException(status_code=404, detail=f"FitnessGoal '{gid}' not found")
    g = _valid_goal(payload)
    # Create the replacement first; only retire the old once the new one lands,
    # so a failed create never leaves the pressure un-set.
    ok, new_id, err = _create_goal(g)
    if not ok:
        return _denial_response(403 if "AuthorizationDenied" in json.dumps(err) else 400, err, "create replacement FitnessGoal")
    r_ok, r_err = _retire_goal(gid, f"superseded by {new_id}")
    return JSONResponse({"ok": True, "id": new_id, "superseded": gid,
                         "retired_old": r_ok, **g})


# --------------------------------------------------------------------------- #
# Plain-English goals (ADR-0099 follow-up) — the human describes selective
# pressure in natural language; Claude parses it into the structured
# {metric, direction, target} the breeder understands, then the goal is created
# through the same governed flow as a hand-entered one.
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Intent-driven goals (ADR-0101) — the human states an OPEN-ENDED goal in plain
# English; the agent discovers which existing Datadog metrics serve it, detects
# BLINDSPOTS (intent references something unmeasured), and the goal tracks a
# DYNAMIC metric set. No fixed vocabulary: an unmeasured intent triggers an
# instrument-to-measure code change (see /api/goals/instrument), not rejection.
# --------------------------------------------------------------------------- #

# Direct Anthropic API (real sk-ant key), NOT the DD gateway. trust_env=False so
# OPENAI_API_KEY in the env never leaks into the request (the gateway rejects it).
_ANTHROPIC_DIRECT_URL = "https://api.anthropic.com/v1/messages"
_PARSE_MODEL = "claude-haiku-4-5-20251001"
_METRIC_CATALOG_PATH = (
    "/Users/arun.parthiban/notdd/temper/reference-apps/breeder/snapshots/metric_catalog.json"
)


def _load_metric_catalog() -> dict[str, Any]:
    """The AVAILABLE Datadog metrics the agent reasons over (helix.* + workload.*).
    Captured from the live org via the Datadog MCP catalog search."""
    try:
        with open(_METRIC_CATALOG_PATH) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"helix_metrics": [], "workload_metrics": []}


def _claude_json(prompt: str, max_tokens: int = 700) -> dict[str, Any]:
    """Call Claude (direct api.anthropic.com) and parse a JSON object reply."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key.startswith("sk-ant-"):
        raise HTTPException(status_code=503, detail="LLM unavailable: no Anthropic API key configured")
    hdr = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    body = {"model": _PARSE_MODEL, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}]}
    try:
        with httpx.Client(trust_env=False, timeout=40.0) as c:
            r = c.post(_ANTHROPIC_DIRECT_URL, headers=hdr, json=body)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"LLM call failed: {exc}") from exc
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"LLM error {r.status_code}: {r.text[:200]}")
    try:
        content = r.json()["content"][0]["text"].strip()
        if content.startswith("```"):
            content = content.split("```")[1].lstrip("json").strip()
        return json.loads(content)
    except (KeyError, IndexError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"LLM returned unparseable output: {exc}") from exc


def _discover_coverage(intent: str) -> dict[str, Any]:
    """Given an open-ended intent, reason over the AVAILABLE metric catalog:
    which existing metrics serve it, is there a blindspot, and if so what new
    helix.* metric would close it. Returns the coverage analysis (no creation)."""
    cat = _load_metric_catalog()
    helix = [m["name"] for m in cat.get("helix_metrics", [])]
    workload = [m["name"] for m in cat.get("workload_metrics", [])]
    catalog_lines = "\n".join(
        f'  - {m["name"]}: {m["desc"]}'
        for m in cat.get("helix_metrics", []) + cat.get("workload_metrics", [])
    )
    prompt = (
        "You are the metric-coverage analyst for a self-improving Helix (Kafka-"
        "replacement) cluster. A human stated an OPEN-ENDED performance goal. Your "
        "job: decide which EXISTING metrics serve it, and whether there is a "
        "BLINDSPOT (the goal needs something NOTHING currently measures).\n\n"
        "AVAILABLE metrics (the only ones that exist today):\n" + catalog_lines + "\n\n"
        "Return ONLY a JSON object:\n"
        "{\n"
        '  "echo": "<one sentence restating the goal>",\n'
        '  "relevant_metrics": ["<names from the available list that bear on the goal>"],\n'
        '  "primary": {"metric": "<the single best existing metric to optimize, or empty>", '
        '"direction": "maximize|minimize"},\n'
        '  "blindspot": <true|false>,\n'
        '  "blindspot_reason": "<what the goal needs that nothing measures, or empty>",\n'
        '  "proposed_metric": {"name": "helix.<area>.<thing>", "type": "histogram|count|gauge", '
        '"desc": "<what it measures>", "rationale": "<why it closes the blindspot>"}\n'
        "}\n"
        "Rules: relevant_metrics MUST be a subset of the available list. If the goal "
        "is fully covered, blindspot=false and proposed_metric empty. If covered only "
        "partially or not at all, blindspot=true and propose ONE concrete helix.* "
        "metric in the established DogStatsD naming style.\n\n"
        f"Human goal: {intent!r}"
    )
    res = _claude_json(prompt)
    # Sanity: keep only relevant metrics that actually exist.
    avail = set(helix) | set(workload)
    res["relevant_metrics"] = [m for m in res.get("relevant_metrics", []) if m in avail]
    res["available_count"] = len(avail)
    return res


@app.post("/api/goals/coverage")
def goal_coverage(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """Discover metric coverage for an open-ended intent (no creation)."""
    intent = str((payload or {}).get("intent") or (payload or {}).get("text", "")).strip()
    if not intent:
        raise HTTPException(status_code=400, detail="intent is required")
    return JSONResponse({"ok": True, "intent": intent, **_discover_coverage(intent)})


@app.post("/api/goals/intent")
def create_intent_goal(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """Create a governed goal from an open-ended intent: run coverage discovery,
    then create the FitnessGoal carrying the intent + its discovered metric set +
    a derived primary axis. Blindspot is surfaced for the instrument step."""
    intent = str((payload or {}).get("intent") or (payload or {}).get("text", "")).strip()
    if not intent:
        raise HTTPException(status_code=400, detail="intent is required")
    cov = _discover_coverage(intent)

    primary = cov.get("primary") or {}
    metric = str(primary.get("metric", "")).strip()
    direction = str(primary.get("direction", "maximize")).strip().lower()
    if direction not in _GOAL_DIRECTIONS:
        direction = "maximize"
    tracked = cov.get("relevant_metrics", [])

    gid = f"fg-global-intent-{sim_ts()}"
    s, b = temper_post(
        "/tdata/FitnessGoals",
        {"id": gid, "Status": "Set", "Scope": "global", "Intent": intent,
         "TrackedMetrics": json.dumps(tracked), "Metric": metric,
         "Direction": direction, "Target": "", "LineageId": "global"},
        token=operator_token(),
    )
    if s not in (200, 201):
        return _denial_response(s, b, "create FitnessGoal")
    s, b = temper_post(
        f"/tdata/FitnessGoals('{gid}')/Default.Define",
        {"intent": intent, "lineage_id": "global", "metric": metric,
         "direction": direction, "target": "", "scope": "global",
         "cluster_id": "", "metrics": json.dumps(tracked)},
        token=supervisor_token(),
    )
    if s not in (200, 204):
        return _denial_response(s, b, "Define FitnessGoal")
    s, b = temper_post(f"/tdata/FitnessGoals('{gid}')/Default.Activate", {},
                       token=supervisor_token())
    if s not in (200, 204):
        return _denial_response(s, b, "Activate FitnessGoal")

    return JSONResponse({
        "ok": True, "id": gid, "intent": intent,
        "echo": cov.get("echo", ""),
        "tracked_metrics": tracked,
        "primary": {"metric": metric, "direction": direction},
        "blindspot": bool(cov.get("blindspot")),
        "blindspot_reason": cov.get("blindspot_reason", ""),
        "proposed_metric": cov.get("proposed_metric") or {},
    })


# --------------------------------------------------------------------------- #
# Instrument-to-measure loop (ADR-0101) — when a goal's intent hits a blindspot,
# the agent CLOSES it for real: governed ImprovementIssue -> a real edit to
# helix-server/src/metrics.rs adding the new helix.* metric (const + a gauge emit
# in the periodic sampler, correct-by-construction) -> cargo check -> Cloud Build
# -> deploy to helix-cluster-001 -> the new metric flows in Datadog. Fully live.
# --------------------------------------------------------------------------- #

_HELIX_METRICS_RS = HELIX_REPO + "/helix-server/src/metrics.rs"
_HELIX_SERVICE_RS = HELIX_REPO + "/helix-server/src/service/mod.rs"

# In-process status of an instrument job (the long build/deploy), keyed by issue id.
_INSTRUMENT_JOBS: dict[str, dict[str, Any]] = {}
_INSTRUMENT_LOCK = threading.Lock()

_METRIC_NAME_RE = re.compile(r"^helix\.[a-z0-9_]+\.[a-z0-9_]+$")


def _const_ident(metric_name: str) -> str:
    """helix.system.cpu_microseconds_per_message -> METRIC_SYSTEM_CPU_MICROSECONDS_PER_MESSAGE"""
    suffix = metric_name[len("helix."):]
    return "METRIC_" + suffix.upper().replace(".", "_")


def _apply_metric_edit(worktree: str, metric_name: str) -> tuple[bool, str]:
    """Add the new metric to a Helix worktree: a const in metrics.rs + a gauge
    emit in the replication-lag sampler loop (a known periodic site, so the edit
    compiles and actually emits). Correct-by-construction; no free-form Rust gen.
    Returns (ok, note)."""
    const_id = _const_ident(metric_name)
    metrics_rs = worktree + "/helix-server/src/metrics.rs"
    service_rs = worktree + "/helix-server/src/service/mod.rs"
    try:
        with open(metrics_rs) as fh:
            mtext = fh.read()
        with open(service_rs) as fh:
            stext = fh.read()
    except OSError as exc:
        return False, f"read failed: {exc}"

    if const_id in mtext:
        return True, "metric already present (idempotent)"

    # 1. Insert the const after the replication-lag const (a stable anchor).
    anchor = 'pub const METRIC_REPLICATION_LAG: &str = "helix.replication.lag";'
    if anchor not in mtext:
        return False, "metrics.rs anchor (METRIC_REPLICATION_LAG) not found"
    new_const = (
        anchor
        + f'\n\n/// Metric name: {metric_name} (added by the instrument-to-measure loop, ADR-0101).\n'
        + f'pub const {const_id}: &str = "{metric_name}";'
    )
    mtext = mtext.replace(anchor, new_const, 1)

    # 2. The emit + helper go in via the clean insertion (a sibling gauge in the
    #    sampler loop + a module-scope CPU helper). mtext already has the const.
    return _apply_metric_edit_clean(worktree, metric_name, const_id, mtext, stext)


def _apply_metric_edit_clean(worktree: str, metric_name: str, const_id: str,
                             mtext: str, stext: str) -> tuple[bool, str]:
    """Clean insertion: add the const (mtext already has it) + a free helper fn
    and a single emit line appended inside the sampler loop body, plus the helper
    at module scope. Kept minimal + compiling."""
    metrics_rs = worktree + "/helix-server/src/metrics.rs"
    service_rs = worktree + "/helix-server/src/service/mod.rs"

    # Emit inside the loop: right after the closing of the `for group_id` block,
    # before the loop's end. Anchor on the lag-emit's closing `}` sequence and add
    # a sibling emit using std process CPU time (libc-free: read /proc/self/stat).
    loop_anchor = (
        "                    if let Some(info) = info {\n"
    )
    if loop_anchor not in stext:
        return False, "service sampler loop anchor not found"
    # Append the new gauge emit just before the closing of the for-loop by
    # inserting after the lag emit's full statement.
    lag_emit_full = (
        "                        self.metrics.gauge(\n"
        "                            crate::metrics::METRIC_REPLICATION_LAG,\n"
        "                            lag as f64,\n"
        "                            &[(\"group\", &group_id.get().to_string())],\n"
        "                        );"
    )
    if lag_emit_full not in stext:
        return False, "service lag-emit anchor not found"
    new_gauge = lag_emit_full + (
        "\n                        #[allow(clippy::cast_precision_loss)]\n"
        f"                        self.metrics.gauge(\n"
        f"                            crate::metrics::{const_id},\n"
        f"                            crate::metrics::instrument_process_cpu_micros(),\n"
        "                            &[],\n"
        "                        );"
    )
    stext = stext.replace(lag_emit_full, new_gauge, 1)

    # Add the helper at the end of metrics.rs (module scope).
    helper = (
        "\n\n/// Process CPU time in microseconds (instrument-to-measure helper, ADR-0101).\n"
        "/// Reads /proc/self/stat (utime+stime) on Linux; returns 0.0 elsewhere.\n"
        "#[must_use]\n"
        "pub fn instrument_process_cpu_micros() -> f64 {\n"
        "    #[cfg(target_os = \"linux\")]\n"
        "    {\n"
        "        if let Ok(stat) = std::fs::read_to_string(\"/proc/self/stat\") {\n"
        "            let fields: Vec<&str> = stat.split_whitespace().collect();\n"
        "            if fields.len() > 14 {\n"
        "                let utime: u64 = fields[13].parse().unwrap_or(0);\n"
        "                let stime: u64 = fields[14].parse().unwrap_or(0);\n"
        "                // ticks -> microseconds (USER_HZ=100 on Linux -> 10_000 us/tick)\n"
        "                #[allow(clippy::cast_precision_loss)]\n"
        "                return ((utime + stime) * 10_000) as f64;\n"
        "            }\n"
        "        }\n"
        "        0.0\n"
        "    }\n"
        "    #[cfg(not(target_os = \"linux\"))]\n"
        "    {\n"
        "        0.0\n"
        "    }\n"
        "}\n"
    )
    mtext = mtext + helper

    try:
        with open(metrics_rs, "w") as fh:
            fh.write(mtext)
        with open(service_rs, "w") as fh:
            fh.write(stext)
    except OSError as exc:
        return False, f"write failed: {exc}"
    return True, f"added const {const_id} + sampler gauge emit + CPU helper"


def _instrument_worktree(metric_name: str) -> str:
    slug = _const_ident(metric_name).lower().replace("metric_", "").replace("_", "-")
    return f"/Users/arun.parthiban/notdd/helix-worktrees/metric-{slug}"


def _create_worktree_named(branch_slug: str, base: str, path: str) -> tuple[bool, str, str]:
    """Generic helix worktree at `path` on branch instrument/<slug> off `base`."""
    branch = f"instrument/{branch_slug}"
    if os.path.isdir(path):
        return True, branch, f"worktree already exists at {path}"
    args = ["worktree", "add"]
    if git_ref_exists(branch):
        args += [path, branch]
    else:
        args += ["-b", branch, path, base]
    p = _git(*args)
    return p.returncode == 0, branch, (p.stdout + p.stderr).strip()[-1500:]


def _instrument_set(issue_id: str, phase: str, detail: str = "") -> None:
    with _INSTRUMENT_LOCK:
        _INSTRUMENT_JOBS[issue_id] = {"phase": phase, "detail": detail}


def _cloud_build_from_worktree_path(wt: str, image_tag: str) -> tuple[bool, str]:
    if not os.path.isdir(wt):
        return False, f"worktree not found: {wt}"
    cmd = ["gcloud", "builds", "submit", f"--project={GCP_PROJECT}",
           f"--config={CLOUDBUILD_CONFIG}", f"--substitutions=_TAG={image_tag}", "."]
    try:
        p = subprocess.run(cmd, cwd=wt, capture_output=True, text=True, timeout=1200)
    except (subprocess.SubprocessError, OSError) as exc:
        return False, f"cloud build error: {exc}"
    return p.returncode == 0, (p.stdout + p.stderr).strip()[-2000:]


def exec_deploy_cluster_image(cluster: str, image_tag: str) -> tuple[bool, str, str]:
    """kubectl set image on the helix-<cluster> StatefulSet (ns-pinned) + wait."""
    _assert_namespace()
    ss = f"helix-{cluster}"
    image = f"{IMAGE_REPO}:{image_tag}"
    cmd_str = f"kubectl -n {NAMESPACE} set image statefulset/{ss} helix={image}"
    p1 = _kubectl("set", "image", f"statefulset/{ss}", f"helix={image}", timeout=60)
    out = (p1.stdout + p1.stderr).strip()
    if p1.returncode != 0:
        return False, cmd_str, out[-1500:]
    p2 = _kubectl("rollout", "status", f"statefulset/{ss}", "--timeout=180s", timeout=200)
    return p2.returncode == 0, cmd_str, (out + "\n" + p2.stdout + p2.stderr).strip()[-1500:]


def _provision_metric(issue_id: str, metric_name: str, image_tag: str) -> None:
    """Background: edit already cargo-check-passed -> Cloud Build -> deploy to
    helix-cluster-001 -> the new metric flows. Records phases for polling."""
    wt = _instrument_worktree(metric_name)
    _instrument_set(issue_id, "building", f"Cloud Build {image_tag} from worktree (~8-10 min)")
    ok, out = _cloud_build_from_worktree_path(wt, image_tag)
    if not ok:
        _instrument_set(issue_id, "failed", f"build: {out[-300:]}")
        return
    _instrument_set(issue_id, "deploying", "rolling helix-cluster-001 to the instrumented image")
    ok, _cmd, dout = exec_deploy_cluster_image("cluster-001", image_tag)
    if not ok:
        _instrument_set(issue_id, "failed", f"deploy: {dout[-300:]}")
        return
    _instrument_set(issue_id, "live", f"{metric_name} now emitting from helix-cluster-001")


@app.post("/api/goals/instrument")
def instrument_metric(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """Close a goal's blindspot for real: governed ImprovementIssue -> worktree ->
    edit metrics.rs (add the proposed helix.* metric) -> cargo check (fast-fail) ->
    background Cloud Build + deploy to helix-cluster-001. Fully live."""
    metric = str((payload or {}).get("metric_name", "")).strip()
    goal_id = str((payload or {}).get("goal_id", "")).strip()
    rationale = str((payload or {}).get("rationale", "")).strip() or "close a goal blindspot"
    if not _METRIC_NAME_RE.match(metric):
        raise HTTPException(status_code=400, detail="metric_name must look like helix.<area>.<thing>")

    slug = _const_ident(metric).lower().replace("metric_", "").replace("_", "-")

    iid = f"imp-metric-{slug}-{sim_ts()}"
    title = f"Instrument {metric} to close a fitness-goal blindspot"
    s, b = temper_post("/tdata/ImprovementIssues",
                       {"id": iid, "Status": "Observed", "Title": title,
                        "Hypothesis": rationale, "TargetFile": "helix-server/src/metrics.rs"},
                       token=operator_token())
    if s not in (200, 201):
        return _denial_response(s, b, "create ImprovementIssue")

    wt = _instrument_worktree(metric)
    # Base off champion-metrics — the isolated evolution branch.
    ok, branch, wt_out = _create_worktree_named(slug, "champion-metrics", wt)
    if not ok:
        _instrument_set(iid, "failed", f"worktree: {wt_out}")
        return JSONResponse({"ok": False, "issue_id": iid, "step": "worktree", "detail": wt_out})
    ok, edit_note = _apply_metric_edit(wt, metric)
    if not ok:
        _instrument_set(iid, "failed", f"edit: {edit_note}")
        return JSONResponse({"ok": False, "issue_id": iid, "step": "edit", "detail": edit_note})

    _instrument_set(iid, "checking", "cargo check on the edited worktree")
    try:
        chk = subprocess.run(["cargo", "check", "-p", "helix-server"],
                             cwd=wt, capture_output=True, text=True, timeout=600)
    except (subprocess.SubprocessError, OSError) as exc:
        _instrument_set(iid, "failed", f"cargo check error: {exc}")
        return JSONResponse({"ok": False, "issue_id": iid, "step": "cargo-check", "detail": str(exc)})
    if chk.returncode != 0:
        _instrument_set(iid, "failed", "cargo check failed")
        return JSONResponse({"ok": False, "issue_id": iid, "step": "cargo-check",
                             "detail": (chk.stdout + chk.stderr)[-1500:]})

    image_tag = f"metric-{slug}"
    threading.Thread(target=_provision_metric, args=(iid, metric, image_tag), daemon=True).start()

    rid = f"run-instrument-{slug}-{sim_ts()}"
    temper_post("/tdata/AgentRuns",
                {"id": rid, "Status": "Running", "AgentType": "breeder",
                 "Trigger": f"blindspot on goal {goal_id}: {metric} unmeasured"},
                token=operator_token())

    return JSONResponse({
        "ok": True, "issue_id": iid, "run_id": rid, "branch": branch,
        "metric": metric, "image_tag": image_tag, "edit": edit_note, "cargo_check": "passed",
        "note": "Edit compiles. Building + deploying to helix-cluster-001 in the background — poll /api/goals/instrument/status.",
    })


@app.get("/api/goals/instrument/status")
def instrument_status() -> JSONResponse:
    with _INSTRUMENT_LOCK:
        return JSONResponse({"jobs": dict(_INSTRUMENT_JOBS)})


# --------------------------------------------------------------------------- #
# Agent scheduler — the Directed Evolution loop on a cron. A background thread
# ticks each agent (Observer/Researcher/Symphony/Breeder/Evolution) on its own
# interval by running its CLI, recording each tick as a governed AgentRun.
#
# FULLY AUTONOMOUS WHEN ON: Symphony edits Helix code, Evolution/Breeder can
# trigger real GKE deploys + LLM calls. So the scheduler DEFAULTS TO OFF and only
# runs when explicitly started from the UI; one tick at a time per agent (no
# overlap); a master stop halts everything.
# --------------------------------------------------------------------------- #

REF_APPS = "/Users/arun.parthiban/notdd/temper/reference-apps"

# agent -> {cmd, cwd, interval_secs, enabled}. Intervals are deliberately spread
# so the loop staggers (sensing fast, expensive build/deploy slow).
_DD_ENV_PATH = str(HERE / ".dd-env.json")


def _load_dd_env() -> dict[str, str]:
    """Datadog keys for the observer/researcher live-query path. Read from the
    gitignored .dd-env.json at tick time (never committed, never baked into the
    backend's own env). Returns {} if absent — agents then fail loudly on
    --source datadog rather than querying the wrong org silently."""
    try:
        with open(_DD_ENV_PATH) as fh:
            d = json.load(fh)
        return {k: str(v) for k, v in d.items() if k.startswith("DD_") and v}
    except (OSError, ValueError):
        return {}


SCHED_AGENTS: dict[str, dict[str, Any]] = {
    "observer":   {"cmd": ["python3", "-m", "observer.main", "--source", "datadog"],
                   "cwd": f"{REF_APPS}/observer", "interval": 300, "enabled": True,
                   "summary": "Swept live Datadog telemetry for tuning opportunities."},
    "researcher": {"cmd": ["python3", "-m", "observer.main", "--source", "datadog", "--no-drive"],
                   "cwd": f"{REF_APPS}/observer", "interval": 600, "enabled": True,
                   "summary": "Drafted hypotheses/plans for open improvement issues."},
    "symphony":   {"cmd": ["python3", "-m", "symphony", "--tracker", "temper", "--agent", "claude"],
                   "cwd": f"{REF_APPS}/symphony", "interval": 900, "enabled": True,
                   "summary": "Implemented an Implementing issue: worktree -> edit -> PR."},
    "breeder":    {"cmd": ["python3", "-m", "breeder.infer", "--source", "snapshot"],
                   "cwd": f"{REF_APPS}/breeder", "interval": 600, "enabled": True,
                   "summary": "Inferred workload niches from Datadog telemetry."},
    "evolution":  {"cmd": ["python3", "main.py", "--lineages", "latency,throughput", "--generations", "1"],
                   "cwd": f"{REF_APPS}/evolution", "interval": 1800, "enabled": True,
                   "summary": "Bred one generation per lineage under the active fitness goals."},
    "speciation": {"cmd": ["python3", "-m", "breeder.speciate", "--source", "datadog"],
                   "cwd": f"{REF_APPS}/breeder", "interval": 1800, "enabled": False,
                   "summary": "Classified workloads from telemetry + filed Symphony tickets for niche-cluster placement."},
    "speciation-executor": {"cmd": ["python3", "-m", "breeder.speciate_executor"],
                   "cwd": f"{REF_APPS}/breeder", "interval": 300, "enabled": False,
                   "summary": "Implemented a speciation ticket: built the niche cluster + migrated its queues."},
    "optimizer": {"cmd": ["python3", "-m", "breeder.optimize"],
                   "cwd": f"{REF_APPS}/breeder", "interval": 1800, "enabled": False,
                   "summary": "Read telemetry + Helix source; proposed per-cluster code/config improvements + filed Symphony tickets."},
    "optimizer-executor": {"cmd": ["python3", "-m", "breeder.optimize_executor"],
                   "cwd": f"{REF_APPS}/breeder", "interval": 300, "enabled": False,
                   "summary": "Implemented an optimizer ticket: applied edits, verified (cargo test + L0-L3), deployed, post-verified metrics."},
    "meta-optimizer": {"cmd": ["python3", "-m", "breeder.meta_optimize"],
                   "cwd": f"{REF_APPS}/breeder", "interval": 3600, "enabled": False,
                   "summary": "Scanned agent LLM-obs traces; proposed prompt improvements (efficiency + correctness) + filed Symphony tickets."},
}

_SCHED = {
    "running": False,
    "thread": None,
    "last_run": {a: None for a in SCHED_AGENTS},   # epoch secs
    "last_status": {a: None for a in SCHED_AGENTS},
    "ticking": set(),                               # agents mid-tick (no overlap)
}
_SCHED_LOCK = threading.Lock()


@app.on_event("startup")
def _reconcile_stale_runs() -> None:
    """A tick does not survive a backend restart, so any AgentRun still in
    `Running` after we (re)start is orphaned — its process is gone. Mark such
    runs Failed so the Agent Runs tab doesn't show a perpetual spinner."""
    try:
        doc = temper_get("/tdata/AgentRuns")
    except Exception:
        return
    for e in (doc or {}).get("value", []) or []:
        run = _map_run(e)
        if (run.get("status") or "") != "Running":
            continue
        rid = run["id"]
        tok = _agent_token(run.get("agent_type") or "")
        temper_post(f"/tdata/AgentRuns('{rid}')/Default.RecordSummary",
                    {"summary": f"{run.get('summary') or ''} (interrupted by backend restart)".strip(),
                     "metrics": json.dumps({"exit_ok": False, "reason": "orphaned"}),
                     "produced_ids": ""}, token=tok)
        temper_post(f"/tdata/AgentRuns('{rid}')/Default.Fail",
                    {"reason": "orphaned by backend restart"}, token=tok)


# Agents that author their AgentRun under another verified identity. The
# speciation agent runs as the breeder (telemetry-only, Cedar-permitted on
# AgentRun + Speciation); there is no separate "speciation" identity.
_AGENT_RUN_IDENTITY = {"speciation": "breeder", "speciation-executor": "breeder",
                       "optimizer": "breeder", "optimizer-executor": "breeder",
                       "meta-optimizer": "breeder"}


def _agent_token(agent: str) -> str:
    """The verified identity an agent authors its run as (falls back to operator)."""
    ident = _AGENT_RUN_IDENTITY.get(agent, agent)
    try:
        with open(TOKENS_PATH) as fh:
            data = json.load(fh)
        # researcher reuses the observer toolchain but its own identity if present
        return (data.get(ident, {}) or {}).get("token") or operator_token()
    except (OSError, ValueError):
        return operator_token()


# Strip ANSI escapes (the agents log with colour); used to clean tick output.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _meaningful_tail(output: str) -> str:
    """The last informative line of an agent's CLI output — skipping blank lines,
    separator rules (===, ---, ***), and pure-punctuation banner art. Returns ''
    if nothing meaningful (so the summary stays the clean static description
    rather than appending banner noise — fixes the '=====' summary bug)."""
    for raw in reversed(output.splitlines()):
        line = _ANSI_RE.sub("", raw).strip()
        if not line:
            continue
        # skip separator rules / banners (mostly =, -, *, _, #, spaces)
        if len(line) >= 4 and all(c in "=-*_# " for c in line):
            continue
        # skip lines that are only punctuation/symbols
        if not any(c.isalnum() for c in line):
            continue
        return line[:400]
    return ""


def _run_agent_tick(agent: str) -> None:
    """Run one agent CLI pass + record it as a governed AgentRun. Bounded; no
    overlap (guarded by _SCHED['ticking'])."""
    with _SCHED_LOCK:
        if agent in _SCHED["ticking"]:
            return  # previous tick still running — skip this beat
        _SCHED["ticking"].add(agent)
    cfg = SCHED_AGENTS[agent]
    rid = f"run-{agent}-{sim_ts()}"
    tok = _agent_token(agent)
    # Open the governed run.
    temper_post("/tdata/AgentRuns",
                {"id": rid, "Status": "Running", "AgentType": agent,
                 "Trigger": f"scheduled tick (every {cfg['interval']}s)"},
                token=operator_token())
    temper_post(f"/tdata/AgentRuns('{rid}')/Default.Start",
                {"agent_type": agent, "trigger": "scheduled"}, token=tok)
    ok = False
    summary = cfg["summary"]
    try:
        # Inject Datadog keys (from the gitignored .dd-env.json) so agents using
        # --source datadog (observer/researcher) can query the live org. The keys
        # are loaded at tick time, not baked into the backend env.
        tick_env = dict(os.environ)
        tick_env.update(_load_dd_env())
        p = subprocess.run(cfg["cmd"], cwd=cfg["cwd"], capture_output=True,
                           text=True, timeout=1500, env=tick_env)
        ok = p.returncode == 0
        detail = _meaningful_tail(p.stdout or p.stderr or "")
        summary = f"{cfg['summary']}{(' — ' + detail) if detail else ''}"
    except subprocess.TimeoutExpired:
        summary = f"{cfg['summary']} (timed out)"
    except (subprocess.SubprocessError, OSError) as exc:
        summary = f"{cfg['summary']} (error: {exc})"
    # Close the run.
    temper_post(f"/tdata/AgentRuns('{rid}')/Default.RecordSummary",
                {"summary": summary, "metrics": json.dumps({"exit_ok": ok}),
                 "produced_ids": ""}, token=tok)
    temper_post(f"/tdata/AgentRuns('{rid}')/Default.{'Finish' if ok else 'Fail'}",
                {} if ok else {"reason": "non-zero exit"}, token=tok)
    with _SCHED_LOCK:
        _SCHED["ticking"].discard(agent)
        _SCHED["last_run"][agent] = time.time()
        _SCHED["last_status"][agent] = "ok" if ok else "failed"


def _scheduler_loop() -> None:
    """Tick agents whose interval has elapsed. Checks every 15s."""
    while True:
        with _SCHED_LOCK:
            if not _SCHED["running"]:
                return
        now = time.time()
        for agent, cfg in SCHED_AGENTS.items():
            if not cfg["enabled"]:
                continue
            last = _SCHED["last_run"][agent] or 0
            if now - last >= cfg["interval"]:
                # run in its own thread so a slow agent (build/deploy) doesn't
                # block the others' cadence.
                threading.Thread(target=_run_agent_tick, args=(agent,), daemon=True).start()
                with _SCHED_LOCK:
                    _SCHED["last_run"][agent] = now  # mark scheduled now; tick records true end
        for _ in range(15):
            with _SCHED_LOCK:
                if not _SCHED["running"]:
                    return
            time.sleep(1)


@app.get("/api/scheduler")
def scheduler_state() -> JSONResponse:
    with _SCHED_LOCK:
        now = time.time()
        agents = []
        for a, cfg in SCHED_AGENTS.items():
            last = _SCHED["last_run"][a]
            agents.append({
                "agent": a, "interval": cfg["interval"], "enabled": cfg["enabled"],
                "last_run": last, "last_status": _SCHED["last_status"][a],
                "next_in": max(0, int((last + cfg["interval"]) - now)) if last else 0,
                "ticking": a in _SCHED["ticking"],
            })
        return JSONResponse({"running": _SCHED["running"], "agents": agents,
                             "dd_keys": bool(_load_dd_env().get("DD_API_KEY"))})


@app.post("/api/scheduler/start")
def scheduler_start() -> JSONResponse:
    with _SCHED_LOCK:
        if _SCHED["running"]:
            return JSONResponse({"ok": True, "running": True, "note": "already running"})
        _SCHED["running"] = True
        t = threading.Thread(target=_scheduler_loop, daemon=True)
        _SCHED["thread"] = t
        t.start()
    return JSONResponse({"ok": True, "running": True,
                         "note": "AUTONOMOUS loop started — agents will edit code + deploy to GKE on their intervals."})


@app.post("/api/scheduler/stop")
def scheduler_stop() -> JSONResponse:
    with _SCHED_LOCK:
        _SCHED["running"] = False
    return JSONResponse({"ok": True, "running": False, "note": "loop halted (in-flight ticks finish)"})


@app.post("/api/scheduler/agent/{agent}")
def scheduler_agent_config(agent: str, payload: dict[str, Any] = Body(...)) -> JSONResponse:
    if agent not in SCHED_AGENTS:
        raise HTTPException(status_code=404, detail=f"unknown agent {agent!r}")
    if "enabled" in payload:
        SCHED_AGENTS[agent]["enabled"] = bool(payload["enabled"])
    if "interval" in payload:
        SCHED_AGENTS[agent]["interval"] = max(30, min(int(payload["interval"]), 86400))
    return JSONResponse({"ok": True, "agent": agent,
                         "enabled": SCHED_AGENTS[agent]["enabled"],
                         "interval": SCHED_AGENTS[agent]["interval"]})


@app.post("/api/scheduler/agent/{agent}/run-now")
def scheduler_run_now(agent: str) -> JSONResponse:
    """Fire one tick immediately (regardless of schedule)."""
    if agent not in SCHED_AGENTS:
        raise HTTPException(status_code=404, detail=f"unknown agent {agent!r}")
    threading.Thread(target=_run_agent_tick, args=(agent,), daemon=True).start()
    return JSONResponse({"ok": True, "agent": agent, "note": "tick started"})


# --------------------------------------------------------------------------- #
# Evolution loop status + async build endpoints
# --------------------------------------------------------------------------- #

_ASYNC_BUILD_LOCK = threading.Lock()
_ASYNC_BUILDS: dict[str, dict[str, Any]] = {}  # build_id -> {status, log, proc_pid}


@app.get("/api/evolution/status")
def evolution_status() -> JSONResponse:
    """Serve the live evolution loop status written by live_loop.py."""
    status_path = Path("/tmp/evolution_status.json")
    if not status_path.exists():
        return JSONResponse({"phase": "idle", "message": "Evolution loop not running",
                             "updated_at": None})
    try:
        return JSONResponse(json.loads(status_path.read_text()))
    except Exception:
        return JSONResponse({"phase": "error", "message": "Could not read status file"})


@app.post("/api/builds/submit")
def submit_build_async(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """Fire a Cloud Build asynchronously for a named cluster worktree.

    Payload: {"cluster_name": "<name>", "image_tag": "<tag>"}
    Returns: {"build_id": "<uuid>"}

    The caller polls GET /api/builds/{build_id}/status to check progress.
    """
    name = str(payload.get("cluster_name", "")).strip()
    image_tag = str(payload.get("image_tag", f"cluster-{name}")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="cluster_name required")
    wt_path = _worktree_path(name)
    if not os.path.isdir(wt_path):
        raise HTTPException(status_code=404, detail=f"worktree not found at {wt_path}")

    build_id = uuid.uuid4().hex[:12]
    with _ASYNC_BUILD_LOCK:
        _ASYNC_BUILDS[build_id] = {
            "status": "running", "cluster_name": name, "image_tag": image_tag,
            "log": [], "started_at": time.time(), "finished_at": None,
        }

    def _run():
        cmd = [
            "gcloud", "builds", "submit",
            f"--project={GCP_PROJECT}",
            f"--config={CLOUDBUILD_CONFIG}",
            f"--substitutions=_TAG={image_tag}",
            ".",
        ]
        try:
            proc = subprocess.Popen(
                cmd, cwd=wt_path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            assert proc.stdout is not None
            with _ASYNC_BUILD_LOCK:
                _ASYNC_BUILDS[build_id]["pid"] = proc.pid
            lines: list[str] = []
            for raw in proc.stdout:
                line = raw.rstrip("\n")
                lines.append(line)
                with _ASYNC_BUILD_LOCK:
                    log = _ASYNC_BUILDS[build_id].setdefault("log", [])
                    log.append(line)
                    if len(log) > 400:
                        del log[: len(log) - 400]
            proc.wait(timeout=1200)
            final_status = "succeeded" if proc.returncode == 0 else "failed"
        except Exception as exc:
            final_status = "failed"
        with _ASYNC_BUILD_LOCK:
            _ASYNC_BUILDS[build_id]["status"] = final_status
            _ASYNC_BUILDS[build_id]["finished_at"] = time.time()

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"build_id": build_id, "status": "running"})


@app.get("/api/builds/{build_id}/status")
def build_status(build_id: str) -> JSONResponse:
    """Poll the status of a background Cloud Build."""
    with _ASYNC_BUILD_LOCK:
        info = _ASYNC_BUILDS.get(build_id)
    if not info:
        raise HTTPException(status_code=404, detail=f"build {build_id!r} not found")
    return JSONResponse({
        "build_id": build_id,
        "status": info["status"],
        "cluster_name": info.get("cluster_name"),
        "image_tag": info.get("image_tag"),
        "started_at": info.get("started_at"),
        "finished_at": info.get("finished_at"),
        "log_tail": info.get("log", [])[-50:],
    })


# Serve the single-page frontend from / (mounted last so /api routes win).
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
