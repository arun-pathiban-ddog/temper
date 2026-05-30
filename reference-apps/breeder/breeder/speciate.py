"""Autonomous workload speciation (ADR-0102).

Closes the loop the classifier opened: read Helix per-workload telemetry, classify
each queue into a niche FROM TELEMETRY ONLY (breeder.infer — no Temper/config read),
then for each niche isolate it onto a dedicated, niche-tuned Helix cluster and
migrate the matching queues onto it. Every step is a governed Temper entity
(Speciation ledger + Cluster + Migration), Cedar-gated under the breeder identity.

The breeder remains Cedar-denied read on Queue/Producer/Consumer (ADR-0099): queues
are named by the string from telemetry tags, never by reading a workload entity.
The actual workload re-point is performed by the deploy-service executor (via the
UI's /api/migrations), not by this agent.

Run modes:
  python3 -m breeder.speciate --source snapshot --dry-run   # plan only, no writes
  python3 -m breeder.speciate --source datadog              # live, autonomous

Wall-clock: a niche cluster that does not yet exist triggers a real ~8-10 min Cloud
Build. The agent PREFERS reusing a Live cluster already on df-niche/<niche>, so a
demo with pre-built niche images is classify+migrate (fast). Set
--build-missing=false to skip niches whose cluster isn't pre-built (no live builds).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Reuse the telemetry-only classifier (and, transitively, the observer's Datadog
# source). NO Temper import here — classification stays firewalled.
from breeder.infer import infer_all, _build_source, discover_queues  # noqa: E402

# The genome source-rewriter — the SAME mechanism the evolution harness uses. The
# agent DERIVES each niche cluster itself: it commits the inferred genome to a
# branch and builds the image; nothing is pre-provisioned.
_EVOLUTION = Path(__file__).resolve().parents[2] / "evolution"
sys.path.insert(0, str(_EVOLUTION))
from genome import Genome, apply_to_source  # noqa: E402

UI_URL = os.environ.get("SPECIATE_UI_URL", "http://127.0.0.1:4100")
TEMPER_URL = os.environ.get("SPECIATE_TEMPER_URL", "http://127.0.0.1:3000")
TENANT = os.environ.get("SPECIATE_TENANT", "dark-factory")
TOKENS_PATH = Path(__file__).resolve().parents[2] / "identity" / "tokens.json"

# Helix repo + base branch the niche genome is committed onto. champion-metrics
# carries the DogStatsD exporter the workloads need.
HELIX_REPO = Path(os.environ.get("SPECIATE_HELIX_REPO", "/Users/arun.parthiban/notdd/helix"))
NICHE_BASE_BRANCH = os.environ.get("SPECIATE_BASE_BRANCH", "champion-metrics")

# Helix replica count per niche cluster (kept small for the demo / cost).
NICHE_REPLICAS = int(os.environ.get("SPECIATE_REPLICAS", "1"))
# A niche cluster's durable branch — the agent commits the genome here, then
# builds the image off it.
NICHE_BRANCH_PREFIX = "df-niche/"


class SpeciateError(RuntimeError):
    pass


@dataclass
class _Http:
    """Minimal stdlib HTTP client. UI calls carry no auth (server holds tokens);
    Temper /tdata calls carry the breeder bearer + tenant header."""

    base_url: str
    token: str | None = None
    tenant: str | None = None
    timeout: float = 30.0

    def _req(self, method: str, path: str, body: dict | None = None):
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json"}
        if data:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.tenant:
            headers["X-Tenant-Id"] = self.tenant
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            return exc.code, {"error": raw[:400]}
        except urllib.error.URLError as exc:
            raise SpeciateError(f"cannot reach {url}: {exc.reason}") from exc

    def get(self, path: str):
        return self._req("GET", path)

    def post(self, path: str, body: dict):
        return self._req("POST", path, body)


def _load_breeder_token() -> str:
    try:
        toks = json.load(open(TOKENS_PATH))
        return toks["breeder"]["token"]
    except (OSError, ValueError, KeyError) as exc:
        raise SpeciateError(f"cannot load breeder token from {TOKENS_PATH}: {exc}") from exc


# Cluster names are validated server-side as DNS-label slugs: 3-20 chars,
# start with a letter, end alphanumeric. Niche slugs like
# "high-throughput-batch" overflow 20 chars, so map each niche to a short,
# stable cluster name. The mapping must match prebuild_niches.py.
_NICHE_CLUSTER_NAME = {
    "high-throughput-batch": "niche-htb",      # full slug overflows 20-char limit
    "bursty": "niche-bursty",
    "low-latency": "niche-low-latency",        # 17 chars — fits
    "steady": "niche-steady",
}


def _niche_cluster_name(niche: str) -> str:
    if niche in _NICHE_CLUSTER_NAME:
        return _NICHE_CLUSTER_NAME[niche]
    # Fallback: truncate to the 20-char DNS-label budget, dns-safe.
    return f"niche-{niche}".replace("_", "-")[:20].rstrip("-")


def _valid_cluster_slug(name: str) -> str:
    """Coerce an LLM-proposed cluster name into the server's DNS-label rule:
    lowercase a-z0-9-, start with a letter, end alphanumeric, 3-20 chars."""
    import re
    s = re.sub(r"[^a-z0-9-]", "-", str(name).strip().lower()).strip("-")
    if not s or not s[0].isalpha():
        s = "niche-" + s
    s = s[:20].rstrip("-")
    return s if len(s) >= 3 else "niche-cluster"


# ---------------------------------------------------------------------------
# Idempotency / convergence
# ---------------------------------------------------------------------------

def _live_clusters(ui: _Http) -> dict[str, dict]:
    """Map cluster NAME -> the best entity for that name. Names can collide across
    re-runs (a Torndown/Failed entity + a fresh Live one share the name), so we
    must NOT do last-wins — prefer a Live entity, else the newest non-terminal one.
    Otherwise the executor's 'is it Live yet' poll can resolve to a stale Torndown
    entity and never converge."""
    _, doc = ui.get("/api/clusters")
    clusters = doc.get("clusters", []) if isinstance(doc, dict) else (doc or [])

    def rank(c: dict) -> tuple:
        st = (c.get("status") or c.get("Status") or "").lower()
        # Live wins; then Building/Deploying; then anything; terminal states last.
        order = {"live": 3, "deploying": 2, "building": 2}.get(st, 1)
        if st in ("torndown", "failed"):
            order = 0
        return (order, c.get("id") or c.get("Id") or "")  # tie-break: newest id

    out: dict[str, dict] = {}
    for c in clusters:
        name = (c.get("name") or c.get("Name") or "").strip()
        if not name:
            continue
        if name not in out or rank(c) > rank(out[name]):
            out[name] = c
    return out


def _converged_niches(ui: _Http) -> dict[str, set[str]]:
    """target_cluster -> set(queues) for every Converged Speciation (the
    convergence ledger). Keyed by the cluster a queue was placed onto."""
    _, doc = ui.get("/api/speciations")
    specs = doc.get("speciations", []) if isinstance(doc, dict) else (doc or [])
    out: dict[str, set[str]] = {}
    for s in specs:
        if (s.get("status") or s.get("Status") or "").lower() != "converged":
            continue
        target = (s.get("target_cluster") or s.get("TargetCluster") or "").strip()
        # Key off the queues that ACTUALLY moved, not the proposed set. A partial
        # migration then only marks the moved queues as placed; the rest retry.
        moved = (s.get("moved") or s.get("Moved") or "")
        qs = {q.strip() for q in moved.split(",") if q.strip()}
        if target:
            out.setdefault(target, set()).update(qs)
    return out


# ---------------------------------------------------------------------------
# Speciation entity lifecycle (breeder token, governed)
# ---------------------------------------------------------------------------

def _sim_ts(temper: _Http) -> str:
    # Reuse the UI's id convention loosely; a monotonic-ish suffix is enough for
    # uniqueness here. (Not sim_now — this is an out-of-tree agent.)
    return str(int(time.time() * 1000))


def _open_speciation(temper: _Http, niche: str, queues: list[str], target: str,
                     genome: dict, motivation: str) -> str:
    sid = f"spec-{niche}-{_sim_ts(temper)}".replace("_", "-")
    qcsv = ",".join(queues)
    gjson = json.dumps(genome)
    s, b = temper.post("/tdata/Speciations", {
        "id": sid, "Status": "Proposed", "Niche": niche, "Queues": qcsv,
        "TargetCluster": target, "Genome": gjson, "Motivation": motivation,
        "Namespace": TENANT,
    })
    if s not in (200, 201):
        raise SpeciateError(f"create Speciation failed: {b}")
    for action, body in [
        ("Propose", {"niche": niche, "queues": qcsv, "target_cluster": target,
                     "genome": gjson, "motivation": motivation}),
        ("ProvisionCluster", {}),
    ]:
        s, b = temper.post(f"/tdata/Speciations('{sid}')/Default.{action}", body)
        if s not in (200, 204):
            raise SpeciateError(f"{action} Speciation failed: {b}")
    return sid


def _record_cluster(temper: _Http, sid: str, cluster_id: str, image_tag: str) -> None:
    for action, body in [
        ("RecordCluster", {"cluster_id": cluster_id, "image_tag": image_tag}),
        ("BeginMigration", {}),
    ]:
        s, b = temper.post(f"/tdata/Speciations('{sid}')/Default.{action}", body)
        if s not in (200, 204):
            raise SpeciateError(f"{action} Speciation failed: {b}")


def _record_migrations(temper: _Http, sid: str, migration_ids: list[str], moved: list[str]) -> None:
    for action, body in [
        ("RecordMigrations", {"migration_ids": ",".join(migration_ids), "moved": ",".join(moved)}),
        ("MarkConverged", {}),
    ]:
        s, b = temper.post(f"/tdata/Speciations('{sid}')/Default.{action}", body)
        if s not in (200, 204):
            raise SpeciateError(f"{action} Speciation failed: {b}")


def _mark_failed(temper: _Http, sid: str) -> None:
    temper.post(f"/tdata/Speciations('{sid}')/Default.MarkFailed", {})


# ---------------------------------------------------------------------------
# Cluster provisioning — the agent DERIVES each niche cluster itself: it builds
# the genome (from the inferred niche), commits it to a branch, and builds the
# image off that branch. Nothing is pre-provisioned.
# ---------------------------------------------------------------------------

def _genome_for(genome_deltas: dict) -> Genome:
    """Build a Genome from the niche's genome deltas (relative to baseline)."""
    g = Genome()  # baseline: linger_ms=1, max_inflight=5, append_batch_size=1000
    for k, v in (genome_deltas or {}).items():
        g = g.with_gene(k, v)  # frozen — returns a new Genome
    return g


def _git(*args: str) -> tuple[int, str]:
    p = subprocess.run(["git", "-C", str(HELIX_REPO), *args], capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def _commit_niche_genome(niche: str, genome: Genome) -> tuple[str | None, str]:
    """Commit the niche genome onto df-niche/<niche> off champion-metrics and
    return (branch, short_sha). This is the agent authoring the niche's code — the
    SAME genome.apply_to_source mechanism the evolution harness uses. Returns
    (None, reason) on failure."""
    branch = f"{NICHE_BRANCH_PREFIX}{niche}".replace("_", "-")
    rc, out = _git("rev-parse", "--verify", NICHE_BASE_BRANCH)
    if rc != 0:
        return (None, f"base branch {NICHE_BASE_BRANCH} not found: {out}")
    # Reset the branch to the base so re-runs are clean/idempotent.
    _git("branch", "-f", branch, NICHE_BASE_BRANCH)

    wt = HELIX_REPO.parent / "helix-worktrees" / f"niche-build-{niche}".replace("_", "-")
    _git("worktree", "remove", "--force", str(wt))  # best-effort cleanup
    rc, out = _git("worktree", "add", "--force", str(wt), branch)
    if rc != 0:
        return (None, f"worktree add for {branch} failed: {out}")
    try:
        changed = apply_to_source(genome, wt)
        if changed:
            subprocess.run(["git", "-C", str(wt), "add", *changed], check=True)
            msg = (f"df-niche({niche}): apply telemetry-inferred genome {genome.to_dict()}\n\n"
                   f"Authored by the speciation agent (ADR-0102) — niche tuning derived from "
                   f"observed telemetry. Genes: linger_ms={genome.linger_ms}, "
                   f"max_inflight={genome.max_inflight}, append_batch_size={genome.append_batch_size}.")
            subprocess.run(["git", "-C", str(wt), "commit", "-m", msg], check=True)
        # else: baseline genome (steady) — branch stays at base, builds vanilla.
        rc, sha = _git("rev-parse", "--short", branch)
        return (branch, sha if rc == 0 else branch)
    except (subprocess.CalledProcessError, OSError) as exc:
        return (None, f"genome commit for {niche} failed: {exc}")
    finally:
        _git("worktree", "remove", "--force", str(wt))


def _ensure_niche_cluster(ui: _Http, niche: str, name: str, genome: Genome,
                          build_missing: bool, wait_minutes: int) -> tuple[str | None, str]:
    """Return (cluster_id, image_tag) for the target cluster `name`, Live.

    Reuses an existing Live cluster of that name ONLY if one already exists (e.g.
    a prior converged run). Otherwise the agent authors the genome branch itself
    (df-niche/<niche>) and builds the cluster off it (real Cloud Build). Returns
    (None, reason) if the cluster can't be made Live.
    """
    live = _live_clusters(ui)
    if name in live and (live[name].get("status") or "").lower() == "live":
        c = live[name]
        return (c.get("id") or c.get("Id"), c.get("image_tag") or f"cluster-{name}")

    if not build_missing:
        return (None, f"niche cluster {name} absent and --build-missing=false")

    # The agent authors the niche's code: commit the inferred genome to a branch.
    branch, ref = _commit_niche_genome(niche, genome)
    if branch is None:
        return (None, ref)
    print(f"  [build] {niche}: authored genome on {branch} ({ref}); building cluster {name}", file=sys.stderr)

    # Create + approve the cluster off the agent-authored branch (Phase-3 fix
    # makes the build use this base, not main).
    s, b = ui.post("/api/clusters", {"name": name, "base": branch, "replicas": NICHE_REPLICAS})
    if s not in (200, 201) or not b.get("ok"):
        return (None, f"create cluster {name} failed: {b}")
    cid = b.get("id")
    s, b = ui.post(f"/api/clusters/{cid}/approve", {})
    if s not in (200, 201) or not b.get("ok"):
        return (None, f"approve cluster {name} failed: {b}")

    # Poll until Live (real ~8-10 min Cloud Build).
    deadline = time.monotonic() + wait_minutes * 60
    while time.monotonic() < deadline:
        live = _live_clusters(ui)
        c = live.get(name)
        if c:
            st = (c.get("status") or "").lower()
            if st == "live":
                return (c.get("id") or cid, c.get("image_tag") or f"cluster-{name}")
            if st == "failed":
                return (None, f"cluster {name} build failed")
        time.sleep(15)
    return (None, f"cluster {name} did not go Live within {wait_minutes}m")


def _migrate_queue(ui: _Http, queue: str, to_cluster: str, reason: str) -> tuple[bool, str]:
    s, b = ui.post("/api/migrations", {
        "queue": queue, "to_cluster": to_cluster, "from_cluster": "helix", "reason": reason,
    })
    ok = s in (200, 201) and bool(b.get("ok"))
    return ok, (b.get("id") or "")


# ---------------------------------------------------------------------------
# Main speciation pass
# ---------------------------------------------------------------------------

def speciate(src, queues: list[str], window_min: int, *, dry_run: bool,
             build_missing: bool, wait_minutes: int, classifier: str = "llm",
             placement: bool = True, file_tickets: bool = True) -> dict:
    # Build the placement plan from telemetry. Each plan item is a target cluster:
    # {cluster, niche, genome, queues, motivation}. Telemetry-only — no config read.
    if classifier == "llm" and placement:
        # The LLM proposes the WHOLE plan: it discovers queues, classifies them,
        # AND decides cluster placement (may co-locate queues that share a niche).
        from breeder.llm_classify import propose_placement_llm
        plan = propose_placement_llm(window_min)
    else:
        # Classify per queue, then group by niche into one cluster per niche.
        if classifier == "llm":
            from breeder.llm_classify import classify_all_llm
            inferences = classify_all_llm(src, queues, window_min)
        else:
            inferences = infer_all(src, queues, window_min)
        by_niche: dict[str, list] = {}
        for inf in inferences:
            by_niche.setdefault(inf.niche, []).append(inf)
        plan = []
        for niche, infs in sorted(by_niche.items()):
            plan.append({
                "cluster": _niche_cluster_name(niche),
                "niche": niche,
                "queues": sorted({i.queue for i in infs}),
                "genome": infs[0].genome,
                "motivation": infs[0].motivation,
            })

    if dry_run:
        print("DRY RUN — placement plan (no writes):\n")
        for p in plan:
            print(f"  cluster={p['cluster']:20s} niche={p['niche']:22s} genome={p['genome']}")
            print(f"      queues={p['queues']}")
            print(f"      {p['motivation']}\n")
        print(f"speciation (dry-run): {len(plan)} clusters, "
              f"{sum(len(p['queues']) for p in plan)} queues would be placed.")
        return {"dry_run": True, "plan": plan}

    ui = _Http(UI_URL)

    # DEFAULT: file a Symphony ticket per placement (auto-advanced to Implementing
    # by the backend). The speciation EXECUTOR then claims each ticket and does the
    # real cluster-build + migrate. The agent itself is propose-only here — it does
    # NOT build clusters. (--no-tickets falls back to the inline build path below.)
    if file_tickets:
        s, b = ui.post("/api/speciation/tickets", {"placements": plan})
        filed = (b or {}).get("filed", []) if isinstance(b, dict) else []
        ok_n = sum(1 for f in filed if f.get("ok"))
        # Lead the summary with the human-readable PROPOSALS (niche → cluster ←
        # queues) so the Agent Runs tab shows what the agent decided, not ticket
        # ids. The full plan is also returned for the run detail.
        proposals = "; ".join(
            f"{p['niche']}→{p['cluster']}←[{','.join(p['queues'])}]" for p in plan
        )
        summary = (f"speciation: proposed {len(plan)} placement(s) → {ok_n} Symphony ticket(s): {proposals}")
        print(summary)
        return {"filed": filed, "plan": plan, "summary": summary,
                "proposals": [{"niche": p["niche"], "cluster": p["cluster"],
                               "queues": p["queues"], "genome": p["genome"],
                               "motivation": p.get("motivation", "")} for p in plan]}

    temper = _Http(TEMPER_URL, token=_load_breeder_token(), tenant=TENANT)

    converged = _converged_niches(ui)
    niches_isolated = 0
    queues_migrated: list[str] = []
    skipped: list[str] = []

    for p in plan:
        niche, qs, genome, motivation = p["niche"], p["queues"], p["genome"], p["motivation"]
        target = _valid_cluster_slug(p.get("cluster") or _niche_cluster_name(niche))
        already = converged.get(target, set())
        fresh = [q for q in qs if q not in already]
        if not fresh:
            skipped.append(f"{target}(all {len(qs)} queues already placed)")
            continue

        sid = _open_speciation(temper, niche, fresh, target, genome, motivation)
        try:
            cid, image_tag = _ensure_niche_cluster(
                ui, niche, target, _genome_for(genome), build_missing, wait_minutes)
            if cid is None:
                print(f"  [skip] {target}: {image_tag}", file=sys.stderr)
                _mark_failed(temper, sid)
                skipped.append(f"{target}({image_tag})")
                continue
            _record_cluster(temper, sid, cid, image_tag)

            mig_ids, moved = [], []
            for q in fresh:
                ok, mid = _migrate_queue(ui, q, target, f"placement '{niche}' — {motivation[:120]}")
                if ok:
                    mig_ids.append(mid)
                    moved.append(q)
                    queues_migrated.append(q)
                else:
                    print(f"  [warn] migrate {q} -> {target} failed", file=sys.stderr)
            # Only converge if at least one queue actually moved. Converging with
            # moved=[] would poison the ledger (the cluster records as placed and
            # is skipped forever) even though nothing migrated. Mark Failed so the
            # next pass retries.
            if not moved:
                print(f"  [fail] {target}: cluster Live but no queues migrated", file=sys.stderr)
                _mark_failed(temper, sid)
                skipped.append(f"{target}(0 queues moved)")
                continue
            _record_migrations(temper, sid, mig_ids, moved)
            niches_isolated += 1
            print(f"  [ok] placed {niche} onto {target}: migrated {moved}")
        except SpeciateError as exc:
            print(f"  [fail] {target}: {exc}", file=sys.stderr)
            _mark_failed(temper, sid)
            skipped.append(f"{niche}(error)")

    # Final one-line summary — this is the line the Agent Runs tab surfaces
    # (via _meaningful_tail). Lead with the placement so it reads at a glance.
    placed = ", ".join(f"{p['cluster']}<-[{','.join(p['queues'])}]" for p in plan)
    summary = (f"speciation: placed {len(queues_migrated)} queue(s) onto "
               f"{niches_isolated} niche cluster(s): {placed}"
               + (f"; skipped {len(skipped)} ({'; '.join(skipped)})" if skipped else ""))
    print(summary)
    return {"niches_isolated": niches_isolated, "queues_migrated": queues_migrated,
            "skipped": skipped, "plan": plan, "summary": summary}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Autonomous workload speciation (breeder, ADR-0102).")
    ap.add_argument("--source", choices=["snapshot", "datadog"], default="snapshot")
    ap.add_argument("--snapshot", default=str(Path(__file__).resolve().parents[1] / "snapshots" / "niches.json"))
    ap.add_argument("--queues", default="", help="comma queue list (live mode; telemetry-sourced)")
    ap.add_argument("--window", default="30m")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; no writes/builds/migrations")
    ap.add_argument("--build-missing", default="true", choices=["true", "false"],
                    help="agent authors+builds the niche cluster itself if absent "
                         "(real ~8-10m Cloud Build). Default true — true autonomy.")
    ap.add_argument("--wait-minutes", type=int, default=20, help="cluster-live poll timeout")
    ap.add_argument("--classifier", choices=["llm", "heuristic"], default="llm",
                    help="llm = an agent queries telemetry + reasons (default); "
                         "heuristic = static-threshold classifier. Both telemetry-only.")
    ap.add_argument("--no-placement", action="store_true",
                    help="disable LLM placement (one cluster per niche by group instead of "
                         "letting the agent propose clusters + co-locations)")
    ap.add_argument("--no-tickets", action="store_true",
                    help="build + migrate INLINE instead of filing Symphony tickets for the "
                         "speciation executor (default: file tickets, executor implements)")
    args = ap.parse_args(argv)

    window_min = int(args.window.rstrip("m")) if args.window.endswith("m") else 30

    # LLM mode queries Datadog through claude -p's own MCP, so it doesn't need the
    # Python DD source. It can also DISCOVER the queues itself from telemetry tags
    # when no --queues hint and no snapshot is given.
    if args.classifier == "llm":
        src = None
        queues = [q for q in (args.queues or "").split(",") if q.strip()]
        # In placement mode the LLM discovers queues itself; only pre-discover for
        # the classify-then-group path (--no-placement).
        if args.no_placement and not queues:
            if args.source == "snapshot":
                try:
                    src = _build_source(args)
                    queues = discover_queues(src)
                except SystemExit:
                    src = None
            if not queues:
                from breeder.llm_classify import discover_queues_llm
                queues = discover_queues_llm(window_min)
            if not queues:
                print("No queues discoverable from telemetry.", file=sys.stderr)
                return 1
    else:
        src = _build_source(args)
        queues = discover_queues(src)
        if not queues:
            print("No queues discoverable from telemetry "
                  "(no --queues hint or snapshot).", file=sys.stderr)
            return 1

    try:
        speciate(src, queues, window_min,
                 placement=(not args.no_placement),
                 file_tickets=(not args.no_tickets),
                 dry_run=args.dry_run,
                 build_missing=(args.build_missing == "true"),
                 wait_minutes=args.wait_minutes,
                 classifier=args.classifier)
    except SpeciateError as exc:
        print(f"speciation: FAILED — {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
