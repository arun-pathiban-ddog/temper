"""Live evolution loop configuration (ADR-0095 live extension).

All parameters are overridable via environment variable or CLI flag.
See live_loop.py --help for the full CLI.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

HELIX_REPO = Path("/Users/arun.parthiban/notdd/helix")
EVOLUTION_WORKTREES = Path("/Users/arun.parthiban/notdd/evolution-worktrees")
TOKENS_PATH = Path(__file__).resolve().parents[1] / "identity" / "tokens.json"
DD_ENV_PATH = Path(__file__).resolve().parents[0].parent / "deploy-history" / ".dd-env.json"
EVOLUTION_STATUS_PATH = Path("/tmp/evolution_status.json")


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, str(default)))


def _env_float(key: str, default: float) -> float:
    return float(os.environ.get(key, str(default)))


def load_dd_env() -> dict:
    """Load Datadog + Anthropic credentials from .dd-env.json."""
    try:
        return json.loads(DD_ENV_PATH.read_text())
    except Exception:
        return {}


@dataclass
class LiveEvolutionConfig:
    # ── Stage stopping criteria ─────────────────────────────────────────────
    max_variants_per_stage: int = field(
        default_factory=lambda: _env_int("EVOLUTION_MAX_VARIANTS", 12)
    )
    max_rounds_per_stage: int = field(
        default_factory=lambda: _env_int("EVOLUTION_MAX_ROUNDS", 3)
    )

    # ── Parallelism ─────────────────────────────────────────────────────────
    # Number of variants built/deployed/measured simultaneously per batch.
    # Set to 1 for sequential (debug) mode.
    batch_size: int = field(
        default_factory=lambda: _env_int("EVOLUTION_BATCH_SIZE", 4)
    )

    # ── Traffic / measurement ───────────────────────────────────────────────
    # How long to observe all variant clusters simultaneously after deploy.
    traffic_window_minutes: int = field(
        default_factory=lambda: _env_int("EVOLUTION_TRAFFIC_WINDOW_MINUTES", 3)
    )

    # ── Post-merge cooldown ─────────────────────────────────────────────────
    # How long to wait after promoting a champion before starting stage I+1.
    champion_cooldown_minutes: int = field(
        default_factory=lambda: _env_int("EVOLUTION_COOLDOWN_MINUTES", 5)
    )

    # ── Workload cloning ────────────────────────────────────────────────────
    # Source cluster whose Producer/Consumer Temper entities are cloned to
    # each variant. The baseline champion is always "helix".
    workload_source_cluster: str = field(
        default_factory=lambda: _env("EVOLUTION_WORKLOAD_SOURCE", "helix-pranav-exp")
    )
    # All queues on variant clusters must start with this prefix.
    queue_prefix_invariant: str = field(
        default_factory=lambda: _env("EVOLUTION_QUEUE_PREFIX", "shopping_store")
    )

    # ── Fitness goals (pre-created in Temper) ───────────────────────────────
    # Maps lineage name → Temper FitnessGoal entity ID.
    # The Observer picks which goal is active at stage start.
    fitness_goal_ids: dict = field(default_factory=lambda: {
        "latency":    "goal-live-live-latency",
        "throughput": "goal-live-live-throughput",
    })

    # ── Proposer ────────────────────────────────────────────────────────────
    # Fire the meta-LLM direction call every N variants within a stage.
    meta_rec_interval: int = field(
        default_factory=lambda: _env_int("EVOLUTION_META_REC_INTERVAL", 3)
    )

    # ── Agent model configuration ────────────────────────────────────────────
    # Each agent role can be pointed at a different Anthropic model.
    # Override via environment variable or by editing these defaults.
    #
    # coding_agent   — proposes code mutations (needs strong reasoning)
    # meta_rec       — high-level direction hints between batches (cheap, fast)
    # observer       — selects active lineage from telemetry (light reasoning)
    # spec_updater   — rewrites EVOLUTION_GUIDE.md after champion promotion
    # coding_agent   — implements a hypothesis into a concrete code mutation
    model_coding_agent: str = field(
        default_factory=lambda: _env("EVOLUTION_MODEL_CODING", "claude-opus-4-6")
    )
    # research_agent — deep-thinking plan-mode agent that produces hypotheses
    model_research_agent: str = field(
        default_factory=lambda: _env("EVOLUTION_MODEL_RESEARCH", "claude-opus-4-6")
    )
    # meta_rec       — high-level direction hints between batches (cheap, fast)
    model_meta_rec: str = field(
        default_factory=lambda: _env("EVOLUTION_MODEL_META_REC", "claude-haiku-4-6")
    )
    # observer       — selects active lineage from telemetry
    model_observer: str = field(
        default_factory=lambda: _env("EVOLUTION_MODEL_OBSERVER", "claude-haiku-4-6")
    )
    # spec_updater   — rewrites EVOLUTION_GUIDE.md after champion promotion
    model_spec_updater: str = field(
        default_factory=lambda: _env("EVOLUTION_MODEL_SPEC_UPDATER", "claude-sonnet-4-6")
    )
    # failure_classifier — determines if a failed variant's fault is in the
    #                      hypothesis or the code implementation (cheap)
    model_failure_classifier: str = field(
        default_factory=lambda: _env("EVOLUTION_MODEL_CLASSIFIER", "claude-haiku-4-6")
    )

    # ── Variant refinement ──────────────────────────────────────────────────
    # How many times a variant may be refined after failing fitness.
    # 0 = no refinement (classic), 1 = one retry, 2 = deeper search (default).
    # Each refinement round triggers a full build→deploy→measure cycle.
    max_refinements: int = field(
        default_factory=lambda: _env_int("EVOLUTION_MAX_REFINEMENTS", 2)
    )

    # ── Datadog metric mapping ──────────────────────────────────────────────
    # DD metric names (histogram type |h — agent computes .95percentile etc.)
    DD_LATENCY_METRIC: str = "helix.produce.latency_ms.95percentile"
    DD_THROUGHPUT_METRIC: str = "helix.produce.latency_ms.count"

    # DogStatsD cluster tag: `cluster:helix-<cluster_name>` (from _gen_k8s_yaml).
    # Champion cluster: cluster_id is set to "helix" by default in main.rs,
    # but when deployed by app.py's _gen_k8s_yaml, it's "helix-helix".
    # Confirm the exact value after first deploy via DD Metrics Explorer.
    champion_cluster_dd_tag: str = field(
        default_factory=lambda: _env("EVOLUTION_CHAMPION_DD_TAG", "helix-cluster-001")
    )

    # ── Champion auto-redeploy ──────────────────────────────────────────────
    # After a stage promotes a new champion, optionally rebuild the helix
    # image from the merged champion-metrics-pranav-clone tip and roll the
    # *baseline* k8s cluster forward. Without this, the running champion
    # cluster lags behind the source branch and baseline metrics never reflect
    # accumulated improvements.
    champion_cluster_name: str = field(
        default_factory=lambda: _env("EVOLUTION_CHAMPION_CLUSTER", "cluster-001")
    )
    auto_redeploy_champion: bool = field(
        default_factory=lambda: _env("EVOLUTION_AUTO_REDEPLOY", "1") not in ("0", "false", "no")
    )
    # GCP / k8s constants (mirrored from deploy-history/app.py).
    gcp_project: str = "datadog-sandbox"
    namespace: str = "dark-factory"
    image_repo: str = "us-west3-docker.pkg.dev/datadog-sandbox/dark-factory-helix/helix-server"
    cloudbuild_config: str = "/tmp/helix-cloudbuild.yaml"
    # Pin the GKE context so redeploys don't accidentally run against whatever
    # context the operator's shell happens to be on (e.g. gizmo staging).
    # Matches KUBE_CONTEXT in deploy-history/app.py.
    kube_context: str = field(
        default_factory=lambda: _env(
            "EVOLUTION_KUBE_CONTEXT",
            "gke_datadog-sandbox_us-west3_gs-us-west3",
        )
    )

    # ── API endpoints ───────────────────────────────────────────────────────
    ui_base_url: str = field(
        default_factory=lambda: _env("EVOLUTION_UI_URL", "http://127.0.0.1:4100")
    )
    temper_base_url: str = field(
        default_factory=lambda: _env("EVOLUTION_TEMPER_URL", "http://127.0.0.1:3000")
    )
    tenant: str = "dark-factory"

    # ── Paths ───────────────────────────────────────────────────────────────
    helix_repo: Path = HELIX_REPO
    evolution_worktrees: Path = EVOLUTION_WORKTREES
    evolution_guide_path: str = "EVOLUTION_GUIDE.md"
    tokens_path: Path = TOKENS_PATH

    # ── Helix lineage branch ────────────────────────────────────────────────
    # The branch we evolve from and promote champions back to.
    # arun/champion-metrics-pranav-clone is the isolated clone used for this
    # experiment so parallel evolution runs don't conflict on champion-metrics.
    champion_branch: str = "champion-metrics-pranav-clone"
    champion_remote: str = "arun"

    def variant_cluster_dd_tag(self, cluster_name: str) -> str:
        """Return the DD cluster: tag value for a variant cluster."""
        return f"helix-{cluster_name}"

    def load_tokens(self) -> dict:
        import json
        return json.loads(self.tokens_path.read_text())

    def load_dd_credentials(self) -> dict:
        creds = load_dd_env()
        # Also fall back to environment variables
        for key in ("DD_API_KEY", "DD_APP_KEY", "DD_SITE", "ANTHROPIC_API_KEY"):
            if key not in creds:
                val = os.environ.get(key, "")
                if val:
                    creds[key] = val
        return creds

    def evolution_guide(self) -> str:
        """Read EVOLUTION_GUIDE.md from the helix repo."""
        guide = self.helix_repo / self.evolution_guide_path
        if guide.exists():
            return guide.read_text()
        return "(EVOLUTION_GUIDE.md not found — create it on the champion-metrics-pranav-clone branch)"
