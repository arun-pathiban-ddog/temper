"""Speciation executor (ADR-0102).

The speciation agent (breeder.speciate) is propose-only: it classifies workloads
from telemetry and files one Symphony ticket (ImprovementIssue) per proposed
cluster placement, auto-advanced to Implementing. THIS executor claims those
Implementing tickets and does the real work Symphony's code-implementer cannot:
author the niche genome, build + deploy the cluster on GKE, and migrate the
placement's queues onto it. Then it advances the ticket to Done (or Fails it).

Why a separate executor: Symphony's implementer is `claude -p` restricted to
Edit/Read/Write — it can only change code + open PRs, never call the control-plane
APIs. The executor has that access (it drives /api/clusters + /api/migrations via
the same governed path the inline agent used). One ticket implemented per tick.

Run: python3 -m breeder.speciate_executor   (the scheduler runs this on a timer)
"""

from __future__ import annotations

import sys

# Reuse the build + migrate primitives from the speciation agent.
from breeder.speciate import (
    _Http, UI_URL, TEMPER_URL, TENANT, _load_breeder_token,
    _ensure_niche_cluster, _migrate_queue, _genome_for,
    _open_speciation, _record_cluster, _record_migrations, _mark_failed,
    SpeciateError,
)


def _claim_tickets(ui: _Http) -> list[dict]:
    _, doc = ui.get("/api/speciation/tickets")
    tickets = doc.get("tickets", []) if isinstance(doc, dict) else []
    return [t for t in tickets if (t.get("status") or "") == "Implementing"]


def _implement(ui: _Http, temper: _Http, ticket: dict, wait_minutes: int) -> tuple[bool, str]:
    """Build the niche cluster + migrate the ticket's queues. Returns (ok, detail)."""
    iid = ticket["issue_id"]
    pl = ticket.get("placement") or {}
    niche = str(pl.get("niche", "steady"))
    target = ticket.get("cluster") or str(pl.get("cluster", ""))
    genome = pl.get("genome") or {}
    queues = [str(q) for q in pl.get("queues", []) if q]
    if not target or not queues:
        return False, f"{iid}: malformed placement (cluster/queues missing)"

    # Record a governed Speciation ledger entry for this placement.
    sid = _open_speciation(temper, niche, queues, target, genome,
                           f"executor implementing ticket {iid}")
    try:
        cid, image_tag = _ensure_niche_cluster(
            ui, niche, target, _genome_for(genome), build_missing=True, wait_minutes=wait_minutes)
        if cid is None:
            _mark_failed(temper, sid)
            return False, f"{iid}: cluster {target} not Live ({image_tag})"
        _record_cluster(temper, sid, cid, image_tag)

        mig_ids, moved = [], []
        for q in queues:
            ok, mid = _migrate_queue(ui, q, target, f"speciation ticket {iid}: place on {target}")
            if ok:
                mig_ids.append(mid)
                moved.append(q)
            else:
                print(f"  [warn] migrate {q} -> {target} failed", file=sys.stderr)
        if not moved:
            _mark_failed(temper, sid)
            return False, f"{iid}: cluster {target} Live but no queues migrated"
        _record_migrations(temper, sid, mig_ids, moved)
        return True, f"{iid}: built {target} ({image_tag}), migrated {moved}"
    except SpeciateError as exc:
        _mark_failed(temper, sid)
        return False, f"{iid}: {exc}"


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Speciation executor — implements Implementing speciation tickets.")
    ap.add_argument("--wait-minutes", type=int, default=20, help="cluster-live poll timeout")
    ap.add_argument("--max-per-tick", type=int, default=1, help="tickets to implement per run")
    args = ap.parse_args(argv)

    ui = _Http(UI_URL)
    temper = _Http(TEMPER_URL, token=_load_breeder_token(), tenant=TENANT)

    tickets = _claim_tickets(ui)
    if not tickets:
        print("speciation-executor: no Implementing speciation tickets to claim.")
        return 0

    done = []
    for ticket in tickets[: args.max_per_tick]:
        iid = ticket["issue_id"]
        print(f"  [claim] {iid} -> cluster {ticket.get('cluster')}", flush=True)
        ok, detail = _implement(ui, temper, ticket, args.wait_minutes)
        # Advance (or fail) the ticket through the governed lifecycle.
        ui.post(f"/api/speciation/tickets/{iid}/advance",
                {"ok": ok, "result": detail})
        done.append(f"{'OK' if ok else 'FAIL'} {detail}")
        print(f"  [{'done' if ok else 'fail'}] {detail}", flush=True)

    remaining = max(0, len(tickets) - args.max_per_tick)
    summary = (f"speciation-executor: implemented {len(done)} ticket(s)"
               + (f", {remaining} still queued" if remaining else "")
               + " :: " + " | ".join(done))
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
