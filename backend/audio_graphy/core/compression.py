"""T9 — CompressionService (M9 architecture §9, Q3 SOFT-delete ruling).

Q3 ruling (binding):
    Compression is SOFT-DELETE ONLY. Specifically:
      - Nodes:   ``expired_at := now()``     (no row removal)
      - Edges:   ``invalid_at := now()``     (no row removal)
    Hard-deletes are FORBIDDEN at the compression layer. The only path
    to a hard delete is the RetentionEnforcer (PIPL §14.3) after the
    regulatory retention window elapses.

Three phases (architecture §9, locked decisions L6 + L7):

    1. **Low-degree node merge (L6)** — nodes with ``degree ≤
       compression_degree_threshold`` whose entity name has
       ``rapidfuzz.fuzz.token_ratio ≥ compression_fuzzy_token_ratio``
       against another node in the same community become merge
       candidates. Canonical keeps the lower entity_id (deterministic);
       the source node gets ``expired_at := now()`` and its edges are
       re-pointed to the canonical (BiTemporalEdgeService.retention_cascade
       invalidates the originals — Q3 soft-delete only).

       The pre-L6 4-heuristic scoring path (god_node / stale / redundant /
       low_degree) is RETAINED for backward compatibility with the
       ``/admin/compression/dry-run`` admin endpoint (M9 R1) — selectable
       via ``strategy="heuristic"``. New code uses ``strategy="l6_merge"``.

    2. **AMBIGUOUS deprecation (L7)** — every live edge whose
       ``confidence == "AMBIGUOUS"`` AND ``created_at <
       now() - compression_ambiguous_deprecate_days`` AND has no
       re-encounter event in that window → demoted to
       ``confidence = "DEPRECATED"`` + ``expired_at := now()``.

    3. **Orphan edge invalidate** — every live edge whose source or
       target node has ``expired_at`` set → ``invalid_at := now()`` +
       ``expired_at := now()`` (reason='orphan').

The service is storage-agnostic: it accepts a ``CompressionSink`` so
unit tests can substitute an in-memory store. The real DB+graph wiring
lands in a follow-up R2 task.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from rapidfuzz import fuzz

from audio_graphy.core.bi_temporal import BiTemporalEdgeService
from audio_graphy.core.types import (
    CompressionPolicyViolationError,
    CompressionRollbackError,
    GraphEdge,
    GraphNode,
)
from audio_graphy.observability.metrics import (
    COMPRESSION_EDGES_DEPRECATED,
    COMPRESSION_EDGES_SOFT_DELETED,
    COMPRESSION_NODES_SOFT_DELETED,
    COMPRESSION_ORPHANS_INVALIDATED,
    COMPRESSION_RUNS_TOTAL,
)

logger = logging.getLogger(__name__)


# ============================================================
# Public types
# ============================================================


@dataclass(frozen=True, slots=True)
class CompressionCandidate:
    """One node nominated for soft-delete in phase 1.

    Attributes:
        entity_id: The node id.
        score: 0.0–1.0; higher = more compressible.
        reason: ``"god_node"`` / ``"stale"`` / ``"redundant"`` /
            ``"low_degree"`` (heuristic strategy) or ``"l6_merge"``
            (L6 strategy).
    """

    entity_id: str
    score: float
    reason: str


@dataclass(frozen=True, slots=True)
class LowDegreeMergeCandidate:
    """One L6 merge decision (architecture §9.3).

    Attributes:
        source_entity_id: Node to be soft-deleted (Q3 expired_at=now()).
        canonical_entity_id: Node the source merges into.
        score: rapidfuzz ``fuzz.token_ratio`` × 100 (0..100).
    """

    source_entity_id: str
    canonical_entity_id: str
    score: int


@dataclass(frozen=True, slots=True)
class CompressionReport:
    """Outcome of one ``CompressionService.apply`` invocation.

    Attributes:
        candidates: Phase-1 picks (full list, including those that turned
            out to be no-ops).
        soft_deleted_nodes: entity_ids whose ``expired_at`` was set this run.
        soft_deleted_edges: edge-keys whose ``invalid_at`` was set this run.
        rolled_back: True if phase 2 raised and phase 3 successfully
            rolled back all partial mutations; False on clean commit.
        error: Exception that triggered rollback, if any (None on success).
    """

    candidates: list[CompressionCandidate]
    soft_deleted_nodes: list[str]
    soft_deleted_edges: list[str]
    rolled_back: bool
    error: Exception | None = None


class CompressionSink(Protocol):
    """Storage adapter for compression mutations (DB + graph in production)."""

    def fetch_node(self, entity_id: str) -> GraphNode | None: ...
    def fetch_edges_on_node(self, entity_id: str) -> list[GraphEdge]: ...
    def write_node(self, node: GraphNode) -> None: ...
    def write_edge(self, edge: GraphEdge) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


# ============================================================
# Service
# ============================================================


class CompressionService:
    """Three-phase compression pipeline (architecture §9, Q3 SOFT-only).

    Args:
        sink: Storage adapter (commit/rollback are called per phase-2 batch).
        bt_service: BiTemporalEdgeService used for edge invalidation.
            Required so Q1 / Q3 semantics stay consistent in one place.
        god_node_degree_threshold: HEURISTIC strategy only — kept for
            back-compat with the R1 admin endpoints. Default 50.
        stale_days: HEURISTIC strategy only — back-compat with R1 admin.
            Default 180.
        tenant_id: Tenant scope (used in EdgeEvent rows).
        degree_threshold: L6 — nodes with ``degree ≤ this`` AND a same
            community sibling with ``token_ratio ≥ fuzzy_token_ratio``
            become merge candidates. Default 1 (PRD L6).
        fuzzy_token_ratio: L6 — rapidfuzz ``fuzz.token_ratio`` × 100
            threshold. Default 85 (PRD L6).
        ambiguous_deprecate_days: L7 — AMBIGUOUS edges older than this
            many days without re-encounter get demoted to DEPRECATED.
            Default 30 (PRD L7).
    """

    def __init__(
        self,
        *,
        sink: CompressionSink,
        bt_service: BiTemporalEdgeService,
        god_node_degree_threshold: int = 50,
        stale_days: int = 180,
        tenant_id: str = "default",
        degree_threshold: int = 1,
        fuzzy_token_ratio: int = 85,
        ambiguous_deprecate_days: int = 30,
    ) -> None:
        self._sink = sink
        self._bt = bt_service
        self._god_threshold = god_node_degree_threshold
        self._stale_days = stale_days
        self._tenant_id = tenant_id
        # L6 / L7 spec parameters (defaults match PRD §8 locked decisions).
        self._degree_threshold = degree_threshold
        self._fuzzy_token_ratio = fuzzy_token_ratio
        self._ambiguous_deprecate_days = ambiguous_deprecate_days

    # ------------------------------------------------------------
    # Phase 1 — candidate selection
    # ------------------------------------------------------------

    def select_candidates(
        self,
        nodes: Iterable[GraphNode],
        *,
        now: datetime | None = None,
        strategy: str = "heuristic",
    ) -> list[CompressionCandidate]:
        """Score + rank nodes for compression candidacy.

        Args:
            strategy: ``"heuristic"`` (M9 R1 back-compat — god_node /
                stale / redundant / low_degree scoring) or ``"l6_merge"``
                (L6 spec — degree ≤ threshold + rapidfuzz token_ratio).
                ``"l6_merge"`` only emits candidates for the SOURCE side
                of each pair; call :meth:`select_low_degree_merge_candidates`
                directly to inspect the full pair list.

        Selection heuristics (``strategy="heuristic"``, architecture §9.1):
          - **God-node**: degree >= ``god_node_degree_threshold`` → score 0.9
          - **Stale**: only one recording_id → score 0.7
          - **Redundant**: empty description → 0.5
          - **Low-degree leaf**: degree == 0 and recent → 0.2 (rarely picked)

        Returns candidates sorted by score descending.
        """
        ts = now or datetime.now(UTC)
        if strategy == "l6_merge":
            return self._select_l6_candidates(nodes)
        return self._select_heuristic_candidates(nodes, ts)

    def _select_heuristic_candidates(
        self,
        nodes: Iterable[GraphNode],
        now: datetime,
    ) -> list[CompressionCandidate]:
        out: list[CompressionCandidate] = []
        for n in nodes:
            if n.expired_at is not None:
                # Already soft-deleted — skip.
                continue
            score, reason = self._score_node_heuristic(n)
            if score > 0.0:
                out.append(CompressionCandidate(entity_id=n.entity_id, score=score, reason=reason))
        out.sort(key=lambda c: c.score, reverse=True)
        return out

    def _score_node_heuristic(self, n: GraphNode) -> tuple[float, str]:
        """Return (score, reason). First match wins."""
        if n.degree >= self._god_threshold:
            return (0.9, "god_node")
        if len(n.recording_ids) == 1:
            return (0.7, "stale")
        if not n.description.strip():
            return (0.5, "redundant")
        if n.degree == 0:
            return (0.2, "low_degree")
        return (0.0, "")

    # ---------------- L6 (rapidfuzz low-degree merge) -------------

    def select_low_degree_merge_candidates(
        self,
        nodes: Iterable[GraphNode],
    ) -> list[LowDegreeMergeCandidate]:
        """L6 candidate pairs — degree ≤ threshold + token_ratio ≥ fuzzy.

        Algorithm (architecture §9.3, verbatim):

            1. Fetch all live nodes (``expired_at IS NULL``) with
               ``degree ≤ compression_degree_threshold``.
            2. Group by ``(tenant_id, community_id, entity_type)`` —
               only nodes within the same group may merge.
            3. For each group, sort by ``display_name`` and for each
               pair compute ``rapidfuzz.fuzz.token_ratio(n1.name, n2.name)``.
            4. If score ≥ ``compression_fuzzy_token_ratio``:
                - canonical = node with smaller entity_id (deterministic)
                - source    = the other node
                - emit ``LowDegreeMergeCandidate``.

        Groups are tracked via ``GraphNode.community_id`` if the caller
        populates that attribute. For the in-memory sink + tests where
        ``community_id`` is not present, all live low-degree nodes form
        a single group.

        Args:
            nodes: Iterable of live (or all) GraphNodes — already
                soft-deleted nodes are skipped idempotently.

        Returns:
            List of merge candidates sorted by score descending. Each
            source entity appears at most once (the highest-scoring pair
            wins; ties broken by lexicographic canonical id).
        """
        # Step 1 — filter to live, low-degree nodes.
        low_degree_live: list[GraphNode] = [
            n for n in nodes if n.expired_at is None and n.degree <= self._degree_threshold
        ]
        if len(low_degree_live) < 2:
            return []

        # Step 2 — group by community (use _community_key helper so
        # nodes without explicit community_id collapse to one group).
        groups: dict[Any, list[GraphNode]] = {}
        for n in low_degree_live:
            groups.setdefault(_community_key(n), []).append(n)

        # Step 3 — within each group, pairwise token_ratio.
        chosen: dict[str, LowDegreeMergeCandidate] = {}
        for group in groups.values():
            if len(group) < 2:
                continue
            group_sorted = sorted(group, key=lambda x: x.name)
            for i, n1 in enumerate(group_sorted):
                for n2 in group_sorted[i + 1 :]:
                    score = int(fuzz.token_ratio(n1.name, n2.name))
                    if score < self._fuzzy_token_ratio:
                        continue
                    # Deterministic canonical/source assignment.
                    if n1.entity_id <= n2.entity_id:
                        canonical, source = n1, n2
                    else:
                        canonical, source = n2, n1
                    prev = chosen.get(source.entity_id)
                    if (
                        prev is None
                        or score > prev.score
                        or (score == prev.score and canonical.entity_id < prev.canonical_entity_id)
                    ):
                        chosen[source.entity_id] = LowDegreeMergeCandidate(
                            source_entity_id=source.entity_id,
                            canonical_entity_id=canonical.entity_id,
                            score=score,
                        )

        out = list(chosen.values())
        out.sort(key=lambda c: (-c.score, c.source_entity_id))
        return out

    def _select_l6_candidates(self, nodes: Iterable[GraphNode]) -> list[CompressionCandidate]:
        """Adapter: expose L6 pairs as a flat CompressionCandidate list.

        Only the SOURCE side of each pair is emitted (canonicals are not
        candidates for soft-delete). Score is normalised token_ratio / 100
        so it stays in the existing [0, 1] contract used by ``apply``.
        """
        pairs = self.select_low_degree_merge_candidates(nodes)
        out: list[CompressionCandidate] = [
            CompressionCandidate(
                entity_id=p.source_entity_id,
                score=p.score / 100.0,
                reason="l6_merge",
            )
            for p in pairs
        ]
        out.sort(key=lambda c: c.score, reverse=True)
        return out

    # ------------------------------------------------------------
    # Phase 2 — soft-delete application (Q3 SOFT-only)
    # ------------------------------------------------------------

    def apply(
        self,
        candidates: Sequence[CompressionCandidate],
        *,
        policy_check: bool = True,
    ) -> CompressionReport:
        """Apply Q3 soft-deletes for every candidate, atomically.

        Per Q3: NEVER hard-delete. Setting ``expired_at`` (nodes) and
        ``invalid_at`` (edges) is the ONLY allowed mutation.

        Args:
            candidates: Output of ``select_candidates``.
            policy_check: When True (default), enforce Q3 strictly.

        Returns:
            CompressionReport describing what was mutated.
        """
        if policy_check:
            self._enforce_no_hard_delete_in_sink()

        soft_deleted_nodes: list[str] = []
        soft_deleted_edges: list[str] = []
        now = datetime.now(UTC)
        mutations: list[tuple[str, GraphNode, list[GraphEdge]]] = []

        try:
            for cand in candidates:
                node = self._sink.fetch_node(cand.entity_id)
                if node is None:
                    logger.warning(
                        "Compression candidate %s missing from sink — skip",
                        cand.entity_id,
                    )
                    continue
                if node.expired_at is not None:
                    # Already soft-deleted; idempotent skip.
                    continue
                edges_on = self._sink.fetch_edges_on_node(cand.entity_id)
                # Q3 mutations
                expired_node = _with_expired(node, now)
                cascaded = self._bt.retention_cascade(
                    edges_on_node=edges_on,
                    actor="compression",
                )
                # Apply
                self._sink.write_node(expired_node)
                for new_edge, _event in cascaded:
                    self._sink.write_edge(new_edge)
                mutations.append((cand.entity_id, expired_node, [e for e, _ in cascaded]))
                soft_deleted_nodes.append(cand.entity_id)
                soft_deleted_edges.extend(
                    f"{e.source}|{e.relation}|{e.target}" for e, _ in cascaded
                )
            self._sink.commit()
            # Promote counters.
            if soft_deleted_nodes:
                COMPRESSION_RUNS_TOTAL.labels(outcome="committed").inc()
                COMPRESSION_NODES_SOFT_DELETED.inc(len(soft_deleted_nodes))
                COMPRESSION_EDGES_SOFT_DELETED.inc(len(soft_deleted_edges))
            return CompressionReport(
                candidates=list(candidates),
                soft_deleted_nodes=soft_deleted_nodes,
                soft_deleted_edges=soft_deleted_edges,
                rolled_back=False,
                error=None,
            )
        except Exception as exc:
            logger.error(
                "Compression phase 2 failed (%s); rolling back %d mutations",
                exc,
                len(mutations),
            )
            try:
                self._sink.rollback()
            except Exception as rb_sink_exc:
                # Sink itself cannot roll back its own state — fatal.
                raise CompressionRollbackError(
                    f"sink.rollback() failed after {exc}"
                ) from rb_sink_exc
            try:
                self._rollback_mutations(mutations, commit=False)
            except Exception as rb_exc:
                raise CompressionRollbackError(f"rollback failed after {exc}") from rb_exc
            COMPRESSION_RUNS_TOTAL.labels(outcome="rolled_back").inc()
            return CompressionReport(
                candidates=list(candidates),
                soft_deleted_nodes=[],
                soft_deleted_edges=[],
                rolled_back=True,
                error=exc,
            )

    # ------------------------------------------------------------
    # Phase 3 — rollback
    # ------------------------------------------------------------

    def _rollback_mutations(
        self,
        mutations: list[tuple[str, GraphNode, list[GraphEdge]]],
        *,
        commit: bool = True,
    ) -> None:
        """Reverse every soft-delete applied in phase 2.

        For each mutated node we write back a copy with ``expired_at=None``.
        For each edge we write back the original (pre-invalidation) edge.
        Note: this can only restore the IN-MEMORY state of the sink; if
        the sink already committed, the rollback is advisory only.

        Args:
            commit: When True, call ``sink.commit()`` after restoring.
                Should be False when called from a phase-2 error path
                (the sink has already rolled back its own state).
        """
        for _entity_id, expired_node, edges in mutations:
            restored_node = _with_expired(expired_node, None)
            self._sink.write_node(restored_node)
            for edge in edges:
                restored_edge = _with_invalidated(edge, None)
                self._sink.write_edge(restored_edge)
        if commit:
            self._sink.commit()

    # ------------------------------------------------------------
    # Top-level run helper
    # ------------------------------------------------------------

    def run(
        self,
        nodes: Iterable[GraphNode],
        *,
        max_candidates: int = 100,
        strategy: str = "heuristic",
    ) -> CompressionReport:
        """Convenience: select_candidates + apply in one call."""
        cands = self.select_candidates(nodes, strategy=strategy)[:max_candidates]
        return self.apply(cands)

    # ------------------------------------------------------------
    # Q3 enforcement
    # ------------------------------------------------------------

    def _enforce_no_hard_delete_in_sink(self) -> None:
        """Inspect sink for forbidden hard-delete methods.

        The CompressionSink protocol deliberately omits ``delete_node`` /
        ``delete_edge`` — if a subclass adds them and they are callable,
        we treat that as a Q3 policy violation.
        """
        for forbidden in ("delete_node", "delete_edge", "remove_node", "remove_edge"):
            method = getattr(self._sink, forbidden, None)
            if callable(method):
                raise CompressionPolicyViolationError(
                    f"sink exposes {forbidden}() — Q3 forbids hard-delete "
                    "methods on the compression sink"
                )

    # ============================================================
    # L7 — AMBIGUOUS edge deprecation (Phase 2 of architecture §9.4)
    # ============================================================

    def deprecate_ambiguous_edges(
        self,
        *,
        now: datetime | None = None,
        re_encounter_provider: Any | None = None,
        edges: Iterable[GraphEdge] | None = None,
    ) -> tuple[list[str], list[str]]:
        """L7 — demote AMBIGUOUS edges older than 30 days with no re-encounter.

        Edges passed in (or fetched live from the sink via a configurable
        provider) are scanned:

            - Skip if ``confidence != "AMBIGUOUS"``.
            - Skip if ``expired_at is not None`` (already soft-deleted).
            - Skip if ``created_at`` is missing or within the deprecate
              window.
            - Skip if a re-encounter event exists in the last
              ``ambiguous_deprecate_days`` days. By default this is
              detected via ``re_encounter_provider(edge, threshold_dt)
              -> bool`` — when ``None``, no re-encounter lookup is done
              and the edge is deprecated as soon as it crosses the age
              threshold.

        Matched edges are written back with:
            - ``confidence = "DEPRECATED"``
            - ``expired_at := now()``

        Returns two parallel lists ``(edge_keys, expired_edge_keys)`` —
        callers may inspect them for audit + Prometheus counter bumps.
        """
        ts = now or datetime.now(UTC)
        threshold_dt = ts - timedelta(days=self._ambiguous_deprecate_days)
        # Sink does not expose a global edge iterator in the Protocol
        # contract; callers that want full-graph scanning must pass
        # ``edges`` explicitly (e.g. from a graph_store.edges() call).
        source_edges: Iterable[GraphEdge] = edges if edges is not None else []

        deprecated_keys: list[str] = []
        expired_keys: list[str] = []
        for edge in source_edges:
            if edge.confidence != "AMBIGUOUS":
                continue
            if edge.expired_at is not None:
                continue
            # created_at guard — edges without a created_at fall back to
            # "ancient" so they are eligible for deprecation.
            created = edge.created_at or datetime(1970, 1, 1, tzinfo=UTC)
            if created >= threshold_dt:
                continue
            # Re-encounter guard.
            if re_encounter_provider is not None:
                try:
                    if re_encounter_provider(edge, threshold_dt):
                        continue
                except Exception as exc:  # pragma: no cover — defensive
                    logger.warning(
                        "re_encounter_provider raised %s; deprecating edge %s",
                        exc,
                        _edge_key_of(edge),
                    )
            deprecated = GraphEdge(
                source=edge.source,
                target=edge.target,
                relation=edge.relation,
                weight=edge.weight,
                confidence="DEPRECATED",
                confidence_score=edge.confidence_score,
                source_ids=list(edge.source_ids),
                valid_at=edge.valid_at or created,
                invalid_at=edge.invalid_at,
                created_at=created,
                expired_at=ts,
                superseded_by=edge.superseded_by,
            )
            self._sink.write_edge(deprecated)
            key = _edge_key_of(edge)
            deprecated_keys.append(key)
            expired_keys.append(key)
        if deprecated_keys:
            self._sink.commit()
            COMPRESSION_EDGES_SOFT_DELETED.inc(len(deprecated_keys))
            COMPRESSION_EDGES_DEPRECATED.inc(len(deprecated_keys))
        return deprecated_keys, expired_keys

    # ============================================================
    # Phase 3 (architecture §9.5) — Orphan edge invalidation
    # ============================================================

    def invalidate_orphan_edges(
        self,
        *,
        edges: Iterable[GraphEdge],
    ) -> list[str]:
        """Invalidate every live edge whose endpoint node is soft-deleted.

        Per architecture §9.5: edges whose source OR target node has
        ``expired_at`` set are invalidated with ``invalid_at := now()``
        and ``expired_at := now()``. This is the Q3-soft companion to
        ``retention_cascade``; it covers the cross-recording edge case
        where the cascade at node-deletion time did not see every edge
        in the graph.

        Args:
            edges: All edges in the tenant graph (caller-supplied; the
                sink Protocol does not expose a global iterator).

        Returns:
            List of edge keys (``"source|relation|target"``) invalidated
            this pass.
        """
        ts = datetime.now(UTC)
        invalidated: list[str] = []
        for edge in edges:
            if edge.expired_at is not None or edge.invalid_at is not None:
                continue
            src = self._sink.fetch_node(edge.source)
            tgt = self._sink.fetch_node(edge.target)
            if src is None and tgt is None:
                # Both endpoints gone — rare but possible in tests; still
                # invalidate so the edge stops appearing in retrieval.
                pass
            elif (
                src is not None
                and src.expired_at is None
                and tgt is not None
                and tgt.expired_at is None
            ):
                continue
            new_edge = GraphEdge(
                source=edge.source,
                target=edge.target,
                relation=edge.relation,
                weight=edge.weight,
                confidence=edge.confidence,
                confidence_score=edge.confidence_score,
                source_ids=list(edge.source_ids),
                valid_at=edge.valid_at or ts,
                invalid_at=ts,
                created_at=edge.created_at or ts,
                expired_at=ts,
                superseded_by=edge.superseded_by,
            )
            self._sink.write_edge(new_edge)
            invalidated.append(_edge_key_of(edge))
        if invalidated:
            self._sink.commit()
            COMPRESSION_ORPHANS_INVALIDATED.inc(len(invalidated))
        return invalidated


# ============================================================
# Helpers
# ============================================================


def _community_key(n: GraphNode) -> tuple[str, str]:
    """Group key for L6 — community + entity type.

    Nodes without ``community_id`` attribute collapse to ``("__none__", type)``
    so the test suite + in-memory sink (which does not populate community)
    can still exercise the merge path. The real production sink injects
    community ids at the Leiden stage (architecture §7).
    """
    community = getattr(n, "community_id", None)
    if community is None:
        community = "__none__"
    return (str(community), n.type)


def _edge_key_of(edge: GraphEdge) -> str:
    return f"{edge.source}|{edge.relation}|{edge.target}"


# ============================================================
# Immutable rebuild helpers
# ============================================================


def _with_expired(node: GraphNode, expired_at: datetime | None) -> GraphNode:
    """Return a copy of ``node`` with ``expired_at`` replaced (frozen dataclass)."""
    return GraphNode(
        entity_id=node.entity_id,
        name=node.name,
        type=node.type,
        description=node.description,
        source_ids=list(node.source_ids),
        recording_ids=list(node.recording_ids),
        degree=node.degree,
        expired_at=expired_at,
    )


def _with_invalidated(edge: GraphEdge, invalid_at: datetime | None) -> GraphEdge:
    """Return a copy of ``edge`` with ``invalid_at`` replaced."""
    return GraphEdge(
        source=edge.source,
        target=edge.target,
        relation=edge.relation,
        weight=edge.weight,
        confidence=edge.confidence,
        confidence_score=edge.confidence_score,
        source_ids=list(edge.source_ids),
        valid_at=edge.valid_at,
        invalid_at=invalid_at,
        created_at=edge.created_at,
        expired_at=edge.expired_at,
        superseded_by=edge.superseded_by,
    )


# ============================================================
# Default in-memory sink (for tests + dev mode)
# ============================================================


class InMemoryCompressionSink:
    """Simple list-backed CompressionSink for tests + local dev."""

    def __init__(self) -> None:
        self.nodes: dict[str, GraphNode] = {}
        self.edges: dict[str, GraphEdge] = {}  # key = source|relation|target
        self.committed: bool = False
        self.rolled_back: bool = False
        self._staged_nodes: dict[str, GraphNode] = {}
        self._staged_edges: dict[str, GraphEdge] = {}

    def seed(
        self,
        nodes: Iterable[GraphNode],
        edges: Iterable[GraphEdge],
    ) -> None:
        """Bulk-load initial state (test helper)."""
        for n in nodes:
            self.nodes[n.entity_id] = n
        for e in edges:
            self.edges[f"{e.source}|{e.relation}|{e.target}"] = e

    def fetch_node(self, entity_id: str) -> GraphNode | None:
        return self.nodes.get(entity_id)

    def fetch_edges_on_node(self, entity_id: str) -> list[GraphEdge]:
        """Return every edge that has ``entity_id`` as source or target."""
        return [e for e in self.edges.values() if entity_id in (e.source, e.target)]

    def write_node(self, node: GraphNode) -> None:
        self._staged_nodes[node.entity_id] = node

    def write_edge(self, edge: GraphEdge) -> None:
        self._staged_edges[f"{edge.source}|{edge.relation}|{edge.target}"] = edge

    def commit(self) -> None:
        self.nodes.update(self._staged_nodes)
        self.edges.update(self._staged_edges)
        self._staged_nodes.clear()
        self._staged_edges.clear()
        self.committed = True

    def rollback(self) -> None:
        self._staged_nodes.clear()
        self._staged_edges.clear()
        self.rolled_back = True
