# Live Evolution Loop — Implementation Plan

**Status:** Ready to implement  
**Context:** [Session chat — "Directed Evolution Handoff + Live Loop Design"](447f34dc-0d92-4ea0-988d-f4bc5758edde)  
**Diagram:** `assets/helix_hackathon-f019890b-b3b2-46b6-aa62-a1ede68a03ad.png`

---

## 1. What We're Building

The current evolution harness (`reference-apps/evolution/`) runs mutations against a
local docker-compose bench. This plan wires it to the real live GKE path:

**Current (local):**
```
mutate genome → local docker-compose bench → DST cull → git commit
```

**Target (live):**
```
[STARTUP] Teardown orphaned ev-* clusters. Resume stage from git + Temper state.

Observer reads live DD telemetry from champion cluster
  → Observer PICKS the active fitness objective (lineage) based on current telemetry:
      e.g. "p95 latency has spiked 18% → activate latency FitnessGoal"
      (lineage = a named FitnessGoal in Temper with intent/metric/direction/target)
  → Research Agent generates a BATCH of up to 4 candidates
      - Claude Code applies each mutation to its own worktree
        (code changes in helix repo guided by EVOLUTION_GUIDE.md;
         may also include CONTROL PLANE changes: define new queues,
         propose routing/topology changes via Queue/Migration entities)
      - Text feedback from prior variants shapes each proposal
      - Every 3 variants: meta-LLM reads stage history → updates direction

  ──── PARALLEL BATCH (up to 4 simultaneous) ────
  For each candidate in batch:
    → Stage 1: DST cull (cargo test — safety gate, ~5 min)
    → Stage 2: Cloud Build image (async, ~10 min, all 4 fire simultaneously)
    → Deploy variant cluster (all 4 in parallel)
    → Clone prod workload to variant:
        query champion's active Producer/Consumer entities → re-create with cluster=variant
        (same queue names, shapes, rates, msg_size — exact production profile, not synthetic)
        Validate ALL queue names start with "shopping_store" prefix (enforced)
    → Wait traffic_window_minutes=3 (champion + all 4 variants running simultaneously)
    → Read helix.produce.latency_ms / throughput from DD per cluster tag
    → Stop cloned producers/consumers; teardown variant cluster
  ────────────────────────────────────────────────

  → PASS: record Breed(PerfDelta) + FitnessGoal.RecordGeneration
  → FAIL: record cull reason + text feedback for next proposal round

  [loop until stopping criteria: max_variants_per_stage OR max_rounds_per_stage]
  Target: ~3 batches of 4 variants = 12 variants/stage ≈ 1 hour

  → Select best variant (best perf_delta_pct vs champion baseline)
  → Cedar-gated supervisor approval → git merge evolve/<lineage> → champion-metrics
  → Stage summary built dynamically from Breed + RecordGeneration query (no extra field)
  → Retire all Breed entities (store reset for stage I+1)
  → Wait champion_cooldown_minutes (default 5 / prod 60)
  → Observer tick fires on new champion → Stage I+1 begins
```

---

## 2. Design Decisions (Agreed)

| Decision | Choice | Rationale |
|---|---|---|
| Parallel vs sequential variant testing | **Parallel batches of up to 4** | 4 Cloud Builds fire simultaneously; all 4 clusters serve cloned prod traffic during the same 3-min window; tears down in parallel. One batch ≈ 15-20 min → 3 batches/stage ≈ 1 hour. |
| Traffic for variant testing | **Cloned production workload** (not synthetic) | Query champion's active `Producer`/`Consumer` Temper entities → re-create with `cluster=variant`. Same queues, shapes (stable\|bursty\|ramp), rates, msg_size. True production profile. Champion cluster keeps its original workload untouched throughout. |
| Queue naming invariant | All queue names must start with `shopping_store` | Enforced at variant startup: query Queue entities on variant cluster, reject any not matching prefix. Claude Code prompt also includes this constraint explicitly. |
| Observation window per variant | **3 min** (configurable, `traffic_window_minutes=3`) | Short for hourly stages; p95/throughput stabilize quickly under cloned load. Increase to 10 min in prod. |
| Active fitness objective (lineage) | **Observer-determined** at each stage start | Observer reads live DD → picks the most-stressed FitnessGoal. Pre-configured goals: `latency` (minimize p95) and `throughput` (maximize msg/s). Observer selects based on which is furthest from target. Not hardcoded. |
| Mutation types | Code changes + control plane changes | Mutations can modify helix source code AND create/reroute queues via Queue/Migration Temper entities. EVOLUTION_GUIDE.md scopes what's safe. DST is the safety gate. |
| Variant store lifetime | Stage-local, discarded at I→I+1 | Mutations are context-relative; prior-stage variants don't transfer to new base |
| Stage history storage | Built from `FitnessGoal.RecordGeneration` + `Breed` queries | **No new IOA field needed.** `RecordGeneration(generation, gene, fitness, outcome)` already exists. `gene` is repurposed for `change_description`. Meta-LLM queries these at proposal time. |
| Mutation operator | **Claude Code via Symphony** | Runs in the worktree; changes any EVOLVE-SAFE file. Can also emit Temper API calls to define Queue/Migration entities as part of the mutation. |
| Champion promotion | `git merge evolve/<lineage> → champion-metrics` | Becomes base genome for stage I+1 |
| Promotion gate | Cedar supervisor approval (UI or breeder-supervisor token) | Keeps human in the loop per ADR-0095 |
| Stage end trigger | `max_variants_per_stage` OR `max_rounds_per_stage` | Prevents runaway loops |
| Post-merge cooldown | Configurable, default 5 min (testing) / 60 min (prod) | Steady-state telemetry before Observer tick for stage I+1 |
| DST order | BEFORE live deploy (Stage 1) | Don't spin up GKE cluster for a genome that fails DST |
| Candidate generation | Meta-LLM + text feedback (ShinkaEvolve-inspired) | Every 3 variants, a meta-LLM analyzes history and suggests direction |
| Persistence / restart | **Restart from last committed stage** (no snapshot file) | Git + Temper IS the snapshot. Startup cleanup tears down orphaned `ev-*` clusters. |

---

## 3. Files to Create

All new files go in `reference-apps/evolution/`.

### 3.1 `config.py`
Centralized configuration. All parameters are overridable via env var or CLI flag.

```python
@dataclass
class LiveEvolutionConfig:
    # Stage stopping criteria
    max_variants_per_stage: int = 12     # env: EVOLUTION_MAX_VARIANTS
                                         # 12 = 3 batches of 4 ≈ 1 hour
    max_rounds_per_stage: int = 3        # env: EVOLUTION_MAX_ROUNDS

    # Parallel batch size — how many variants run in parallel each batch
    batch_size: int = 4                  # env: EVOLUTION_BATCH_SIZE
                                         # Set to 1 to fall back to sequential for debugging

    # Traffic measurement window (how long to observe all clusters simultaneously)
    traffic_window_minutes: int = 3      # env: EVOLUTION_TRAFFIC_WINDOW_MINUTES
                                         # 3 min for testing; increase to 10 for prod

    # Workload cloning: source cluster whose Producer/Consumer entities are replicated
    # to each variant cluster during measurement. Default = baseline helix cluster.
    workload_source_cluster: str = "helix"  # env: EVOLUTION_WORKLOAD_SOURCE

    # Queue naming invariant: all queues on variant clusters must have this prefix
    queue_prefix_invariant: str = "shopping_store"  # env: EVOLUTION_QUEUE_PREFIX

    # Post-merge cooldown before Observer fires for stage I+1
    champion_cooldown_minutes: int = 5   # env: EVOLUTION_COOLDOWN_MINUTES
                                         # (5 = testing default; set to 60 for prod)

    # Probe workload prefix — all queues starting with this prefix are migrated to
    # the champion cluster for fitness baseline. Sub-queue splits handled automatically.
    # Fitness is measured cluster-wide via DD (not per-queue).
    probe_workload: str = "shopping_store"  # env: EVOLUTION_PROBE_WORKLOAD

    # Meta-recommendation interval (from ShinkaEvolve)
    meta_rec_interval: int = 3           # fire meta-LLM every N variants

    # API endpoints
    ui_base_url: str = "http://127.0.0.1:4100"   # deploy-history app
    temper_base_url: str = "http://127.0.0.1:3000"
    tenant: str = "dark-factory"

    # Available fitness objectives (lineages). The Observer dynamically selects
    # which one to activate at stage start based on live DD telemetry — whichever
    # is furthest from its target. You do not pick the lineage; the Observer does.
    # These correspond to FitnessGoal entities already created in Temper.
    fitness_goal_ids: dict = {
        "latency":    "<temper-fitness-goal-id-for-latency>",    # minimize p95
        "throughput": "<temper-fitness-goal-id-for-throughput>",  # maximize msg/s
    }  # env: EVOLUTION_FITNESS_GOAL_IDS (JSON)

    # Path to EVOLUTION_GUIDE.md in the helix repo
    evolution_guide_path: str = "EVOLUTION_GUIDE.md"  # relative to helix repo root
```

### 3.2 `live_client.py`
HTTP client for the deploy-history UI API (`http://127.0.0.1:4100`).
Wraps cluster create/poll, migration create/poll, and breed operations.

Key methods:
```python
class LiveClient:
    def create_cluster(self, name: str, base: str = "champion-metrics", replicas: int = 1) -> str:
        """POST /api/clusters. Returns cluster_id. Uses replicas=1 for variant testing (cost)."""

    def submit_cloud_build_async(self, worktree: Path, image_tag: str) -> str:
        """
        Submit a Cloud Build for the given worktree WITHOUT waiting.
        Returns a build_job_id that can be polled.
        Calls gcloud builds submit --async, captures the build ID.
        Multiple calls can be in flight simultaneously (GCP handles parallelism).
        """

    def wait_all_builds(self, build_jobs: dict[int, str], timeout_minutes: int = 15):
        """Poll all build_jobs until all complete or timeout. Raises if any fail."""

    def wait_cluster_live(self, cluster_id: str, timeout_minutes: int = 5) -> bool:
        """Poll GET /api/clusters until status == 'Live' or timeout."""

    def wait_all_clusters_live(self, cluster_ids: dict[int, str], timeout_minutes: int = 5):
        """Poll all clusters simultaneously (asyncio or threading)."""

    def clone_workload_to_variant(self, source_cluster: str, target_cluster: str) -> list[str]:
        """
        Replicate the champion cluster's production workload to a variant cluster.
        Steps:
          1. GET /tdata/Producers?$filter=Cluster eq '{source_cluster}' and Status eq 'Running'
          2. GET /tdata/Consumers?$filter=Cluster eq '{source_cluster}' and Status eq 'Running'
          3. For each Producer entity: POST /tdata/Producers/Define with same params
             (queue, rate_per_sec, msg_size, concurrency, shape, shape_params) but cluster=target
             Then POST /tdata/Producers('{id}')/Start
          4. Same for each Consumer entity.
          5. Validate: POST /tdata/Queues?$filter=Cluster eq '{target_cluster}'
             → Assert all queue names start with 'shopping_store'. Raise if not.
        Returns: list of cloned Producer/Consumer entity IDs for teardown.
        This gives EXACT production traffic profiles (same shapes, rates, patterns) without
        synthetic load. Champion cluster keeps its originals running unaffected.
        """

    def teardown_cloned_workload(self, cloned_entity_ids: list[str]):
        """
        Stop all cloned Producer/Consumer entities after measurement.
        POST /tdata/Producers('{id}')/Stop for each producer ID.
        POST /tdata/Consumers('{id}')/Stop for each consumer ID.
        IDs are the entity IDs returned by clone_workload_to_variant().
        """

    def teardown_all(self, cluster_ids: dict[int, str]):
        """Teardown all clusters in parallel. Also stops their cloned workloads."""

    def list_clusters(self) -> list[dict]:
        """GET /api/clusters. Returns all clusters with name, status, id."""

    def teardown_cluster(self, cluster_id: str) -> bool:
        """POST /api/clusters/{id}/teardown."""
```

Note: cluster names must match `^[a-z][a-z0-9-]{1,18}[a-z0-9]$`.
Naming convention for variant clusters: `ev-{lineage_abbrev}-s{stage}-v{variant_num}`
e.g. `ev-thr-s1-v1`, `ev-lat-s1-v2` (10-12 chars, DNS-safe).

### 3.3 `fitness_live.py`
Reads Datadog metrics for a specific cluster over a completed traffic window.
Evaluates against the FitnessGoal target.

```python
@dataclass
class LiveFitnessResult:
    cluster_name: str
    metric: str
    direction: str          # "minimize" | "maximize"
    target: float
    measured: float         # actual observed value from DD
    perf_delta_pct: float   # % improvement vs baseline (positive = better)
    passed: bool            # met the FitnessGoal target
    raw_points: list[float]
    window_start_iso: str
    window_end_iso: str
    note: str               # human-readable summary

def measure_live_fitness(
    cluster_name: str,
    metric: str,           # "p95_latency" | "throughput"
    direction: str,
    target: float,
    baseline_value: float, # from the champion cluster, measured at stage start
    window_start_iso: str,
    window_end_iso: str,
    dd_source: DatadogMetricSource,
) -> LiveFitnessResult:
    """
    Metric mapping:
      p95_latency  → DD query: avg:helix.produce.latency_ms.95percentile{cluster:<name>}
      throughput   → DD query: sum:helix.produce.throughput{cluster:<name>}.as_rate()

    perf_delta_pct:
      minimize: (baseline - measured) / baseline * 100  (positive = improvement)
      maximize: (measured - baseline) / baseline * 100  (positive = improvement)
    
    passed: measured value meets FitnessGoal.target in the right direction.
    """
```

Note on DD cluster scoping: the Helix DogStatsD exporter (from `champion-metrics`
branch `metrics.rs`) should tag metrics with `cluster:<statefulset-name>` so we can
scope queries per variant. Verify this tag exists after first successful cluster deploy.
If not present yet, fall back to `*` scope and compare time windows (less accurate).

### 3.4 `stage_manager.py`
Manages the stage lifecycle end-to-end.

```python
@dataclass
class VariantRecord:
    """One tested variant within a stage."""
    variant_num: int
    cluster_name: str
    gene: str
    new_value: object
    rationale: str
    dst_passed: bool
    live_fitness: LiveFitnessResult | None   # None if culled at DST
    cull_reason: str | None                  # set if culled
    survived: bool                           # True = passed both DST + fitness
    commit_sha: str | None                   # git commit on evolve/<lineage>

@dataclass
class StageContext:
    lineage: str
    stage_num: int           # I, I+1, ...
    fitness_goal_id: str
    base_genome: Genome      # champion genome at start of this stage
    baseline_fitness: float  # DD-measured fitness of champion at stage start
    variants: list[VariantRecord]
    round_num: int           # how many full hypothesis passes completed
    tried_genes: set[str]    # genes mutated this stage (for deduplication within stage)

class StageManager:
    def __init__(self, config: LiveEvolutionConfig, breeder: BreederClient, supervisor: BreederClient):
        ...

    def measure_baseline(self, lineage: str, metric: str, dd_source) -> float:
        """Read current champion cluster's fitness from DD. Used at stage start."""

    def should_stop(self, ctx: StageContext) -> tuple[bool, str]:
        """
        Returns (should_stop, reason).
        Stops when:
          - ctx.variants count >= config.max_variants_per_stage, OR
          - ctx.round_num >= config.max_rounds_per_stage
        """

    def select_champion(self, ctx: StageContext) -> VariantRecord | None:
        """
        From all survived variants, pick the one with the best perf_delta_pct.
        Returns None if no variants survived (stage produced no improvement).
        """

    def record_stage_generations(self, ctx: StageContext):
        """
        Call FitnessGoal.RecordGeneration once per variant tried this stage.
        Uses the existing IOA action — no new field needed.
          generation = ctx.stage_num * 100 + variant_num  (unique across all stages)
          gene       = variant.change_description          (Claude Code's one-line summary)
          fitness    = variant.live_fitness.measured        (raw DD value)
          outcome    = "survived" | "culled"
        Stage history is then queryable via Breed + RecordGeneration at any time.
        The meta-LLM in propose_live.py reads this to build the history prompt.
        """

    def promote_champion(self, ctx: StageContext, champion: VariantRecord) -> bool:
        """
        Cedar-gated: merge evolve/<lineage> to champion-metrics.
        Steps:
          1. breeder creates + plans an ImprovementIssue for the promotion
          2. breeder-supervisor approves it (narrow auto-approve permit)
          3. git merge evolve/<lineage> champion-metrics
          4. git push origin champion-metrics
        Returns True if promoted.
        """

    def retire_stage_breeds(self, ctx: StageContext):
        """Retire all Breed entities from this stage (DB/Store reset for stage I+1)."""

    def advance_stage(self, ctx: StageContext, new_base_genome: Genome) -> StageContext:
        """
        Create stage I+1 context.
        - Increments stage_num
        - Resets variants list (empty store)
        - Updates base_genome to champion genome
        - Resets tried_genes
        """
```

### 3.5 `propose_live.py`
Generates a batch of candidate mutations using Claude Code as the mutation operator.
Replaces the narrow regex-based `propose_candidates` from `harness.py` with general
code changes guided by `EVOLUTION_GUIDE.md` and enriched by text feedback and
periodic meta-LLM recommendations (ShinkaEvolve-inspired).

```python
@dataclass
class TextFeedback:
    """Failure context passed to the next proposal round."""
    variant_num: int
    cull_stage: str           # "DST" | "fitness"
    message: str              # DST failure msg OR "measured X vs target Y, delta Z%"
    gene_hint: str | None     # what the failed variant was trying to change

def propose_batch(
    batch_size: int,                         # up to 4
    fitness_goal: FitnessGoalContext,        # metric, direction, target, lineage
    stage_history: list[VariantRecord],      # all variants tried this stage
    stage_summaries: str,                    # FitnessGoal.GenerationNotes (prior stages)
    text_feedbacks: list[TextFeedback],      # failure messages from recent variants
    evolution_guide: str,                    # contents of helix/EVOLUTION_GUIDE.md
    worktree_base: Path,                     # current tip of evolve/<lineage>
    anthropic_api_key: str,
) -> list[CodingAgentTask]:
    """
    Returns batch_size CodingAgentTasks, each containing:
      - A prompt for Claude Code
      - The worktree branch to run in

    Prompt construction:
      1. Context block: EVOLUTION_GUIDE.md + current genome state (git diff from champion-metrics)
      2. FitnessGoal block: metric, direction, target, what's been tried this stage
      3. Text feedback block: last N failure messages with their cull reasons
      4. [Every 3 variants] Meta-recommendation block (see below)
      5. Instruction: "Make ONE cohesive code change that you believe will improve
         {metric} in the {direction} direction. You may change any file listed as
         safe in EVOLUTION_GUIDE.md. Do not change files marked as INVARIANT.
         Explain your change in one sentence."

    Meta-recommendation (every 3 variants, i.e. variant_num % 3 == 0):
      Call Claude with:
        - Stage history summary (what was tried, DST pass/fail, fitness delta)
        - Stage summaries from prior stages (FitnessGoal.GenerationNotes)
        - FitnessGoal (metric/direction/target)
      Ask: "Based on this history, what high-level area of the codebase should
            the next mutation focus on? Give one concrete direction."
      Prepend the recommendation to the next batch's prompt.
    """
```

**Text feedback mechanic (from ShinkaEvolve `use_text_feedback`):**
```python
# DST failure:
feedback = TextFeedback(
    variant_num=record.variant_num,
    cull_stage="DST",
    message=record.dst_result.failure_message,  # "Durability violation: partition group-N..."
    gene_hint="sync_on_rotation or WAL path",
)

# Fitness failure:
feedback = TextFeedback(
    variant_num=record.variant_num,
    cull_stage="fitness",
    message=f"measured {record.live_fitness.measured:.1f} vs target {target:.1f}, "
            f"delta {record.live_fitness.perf_delta_pct:+.1f}%",
    gene_hint=f"tried changing: {record.live_fitness.note}",
)
```

**Meta-recommendation mechanic (from ShinkaEvolve `meta_rec_interval`):**
```python
# Fires every 3 variants (variant_num % 3 == 0)
META_PROMPT = """
You are analyzing the evolution history of a Helix Kafka-compatible server.
The fitness goal is: {metric} → {direction} to {target}.

Variants tried this stage:
{stage_history_summary}

Prior stage learnings:
{stage_summaries}

Based on this history, recommend ONE high-level area of the Helix codebase
where a code change is most likely to improve {metric}. Be specific about
which module or mechanism (e.g., "WAL batching in helix-wal", "Raft pipeline
depth in helix-raft", "batcher flush logic in helix-server"). In 2 sentences.
"""
```

### 3.6 `helix/EVOLUTION_GUIDE.md` (new file in the helix repo)

This file is the mutation operator's context. It scopes what Claude Code can change
and gives performance intuition so mutations are not random thrashing.

Content structure:
```markdown
# Helix Evolution Guide

## Codebase Map
| Crate | Role | Performance relevance |
|---|---|---|
| helix-server | Kafka broker, batcher, connection handling | HIGH — batcher flush logic is the primary latency lever |
| helix-raft | Raft consensus, log replication, pipelining | HIGH — inflight cap and batch size control replication throughput |
| helix-wal | Write-ahead log, fsync, durability | CRITICAL INVARIANT — do not skip fsync; DST will catch it |
| helix-core | Shared types, error handling | LOW |
| helix-flow | Back-pressure and flow control | MEDIUM |
| helix-routing | Topic/partition routing | LOW |
| helix-runtime | Async runtime, simulation substrate | DO NOT MODIFY |
| helix-tier | Storage tiering | LOW |

## Files Safe to Modify (EVOLVE-SAFE)
- helix-server/src/service/batcher.rs     ← linger, flush thresholds
- helix-server/src/service/mod.rs         ← connection handling
- helix-raft/src/lib.rs                   ← MAX_INFLIGHT, BATCH_SIZE, pipeline constants
- helix-raft/src/replication.rs           ← replication loop timing
- helix-flow/src/lib.rs                   ← back-pressure thresholds
- helix-wal/src/wal.rs                    ← SAFE: buffer sizes, rotation thresholds
                                            INVARIANT: never skip active.file.sync().await

## Files INVARIANT (do not modify)
- helix-runtime/**                        ← simulation substrate; changes break DST
- helix-core/src/error.rs                 ← protocol errors; changes break compatibility
- helix-server/src/main.rs                ← entrypoint; changes break Docker build

## Known Performance Levers (Code Mutations)
1. Batcher linger_ms (batcher.rs): lower=less latency, higher=more throughput
2. MAX_INFLIGHT_APPEND_ENTRIES (lib.rs): higher=better replication pipelining
3. APPEND_ENTRIES_BATCH_SIZE_MAX (lib.rs): higher=fewer round-trips per burst
4. WAL buffer size before rotation: larger=fewer syncs per second
5. Flow control thresholds (helix-flow): can reduce stalls under burst load

## Control Plane Mutations (also allowed)
Agents may ALSO propose topology changes alongside or instead of code changes.
These use the existing Temper IOA entities — no code required, just API calls.

Allowed control plane mutations:
- CREATE new queues (via Queue.Define + Queue.Activate) — MUST use shopping_store prefix
  e.g. split `shopping_store` into `shopping_store_priority` + `shopping_store_bulk`
- REQUEST migrations of producers/consumers to new queues (via Migration.Request)
  e.g. move bursty producers to a dedicated `shopping_store_burst` queue so the
  base queue can be tuned for stable throughput
- ADJUST producer parameters (if existing producers are too aggressive/too gentle)

NOT allowed:
- Renaming or deleting existing queues in the shopping_store namespace
- Creating queues without the shopping_store prefix
- Modifying INVARIANT source files (see above)

## Queue Naming Invariant
ALL queues on a variant cluster MUST have the `shopping_store` prefix.
The live_loop validates this after clone_workload_to_variant() before measurement.
Mutations that attempt to create non-compliant queues are rejected (DST-equivalent gate).

## Safety Gate
All code mutations MUST pass: cargo test -p helix-tests (DST durability tests)
The test `test_dst_shared_wal_basic_durability` is the sentinel for WAL safety.
Control plane mutations do not need to pass DST (they don't change code).
```

This file is committed to `champion-metrics` and stays in sync as the codebase evolves.
It gets updated whenever a new module becomes relevant (e.g., if Symphony adds
compression, add that file to EVOLVE-SAFE with a performance note).

---

### 3.7 `live_loop.py`
Main entry point for the live evolution loop. Wires all components together.

**Startup — resume from last committed state:**
```python
def startup_resume(config, live_client, temper) -> StageContext:
    """
    1. Teardown any orphaned ev-* clusters (left by prior run):
       list all Cluster entities with name matching ev-{lineage}*, status != Torndown
       → live_client.teardown_cluster() each one
    
    2. Read current stage from Temper:
       FitnessGoal.Generation → stage_num
       FitnessGoal.GenerationNotes → stage_summaries (prior stage history)
    
    3. Read current genome from git:
       git checkout champion-metrics → read_from_source(helix_repo) → base_genome
       (champion-metrics IS the base genome — no separate state file needed)
    
    4. Read variants tried this stage from Temper:
       Breed entities with status != Retired → rebuild stage_history
       (If all retired → fresh stage, empty history)
    
    5. Return StageContext ready to continue where it left off.
    """
```

**One parallel batch:**
```python
def run_batch(
    batch_candidates: list[CodingAgentTask],  # up to 4
    ctx: StageContext,
    config: LiveEvolutionConfig,
    live_client: LiveClient,
    dd_source: DatadogMetricSource,
) -> list[VariantRecord]:
    
    # Phase 1: DST cull each candidate sequentially (cargo test, ~5 min each)
    # DST is sequential because it uses the same helix-tests crate; parallel
    # cargo test runs on the same machine would contend.
    # Alternative: DST in parallel on separate worktrees if build times allow.
    dst_results = [verify_durability(task.worktree) for task in batch_candidates]
    
    # Phase 2: Cloud Build — fire ALL that passed DST simultaneously (async)
    build_jobs = {}
    for task, dst in zip(batch_candidates, dst_results):
        if dst.passed:
            job_id = live_client.submit_cloud_build_async(task.worktree, task.image_tag)
            build_jobs[task.variant_num] = job_id
        else:
            # Record DST cull immediately
            record_dst_cull(task, dst)
    
    # Wait for all builds to complete (~10 min, all in parallel)
    live_client.wait_all_builds(build_jobs, timeout_minutes=15)
    
    # Phase 3: Deploy all built clusters simultaneously
    cluster_ids = {}
    for variant_num, job_id in build_jobs.items():
        if live_client.build_succeeded(job_id):
            cluster_name = f"ev-{lineage[:3]}-s{ctx.stage_num}-v{variant_num}"
            cluster_ids[variant_num] = live_client.deploy_cluster(cluster_name)
    live_client.wait_all_clusters_live(cluster_ids, timeout_minutes=5)
    
    # Phase 4: Clone production workload to all variant clusters simultaneously
    # Each variant gets the same queue/producer/consumer configuration as the champion.
    # This is exact production-profile traffic, not synthetic load.
    cloned_workload_ids = {}  # variant_num → list[entity_ids]
    window_start = datetime.utcnow()
    for variant_num, cluster_id in cluster_ids.items():
        cluster_name = f"ev-{lineage[:3]}-s{ctx.stage_num}-v{variant_num}"
        cloned = live_client.clone_workload_to_variant(
            source_cluster=config.workload_source_cluster,  # champion/baseline cluster
            target_cluster=cluster_name,
        )
        cloned_workload_ids[variant_num] = cloned
    
    # Phase 5: Wait observation window (champion + ALL variant clusters running simultaneously)
    # Champion cluster continues running its ORIGINAL workload throughout — no traffic
    # is diverted. Comparison is: variant performance on cloned load vs champion on original.
    print(f"  Observing {len(cluster_ids)} variant clusters for {config.traffic_window_minutes} min...")
    time.sleep(config.traffic_window_minutes * 60)
    window_end = datetime.utcnow()
    
    # Phase 6: Measure fitness for each cluster from DD (per cluster tag)
    records = []
    for variant_num, cluster_id in cluster_ids.items():
        cluster_name = f"ev-{lineage[:3]}-s{ctx.stage_num}-v{variant_num}"
        fitness = measure_live_fitness(cluster_name, ..., window_start, window_end, dd_source)
        records.append(VariantRecord(..., live_fitness=fitness, survived=fitness.passed))
    
    # Phase 7: Stop cloned workloads and teardown all clusters in parallel
    for variant_num, entity_ids in cloned_workload_ids.items():
        live_client.teardown_cloned_workload(entity_ids)
    live_client.teardown_all(cluster_ids)
    
    return records

# Batch timing (4 variants, 3-min window):
#   DST (sequential): 4 × 5 min = 20 min  [can be parallelized later]
#   Cloud Build (parallel): 10 min
#   Deploy + load start: 3 min
#   Observation: 3 min
#   Teardown: 2 min
#   TOTAL: ~38 min for 4 variants
#
# If DST parallelized (separate worktrees): ~20 min for 4 variants
# Target: 3 batches/stage = ~1 hour
```

**One stage:**
```python
def run_stage(ctx: StageContext, config, ...) -> StageContext:
    text_feedbacks = []
    while True:
        stop, reason = stage_mgr.should_stop(ctx)
        if stop:
            print(f"  Stage {ctx.stage_num} stopping: {reason}")
            break
        
        # Propose next batch (up to config.batch_size, default 4)
        batch = propose_batch(
            batch_size=min(config.batch_size, config.max_variants_per_stage - len(ctx.variants)),
            fitness_goal=ctx.fitness_goal,
            stage_history=ctx.variants,
            stage_summaries=temper.get_generation_notes(ctx.fitness_goal_id),
            text_feedbacks=text_feedbacks,
            evolution_guide=read_evolution_guide(helix_repo),
            ...
        )
        
        records = run_batch(batch, ctx, config, live_client, dd_source)
        ctx.variants.extend(records)
        
        # Collect text feedback for next round
        for r in records:
            if not r.survived:
                text_feedbacks.append(make_text_feedback(r))
                print(f"  Variant {r.variant_num} culled: {r.cull_reason}")
            else:
                print(f"  Variant {r.variant_num} SURVIVED: {r.live_fitness.perf_delta_pct:+.1f}%")
    
    return ctx
```

**Stage transition:**
```python
def run_stage_transition(ctx: StageContext, config, ...) -> StageContext | None:
    champion = stage_mgr.select_champion(ctx)
    stage_mgr.write_stage_summary(ctx, champion)
    stage_mgr.retire_stage_breeds(ctx)
    
    if champion is None:
        print(f"  Stage {ctx.stage_num}: no improvement found. Stopping.")
        return None
    
    print(f"  Stage {ctx.stage_num} champion: {champion.change_description} "
          f"({champion.live_fitness.perf_delta_pct:+.1f}%)")
    
    # Promote champion (Cedar-gated)
    stage_mgr.promote_champion(ctx, champion)
    
    # Cooldown
    print(f"  Cooling down {config.champion_cooldown_minutes} min for post-merge telemetry...")
    time.sleep(config.champion_cooldown_minutes * 60)
    
    # Advance to stage I+1
    new_genome = read_from_source(helix_repo)  # read from now-updated champion-metrics
    return stage_mgr.advance_stage(ctx, new_base_genome=new_genome)
```

---

## 4. Required Changes to Existing Files

### 4.1 `reference-apps/deploy-history/app.py` — cluster creation from a git ref

Currently `_create_worktree(name, base)` creates the worktree off `base` (a branch name).
The live loop needs to create a worktree off a specific **commit SHA** (the tip of
`evolve/<lineage>` after applying the mutation). The `base` parameter already supports
any git ref, so the cluster creation API just needs to accept a commit SHA as `base`.

**Change:** In `POST /api/clusters`, allow `base` to be a full commit SHA (40 chars)
in addition to a branch name. The `git_ref_exists` check already handles this via
`git rev-parse`.

No other changes needed in app.py for the cluster path.

### 4.2 `reference-apps/deploy-history/app.py` + `static/app.js` — UI additions

The live loop drives `app.py` over HTTP, so the core evolution loop works without UI changes.
However, three additions improve operability:

#### 4.2a Workload cloning via Temper OData (no new app.py endpoints needed)

Production workload cloning (`clone_workload_to_variant`) goes directly through the
Temper OData API — `live_client.py` calls Temper to query and re-create Producer/Consumer
entities. `app.py` is NOT involved in this path. No new load endpoints are needed.

#### 4.2b New API endpoint: async Cloud Build submit

Currently `_build_cluster_image()` in `app.py` blocks until the build completes.
The parallel batch runner needs to submit N builds without waiting. Add:

```python
@app.post("/api/builds/submit")
async def submit_build_async(body: dict):
    """
    body: { "worktree_path": "...", "image_tag": "ev-thr-s1-v1-abc123" }
    Fires gcloud builds submit --async.
    Returns: { "build_id": "<gcb-build-id>" }
    """

@app.get("/api/builds/{build_id}/status")
async def get_build_status(build_id: str):
    """
    Polls gcloud builds describe {build_id}.
    Returns: { "status": "WORKING" | "SUCCESS" | "FAILURE", "image_tag": "..." }
    """
```

#### 4.2c `static/app.js` — evolution progress panel (optional, nice-to-have)

A read-only panel that shows the live loop state for observability. Not required for the
loop to function — `live_loop.py` prints progress to stdout. Add only if time permits.

```
┌─ Evolution Loop ──────────────────────────────────────┐
│ Lineage: throughput   Stage: 2   Variants tried: 7/12 │
│                                                        │
│ Current batch (4 parallel):                            │
│   ev-thr-s2-v5  ████████░░░░  Building (7 min)        │
│   ev-thr-s2-v6  ██████████░░  Live, observing (1 min) │
│   ev-thr-s2-v7  ████████████  Done ✓ +3.2%            │
│   ev-thr-s2-v8  ████░░░░░░░░  DST running             │
│                                                        │
│ Best so far: v3 (+5.1%)   Stage champion: pending      │
└────────────────────────────────────────────────────────┘
```

Implementation: poll a new `GET /api/evolution/status` endpoint every 5 seconds.
`live_loop.py` writes current state to a local `evolution_status.json` that app.py serves.

#### Summary of required vs optional UI changes

| Change | Required | File |
|---|---|---|
| Allow SHA as cluster `base` (§4.1) | **Required** | `app.py` |
| `POST /api/builds/submit` (async Cloud Build) | **Required** | `app.py` |
| `GET /api/builds/{id}/status` | **Required** | `app.py` |
| `GET /api/evolution/status` (serves status JSON from `live_loop.py`) | **Required** | `app.py` |
| Evolution progress panel (polls `/api/evolution/status` every 5s) | **Required** | `app.js` |
| Workload cloning (clone Producer/Consumer entities) | via Temper OData directly | `live_client.py` only |

---

### 4.3 `reference-apps/evolution/harness.py` — keep local bench path intact

The local bench path (`main.py --lineages latency,throughput`) should continue to work
unchanged. The live loop is a separate entry point (`live_loop.py`). Do NOT modify
`harness.py`, `bench.py`, or `main.py`.

### 4.4 `reference-apps/identity/dark-factory-specs/fitness_goal.ioa.toml` — no changes needed

The existing `RecordGeneration(generation, gene, fitness, outcome)` action already provides
per-variant history. No new field is needed. See §12.2 (resolved).

---

## 5. DD Metric Scoping and Isolation

### 5.1 How isolation works — cluster tag, not queue name

The champion cluster and all variant clusters use **the same queue names** (all starting
with `shopping_store`). This is intentional and correct. Metric isolation is provided
entirely by the `cluster:` DogStatsD tag emitted by each Helix StatefulSet — not by
queue name.

```
Champion cluster "helix":
  Broker at helix-helix.dark-factory.svc.cluster.local:9092
  Topics: shopping_store, shopping_store_checkout, ...
  DogStatsD tag: cluster:helix
  DD metric: avg:helix.produce.latency_ms.95percentile{cluster:helix}

Variant cluster "ev-thr-s1-v1":
  Broker at helix-ev-thr-s1-v1.dark-factory.svc.cluster.local:9092
  Topics: shopping_store, shopping_store_checkout, ...  ← SAME NAMES, separate broker
  DogStatsD tag: cluster:ev-thr-s1-v1
  DD metric: avg:helix.produce.latency_ms.95percentile{cluster:ev-thr-s1-v1}
```

These two metric streams never aggregate together. Each Helix StatefulSet emits metrics
only for its own broker's topics, tagged with its own cluster name. A DD query for
`{cluster:ev-thr-s1-v1}` returns ONLY that variant's data. The `shopping_store` queue
name appears in both clusters but the `cluster:` tag fully disambiguates them.

**The `shopping_store` prefix invariant is NOT for metric isolation** (the cluster tag
handles that). It is for organizational coherence: ensures agents cannot create off-topic
queues that escape the workload namespace, and enables Phase 2 per-sub-queue routing
(see §11).

### 5.2 DD queries used by `fitness_live.py`

```
# p95 latency for a specific variant (minimize)
avg:helix.produce.latency_ms.95percentile{cluster:ev-thr-s1-v1}

# p95 latency for champion/baseline (for delta computation)
avg:helix.produce.latency_ms.95percentile{cluster:helix}

# Throughput for a specific variant (maximize)
sum:helix.produce.throughput{cluster:ev-thr-s1-v1}.as_rate()
```

All queries scope by `cluster:` tag only. Queue name is never part of the DD filter.
This aggregates across ALL queues on the cluster (including sub-queues), which is correct:
a variant that improves by splitting the workload should be credited for the full cluster
improvement, not just the sub-queue it split.

### 5.3 What `fitness_live.py` computes

```python
# For each variant cluster during the observation window:
variant_p95  = dd_query(f"avg:helix.produce.latency_ms.95percentile{{cluster:{variant}}}")
champion_p95 = dd_query(f"avg:helix.produce.latency_ms.95percentile{{cluster:helix}}")

# Delta: positive means improvement (lower latency)
perf_delta_pct = (champion_p95 - variant_p95) / champion_p95 * 100

# Passed if delta >= FitnessGoal.target improvement threshold
passed = perf_delta_pct >= goal_target_pct
```

Champion and variant are queried over the **same time window** (both running simultaneously
during the observation period). No time-shift correction needed.

### 5.4 Validation step after first cluster deploy

```bash
# Verify cluster: tag is being emitted by metrics.rs
curl "https://api.datadoghq.com/api/v1/query" \
  -d "from=$(date -v-5M +%s)&to=$(date +%s)&query=avg:helix.produce.latency_ms.95percentile{cluster:ev-thr-s1-v1}" \
  -H "DD-API-KEY: $DD_API_KEY" -H "DD-APPLICATION-KEY: $DD_APP_KEY"
# Expected: non-empty series. If empty: metrics.rs cluster tag is missing → fix before loop runs.
```

**Fallback** (if `cluster:` tag is absent): scope by time window only — compare the
variant's window against the champion's prior window. Less accurate (background noise)
but functional. Enable via `--no-cluster-tag` flag on `live_loop.py`.

---

## 6. Cluster Naming Convention

Constraint: `^[a-z][a-z0-9-]{1,18}[a-z0-9]$` (3–20 chars)

| Lineage | Stage | Variant | Name | Length |
|---|---|---|---|---|
| throughput | 1 | 1 | `ev-thr-s1-v1` | 13 |
| latency | 1 | 1 | `ev-lat-s1-v1` | 13 |
| throughput | 2 | 3 | `ev-thr-s2-v3` | 13 |

All pass the DNS slug regex.

---

## 7. Running the Live Loop

```bash
cd /Users/pranav.garg/go/src/github.com/DataDog/temper/reference-apps/evolution

# Prerequisites: Temper server (3000) + deploy-history UI (4100) running
# DD_API_KEY, DD_APP_KEY, ANTHROPIC_API_KEY in environment

# Testing (batch=4, 3-min window, 5-min cooldown, ~1 hour per stage)
# Observer automatically picks which fitness objective (latency or throughput) to optimize.
python3 live_loop.py \
  --stages 2 \
  --cooldown-minutes 5 \
  --traffic-window-minutes 3 \
  --batch-size 4 \
  --max-variants 12 \
  --workload-source helix        # champion cluster to clone Producer/Consumer entities from

# Debug/sequential (batch=1, trace one variant at a time)
python3 live_loop.py \
  --stages 1 \
  --cooldown-minutes 5 \
  --traffic-window-minutes 3 \
  --batch-size 1 \
  --max-variants 3

# Production (batch=4, 10-min window, 60-min cooldown)
python3 live_loop.py \
  --stages 5 \
  --cooldown-minutes 60 \
  --traffic-window-minutes 10 \
  --batch-size 4 \
  --max-variants 12 \
  --workload-source helix

# Resume after crash (same args as before — startup_resume() reads state from git + Temper)
python3 live_loop.py --stages 2 --workload-source helix

# Dry run (no Cloud Build / GKE; DD snapshot for fitness; useful for testing the Observer logic)
python3 live_loop.py --dry-run --snapshot snapshots/helix_live_2026-05-24.json
```

---

## 8. Persistence and Restart

**No snapshot file needed.** Git + Temper together are the complete durable state.

| State | Where it lives | How to read on restart |
|---|---|---|
| Current champion genome | `champion-metrics` branch tip | `git checkout champion-metrics && read_from_source(repo)` |
| Current stage number | `FitnessGoal.generations_run` counter | `temper_get("/tdata/FitnessGoals('{id}')")` |
| Stage + variant history | `Breed` entities + `RecordGeneration` records | Query Temper; meta-LLM uses this to build history prompt |
| Variants tried this stage | `Breed` entities (non-Retired) | `temper_get("/tdata/Breeds?$filter=Status ne 'Retired'")` |
| Mutation code changes | `evolve/<lineage>` branch commits | `git log evolve/<lineage>` |

**Startup cleanup** (always runs before resuming):
```python
# Tear down any ev-* clusters left by prior run
orphaned = [c for c in live_client.list_clusters()
            if c["name"].startswith(f"ev-{lineage_abbrev}")
            and c["status"] not in ("Torndown", "Failed")]
for c in orphaned:
    print(f"  [startup] tearing down orphaned cluster {c['name']}")
    live_client.teardown_cluster(c["id"])
```

**What you lose on hard stop** (acceptable):
- In-flight Cloud Builds (GCP cancels them on timeout; orphaned clusters cleaned on restart)
- The 3-min observation window for running variants (those variants are simply not recorded)

**What you never lose:**
- Committed mutations on `evolve/<lineage>` (git)
- Stage summaries (Temper)
- Promoted champions on `champion-metrics` (git + pushed to remote)

---

## 9. Files Summary

| File | Action | Notes |
|---|---|---|
| `reference-apps/evolution/config.py` | **CREATE** | All configurable params incl. `batch_size=4`, `traffic_window_minutes=3` |
| `reference-apps/evolution/live_client.py` | **CREATE** | UI API client; async Cloud Build submit; parallel cluster ops |
| `reference-apps/evolution/fitness_live.py` | **CREATE** | DD-based per-cluster fitness measurement |
| `reference-apps/evolution/propose_live.py` | **CREATE** | Batch proposal via Claude Code + meta-LLM + text feedback |
| `reference-apps/evolution/stage_manager.py` | **CREATE** | Stage lifecycle, startup resume, stopping criteria, champion promotion |
| `reference-apps/evolution/live_loop.py` | **CREATE** | Main entry point; parallel batch runner |
| `helix/EVOLUTION_GUIDE.md` | **CREATE** | Codebase map for Claude Code mutation agent (committed to champion-metrics) |
| `reference-apps/deploy-history/app.py` | **MODIFY** | 4 changes: (1) SHA as cluster `base`, (2) `POST /builds/submit` (async), (3) `GET /builds/{id}/status`, (4) `GET /api/evolution/status` (serves status JSON for UI panel) |
| `reference-apps/deploy-history/static/app.js` | **MODIFY (required)** | Evolution progress panel; polls `GET /api/evolution/status` every 5s |
| `reference-apps/identity/dark-factory-specs/fitness_goal.ioa.toml` | **NO CHANGE** | `RecordGeneration` already exists; no new field needed (§10.2 resolved) |
| `reference-apps/evolution/harness.py` | **NO CHANGE** | Local bench path stays intact |
| `reference-apps/evolution/main.py` | **NO CHANGE** | Local bench entry point stays intact |

---

## 10. Lineage — What It Means and Whether We Still Need It

**What "lineage" means:** A lineage is a named optimization campaign with its own
`FitnessGoal` entity in Temper. Each `FitnessGoal` has an `intent`, a `metric` to move
(e.g. `helix.produce.latency_ms.95percentile`), a `direction` (`minimize`), and a
`target` value. The `lineage_id` field links all stages of the campaign together.

The cluster naming convention (`ev-thr-s1-v1`) uses a lineage abbreviation (`thr`) only
for human readability — so you can tell at a glance which clusters are optimizing which
axis.

**Do we still need it?** Yes, but the selection is now Observer-driven, not hardcoded:
- Two FitnessGoals are pre-created: `latency` and `throughput`
- At stage start, Observer reads live DD → picks whichever goal is furthest from its target
- If p95 latency is 120ms vs target 80ms: Observer activates the `latency` FitnessGoal
- If throughput is 85k vs target 100k msg/s: Observer activates `throughput`
- If both are off: Observer picks the one with the larger % gap
- You can also manually override with `--force-lineage latency` for testing

**What happens if a mutation helps one axis but hurts the other?**
We measure only the active FitnessGoal's metric. The Research Agent's EVOLUTION_GUIDE.md
context tells it which axis is being optimized. A future extension: multi-objective fitness
(Pareto front) — but for now, single-axis per stage is correct.

---

## 11. Specialized Traffic and Per-Queue Variant Routing

The user asked: *can mutations specialize for different traffic patterns, with different
variants handling different sub-queues, each code path specialized for a different load shape?*

**Phase 1 (initial implementation): All variants serve the full cloned workload.**
This is what is described in §3.7. All queues from the champion cluster are cloned to each
variant cluster. Fitness is cluster-wide. This is apples-to-apples.

**Phase 2 (future extension): Sub-queue specialization.**
When agents start proposing queue-topology mutations (splitting `shopping_store` into
`shopping_store_priority` + `shopping_store_bulk`), the fitness model becomes richer:

```
Variant A: code tuned for low-latency → assigned shopping_store_priority traffic
Variant B: code tuned for throughput  → assigned shopping_store_bulk traffic
Champion:  current code                → runs all shopping_store traffic baseline

Test:
  1. Clone all shopping_store* queues to BOTH variant clusters
  2. On variant A cluster: additionally route shopping_store_priority producers
     (via Migration entity: move those specific producers from champion to variant A)
  3. On variant B cluster: route shopping_store_bulk producers similarly
  4. Measure per cluster: variant A scored on p95 for shopping_store_priority workload,
     variant B scored on throughput for shopping_store_bulk workload
  5. If BOTH variants win on their respective axes → promote BOTH to champion:
     merge A's diff + B's diff together → new champion handles both queues better
```

This is "speciation" in the evolutionary biology sense — the system discovers that one
code path cannot be optimal for all traffic patterns, and evolves specialized variants.
The `Migration` IOA entity is already designed for exactly this (see `migration.ioa.toml`).

**Constraint for Phase 2:** All new queues must still start with `shopping_store`.
This prevents unconstrained topology sprawl and ensures the Observer's DD queries
(scoped to `cluster:ev-*`) still capture the full workload.

**Implementation order:** Start with Phase 1 (clone full workload, cluster-wide fitness).
Introduce Phase 2 once agents begin proposing queue-split mutations that DST + Phase 1
fitness cannot adequately evaluate.

---

## 12. Open Questions to Resolve Before Implementation

1. **DD cluster tag**: Does `metrics.rs` on `champion-metrics` emit a `cluster:` tag?
   Must verify after first successful cluster deploy. (§5)

2. **FitnessGoal.GenerationNotes**: ✅ RESOLVED — no new field is needed.
   The existing `FitnessGoal.RecordGeneration(generation, gene, fitness, outcome)` action
   records per-variant outcomes already. The `gene` param is repurposed to hold the
   Claude Code `change_description`. Stage summaries for the meta-LLM are built at
   proposal time by querying:
   - `GET /tdata/FitnessGoals('{id}')/Generations` (if navigation property exists)
   - OR `GET /tdata/Breeds?$filter=Stage eq {n}` to get this stage's variants
   No spec changes needed. The plan no longer references `GenerationNotes`.

3. **DST parallelization**: DST (`cargo test -p helix-tests`) currently runs sequentially
   to avoid machine resource contention. To reduce the batch wall-clock time from ~38 min
   to ~20 min, run each variant's DST in a separate helix worktree on a CI worker or
   separate machine. If running on one machine, run sequentially (the current plan).

4. **Workload cloning — Producer/Consumer query navigation**: Verify that the Temper
   OData endpoint supports filtering by cluster: `GET /tdata/Producers?$filter=Cluster eq 'helix'`.
   If OData $filter is not supported for this field, alternative: read the full list and
   filter client-side. Also verify that `POST /tdata/Producers/Define` + `Start` via live_client
   creates the k8s Deployments as expected (the IOA executor handles this per the spec).

5. **Probe queue**: ✅ RESOLVED — base queue is `shopping_store`.

   **Important caveat:** agents may split the workload into sub-queues over time
   (e.g. `shopping_store_checkout`, `shopping_store_inventory`, ...). The fitness
   measurement must therefore aggregate across **all queues on the variant cluster**,
   not just `shopping_store`.

   **Implementation approach:** scope DD queries by cluster, not by queue:
   ```
   avg:helix.produce.latency_ms.95percentile{cluster:ev-thr-s1-v1}   ← all queues on the cluster
   sum:helix.produce.throughput{cluster:ev-thr-s1-v1}.as_rate()
   ```
   This is cluster-wide aggregation — it naturally includes every queue (and sub-queue)
   running on `helix-<variant>`, regardless of how the workload was split. No need to
   enumerate individual queues or change the measurement code when splits happen.

   The `--probe-queue` CLI flag therefore becomes `--probe-workload shopping_store`
   (used only for the migration step — which queues to redirect — not for DD queries).
   The migration step should discover all queues whose name starts with `shopping_store`
   and migrate all of them to the variant cluster, not just the root queue:
   ```python
   # In live_client.py migrate_queue():
   queues_to_migrate = [q for q in list_active_queues() if q.startswith(probe_workload)]
   for q in queues_to_migrate:
       migrate_queue(q, to_cluster)
   ```

4. **Champion-metrics push auth**: ✅ RESOLVED — push access confirmed to
   `arun-pathiban-ddog/helix` `champion-metrics`.

   The remote is already registered in the local helix repo as `arun`:
   ```bash
   git -C /Users/pranav.garg/go/src/github.com/DataDog/helix remote -v
   # arun  https://github.com/arun-pathiban-ddog/helix.git (fetch/push)
   ```

   The promote step in `stage_manager.py` should:
   ```python
   # Merge winning evolve/<lineage> branch into champion-metrics
   git("checkout", "champion-metrics")
   git("merge", "--no-ff", f"evolve/{lineage}", "-m",
       f"champion({lineage} stage {stage_num}): {champion.change_description} "
       f"({champion.live_fitness.perf_delta_pct:+.1f}%)")
   git("push", "arun", "champion-metrics")
   ```
