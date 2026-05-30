#!/usr/bin/env python3
"""Dark Factory — verified agent identity bootstrap (idempotent).

Issues REAL platform credentials (AgentType.Define + AgentCredential.Issue) for
the governed Helix loop, using the credential-registry path described in
ADR-0033 — NOT X-Temper-Principal header spoofing.

How it works (the real chain):
  1. AgentType.Define   (Draft -> Active)  registers a verified agent type.
  2. AgentCredential.Issue (Active)        maps sha256(bearer_token) -> AgentType,
                                           so the server's IdentityResolver
                                           resolves the bearer token to a
                                           VERIFIED identity (agentTypeVerified=true)
                                           and Cedar trusts principal.agent_type.

The operator (admin) token is the global TEMPER_API_KEY. When the server boots
with TEMPER_API_KEY set, the bearer-auth middleware injects
`X-Temper-Principal-Kind: admin` for that token, giving the operator the
privileged path used here to seed the four agent identities and to drive
`temper decide` approvals.

Entity-set routing: the dark-factory app CSDL declares `AgentTypes` and
`AgentCredentials` entity sets (see ./dark-factory-specs/model.csdl.xml) bound
to the agent OS transition tables (registered via bootstrap_agent_specs
merge=true). Credentials are therefore issued through the governed OData write
path with the operator token.

Idempotent: AgentType.Define from Active is a no-op (409 invalid transition is
tolerated); AgentCredential already-Active with the same key_hash is a no-op.
Re-running never duplicates or rotates a live credential.

Usage:
    export TEMPER_API_KEY=<operator token>      # the global admin key
    python3 register_identities.py [--base-url http://127.0.0.1:3000] [--tenant dark-factory]

Tokens are read from ./tokens.json (generated alongside this script). Keep that
file out of version control in real deployments; here it is the integrator's
local secret store for the demo.
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOKENS_PATH = HERE / "tokens.json"


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def http(method, url, token, tenant, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Tenant-Id", tenant)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw}


def ensure_agent_type(base, op_token, tenant, type_id, type_name):
    """Create the AgentType (Draft) then Define -> Active. Idempotent."""
    # Create the entity (Draft). 409 if it already exists — tolerated.
    status, resp = http(
        "POST", f"{base}/tdata/AgentTypes", op_token, tenant,
        {"id": type_id, "Status": "Draft", "name": type_name},
    )
    if status not in (200, 201, 409):
        print(f"  [warn] create AgentType {type_id}: HTTP {status} {resp}")
    # Define -> Active. No-op (409) if already Active.
    status, resp = http(
        "POST", f"{base}/tdata/AgentTypes('{type_id}')/Default.Define", op_token, tenant,
        {
            "name": type_name,
            "system_prompt": f"Dark Factory governed-loop agent: {type_name}",
            "tool_set": "local",
            "model": "none",
            "max_turns": "0",
            "adapter_config": "{}",
            "default_budget_cents": "0",
        },
    )
    if status in (200, 201):
        print(f"  AgentType '{type_name}' ({type_id}) -> Active")
    elif status == 409:
        print(f"  AgentType '{type_name}' ({type_id}) already Active (idempotent)")
    else:
        print(f"  [warn] Define AgentType {type_id}: HTTP {status} {resp}")
        return False
    return True


def ensure_credential(base, op_token, tenant, key_hash, key_prefix, type_id, instance_id, name):
    """Create the AgentCredential (Active) then Issue (links AgentType). Idempotent."""
    # Create the entity. Entity id == key_hash for O(1) resolver lookup.
    status, resp = http(
        "POST", f"{base}/tdata/AgentCredentials", op_token, tenant,
        {"id": key_hash, "Status": "Active"},
    )
    if status not in (200, 201, 409):
        print(f"  [warn] create AgentCredential {name}: HTTP {status} {resp}")
    # Issue (self-loop on Active) — sets the fields the resolver reads.
    status, resp = http(
        "POST", f"{base}/tdata/AgentCredentials('{key_hash}')/Default.Issue", op_token, tenant,
        {
            "agent_type_id": type_id,
            "agent_instance_id": instance_id,
            "key_hash": key_hash,
            "key_prefix": key_prefix,
            "description": f"Dark Factory governed-loop credential: {name}",
            "created_by": "register_identities.py",
            "expires_at": "",
        },
    )
    if status in (200, 201):
        print(f"  AgentCredential '{name}' issued (instance={instance_id}, hash={key_hash[:8]}...)")
        return True
    print(f"  [warn] Issue AgentCredential {name}: HTTP {status} {resp}")
    return False


def verify_identity(base, tenant, token, expect_name):
    """Confirm the server resolves the bearer token to a verified identity."""
    status, resp = http(
        "POST", f"{base}/api/identity/resolve", None, tenant,
        {"bearer_token": token, "tenant": tenant},
    )
    ok = (
        status in (200, 201)
        and isinstance(resp, dict)
        and resp.get("verified") is True
        and resp.get("agent_type_name") == expect_name
    )
    flag = "OK " if ok else "XX "
    print(f"  [{flag}] resolve {expect_name}: HTTP {status} verified={resp.get('verified')} "
          f"agent_type={resp.get('agent_type_name')} instance={resp.get('agent_instance_id')}")
    return ok


# (agent_type_name used by Cedar, AgentType entity id, AgentCredential instance id)
AGENTS = [
    ("observer", "observer-type", "observer-agent"),
    ("researcher", "researcher-type", "researcher-agent"),
    ("symphony", "symphony-type", "symphony-agent"),
    ("supervisor", "supervisor-type", "supervisor-agent"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("TEMPER_BASE_URL", "http://127.0.0.1:3000"))
    ap.add_argument("--tenant", default=os.environ.get("TEMPER_TENANT", "dark-factory"))
    ap.add_argument("--tokens", default=str(TOKENS_PATH))
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    tenant = args.tenant

    tokens = json.loads(Path(args.tokens).read_text())
    op_token = os.environ.get("TEMPER_API_KEY") or tokens["operator"]["token"]

    print(f"Dark Factory identity bootstrap — tenant '{tenant}' @ {base}")
    print("Operator (admin) token: present" if op_token else "Operator token: MISSING")
    print()

    ok = True
    for name, type_id, instance_id in AGENTS:
        info = tokens[name]
        token = info["token"]
        key_hash = info.get("key_hash") or sha256_hex(token)
        key_prefix = info.get("key_prefix") or token[:8]
        print(f"== {name} ==")
        ok &= ensure_agent_type(base, op_token, tenant, type_id, name)
        ok &= ensure_credential(base, op_token, tenant, key_hash, key_prefix,
                                type_id, instance_id, name)
        print()

    print("Verifying resolution via /api/identity/resolve:")
    for name, _type_id, _instance in AGENTS:
        ok &= verify_identity(base, tenant, tokens[name]["token"], name)

    print()
    print("DONE" if ok else "COMPLETED WITH WARNINGS (see above)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
