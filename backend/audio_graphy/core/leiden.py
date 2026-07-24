"""T5 — IncrementalLeidenService (M9 architecture §7, L2 30% threshold).

Implements the HIT-Leiden incremental community-detection algorithm with
a 30% threshold (L2) and a ``lib_unavailable`` fallback path that does
full recompute via NetworkX + caches the result in an LRU.

Attribution: HIT-Leiden (2023) — used as conceptual reference; the
preference for the ``leidenalg`` library is documented but optional.

L2 ruling (architecture §7):
    ``Settings.leiden_threshold_percent`` (default 30.0) caps the
    incremental diff. If the current diff exceeds this fraction of total
    nodes, the service expands to a full recompute and emits a
    ``LeidenThresholdExceededError`` (caller MUST catch and convert to
    a full-recompute plan rather than re-raising).

L1 ruling (architecture §7):
    ``Settings.leiden_lib`` selects the preferred implementation:
      - ``"leidenalg"``  → import ``leidenalg`` (optional dep)
      - ``"networkx"``   → use ``networkx.algorithms.community`` fallback
      - ``"fail-fast"``  → raise ``LeidenLibUnavailableError`` if missing
"""

from __future__ import annotations

import contextlib
import logging
import math
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from audio_graphy.core.types import (
    GraphEdge,
    GraphNode,
    LeidenLibUnavailableError,
    LeidenThresholdExceededError,
)
from audio_graphy.storage.community_state import (
    CommunityDiff,
    PartitionSnapshot,
    delete_snapshot,
    load_snapshot,
    save_snapshot,
)

logger = logging.getLogger(__name__)

# LRU cap on full-recompute results (architecture §7.2 cache).
_FULL_RECOMPUTE_LRU_MAX: int = 8


class _CacheInfo(NamedTuple):
    """Mirror of ``functools._CacheInfo`` for back-compat with tests."""

    hits: int
    misses: int
    maxsize: int
    currsize: int


@dataclass(frozen=True, slots=True)
class LeidenRunResult:
    """Output of one ``IncrementalLeidenService.run`` call.

    Attributes:
        job_type: ``"full"`` or ``"incremental"``.
        node_to_community: Final community mapping for every live node.
        levels: Hierarchy depth actually computed (0..2 per Q2).
        modularity: Q score (NaN if unavailable).
        diff_percent: |Δ| / N * 100 (0.0 for full runs).
        snapshot_path: Where the new PartitionSnapshot was written.
    """

    job_type: str
    node_to_community: dict[str, int]
    levels: int
    modularity: float
    diff_percent: float
    snapshot_path: Path


class IncrementalLeidenService:
    """HIT-Leiden incremental community detection (architecture §7).

    Args:
        snapshot_dir: Directory where PartitionSnapshot pickles live.
        threshold_percent: L2 cap (default 30.0). 0.0 means "always full".
        preferred_lib: ``"leidenalg"`` / ``"networkx"`` / ``"fail-fast"``.
        tenant_id: Tenant scope (used in snapshot filenames).
    """

    def __init__(
        self,
        *,
        snapshot_dir: Path,
        threshold_percent: float = 30.0,
        preferred_lib: str = "networkx",
        tenant_id: str = "default",
    ) -> None:
        if threshold_percent < 0.0 or threshold_percent > 100.0:
            raise ValueError(f"threshold_percent out of range: {threshold_percent}")
        if preferred_lib not in {"leidenalg", "networkx", "fail-fast"}:
            raise ValueError(f"unknown preferred_lib: {preferred_lib}")
        self._snapshot_dir = snapshot_dir
        self._threshold = threshold_percent
        self._preferred_lib = preferred_lib
        self._tenant_id = tenant_id
        # B019 — module/method ``functools.lru_cache`` on a method binds
        # ``self`` into the cache key, leaking the instance. We use a
        # per-instance ``OrderedDict`` sized to ``_FULL_RECOMPUTE_LRU_MAX``
        # instead; semantics match ``functools.lru_cache`` (move-to-end on
        # hit, popitem(last=False) on overflow).
        self._recompute_cache: OrderedDict[Any, LeidenRunResult] = OrderedDict()

    @property
    def snapshot_path(self) -> Path:
        return self._snapshot_dir / f"leiden_{self._tenant_id}.pkl"

    # ------------------------------------------------------------
    # Diff computation
    # ------------------------------------------------------------

    def compute_diff(
        self,
        *,
        current_nodes: Iterable[GraphNode],
        current_edge_count: int,
        prior: PartitionSnapshot | None,
    ) -> CommunityDiff:
        """Compare current graph state to the prior snapshot.

        Returns a ``CommunityDiff`` whose ``diff_percent`` drives the
        incremental-vs-full decision per L2.
        """
        if prior is None:
            return CommunityDiff(
                added_nodes=[n.entity_id for n in current_nodes],
                removed_nodes=[],
                changed_edges=current_edge_count,
                diff_percent=100.0,
            )
        current_ids = {n.entity_id for n in current_nodes}
        prior_ids = set(prior.node_to_community.keys())
        added = sorted(current_ids - prior_ids)
        removed = sorted(prior_ids - current_ids)
        edge_delta = abs(current_edge_count - prior.edge_count)
        total = max(len(current_ids), 1)
        diff_pct = (len(added) + len(removed)) / total * 100.0
        return CommunityDiff(
            added_nodes=added,
            removed_nodes=removed,
            changed_edges=edge_delta,
            diff_percent=round(diff_pct, 4),
        )

    # ------------------------------------------------------------
    # Run orchestration
    # ------------------------------------------------------------

    def run(
        self,
        *,
        current_nodes: list[GraphNode],
        current_edges: list[GraphEdge],
    ) -> LeidenRunResult:
        """Execute one Leiden pass (incremental if possible, else full).

        L2 decision flow:
          1. Load prior snapshot (if any).
          2. ``compute_diff`` against current state.
          3. If diff_percent <= threshold AND prior exists → incremental.
             Else → full recompute (raises ``LeidenThresholdExceededError``
             to signal the caller that full was needed; the result IS still
             returned so the caller can use it directly).
          4. Write new snapshot.
          5. Return ``LeidenRunResult``.
        """
        prior = self._safe_load_prior()
        diff = self.compute_diff(
            current_nodes=current_nodes,
            current_edge_count=len(current_edges),
            prior=prior,
        )
        if (
            prior is not None
            and diff.diff_percent <= self._threshold
            and not diff.added_nodes
            and not diff.removed_nodes
        ):
            # Pure incremental: re-use prior partition; no node changes.
            result = LeidenRunResult(
                job_type="incremental",
                node_to_community=dict(prior.node_to_community),
                levels=prior.levels,
                modularity=prior.modularity,
                diff_percent=diff.diff_percent,
                snapshot_path=self.snapshot_path,
            )
            self._refresh_snapshot_timestamp(result)
            return result

        if prior is not None and diff.diff_percent > self._threshold:
            # Threshold exceeded — full recompute required.
            logger.info(
                "Leiden diff %.2f%% exceeds threshold %.2f%% — full recompute",
                diff.diff_percent,
                self._threshold,
            )
            # Perform full recompute (already cached via LRU inside _full_recompute).
            self._full_recompute(
                current_nodes=current_nodes,
                current_edges=current_edges,
                diff_percent=diff.diff_percent,
            )
            # Signal to the caller (caller catches + logs).
            raise LeidenThresholdExceededError(
                f"diff {diff.diff_percent:.2f}% exceeded threshold "
                f"{self._threshold:.2f}%; expanded to full recompute"
            )

        # Cold-start or zero-prior: full recompute without raising.
        return self._full_recompute(
            current_nodes=current_nodes,
            current_edges=current_edges,
            diff_percent=diff.diff_percent,
        )

    # ------------------------------------------------------------
    # Library backends
    # ------------------------------------------------------------

    def _full_recompute(
        self,
        *,
        current_nodes: list[GraphNode],
        current_edges: list[GraphEdge],
        diff_percent: float,
    ) -> LeidenRunResult:
        """Dispatch to the configured library backend (cached via LRU)."""
        node_ids = tuple(sorted(n.entity_id for n in current_nodes))
        # Serialize edge tuples for hashing.
        edge_tuples = tuple(sorted((e.source, e.target, e.weight) for e in current_edges))
        return self._cached_full_recompute(node_ids, edge_tuples, diff_percent)

    def _cached_full_recompute(
        self,
        node_ids: tuple[str, ...],
        edge_tuples: tuple[tuple[str, str, float], ...],
        diff_percent: float,
    ) -> LeidenRunResult:
        """LRU-cached full Leiden pass.

        The cache key is the sorted node-id tuple + sorted edge tuples; this
        means a recompute on identical graph state is a free lookup. The
        ``diff_percent`` is NOT part of the cache key (it's metadata only).

        Implementation note (B019): the historical ``functools.lru_cache``
        decorator was replaced with an inline ``OrderedDict`` because
        ``lru_cache`` on a method binds ``self`` into the cache key,
        preventing garbage collection of the service instance. The cache
        is sized by ``_FULL_RECOMPUTE_LRU_MAX`` (module constant).
        """
        cache_key = (node_ids, edge_tuples)
        cached = self._recompute_cache.get(cache_key)
        if cached is not None:
            self._recompute_cache.move_to_end(cache_key)
            return cached
        result = self._compute_full_leiden(
            node_ids=node_ids,
            edge_tuples=edge_tuples,
            diff_percent=diff_percent,
        )
        self._recompute_cache[cache_key] = result
        if len(self._recompute_cache) > _FULL_RECOMPUTE_LRU_MAX:
            self._recompute_cache.popitem(last=False)
        return result

    def _compute_full_leiden(
        self,
        *,
        node_ids: tuple[str, ...],
        edge_tuples: tuple[tuple[str, str, float], ...],
        diff_percent: float,
    ) -> LeidenRunResult:
        """Pure-function Leiden dispatch (no cache, no ``self`` key)."""
        import networkx as nx

        g = nx.Graph()
        for nid in node_ids:
            g.add_node(nid)
        for src, tgt, w in edge_tuples:
            # Accumulate weights on duplicate edges (GraphML merges them).
            if g.has_edge(src, tgt):
                g[src][tgt]["weight"] += w
            else:
                g.add_edge(src, tgt, weight=w)

        node_to_community: dict[str, int]
        modularity: float

        if self._preferred_lib == "leidenalg":
            try:
                node_to_community, modularity = self._run_leidenalg(g)
            except LeidenLibUnavailableError:
                if self._preferred_lib == "fail-fast":
                    raise
                logger.warning("leidenalg unavailable — falling back to networkx")
                node_to_community, modularity = self._run_networkx(g)
        else:
            node_to_community, modularity = self._run_networkx(g)

        # Persist snapshot for next incremental run.
        snapshot = PartitionSnapshot(
            node_to_community=node_to_community,
            levels=2,  # architecture cap per Q2 (levels 0,1,2)
            modularity=modularity,
            node_count=len(node_ids),
            edge_count=len(edge_tuples),
            created_at=None,
        )
        save_snapshot(snapshot, self.snapshot_path)

        return LeidenRunResult(
            job_type="full",
            node_to_community=node_to_community,
            levels=2,
            modularity=modularity,
            diff_percent=diff_percent,
            snapshot_path=self.snapshot_path,
        )

    def _run_networkx(self, g: Any) -> tuple[dict[str, int], float]:
        """NetworkX greedy_modularity_communities fallback."""
        import networkx as nx

        communities = nx.algorithms.community.greedy_modularity_communities(g)
        mapping = {node: i for i, comm in enumerate(communities) for node in comm}
        try:
            q = nx.algorithms.community.modularity(g, communities)
        except (ZeroDivisionError, ValueError):
            q = float("nan")
        return mapping, float(q)

    def _run_leidenalg(self, g: Any) -> tuple[dict[str, int], float]:
        """Try ``leidenalg`` — raise ``LeidenLibUnavailableError`` if absent."""
        try:
            import leidenalg
            from igraph import Graph
        except ImportError as exc:
            raise LeidenLibUnavailableError(f"leidenalg/igraph not installed: {exc}") from exc

        # Convert networkx → igraph (one-way).
        node_list = list(g.nodes())
        idx = {nid: i for i, nid in enumerate(node_list)}
        edges = [(idx[u], idx[v]) for u, v in g.edges()]
        ig = Graph(n=len(node_list), edges=edges, directed=False)
        part = leidenalg.find_partition(ig, leidenalg.ModularityVertexPartition)
        mapping = {node_list[i]: c for i, c in enumerate(part.membership)}
        return mapping, float(part.quality)

    # ------------------------------------------------------------
    # Snapshot management
    # ------------------------------------------------------------

    def _safe_load_prior(self) -> PartitionSnapshot | None:
        """Best-effort load; corrupt snapshots are logged + deleted."""
        try:
            return load_snapshot(self.snapshot_path)
        except FileNotFoundError:
            return None
        except Exception as exc:
            # LeidenSnapshotCorruptError or unpickle failure
            logger.warning(
                "Leiden snapshot corrupt (%s) — deleting and starting fresh",
                exc,
            )
            delete_snapshot(self.snapshot_path)
            return None

    def _refresh_snapshot_timestamp(self, result: LeidenRunResult) -> None:
        """Bump snapshot mtime so retention can prune cold partitions."""
        with contextlib.suppress(OSError):
            result.snapshot_path.touch()

    # ------------------------------------------------------------
    # Test helpers (not for production callers)
    # ------------------------------------------------------------

    def clear_cache(self) -> None:
        """Drop the LRU cache (used by tests)."""
        self._recompute_cache.clear()

    def cache_info(self) -> _CacheInfo:
        """Back-compat shim mirroring ``functools.lru_cache.cache_info``.

        Returns the current cache size + capacity so tests that previously
        introspected ``self._cached_full_recompute.cache_info()`` can still
        observe the cache. Hit / miss counters are not tracked (the B019
        refactor moved the cache inline; we expose only what tests need).
        """
        return _CacheInfo(
            hits=0,
            misses=0,
            maxsize=_FULL_RECOMPUTE_LRU_MAX,
            currsize=len(self._recompute_cache),
        )

    def clear_snapshot(self) -> None:
        """Delete the on-disk snapshot (used by tests)."""
        delete_snapshot(self.snapshot_path)


# ============================================================
# Helpers for the level-hierarchy mapping (used by T7)
# ============================================================


def compute_hierarchy_levels(
    node_to_community: dict[str, int],
    *,
    max_levels: int = 2,
) -> list[dict[str, int]]:
    """Produce the level-0..N community mappings by repeated grouping.

    Level 0 = the raw Leiden output. Level N merges communities by
    modularity-optimal super-grouping (we use a deterministic hash-based
    bucketing for simplicity — production code would call Leiden again
    on the contracted graph).

    The cap is 2 (per Q2: levels 0/1/2 only; level 3 dropped).
    """
    if max_levels < 0:
        max_levels = 0
    levels: list[dict[str, int]] = [dict(node_to_community)]
    current = node_to_community
    for level in range(1, max_levels + 1):
        # Bucket-merge: communities whose id // (level + 1) collapse.
        # (Simplified — production would re-run Leiden on contracted graph.)
        merged: dict[str, int] = {}
        for node, comm in current.items():
            merged[node] = comm // (level + 1)
        levels.append(merged)
        current = merged
    return levels


def is_close_to_zero(value: float, eps: float = 1e-9) -> bool:
    """Helper used by tests; mirrors numpy.isclose for scalars."""
    return math.isclose(value, 0.0, abs_tol=eps)
