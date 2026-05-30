"""HTTP client for the live evolution loop.

Wraps:
  - Temper OData API (clusters, producers, consumers, breeds, fitness goals)
  - deploy-history UI API (cluster lifecycle, build submission)
  - git operations on the helix worktrees

All network calls raise LiveClientError on failure so callers can cleanly
handle transient vs. fatal conditions.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import LiveEvolutionConfig


class LiveClientError(RuntimeError):
    """A live-client operation failed (network, API, subprocess)."""


# ---------------------------------------------------------------------------
# Temper OData thin client
# ---------------------------------------------------------------------------

@dataclass
class TemperClient:
    token: str
    base_url: str
    tenant: str = "dark-factory"
    timeout: float = 30.0

    def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "X-Tenant-Id": self.tenant,
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        if data:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            raise LiveClientError(f"HTTP {exc.code} {method} {path}: {raw[:400]}") from exc
        except urllib.error.URLError as exc:
            raise LiveClientError(f"Cannot reach Temper at {url}: {exc.reason}") from exc

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, body: dict) -> Any:
        return self._request("POST", path, body)

    def list(self, entity_set: str, odata_filter: str | None = None) -> list[dict]:
        path = f"/tdata/{entity_set}"
        if odata_filter:
            # Don't put space in safe — http.client rejects raw spaces in URLs.
            # quote() will turn each ' ' into '%20', which OData accepts.
            encoded = urllib.parse.quote(odata_filter, safe="='()")
            path = f"{path}?$filter={encoded}"
        result = self._request("GET", path)
        if isinstance(result, dict):
            return result.get("value", [])
        return result or []

    def create(self, entity_set: str, body: dict) -> dict:
        return self._request("POST", f"/tdata/{entity_set}", body) or {}

    def action(self, entity_set: str, entity_id: str, action: str, params: dict) -> Any:
        key = urllib.parse.quote(entity_id, safe="")
        return self._request("POST", f"/tdata/{entity_set}('{key}')/Default.{action}", params)


# ---------------------------------------------------------------------------
# deploy-history UI API thin client
# ---------------------------------------------------------------------------

@dataclass
class UIClient:
    base_url: str
    timeout: float = 30.0

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json"}
        if data:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            return exc.code, {"error": raw[:400]}
        except urllib.error.URLError as exc:
            raise LiveClientError(f"Cannot reach UI at {url}: {exc.reason}") from exc

    def get(self, path: str) -> tuple[int, dict]:
        return self._request("GET", path)

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        return self._request("POST", path, body)


# ---------------------------------------------------------------------------
# Main LiveClient
# ---------------------------------------------------------------------------

@dataclass
class LiveClient:
    """Orchestrates cluster lifecycle and workload cloning for the live loop."""

    config: LiveEvolutionConfig
    _breeder: TemperClient = field(init=False)
    _supervisor: TemperClient = field(init=False)
    _ui: UIClient = field(init=False)

    def __post_init__(self):
        tokens = self.config.load_tokens()
        self._breeder = TemperClient(
            token=tokens["breeder"]["token"],
            base_url=self.config.temper_base_url,
            tenant=self.config.tenant,
        )
        self._supervisor = TemperClient(
            token=tokens["breeder-supervisor"]["token"],
            base_url=self.config.temper_base_url,
            tenant=self.config.tenant,
        )
        self._ui = UIClient(base_url=self.config.ui_base_url)

    # --- Cluster lifecycle --------------------------------------------------

    def create_cluster(self, name: str, base: str, replicas: int = 1) -> str:
        """Create a Temper Cluster entity and trigger background provisioning.

        `base` may be a branch name or a commit SHA — git handles both.
        Returns the cluster entity ID (cid). Provisioning runs asynchronously;
        poll wait_cluster_live() until status is Live.
        """
        status, body = self._ui.post("/api/clusters", {
            "name": name,
            "base": base,
            "replicas": str(replicas),
        })
        if status not in (200, 201):
            raise LiveClientError(f"create_cluster {name}: HTTP {status} — {body}")
        # The response is the Cluster entity; approve + apply it.
        cid = body.get("id") or body.get("Id") or body.get("fields", {}).get("Id", "")
        if not cid:
            # Some versions return entity fields directly.
            cid = body.get("ClusterId") or name
        # Approve + Apply to kick off provisioning.
        self._ui.post(f"/api/clusters/{cid}/approve", {})
        return cid

    def wait_cluster_live(
        self, cid: str, timeout_minutes: int = 20, poll_interval_s: int = 15
    ) -> bool:
        """Poll until the cluster is Live or Failed. Returns True if Live."""
        deadline = time.monotonic() + timeout_minutes * 60
        while time.monotonic() < deadline:
            _, clusters = self._ui.get("/api/clusters")
            if isinstance(clusters, dict):
                clusters = clusters.get("clusters", [])
            for c in (clusters or []):
                if c.get("id") == cid or c.get("Id") == cid:
                    st = (c.get("status") or c.get("Status") or "").lower()
                    if st == "live":
                        return True
                    if st in ("failed", "torndown"):
                        return False
            time.sleep(poll_interval_s)
        return False

    def wait_all_clusters_live(
        self, cids: list[str], timeout_minutes: int = 20
    ) -> dict[str, bool]:
        """Poll all clusters simultaneously until Live/Failed or timeout."""
        deadline = time.monotonic() + timeout_minutes * 60
        results: dict[str, bool] = {}
        while time.monotonic() < deadline and len(results) < len(cids):
            _, clusters = self._ui.get("/api/clusters")
            if isinstance(clusters, dict):
                clusters = clusters.get("clusters", [])
            for c in (clusters or []):
                cid = c.get("id") or c.get("Id", "")
                if cid in cids and cid not in results:
                    st = (c.get("status") or c.get("Status") or "").lower()
                    if st == "live":
                        results[cid] = True
                    elif st in ("failed", "torndown"):
                        results[cid] = False
            if len(results) < len(cids):
                time.sleep(15)
        # Anything not resolved = timeout = failed
        for cid in cids:
            if cid not in results:
                results[cid] = False
        return results

    def teardown_cluster(self, cid: str) -> bool:
        """Teardown a variant cluster. Best-effort. Logs success/failure so
        operators can see in the smoke-test output what actually happened.

        The UI cascade stops+deletes all workloads on the cluster, deletes the
        helix-<name> StatefulSet/Services, prunes the worktree, and lands the
        governed Teardown transition.
        """
        status, body = self._ui.post(f"/api/clusters/{cid}/teardown", {})
        if status in (200, 204):
            name = (body.get("name") if isinstance(body, dict) else "") or cid
            print(f"  [teardown] cluster {name} ({cid}): torn down")
            return True
        err = body.get("error") or body.get("detail") or body \
            if isinstance(body, dict) else body
        print(f"  [teardown] warning: cluster {cid}: HTTP {status} {err}")
        return False

    def teardown_all(self, cids: list[str]) -> None:
        """Teardown all clusters. Errors are logged, not raised."""
        for cid in cids:
            try:
                self.teardown_cluster(cid)
            except LiveClientError as exc:
                print(f"  [teardown] warning: cluster {cid}: {exc}")

    def list_clusters(self) -> list[dict]:
        """Return all Cluster entities from Temper."""
        return self._breeder.list("Clusters")

    # --- Workload cloning ---------------------------------------------------

    def clone_workload_to_variant(
        self, source_cluster: str, target_cluster: str
    ) -> list[dict]:
        """Clone all running Producers and Consumers from source to target cluster.

        Goes through the deploy-history UI (/api/workloads/producer and
        /api/workloads/consumer) which handles the operator->create + supervisor->
        Define/Start IOA flow and deploys the pods. Returns a list of dicts with
        {kind, id} for teardown. Validates queue prefix invariant before returning.
        """
        cloned: list[dict] = []
        prefix = self.config.queue_prefix_invariant

        def _field(entity: dict, *names: str) -> str:
            """Read a field from a Temper OData entity.

            Temper wraps payload data in a nested `fields` dict:
                {"entity_id": ..., "status": ..., "fields": {"Queue": ..., ...}}
            Top-level is checked first (status/entity_id live there), then `fields`
            with case-insensitive matching.
            """
            for n in names:
                v = entity.get(n)
                if v not in (None, ""):
                    return str(v)
            nested = entity.get("fields") or {}
            if not isinstance(nested, dict):
                return ""
            lowered = {k.lower(): v for k, v in nested.items()}
            for n in names:
                v = nested.get(n) or lowered.get(n.lower())
                if v not in (None, ""):
                    return str(v)
            return ""

        def _filter_running_on(entities: list[dict], cluster: str) -> list[dict]:
            return [
                e for e in entities
                if _field(e, "Cluster", "cluster").lower() == cluster.lower()
                and _field(e, "Status", "status").lower() == "running"
            ]

        # Query running producers/consumers on source cluster. Trust the server
        # filter when it succeeds; only fall back to client-side filtering on the
        # full list if the filtered request errors out.
        try:
            producers = self._supervisor.list(
                "Producers", f"Cluster eq '{source_cluster}' and Status eq 'Running'"
            )
        except LiveClientError:
            producers = _filter_running_on(self._supervisor.list("Producers"), source_cluster)

        try:
            consumers = self._supervisor.list(
                "Consumers", f"Cluster eq '{source_cluster}' and Status eq 'Running'"
            )
        except LiveClientError:
            consumers = _filter_running_on(self._supervisor.list("Consumers"), source_cluster)

        if not producers and not consumers:
            print(f"  [clone] no running workload on {source_cluster} — skipping clone")
            return []

        def _post_ui_workload(kind: str, body: dict, queue: str) -> str | None:
            """POST to /api/workloads/{kind}; return new entity id or None on failure.

            The UI endpoint implements the full operator->create + supervisor->Define
            + supervisor->Start lifecycle with the right PascalCase/lowercase shapes,
            then deploys the pod via kubectl. That dodges the AuthorizationDenied
            errors we hit when calling /tdata/Default.Define ourselves.
            """
            status, resp = self._ui.post(f"/api/workloads/{kind}", body)
            if status not in (200, 201):
                err = resp.get("error") or resp.get("detail") or resp
                print(f"  [clone] warning: could not clone {kind} (queue={queue}): "
                      f"HTTP {status} {err}")
                return None
            return resp.get("id")

        # Clone producers.
        for p in producers:
            queue = _field(p, "Queue", "queue")
            if queue and not queue.startswith(prefix):
                print(f"  [clone] skipping producer queue {queue!r} (not {prefix}* prefix)")
                continue
            body = {
                "queue": queue,
                "rate_per_sec": int(_field(p, "RatePerSec", "rate_per_sec") or "100"),
                "msg_size": int(_field(p, "MsgSize", "msg_size") or "128"),
                "concurrency": int(_field(p, "Concurrency", "concurrency") or "1"),
                "cluster": target_cluster,
                "shape": _field(p, "Shape", "shape") or "stable",
                "shape_params": _field(p, "ShapeParams", "shape_params") or "{}",
            }
            new_id = _post_ui_workload("producer", body, queue)
            if new_id:
                cloned.append({"kind": "producer", "id": new_id})
                print(f"  [clone] producer → {target_cluster} queue={queue} (id={new_id})")

        # Clone consumers.
        for c in consumers:
            queue = _field(c, "Queue", "queue")
            if queue and not queue.startswith(prefix):
                print(f"  [clone] skipping consumer queue {queue!r} (not {prefix}* prefix)")
                continue
            body = {
                "queue": queue,
                "process_ms": int(_field(c, "ProcessMs", "process_ms") or "10"),
                "concurrency": int(_field(c, "Concurrency", "concurrency") or "1"),
                "cluster": target_cluster,
                "shape": _field(c, "Shape", "shape") or "stable",
                "shape_params": _field(c, "ShapeParams", "shape_params") or "{}",
                "start_offset": _field(c, "StartOffset", "start_offset") or "end",
            }
            new_id = _post_ui_workload("consumer", body, queue)
            if new_id:
                cloned.append({"kind": "consumer", "id": new_id})
                print(f"  [clone] consumer → {target_cluster} queue={queue} (id={new_id})")

        # Validate queue prefix invariant on the target cluster.
        self._validate_queue_prefix(target_cluster)
        return cloned

    def teardown_cloned_workload(self, cloned: list[dict]) -> None:
        """Stop all cloned Producer/Consumer entities via the UI."""
        for item in cloned:
            # Backwards-compat: older state used {"entity_set": "Producers", "id": ...}
            kind = item.get("kind")
            if not kind:
                es = item.get("entity_set", "")
                kind = "producer" if es == "Producers" else (
                    "consumer" if es == "Consumers" else ""
                )
            eid = item.get("id", "")
            if not kind or not eid:
                continue
            status, resp = self._ui.post(f"/api/workloads/{kind}/{eid}/stop", {})
            if status in (200, 204):
                print(f"  [teardown-clone] stopped {kind}/{eid}")
            else:
                err = resp.get("error") or resp.get("detail") or resp
                print(f"  [teardown-clone] warning: {kind}/{eid}: HTTP {status} {err}")

    def _validate_queue_prefix(self, cluster: str) -> None:
        """Assert all queues on the cluster follow the naming invariant."""
        prefix = self.config.queue_prefix_invariant
        try:
            queues = self._supervisor.list("Queues")
            bad = [
                q.get("Name") or q.get("name", "?")
                for q in queues
                if (q.get("cluster") or q.get("Cluster") or "").lower() == cluster.lower()
                and not (q.get("Name") or q.get("name", "")).startswith(prefix)
            ]
            if bad:
                raise LiveClientError(
                    f"Queue prefix invariant violated on {cluster}: "
                    f"{bad} do not start with '{prefix}'"
                )
        except LiveClientError as exc:
            if "prefix invariant" in str(exc):
                raise
            # If listing fails, log a warning rather than aborting.
            print(f"  [validate-queues] warning: could not validate — {exc}")

    # --- Breed recording ----------------------------------------------------

    def record_breed(
        self,
        breed_id: str,
        cluster_name: str,
        change_description: str,
        perf_delta_pct: float,
        survived: bool,
        goal_id: str,
        stage_num: int,
        variant_num: int,
        motivation: str = "",
        dst_passed: bool = True,
        cull_reason: str = "",
        issue_id: str = "",
    ) -> None:
        """Record the variant outcome into the FitnessGoal generation log AND
        materialize a governed Breed entity so the UI's Breeds tab shows it.

        The FitnessGoal RecordGeneration call is the durable record used for
        stage recovery on restart (this is what startup_resume reads).

        The Breed entity is walked through its IOA lifecycle:
          - DST-culled (no cluster):       Proposed -> Cull
          - cluster never went live:       Proposed -> StartBuild -> Cull
          - survived (passed fitness):     Proposed -> StartBuild -> RecordBuild
                                                    -> RecordResult -> MarkPromoted
          - measured but failed fitness:   Proposed -> StartBuild -> RecordBuild
                                                    -> RecordResult -> MarkCulled
        Every step is best-effort; a failure on any one is logged but does not
        block subsequent variants. The breeder token is permitted by breed.cedar
        for create + every lifecycle action.
        """
        outcome = "survived" if survived else "culled"
        try:
            self._breeder.action("FitnessGoals", goal_id, "RecordGeneration", {
                "generation": str(stage_num * 100 + variant_num),
                "gene": change_description[:200],
                "fitness": str(round(perf_delta_pct, 2)),
                "outcome": outcome,
            })
        except LiveClientError as exc:
            print(f"  [temper] warning: could not record generation {breed_id}: {exc}")

        # The cluster_id field on the Breed groups it in the UI. When the variant
        # never produced a cluster (DST-cull), we bucket it under the lineage tag
        # (e.g. "ev-lat-s1-orphans") rather than the magic "none" string so the
        # operator can still find it.
        cluster_id = cluster_name if cluster_name and cluster_name != "none" else \
            f"ev-s{stage_num}-orphans"
        gene = (change_description or "(no description)")[:200]
        motivation_str = (motivation or change_description or "")[:500]

        def _act(action: str, params: dict, label: str) -> bool:
            try:
                self._breeder.action("Breeds", breed_id, action, params)
                return True
            except LiveClientError as exc:
                print(f"  [temper] warning: breed {breed_id} {label}: {exc}")
                return False

        # 1) Create the entity in Proposed state. The Cedar `create` permit is
        #    keyed on agent_type==breeder/supervisor/human so our breeder token
        #    is allowed.
        try:
            self._breeder.create("Breeds", {
                "id": breed_id,
                "Status": "Proposed",
                "ClusterId": cluster_id,
                "Niche": cluster_id,
                "FitnessGoalId": goal_id,
                "IssueId": issue_id,
                "Gene": gene,
                "Motivation": motivation_str,
                "Namespace": self.config.namespace,
            })
        except LiveClientError as exc:
            print(f"  [temper] warning: could not create breed {breed_id}: {exc}")
            return

        # 2) Propose — sets the same fields via the IOA so they're recorded as
        #    governed transition params (Cedar requires Propose, not just create).
        _act("Propose", {
            "cluster_id": cluster_id,
            "niche": cluster_id,
            "fitness_goal_id": goal_id,
            "issue_id": issue_id,
            "gene": gene,
            "motivation": motivation_str,
        }, "Propose")

        # 3) DST-culled variants never built — cull straight from Proposed.
        if not dst_passed:
            reason = cull_reason or "DST durability check failed"
            _act("Cull", {"cull_reason": reason[:200]}, "Cull (DST)")
            return

        # 4) Move into Building -> Verifying.
        if not _act("StartBuild", {}, "StartBuild"):
            return
        # Variants whose cluster never went live: record the failed build and Cull.
        if not cluster_name or cluster_name == "none":
            _act("Cull", {"cull_reason": (cull_reason or "cluster did not go live")[:200]},
                 "Cull (no cluster)")
            return
        _act("RecordBuild", {"build_result": "passed"}, "RecordBuild")

        # 5) Record the measured result.
        delta_str = f"{perf_delta_pct:+.1f}%"
        _act("RecordResult", {
            "perf_delta": delta_str,
            "ci_status": "passed" if survived else "failed",
        }, "RecordResult")

        # 6) Terminal state — promoted or culled.
        if survived:
            _act("MarkPromoted", {}, "MarkPromoted")
        elif cull_reason:
            _act("Cull", {"cull_reason": cull_reason[:200]}, "Cull (fitness)")
        else:
            _act("MarkCulled", {}, "MarkCulled")

    def retire_stage_breeds(self, stage_num: int) -> None:
        """Mark all non-Retired Breeds from this stage as Retired."""
        try:
            breeds = self._breeder.list("Breeds")
            for b in breeds:
                bid = b.get("id") or b.get("Id", "")
                status = (b.get("status") or b.get("Status") or "").lower()
                if bid.startswith(f"breed-s{stage_num}-") and status != "retired":
                    try:
                        self._breeder.action("Breeds", bid, "Retire", {"reason": "stage_complete"})
                    except LiveClientError:
                        pass
        except LiveClientError as exc:
            print(f"  [temper] warning: could not retire breeds: {exc}")

    # --- FitnessGoal queries ------------------------------------------------

    def get_generation_records(self, goal_id: str) -> list[dict]:
        """Fetch all RecordGeneration records for a FitnessGoal (for meta-LLM)."""
        try:
            result = self._breeder.get(f"/tdata/FitnessGoals('{goal_id}')")
            return result.get("generations", []) if isinstance(result, dict) else []
        except LiveClientError:
            return []

    # --- Git operations on helix repo ---------------------------------------

    def git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        repo = cwd or self.config.helix_repo
        return subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True,
            timeout=120, check=False,
        )

    def ensure_evolve_branch(self, lineage: str) -> None:
        """Create evolve/<lineage> branch off champion-metrics-pranav-clone if it doesn't exist."""
        branch = f"evolve/{lineage}"
        r = self.git("rev-parse", "--verify", branch)
        if r.returncode != 0:
            r = self.git("checkout", "-b", branch, self.config.champion_branch)
            if r.returncode != 0:
                raise LiveClientError(
                    f"Could not create branch {branch}: {r.stderr}"
                )
            print(f"  [git] created branch {branch} off {self.config.champion_branch}")
        else:
            print(f"  [git] branch {branch} already exists")

    def create_proposal_worktree(self, stage_num: int, variant_num: int, lineage: str) -> Path:
        """Create a temp worktree for Claude Code to apply a mutation into."""
        wt_name = f"proposal-s{stage_num}-v{variant_num}"
        wt_path = self.config.evolution_worktrees / wt_name
        # Clean up if exists from a prior failed run.
        if wt_path.exists():
            self.git("worktree", "remove", "--force", str(wt_path))
        branch = f"evolve/{lineage}"
        r = self.git("worktree", "add", "-B",
                     f"ev-mut/{wt_name}", str(wt_path), branch)
        if r.returncode != 0:
            raise LiveClientError(f"worktree add failed: {r.stderr}")
        print(f"  [git] worktree {wt_path} created off {branch}")
        return wt_path

    def create_named_proposal_worktree(self, wt_name: str, lineage: str) -> Path:
        """Create a worktree with an explicit name (used for refinement rounds)."""
        wt_path = self.config.evolution_worktrees / wt_name
        if wt_path.exists():
            self.git("worktree", "remove", "--force", str(wt_path))
        branch = f"evolve/{lineage}"
        r = self.git("worktree", "add", "-B",
                     f"ev-mut/{wt_name}", str(wt_path), branch)
        if r.returncode != 0:
            raise LiveClientError(f"worktree add failed: {r.stderr}")
        print(f"  [git] worktree {wt_path} created off {branch}")
        return wt_path

    def remove_proposal_worktree(self, stage_num: int, variant_num: int) -> None:
        wt_name = f"proposal-s{stage_num}-v{variant_num}"
        wt_path = self.config.evolution_worktrees / wt_name
        self.git("worktree", "remove", "--force", str(wt_path))

    def remove_named_worktree(self, wt_name: str) -> None:
        wt_path = self.config.evolution_worktrees / wt_name
        if wt_path.exists():
            self.git("worktree", "remove", "--force", str(wt_path))

    def commit_mutation(
        self, wt_path: Path, lineage: str, stage_num: int, variant_num: int,
        change_description: str
    ) -> str:
        """Stage all changes in worktree and commit. Returns the commit SHA."""
        self.git("add", "-A", cwd=wt_path)
        msg = (
            f"evolve({lineage} s{stage_num}.v{variant_num}): {change_description}\n\n"
            f"Live evolution loop (ADR-0095 live extension)."
        )
        r = subprocess.run(
            ["git", "commit", "--no-verify", "-m", msg],
            cwd=wt_path, capture_output=True, text=True, timeout=30, check=False,
        )
        if r.returncode != 0:
            raise LiveClientError(f"git commit failed: {r.stderr}")
        sha_r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=wt_path, capture_output=True, text=True, timeout=10, check=False,
        )
        return sha_r.stdout.strip()

    def revert_worktree(self, wt_path: Path) -> None:
        """Discard uncommitted changes in the worktree (after DST failure)."""
        subprocess.run(
            ["git", "checkout", "--", "."],
            cwd=wt_path, capture_output=True, text=True, timeout=30, check=False,
        )

    def promote_champion(
        self, lineage: str, stage_num: int, change_description: str,
        perf_delta_pct: float, wt_path: Path
    ) -> bool:
        """Merge the winning variant's commit into champion-metrics-pranav-clone
        and push.

        The variant's commit lives on the worktree's branch (`ev-mut/proposal-
        s{S}-v{V}`), NOT on `evolve/<lineage>` — `git worktree add -B` resets
        the worktree to a new branch starting at evolve/<lineage>, and the
        coding agent's commit lands on the new branch, leaving evolve/<lineage>
        un-advanced. Earlier versions of this method merged `evolve/<lineage>`
        directly, which silently became a no-op merge and left champion without
        the variant's code change (only the spec-updater's EVOLUTION_GUIDE.md
        bump made it into the branch). We now:

          1) read HEAD from the worktree to get the variant SHA
          2) fast-forward `evolve/<lineage>` to that SHA so the lineage branch
             reflects the new champion (and the diff endpoint can use it)
          3) merge `evolve/<lineage>` into the champion branch
          4) assert that the variant SHA is reachable from the champion HEAD
             before returning success — so the spec-updater never runs on a
             no-op promotion

        Returns True only if all four steps succeed.
        """
        # 1) Resolve the winning variant SHA from the worktree.
        sha_r = self.git("rev-parse", "HEAD", cwd=wt_path)
        if sha_r.returncode != 0 or not sha_r.stdout.strip():
            print(f"  [git] could not resolve variant SHA from {wt_path}: {sha_r.stderr}")
            return False
        winning_sha = sha_r.stdout.strip()
        print(f"  [git] promoting variant SHA {winning_sha[:10]} from {wt_path.name}")

        branch = f"evolve/{lineage}"
        commit_msg = (
            f"champion({lineage} stage {stage_num}): {change_description} "
            f"({perf_delta_pct:+.1f}%)"
        )

        # 2) Fast-forward the lineage branch to the winning variant SHA.
        # `branch -f` works whether or not the lineage branch is currently
        # checked out in another worktree because no working tree is touched.
        r = self.git("branch", "-f", branch, winning_sha)
        if r.returncode != 0:
            print(f"  [git] could not advance {branch} to {winning_sha[:10]}: {r.stderr}")
            return False

        # 3) Merge the (now-advanced) lineage branch into champion.
        r = self.git("checkout", self.config.champion_branch)
        if r.returncode != 0:
            print(f"  [git] could not checkout {self.config.champion_branch}: {r.stderr}")
            return False
        r = self.git("merge", "--no-ff", branch, "-m", commit_msg)
        if r.returncode != 0:
            print(f"  [git] merge failed: {r.stderr}")
            self.git("merge", "--abort")
            return False

        # 4) Assert the variant SHA is now in champion's history. If `git merge`
        # was a no-op for any reason, this guard catches it before we update
        # the spec/deploy a phantom champion.
        check = self.git("merge-base", "--is-ancestor", winning_sha, self.config.champion_branch)
        if check.returncode != 0:
            print(f"  [git] PROMOTION FAILED: variant SHA {winning_sha[:10]} is NOT "
                  f"an ancestor of {self.config.champion_branch} after merge — aborting.")
            return False

        r = self.git("push", self.config.champion_remote, self.config.champion_branch)
        if r.returncode != 0:
            print(f"  [git] push failed (non-fatal, local merge done): {r.stderr}")
        print(f"  [git] promoted {branch} → {self.config.champion_branch}: {commit_msg}")
        return True

    # --- Champion auto-redeploy --------------------------------------------

    def redeploy_champion_cluster(self, stage_num: int, image_tag: str | None = None) -> bool:
        """Rebuild the helix image from the current champion branch HEAD and roll
        the baseline k8s StatefulSet (`helix-<champion_cluster_name>`) forward.

        Two phases:
          1) Cloud Build  : `gcloud builds submit --config=...` from the helix
                            repo (~8-10 min)
          2) Rolling deploy: `kubectl -n <ns> set image statefulset/helix-<name>
                              helix=<image>` then `rollout status` (~3 min)

        Returns True only if BOTH succeed. The new image keeps emitting the
        same `cluster:helix-<name>` DD tag, so the baseline tag in config does
        not need to change.
        """
        helix_repo = str(self.config.helix_repo)
        # Determine the image tag from the current champion SHA if not provided.
        if not image_tag:
            r = self.git("rev-parse", "--short", "HEAD")
            sha = r.stdout.strip() if r.returncode == 0 else "head"
            image_tag = f"champion-s{stage_num}-{sha}"
        full_image = f"{self.config.image_repo}:{image_tag}"
        ss = f"helix-{self.config.champion_cluster_name}"

        # 1) Cloud Build
        print(f"  [redeploy] Cloud Build {image_tag} from {helix_repo} (~8-10 min)…")
        cmd = [
            "gcloud", "builds", "submit",
            f"--project={self.config.gcp_project}",
            f"--config={self.config.cloudbuild_config}",
            f"--substitutions=_TAG={image_tag}",
            ".",
        ]
        try:
            p = subprocess.run(cmd, cwd=helix_repo, capture_output=True,
                               text=True, timeout=1500)
        except (subprocess.SubprocessError, OSError) as exc:
            print(f"  [redeploy] cloud build error: {exc}")
            return False
        if p.returncode != 0:
            print(f"  [redeploy] cloud build failed:\n{(p.stdout+p.stderr)[-1500:]}")
            return False
        print(f"  [redeploy] image pushed: {full_image}")

        # 2) Rolling deploy on the baseline StatefulSet.
        # Pin --context so we don't deploy against whatever cluster the
        # operator's shell happens to be on (e.g. gizmo staging).
        ctx_args = ["--context", self.config.kube_context]
        print(f"  [redeploy] kubectl --context {self.config.kube_context} "
              f"-n {self.config.namespace} set image statefulset/{ss} helix={full_image}")
        try:
            p1 = subprocess.run(
                ["kubectl", *ctx_args, "-n", self.config.namespace,
                 "set", "image", f"statefulset/{ss}",
                 f"helix={full_image}"],
                capture_output=True, text=True, timeout=60, check=False,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            print(f"  [redeploy] kubectl set image error: {exc}")
            return False
        if p1.returncode != 0:
            print(f"  [redeploy] kubectl set image failed: {p1.stdout+p1.stderr}")
            return False

        print(f"  [redeploy] waiting for rollout (up to 5 min)…")
        try:
            p2 = subprocess.run(
                ["kubectl", *ctx_args, "-n", self.config.namespace,
                 "rollout", "status", f"statefulset/{ss}", "--timeout=300s"],
                capture_output=True, text=True, timeout=320, check=False,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            print(f"  [redeploy] rollout status error: {exc}")
            return False
        if p2.returncode != 0:
            print(f"  [redeploy] rollout did not complete: {p2.stdout+p2.stderr}")
            return False
        print(f"  [redeploy] champion {ss} now on {image_tag}")
        return True
