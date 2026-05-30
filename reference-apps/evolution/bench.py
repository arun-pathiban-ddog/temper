"""Stage 1 fitness measurement: real local Helix bench.

Per generation, the breeding harness calls `measure_local(repo)` after the
lineage's source mutation has been applied + the Helix image rebuilt. It runs a
3-node local Helix cluster (docker compose) and a `kafka-producer-perf-test`
load, then parses real throughput + latency-percentile numbers.

This is the cheap-but-real middle stage of the fitness cascade:
  Stage 0  cost model        (instant, in the harness)
  Stage 1  THIS local bench  (~30-60s, real measured numbers)
  Stage 2  verification      (the cull — wal durability DST etc.)
  Stage 3  GKE deploy+Datadog (champion only)

Environment notes (see memory dark-factory-local-bench-gotchas):
- Uses the proven multi-stage docker/Dockerfile (builds Linux ELF inside Docker).
  Requires the Colima/Docker VM to have >=8GB RAM or the Rust build OOMs.
- 3-node Kafka path (works), NOT the single-node gRPC path (flaky in Track A).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

HELIX_REPO = Path("/Users/arun.parthiban/notdd/helix")
COMPOSE_DIR = HELIX_REPO / "docker"
KAFKA_IMAGE = "confluentinc/cp-kafka:7.7.1"
DOCKER_NETWORK = "docker_helix-net"
BROKERS = "helix-node1:9092,helix-node2:9092,helix-node3:9092"

# Parses the single summary line kafka-producer-perf-test prints, e.g.:
# "50000 records sent, 41220.1 records/sec (20.13 MB/sec), 847.18 ms avg latency,
#  966.00 ms max latency, 844 ms 50th, 895 ms 95th, 939 ms 99th, 940 ms 99.9th."
_PERF_RE = re.compile(
    r"(?P<sent>\d+)\s+records sent,\s+(?P<tput>[\d.]+)\s+records/sec.*?"
    r"(?P<p50>\d+)\s+ms\s+50th.*?(?P<p95>\d+)\s+ms\s+95th.*?(?P<p99>\d+)\s+ms\s+99th",
    re.DOTALL,
)


@dataclass
class Measurement:
    """Real measured fitness for one generation's config."""

    throughput_rps: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    records: int

    def summary(self) -> str:
        return (
            f"throughput={self.throughput_rps:,.0f} rps  "
            f"p50={self.p50_ms}ms p95={self.p95_ms}ms p99={self.p99_ms}ms"
        )


def _run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, cwd=COMPOSE_DIR, capture_output=True, text=True, timeout=timeout, check=False
    )


def rebuild_image(tag: str = "helix-server:local") -> None:
    """Rebuild the local Helix image from the (mutated) source tree.

    Uses the proven multi-stage Dockerfile so the binary is real Linux ELF.
    Fast on incremental rebuilds thanks to BuildKit cache mounts.
    """
    res = subprocess.run(
        ["docker", "build", "-t", tag, "-f", "docker/Dockerfile", "."],
        cwd=HELIX_REPO,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"image build failed:\n{res.stderr[-2000:]}")


def cluster_up(partitions: int = 4) -> None:
    """Start the 3-node local cluster from the pre-built image (no rebuild)."""
    _run(["docker", "compose", "-f", "docker-compose.yml",
          "-f", "docker-compose.local.yml", "down", "-v"])
    env_cmd = ["docker", "compose", "-f", "docker-compose.yml",
               "-f", "docker-compose.local.yml", "up", "-d"]
    res = subprocess.run(
        env_cmd, cwd=COMPOSE_DIR, capture_output=True, text=True,
        env={"HELIX_PARTITIONS": str(partitions), "PATH": _path()}, check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"cluster up failed:\n{res.stderr[-2000:]}")


def cluster_down() -> None:
    _run(["docker", "compose", "-f", "docker-compose.yml",
          "-f", "docker-compose.local.yml", "down", "-v"])


def run_perf(topic: str, num_records: int = 50000, record_size: int = 512,
             linger_ms: int = 5) -> Measurement:
    """Run kafka-producer-perf-test and parse real fitness numbers."""
    res = _run([
        "docker", "run", "--rm", "--network", DOCKER_NETWORK, KAFKA_IMAGE,
        "kafka-producer-perf-test",
        "--topic", topic,
        "--num-records", str(num_records),
        "--record-size", str(record_size),
        "--throughput", "-1",
        "--producer-props",
        f"bootstrap.servers={BROKERS}",
        "acks=all", f"linger.ms={linger_ms}",
        "batch.size=65536", "buffer.memory=134217728",
    ], timeout=300)
    out = res.stdout + res.stderr
    m = _PERF_RE.search(out)
    if not m:
        raise RuntimeError(f"could not parse perf output:\n{out[-1500:]}")
    return Measurement(
        throughput_rps=float(m["tput"]),
        p50_ms=float(m["p50"]),
        p95_ms=float(m["p95"]),
        p99_ms=float(m["p99"]),
        records=int(m["sent"]),
    )


def _path() -> str:
    import os
    return os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")


def measure_local(rebuild: bool = True, partitions: int = 4,
                  num_records: int = 50000) -> Measurement:
    """Full Stage-1 cycle: (rebuild image from mutated source) -> cluster up ->
    wait for leader election -> perf test -> parse -> cluster down.

    Returns a Measurement the harness scores against the lineage's FitnessGoal.
    """
    import time
    import uuid

    if rebuild:
        rebuild_image()
    cluster_up(partitions=partitions)
    try:
        # Allow leader election + topic auto-create to settle.
        time.sleep(12)
        topic = f"evolve-{uuid.uuid4().hex[:8]}"
        return run_perf(topic, num_records=num_records)
    finally:
        cluster_down()


if __name__ == "__main__":
    # Smoke test: assumes the image + cluster machinery work.
    print("[bench] measuring local Helix fitness ...")
    measurement = measure_local(rebuild=False)
    print("[bench]", measurement.summary())
