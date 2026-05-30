"""DRIVE phase: a thin HTTP client for the Temper server.

This speaks the same HTTP API the Temper MCP ``execute`` sandbox uses under the
hood (see ``crates/temper-sandbox/src/dispatch.rs`` and ``http.rs``):

* create:        POST /tdata/{Set}                         (body = fields)
* action:        POST /tdata/{Set}('{id}')/Temper.{Action} (body = params)
* get:           GET  /tdata/{Set}('{id}')
* list:          GET  /tdata/{Set}[?$filter=...]
* specs:         GET  /observe/specs   and  /observe/specs/{entity}
* decisions:     GET  /api/tenants/{tenant}/decisions[?status=...]
* submit_specs:  POST /api/specs/load-inline

Identity is conveyed with ``X-Tenant-Id`` plus the local-dev pass-through headers
``X-Temper-Principal-Id`` / ``X-Temper-Principal-Kind`` / ``X-Temper-Agent-Type``
(matching ``AgentIdentity`` in the sandbox), or an ``Authorization: Bearer`` token
when one is configured.

A Cedar denial is a 403 with ``error.code == "AuthorizationDenied"`` and a
``PD-<uuid>`` decision id embedded in the message. We surface it (never
self-approve) and poll until a human resolves it.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class TemperUnavailable(RuntimeError):
    """The Temper server is unreachable, or the required spec is not deployed."""


@dataclass
class TemperDenied(RuntimeError):
    """A Cedar policy denied the action. A human must approve via the Observe UI."""

    decision_id: str | None
    message: str

    def __str__(self) -> str:  # noqa: D401
        return f"Cedar denied (decision={self.decision_id}): {self.message}"


_PD_RE = re.compile(r"PD-[0-9a-fA-F-]+")


@dataclass
class TemperClient:
    """Minimal Temper HTTP client scoped to one tenant."""

    base_url: str = "http://127.0.0.1:3000"
    tenant: str = "dark-factory"
    principal_id: str = "agent-c2-observer"
    principal_kind: str = "agent"
    agent_type: str = "claude-code"
    session_id: str | None = None
    api_key: str | None = None
    timeout: float = 30.0

    # --- low-level request ---
    def _headers(self, principal_kind: str | None = None) -> dict[str, str]:
        headers = {
            "X-Tenant-Id": self.tenant,
            "Accept": "application/json",
        }
        if self.principal_id:
            headers["X-Temper-Principal-Id"] = self.principal_id
        kind = principal_kind or self.principal_kind
        if kind:
            headers["X-Temper-Principal-Kind"] = kind
        if self.agent_type:
            headers["X-Temper-Agent-Type"] = self.agent_type
        if self.session_id:
            headers["X-Session-Id"] = self.session_id
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        principal_kind: str | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        data = None
        headers = self._headers(principal_kind)
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                if not raw.strip():
                    return None
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return raw
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            if exc.code == 403:
                self._raise_if_cedar_denied(raw)
            if exc.code == 404:
                raise TemperUnavailable(
                    f"404 from {path}: entity set/spec likely not deployed yet "
                    f"for tenant {self.tenant!r}. Body: {raw[:300]}"
                ) from exc
            raise TemperUnavailable(f"HTTP {exc.code} from {path}: {raw[:500]}") from exc
        except urllib.error.URLError as exc:
            raise TemperUnavailable(
                f"cannot reach Temper at {url}: {exc.reason}"
            ) from exc

    @staticmethod
    def _raise_if_cedar_denied(raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise TemperDenied(decision_id=None, message=raw[:500])
        err = payload.get("error", {})
        if err.get("code") == "AuthorizationDenied":
            msg = err.get("message", "Authorization denied")
            match = _PD_RE.search(msg)
            raise TemperDenied(
                decision_id=match.group(0) if match else None, message=msg
            )
        raise TemperDenied(decision_id=None, message=json.dumps(payload)[:500])

    # --- discovery ---
    def specs(self) -> Any:
        return self._request("GET", "/observe/specs")

    def spec_detail(self, entity: str) -> Any:
        return self._request("GET", f"/observe/specs/{entity}")

    def has_entity_set(self, entity_set: str) -> bool:
        """True if the OData entity set is deployed for this tenant.

        Probes the data plane (``GET /tdata/{Set}``) rather than the
        ``/observe/specs`` admin route, which is authorization-gated per tenant.
        A deployed set returns an OData collection; an undeployed one returns a
        404 ``EntitySetNotFound``.
        """
        try:
            self._request("GET", f"/tdata/{entity_set}?$top=1")
            return True
        except TemperUnavailable:
            return False

    # --- entity ops ---
    def create(self, entity_set: str, fields: dict[str, Any]) -> Any:
        return self._request("POST", f"/tdata/{entity_set}", fields)

    def get(self, entity_set: str, entity_id: str) -> Any:
        key = urllib.parse.quote(entity_id, safe="")
        return self._request("GET", f"/tdata/{entity_set}('{key}')")

    def list(self, entity_set: str, odata_filter: str | None = None) -> Any:
        path = f"/tdata/{entity_set}"
        if odata_filter:
            filt = odata_filter.removeprefix("$filter=").strip()
            encoded = filt.replace(" ", "%20").replace("'", "%27")
            path = f"{path}?$filter={encoded}"
        body = self._request("GET", path)
        if isinstance(body, dict) and "value" in body:
            return body["value"]
        return body

    def action(
        self, entity_set: str, entity_id: str, action_name: str, params: dict[str, Any]
    ) -> Any:
        key = urllib.parse.quote(entity_id, safe="")
        return self._request(
            "POST", f"/tdata/{entity_set}('{key}')/Temper.{action_name}", params
        )

    def patch(self, entity_set: str, entity_id: str, fields: dict[str, Any]) -> Any:
        key = urllib.parse.quote(entity_id, safe="")
        return self._request("PATCH", f"/tdata/{entity_set}('{key}')", fields)

    # --- governance ---
    # NOTE: the agent uses ONLY its own identity. It never self-asserts admin/
    # operator to read or resolve decisions -- that would be a privilege
    # escalation. If the decisions endpoint denies the agent (it is gated behind
    # `manage_policies` for this tenant), polling surfaces that honestly so the
    # human can resolve the decision in the Observe UI.
    def decisions(self, status: str | None = None) -> Any:
        path = f"/api/tenants/{self.tenant}/decisions"
        if status:
            path = f"{path}?status={status}"
        return self._request("GET", path)

    def decision_status(self, decision_id: str) -> str:
        """Return the decision's status, or a marker if the agent can't read it.

        ``no_read_access`` means the decisions endpoint denied the agent's own
        identity (gated behind ``manage_policies``); the human must resolve the
        decision in the Observe UI. The agent does not escalate to read it.
        """
        try:
            data = self.decisions()
        except (TemperDenied, TemperUnavailable):
            return "no_read_access"
        for d in (data or {}).get("decisions", []):
            if d.get("id") == decision_id:
                return d.get("status", "unknown")
        return "not_found"

    def poll_decision(
        self, decision_id: str, timeout_s: float = 120.0, interval_s: float = 3.0
    ) -> str:
        """Block until a human resolves the decision (or timeout). Never approves.

        Stops early and returns ``no_read_access`` if the agent's identity cannot
        read the decisions endpoint -- it will not escalate privileges to poll.
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            status = self.decision_status(decision_id)
            if status == "no_read_access":
                return status
            if status in ("Approved", "Denied", "approved", "denied"):
                return status
            time.sleep(interval_s)
        return "timeout"

    # --- spec submission (governed) ---
    def submit_specs(self, specs: dict[str, str]) -> Any:
        payload = {"tenant": self.tenant, "specs": specs}
        try:
            return self._request("POST", "/api/specs/load-inline", payload)
        except TemperDenied:
            raise
