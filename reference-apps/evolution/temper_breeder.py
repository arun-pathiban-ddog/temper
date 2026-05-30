"""Thin Temper HTTP client for the breeding harness (ADR-0095).

Drives FitnessGoal + ImprovementIssue entities in the `dark-factory` tenant using
REAL verified bearer credentials (the breeder / breeder-supervisor identities from
register_breeders.py), NOT header spoofing. Mirrors the observer's temper_client
but is scoped to what the harness needs.

Two identities:
  - breeder            (planner+implementer; drives Observe/BeginPlanning/WritePlan,
                        AssignPlanner=itself, RecordGeneration)
  - breeder-supervisor (the distinct approver; ApprovePlan + FitnessGoal Define/Activate)

Cedar denials surface as TemperDenied (with the PD-<id> decision id) — the harness
never self-approves through a forbidden path; the breeder-supervisor's auto-approve
permit is a legitimate, narrow, verified-identity grant.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PD_RE = re.compile(r"PD-[0-9a-fA-F-]+")
TOKENS_PATH = Path(__file__).resolve().parents[1] / "identity" / "tokens.json"


class TemperUnavailable(RuntimeError):
    """The Temper server is unreachable, or the required spec is not deployed."""


@dataclass
class TemperDenied(RuntimeError):
    """A Cedar policy denied the action."""

    decision_id: str | None
    message: str

    def __str__(self) -> str:
        return f"Cedar denied (decision={self.decision_id}): {self.message}"


def load_tokens(path: str | Path = TOKENS_PATH) -> dict:
    return json.loads(Path(path).read_text())


@dataclass
class BreederClient:
    """Temper client for one bearer identity, scoped to one tenant."""

    token: str
    base_url: str = "http://127.0.0.1:3000"
    tenant: str = "dark-factory"
    timeout: float = 30.0

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "X-Tenant-Id": self.tenant,
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            if exc.code == 403:
                self._raise_cedar(raw)
            raise TemperUnavailable(f"HTTP {exc.code} from {path}: {raw[:400]}") from exc
        except urllib.error.URLError as exc:
            raise TemperUnavailable(f"cannot reach Temper at {url}: {exc.reason}") from exc

    @staticmethod
    def _raise_cedar(raw: str) -> None:
        try:
            payload = json.loads(raw)
            msg = payload.get("error", {}).get("message", raw[:400])
        except json.JSONDecodeError:
            msg = raw[:400]
        m = _PD_RE.search(msg)
        raise TemperDenied(decision_id=m.group(0) if m else None, message=msg)

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
            encoded = urllib.parse.quote(filt, safe="=' ")
            path = f"{path}?$filter={encoded}"
        body = self._request("GET", path)
        if isinstance(body, dict) and "value" in body:
            return body["value"]
        return body

    def action(self, entity_set: str, entity_id: str, action: str, params: dict[str, Any]) -> Any:
        key = urllib.parse.quote(entity_id, safe="")
        return self._request("POST", f"/tdata/{entity_set}('{key}')/Temper.{action}", params)

    def has_entity_set(self, entity_set: str) -> bool:
        try:
            self._request("GET", f"/tdata/{entity_set}?$top=1")
            return True
        except TemperUnavailable:
            return False
