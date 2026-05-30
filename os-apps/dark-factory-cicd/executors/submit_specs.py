#!/usr/bin/env python3
"""Submit the dark-factory-cicd specs + Cedar policies to a running Temper server.

This drives the same /api/specs/load-inline endpoint the MCP `submit_specs`
method uses. Spec submission is Cedar-gated on `submit_specs`/`SpecRegistry`;
if denied you will get HTTP 403 with a decision_id to surface to a human.

Usage:
  python3 submit_specs.py            # submit to 127.0.0.1:3000, tenant dark-factory
  TEMPER_URL=... TENANT=... python3 submit_specs.py
"""
import json
import os
import sys
import urllib.error
import urllib.request

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = os.environ.get("TEMPER_URL", "http://127.0.0.1:3000")
TENANT = os.environ.get("TENANT", "dark-factory")
# Spec authoring is a design-time, operator action (SKILL: "Activation is
# governed; design is not"). The developer/operator submits as the platform
# admin principal. Set TEMPER_AS_AGENT=1 to instead submit as a plain agent and
# exercise the Cedar gate (you will get a decision_id to approve).
AS_AGENT = os.environ.get("TEMPER_AS_AGENT") == "1"

SPEC_FILES = [
    "specs/improvement_issue.ioa.toml",
    "specs/ci_run.ioa.toml",
    "specs/deploy.ioa.toml",
    "specs/model.csdl.xml",
]
CEDAR_FILES = [
    "policies/improvement_issue.cedar",
    "policies/ci_run.cedar",
    "policies/deploy.cedar",
]


def read(rel):
    with open(os.path.join(APP_DIR, rel)) as fh:
        return fh.read()


def main():
    specs = {f: read(f) for f in SPEC_FILES}
    cedar = "\n\n".join(read(f) for f in CEDAR_FILES)
    body = {
        "tenant": TENANT,
        "app_name": "dark-factory-cicd",
        "specs": specs,
        "cedar_policies": cedar,
    }
    headers = {"Content-Type": "application/json", "X-Tenant-Id": TENANT}
    if not AS_AGENT:
        headers["X-Temper-Principal-Kind"] = "admin"
        headers["X-Temper-Principal-Id"] = "agent-b-operator"
    req = urllib.request.Request(
        f"{URL}/api/specs/load-inline",
        data=json.dumps(body).encode(),
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            print("HTTP", r.status)
            print(r.read().decode())
    except urllib.error.HTTPError as e:
        payload = e.read().decode()
        print("HTTP", e.code)
        print(payload)
        if e.code == 403:
            print(
                "\nSpec submission DENIED by Cedar. Surface the decision_id above "
                "to a human to approve via the Observe UI, then re-run.",
                file=sys.stderr,
            )
            sys.exit(2)
        sys.exit(1)


if __name__ == "__main__":
    main()
