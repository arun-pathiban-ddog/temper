#!/usr/bin/env python3
"""Dark Factory H2 — verified BREEDER identity bootstrap (idempotent).

Registers the two directed-evolution agent identities (ADR-0095) via the same
REAL credential chain as register_identities.py (AgentType.Define +
AgentCredential.Issue), NOT header spoofing:

  - `breeder`            drives the FitnessGoal + ImprovementIssue early phases
                         (planner+implementer); records generations.
  - `breeder-supervisor` the DISTINCT verified identity permitted to ApprovePlan
                         on FitnessGoal-driven issues (narrow auto-approve).

Role separation holds: the breeder cannot approve its own plan
(forbid planner_id==principal.id in improvement_issue.cedar); only the separate
breeder-supervisor can.

Usage:
    export TEMPER_API_KEY=<operator token>      # the global admin key
    python3 register_breeders.py [--base-url http://127.0.0.1:3000] [--tenant dark-factory]

Tokens are read from ./tokens.json. Idempotent (Define-from-Active and
Issue-self-loop are no-ops).
"""

import argparse
import os
import sys
import json
from pathlib import Path

# Reuse the proven helpers from the sibling bootstrap (same dir).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from register_identities import (  # noqa: E402
    TOKENS_PATH,
    ensure_agent_type,
    ensure_credential,
    sha256_hex,
    verify_identity,
)

# (agent_type_name used by Cedar, AgentType entity id, AgentCredential instance id)
BREEDERS = [
    ("breeder", "breeder-type", "breeder-agent"),
    ("breeder-supervisor", "breeder-supervisor-type", "breeder-supervisor-agent"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("TEMPER_BASE_URL", "http://127.0.0.1:3000"))
    ap.add_argument("--tenant", default=os.environ.get("TEMPER_TENANT", "dark-factory"))
    ap.add_argument("--tokens", default=str(TOKENS_PATH))
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    tenant = args.tenant
    tokens = json.loads(Path(args.tokens).read_text())
    op_token = os.environ.get("TEMPER_API_KEY") or tokens["operator"]["token"]

    print(f"Dark Factory BREEDER identity bootstrap — tenant '{tenant}' @ {base}")
    print("Operator (admin) token: present" if op_token else "Operator token: MISSING")
    print()

    ok = True
    for name, type_id, instance_id in BREEDERS:
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
    for name, _type_id, _instance in BREEDERS:
        ok &= verify_identity(base, tenant, tokens[name]["token"], name)

    print()
    print("DONE" if ok else "COMPLETED WITH WARNINGS (see above)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
