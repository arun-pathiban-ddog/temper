"""The genome: the mutable gene set of a Helix lineage, read from / written to source.

A "genome" is a small config the breeding harness reads out of a Helix worktree's
source tree and writes back into it (ADR-0095). Four concrete tuning knobs:

  - linger_ms          helix-server/src/service/batcher.rs  (`.unwrap_or(1)`)
                       ↑ throughput, ↑ latency
  - max_inflight       helix-raft/src/lib.rs  MAX_INFLIGHT_APPEND_ENTRIES (5)
                       ↑ throughput
  - append_batch_size  helix-raft/src/lib.rs  APPEND_ENTRIES_BATCH_SIZE_MAX (1000)
                       ↑ throughput
  - sync_on_rotation   helix-wal/src/wal.rs   WalConfig default (true)
                       **LETHAL if false** — disabling fsync durability trips the
                       WAL durability DST (the proven cull).

`sync_on_rotation` is special. Flipping only the config bool does NOT trip
`test_dst_shared_wal_basic_durability` (that test drives durability through an
explicit `wal.sync()`, not segment rotation). To make the lethal gene *actually*
lethal — i.e. to faithfully represent the latency-greedy "skip the fsync"
shortcut — `apply_to_source` patches the active-segment fsync block inside
`pub async fn sync()` to skip `active.file.sync().await` while still advancing
`durable_index`. This is the EXACT proven mutation from the lethal-mutation spike
(memory dark-factory-lethal-mutation-spike): the WAL claims durability without
persisting, so a crash loses synced-but-unflushed data. The verification cull
(verify.py) catches it deterministically.

Everything here is pure str/regex file I/O over a given repo path — no Temper,
no Docker. The harness points it at a per-lineage git worktree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

# Gene source locations, relative to a Helix repo root.
BATCHER_PATH = "helix-server/src/service/batcher.rs"
RAFT_LIB_PATH = "helix-raft/src/lib.rs"
WAL_PATH = "helix-wal/src/wal.rs"

# --- regexes that locate each gene's value in source ---
# linger_ms: the `.unwrap_or(N)` that terminates the HELIX_BATCHER_LINGER_MS chain.
_LINGER_RE = re.compile(r"(HELIX_BATCHER_LINGER_MS[\s\S]*?\.unwrap_or\()(\d+)(\))")
_INFLIGHT_RE = re.compile(r"(MAX_INFLIGHT_APPEND_ENTRIES:\s*u32\s*=\s*)(\d+)")
_BATCHSIZE_RE = re.compile(r"(APPEND_ENTRIES_BATCH_SIZE_MAX:\s*u32\s*=\s*)(\d+)")
# sync_on_rotation default in WalConfig::new
_SYNC_ROT_RE = re.compile(r"(sync_on_rotation:\s*)(true|false)(,?\s*//)")

# The pristine active-segment fsync block inside `pub async fn sync()`.
_FSYNC_BLOCK = """        // Sync the active segment.
        if let Some(active) = &self.active_segment {
            let result = active.file.sync().await;
            debug!(
                bytes = self.bytes_since_sync,
                success = result.is_ok(),
                "Syncing active segment"
            );
            result?;
        }"""

# The lethal replacement: skip the fsync, but the durable_index advance below
# still runs. This is the proven cull (memory dark-factory-lethal-mutation-spike).
_FSYNC_BLOCK_LETHAL = """        // EVOLVE-MUTATION(latency, sync_on_rotation=false): skip the active-segment
        // fsync to shave the ~1.3ms penalty, but still advance durable_index below.
        // This is the latency-greedy shortcut the cull must catch: durability is
        // claimed without persistence, so a crash loses synced-but-unflushed data.
        if let Some(active) = &self.active_segment {
            let _ = active; // fsync skipped
            debug!(
                bytes = self.bytes_since_sync,
                "Skipping active segment fsync (sync_on_rotation=false)"
            );
        }"""


@dataclass(frozen=True)
class Genome:
    """One lineage's gene values. Frozen — mutate via :meth:`with_gene`."""

    linger_ms: int = 1
    max_inflight: int = 5
    append_batch_size: int = 1000
    sync_on_rotation: bool = True

    GENES = ("linger_ms", "max_inflight", "append_batch_size", "sync_on_rotation")

    def with_gene(self, gene: str, value) -> "Genome":
        if gene not in self.GENES:
            raise ValueError(f"unknown gene {gene!r}; genes are {self.GENES}")
        return replace(self, **{gene: value})

    def to_dict(self) -> dict:
        return {
            "linger_ms": self.linger_ms,
            "max_inflight": self.max_inflight,
            "append_batch_size": self.append_batch_size,
            "sync_on_rotation": self.sync_on_rotation,
        }

    def summary(self) -> str:
        return (
            f"linger_ms={self.linger_ms} max_inflight={self.max_inflight} "
            f"append_batch_size={self.append_batch_size} "
            f"sync_on_rotation={self.sync_on_rotation}"
        )

    def diff(self, other: "Genome") -> dict:
        """Genes that differ between two genomes -> {gene: (self, other)}."""
        return {
            g: (getattr(self, g), getattr(other, g))
            for g in self.GENES
            if getattr(self, g) != getattr(other, g)
        }


def _read(repo: Path, rel: str) -> str:
    return (repo / rel).read_text(encoding="utf-8")


def _write(repo: Path, rel: str, text: str) -> None:
    (repo / rel).write_text(text, encoding="utf-8")


def read_from_source(repo: str | Path) -> Genome:
    """Read the current genome straight out of a Helix repo/worktree's source."""
    repo = Path(repo)
    batcher = _read(repo, BATCHER_PATH)
    raft = _read(repo, RAFT_LIB_PATH)
    wal = _read(repo, WAL_PATH)

    linger_m = _LINGER_RE.search(batcher)
    inflight_m = _INFLIGHT_RE.search(raft)
    batch_m = _BATCHSIZE_RE.search(raft)
    sync_m = _SYNC_ROT_RE.search(wal)
    if not (linger_m and inflight_m and batch_m and sync_m):
        missing = [
            n
            for n, m in [
                ("linger_ms", linger_m),
                ("max_inflight", inflight_m),
                ("append_batch_size", batch_m),
                ("sync_on_rotation", sync_m),
            ]
            if not m
        ]
        raise RuntimeError(f"could not read genes from source: {missing} (repo={repo})")

    # If the lethal fsync-skip patch is already applied, sync_on_rotation is
    # effectively false regardless of the config bool. Treat the patch as truth.
    fsync_skipped = _FSYNC_BLOCK_LETHAL.strip().splitlines()[0].strip() in wal
    sync_on_rotation = (sync_m.group(2) == "true") and not fsync_skipped

    return Genome(
        linger_ms=int(linger_m.group(2)),
        max_inflight=int(inflight_m.group(2)),
        append_batch_size=int(batch_m.group(2)),
        sync_on_rotation=sync_on_rotation,
    )


def apply_to_source(genome: Genome, repo: str | Path) -> list[str]:
    """Write the genome's gene values into a Helix repo/worktree's source.

    Returns the list of files changed. Idempotent: writing the same genome twice
    is a no-op on disk.
    """
    repo = Path(repo)
    changed: list[str] = []

    # --- linger_ms ---
    batcher = _read(repo, BATCHER_PATH)
    new_batcher = _LINGER_RE.sub(
        lambda m: f"{m.group(1)}{genome.linger_ms}{m.group(3)}", batcher, count=1
    )
    if new_batcher != batcher:
        _write(repo, BATCHER_PATH, new_batcher)
        changed.append(BATCHER_PATH)

    # --- max_inflight + append_batch_size (same file) ---
    raft = _read(repo, RAFT_LIB_PATH)
    new_raft = _INFLIGHT_RE.sub(
        lambda m: f"{m.group(1)}{genome.max_inflight}", raft, count=1
    )
    new_raft = _BATCHSIZE_RE.sub(
        lambda m: f"{m.group(1)}{genome.append_batch_size}", new_raft, count=1
    )
    if new_raft != raft:
        _write(repo, RAFT_LIB_PATH, new_raft)
        changed.append(RAFT_LIB_PATH)

    # --- sync_on_rotation: config bool + the LETHAL fsync-skip patch ---
    wal = _read(repo, WAL_PATH)
    desired_bool = "true" if genome.sync_on_rotation else "false"
    new_wal = _SYNC_ROT_RE.sub(
        lambda m: f"{m.group(1)}{desired_bool}{m.group(3)}", wal, count=1
    )
    if genome.sync_on_rotation:
        # Safe: ensure the pristine fsync block is present (revert any lethal patch).
        if _FSYNC_BLOCK_LETHAL in new_wal:
            new_wal = new_wal.replace(_FSYNC_BLOCK_LETHAL, _FSYNC_BLOCK)
    else:
        # Lethal: apply the proven fsync-skip patch (skip sync, still advance index).
        if _FSYNC_BLOCK in new_wal:
            new_wal = new_wal.replace(_FSYNC_BLOCK, _FSYNC_BLOCK_LETHAL)
        elif _FSYNC_BLOCK_LETHAL not in new_wal:
            raise RuntimeError(
                "could not locate the active-segment fsync block in wal.rs to apply "
                "the sync_on_rotation=false lethal mutation (source drifted?)"
            )
    if new_wal != wal:
        _write(repo, WAL_PATH, new_wal)
        changed.append(WAL_PATH)

    return changed


if __name__ == "__main__":
    import sys

    repo = sys.argv[1] if len(sys.argv) > 1 else "/Users/arun.parthiban/notdd/helix"
    g = read_from_source(repo)
    print(f"[genome] {repo}")
    print(f"[genome] {g.summary()}")
