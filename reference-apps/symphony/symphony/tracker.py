"""Component 1: Tracker adapter.

Reads candidate work from Agent-B's Temper ``ImprovementIssue`` entity and writes
the PR url back. Per the contract, C1 CONSUMES this entity (does not own its
spec): it reads issues in state ``Implementing`` carrying ``BranchName``,
``TargetFile`` and ``Plan``, and calls the ``AttachPr`` action when a PR opens.

Two implementations:
  * ``FakeTracker``  — hardcoded issue dict; used for tests and the offline demo
                       while B's spec is not yet deployed.
  * ``TemperTracker`` — real HTTP client against the Temper OData data plane.

Both satisfy the ``Tracker`` protocol so the orchestrator does not care which.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional, Protocol

from .config import SymphonyConfig


@dataclass
class Issue:
    """The subset of ``ImprovementIssue`` fields C1 needs (contract names)."""

    id: str
    title: str
    status: str
    hypothesis: str = ""
    target_file: str = ""
    plan: str = ""
    acceptance_criteria: str = ""
    branch_name: str = ""
    pr_url: str = ""
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_odata(cls, row: dict) -> "Issue":
        """Build from an OData entity row. Tolerates Pascal/snake casing.

        The Temper list endpoint returns the entity-state envelope
        ``{entity_type, entity_id, status, fields: {...}}`` rather than a flat
        projection, so flatten ``fields`` and fall back to the envelope keys
        (``entity_id``/``status``) when the contract field is absent.
        """
        merged = dict(row)
        if isinstance(row.get("fields"), dict):
            for k, v in row["fields"].items():
                merged.setdefault(k, v)
        merged.setdefault("Id", row.get("entity_id"))
        merged.setdefault("Status", row.get("status"))

        def pick(*names: str) -> str:
            for n in names:
                if n in merged and merged[n] is not None:
                    return str(merged[n])
            return ""

        row = merged

        return cls(
            id=pick("Id", "id"),
            title=pick("Title", "title"),
            status=pick("Status", "status", "state"),
            hypothesis=pick("Hypothesis", "hypothesis"),
            target_file=pick("TargetFile", "target_file"),
            plan=pick("Plan", "plan"),
            acceptance_criteria=pick("AcceptanceCriteria", "acceptance_criteria"),
            branch_name=pick("BranchName", "branch_name"),
            pr_url=pick("PrUrl", "pr_url"),
            raw=row,
        )


class Tracker(Protocol):
    """Tracker seam: the orchestrator only needs these two operations."""

    def claim_issue(self) -> Optional[Issue]:
        """Return the next issue in state ``Implementing``, or None."""
        ...

    def attach_pr(self, issue: Issue, pr_url: str) -> dict:
        """Record the PR url on the issue (the ``AttachPr`` action)."""
        ...


# A safe no-op demo issue: it targets a code-comment-only change near the
# linger_ms constant in Helix's batcher, so worktree+commit+PR mechanics can be
# proven WITHOUT any risky behavioral change.
DEMO_ISSUE = Issue(
    id="DF-DEMO-1",
    title="Document the linger_ms batching trade-off in the batcher",
    status="Implementing",
    hypothesis=(
        "The 1ms default linger is intentional but under-documented; a reader "
        "scanning batcher.rs cannot tell why it is so low without git archaeology."
    ),
    target_file="helix-server/src/service/batcher.rs",
    plan=(
        "Add a short clarifying comment near the `linger_ms` field in "
        "`BatcherConfig` explaining the latency/throughput trade-off and the "
        "HELIX_BATCHER_LINGER_MS override. Do NOT change any value or behavior."
    ),
    acceptance_criteria=(
        "Only a comment is added; no constant value changes; `cargo check` would "
        "still compile; the diff touches only batcher.rs."
    ),
    branch_name="darkfactory/DF-DEMO-1",
)


class FakeTracker:
    """In-memory tracker backed by a hardcoded issue (default: ``DEMO_ISSUE``)."""

    def __init__(self, issue: Optional[Issue] = None) -> None:
        self._issue = issue or DEMO_ISSUE
        self._claimed = False
        self.attached: list[tuple[str, str]] = []

    def claim_issue(self) -> Optional[Issue]:
        if self._claimed:
            return None
        self._claimed = True
        return self._issue

    def attach_pr(self, issue: Issue, pr_url: str) -> dict:
        issue.pr_url = pr_url
        self.attached.append((issue.id, pr_url))
        return {"ok": True, "issue_id": issue.id, "pr_url": pr_url, "tracker": "fake"}


class TemperTracker:
    """HTTP client against the Temper OData data plane (``/tdata``).

    Reads ``ImprovementIssues`` filtered to ``Status eq 'Implementing'`` and
    fires the ``AttachPr`` bound action. Tenant flows via ``X-Tenant-Id``.

    If B's spec is not deployed yet the entity set returns 404; ``claim_issue``
    treats that as "no work" (returns None) and surfaces a clear message rather
    than crashing — this is the contract's "C1 not blocked by B" path.
    """

    def __init__(self, config: SymphonyConfig) -> None:
        self.config = config
        self.base = config.temper_url.rstrip("/")
        self.last_error: Optional[str] = None
        # Bearer token for the credential-resolved identity (ADR-0033). Symphony
        # presents the verified `symphony` agent credential; the server resolves
        # it to agent_type=symphony and Cedar gates StartWork/AttachPr to the
        # issue's assignee. Sourced from SYMPHONY_TEMPER_TOKEN or TEMPER_API_KEY.
        import os as _os
        self.token = _os.environ.get("SYMPHONY_TEMPER_TOKEN") or _os.environ.get("TEMPER_API_KEY")

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> tuple[int, dict]:
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-Tenant-Id", self.config.tenant)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode() if e.fp else ""
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {"error": raw}
            return e.code, payload
        except urllib.error.URLError as e:
            self.last_error = f"cannot reach Temper at {url}: {e.reason}"
            return 0, {"error": self.last_error}

    def claim_issue(self) -> Optional[Issue]:
        # OData $filter on the Status field per the contract.
        flt = urllib.parse.quote("Status eq 'Implementing'")
        status, payload = self._request("GET", f"/tdata/{self.config.entity_set}?$filter={flt}")
        if status == 0:
            print(f"[tracker] {self.last_error}")
            return None
        if status == 404:
            self.last_error = (
                f"entity set '{self.config.entity_set}' not found for tenant "
                f"'{self.config.tenant}' — Agent-B's ImprovementIssue spec is not "
                f"deployed yet. Use --tracker fake to run the demo."
            )
            print(f"[tracker] {self.last_error}")
            return None
        if status >= 400:
            self.last_error = f"HTTP {status} listing issues: {payload}"
            print(f"[tracker] {self.last_error}")
            return None

        rows = payload.get("value", payload if isinstance(payload, list) else [])
        if not rows:
            return None
        # Skip speciation tickets: those are infra-placement issues (create cluster
        # + migrate queues) handled by the speciation EXECUTOR, not by Symphony's
        # code-implementer (which can only Edit/Read/Write + open PRs). They are
        # marked with a TargetFile of "speciation://<cluster>".
        for row in rows:
            issue = Issue.from_odata(row)
            # Skip infra/optimizer tickets handled by dedicated executors, not by
            # Symphony's generic code-implementer: speciation:// (create cluster +
            # migrate) and opt:// (optimizer: edit + verify + deploy + post-verify).
            tf = issue.target_file or ""
            if tf.startswith("speciation://") or tf.startswith("opt://") or tf.startswith("meta://"):
                continue
            if not issue.branch_name:
                issue.branch_name = self.config.branch_for(issue.id)
            return issue
        return None

    def attach_pr(self, issue: Issue, pr_url: str) -> dict:
        # OData bound action: POST /tdata/Set('id')/Namespace.AttachPr
        key = urllib.parse.quote(issue.id)
        action = f"{self.config.odata_namespace}.AttachPr"
        path = f"/tdata/{self.config.entity_set}('{key}')/{action}"
        status, payload = self._request("POST", path, {"pr_url": pr_url})

        # Surface Cedar denials per the SKILL contract — do NOT self-approve.
        if isinstance(payload, dict) and payload.get("status") == "authorization_denied":
            decision_id = payload.get("decision_id")
            print(
                f"[tracker] AttachPr denied by Cedar. Decision {decision_id} pending. "
                f"Approve in the Observe UI; the PR url is {pr_url}."
            )
            return {"ok": False, "authorization_denied": True, "decision_id": decision_id}

        if status >= 400 or status == 0:
            print(f"[tracker] AttachPr failed (HTTP {status}): {payload}")
            return {"ok": False, "status": status, "payload": payload}

        issue.pr_url = pr_url
        return {"ok": True, "issue_id": issue.id, "pr_url": pr_url, "tracker": "temper", "payload": payload}


def make_tracker(config: SymphonyConfig) -> Tracker:
    """Select the tracker implementation from config (``temper`` | ``fake``)."""
    if config.tracker_name == "fake":
        return FakeTracker()
    if config.tracker_name == "temper":
        return TemperTracker(config)
    raise ValueError(f"unknown tracker '{config.tracker_name}' (expected 'temper' or 'fake')")
