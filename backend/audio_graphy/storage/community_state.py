"""T5 — Community state storage for HIT-Leiden incremental (M9 §7.2).

Persisted to disk via pickle so that subsequent incremental Leiden runs
can load the prior partition and compute the diff against the current
graph state.

Two dataclasses:
    - ``PartitionSnapshot``  — serialised Leiden partition from a prior run
    - ``CommunityDiff``      — incremental delta (added/removed/changed nodes)

Attribution: the HIT-Leiden incremental paradigm is taken from
"Hierarchical Incremental Leiden" (HIT-Leiden, 2023) — used here as a
conceptual reference, MIT-clean.
"""

from __future__ import annotations

import contextlib
import pickle
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from audio_graphy.core.types import LeidenSnapshotCorruptError


@dataclass(frozen=True, slots=True)
class PartitionSnapshot:
    """Serialised Leiden partition from a prior successful run.

    Attributes:
        node_to_community: Mapping ``entity_id -> community_id`` (level-0).
        levels: Number of hierarchy levels computed (0..3, capped at 2 per Q2).
        modularity: Q modularity score at snapshot time.
        node_count: Number of nodes covered at snapshot time.
        edge_count: Number of edges covered at snapshot time.
        created_at: Wall-clock time the snapshot was written.
        algorithm_version: Schema version of this dataclass (for forward-compat).
    """

    node_to_community: dict[str, int]
    levels: int
    modularity: float
    node_count: int
    edge_count: int
    created_at: Any  # datetime, but kept as Any to avoid pickle portability issues
    algorithm_version: int = 1


@dataclass(frozen=True, slots=True)
class CommunityDiff:
    """Incremental delta computed by ``IncrementalLeidenService.compute_diff``.

    Attributes:
        added_nodes: entity_ids that exist now but were absent at snapshot.
        removed_nodes: entity_ids that existed at snapshot but are gone now.
        changed_edges: count of edges added/removed since snapshot (heuristic
            approximation — full edge diff is expensive).
        diff_percent: ``|Δ| / N * 100`` where N = current node count. Used to
            decide between incremental vs full recompute per L2 30% threshold.
    """

    added_nodes: list[str] = field(default_factory=list)
    removed_nodes: list[str] = field(default_factory=list)
    changed_edges: int = 0
    diff_percent: float = 0.0


# ============================================================
# Persistence helpers
# ============================================================


def save_snapshot(snapshot: PartitionSnapshot, path: Path) -> None:
    """Pickle ``snapshot`` to ``path`` (atomic via temp + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as f:
        pickle.dump(snapshot, f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(path)


def load_snapshot(path: Path) -> PartitionSnapshot:
    """Load a snapshot; raise ``LeidenSnapshotCorruptError`` on any failure."""
    if not path.exists():
        raise LeidenSnapshotCorruptError(f"snapshot not found: {path}")
    try:
        with path.open("rb") as f:
            obj = pickle.load(f)  # noqa: S301
    except (pickle.PickleError, EOFError, OSError) as exc:
        raise LeidenSnapshotCorruptError(f"cannot load {path}: {exc}") from exc
    if not isinstance(obj, PartitionSnapshot):
        raise LeidenSnapshotCorruptError(
            f"snapshot {path} contains {type(obj).__name__}, expected PartitionSnapshot"
        )
    if obj.algorithm_version != 1:
        raise LeidenSnapshotCorruptError(
            f"snapshot algorithm_version={obj.algorithm_version} unsupported"
        )
    return obj


def delete_snapshot(path: Path) -> None:
    """Remove a snapshot file (idempotent)."""
    if path.exists():
        path.unlink()
    # Also remove any stale .tmp sidecar.
    tmp = path.with_suffix(path.suffix + ".tmp")
    if tmp.exists():
        with contextlib.suppress(OSError):
            if tmp.is_dir():
                shutil.rmtree(tmp)
            else:
                tmp.unlink()
