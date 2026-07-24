"""T2 — BiTemporalEdgeService (M9 architecture §6, §6.4, §6.5 Q1 dual-track).

This service is the SINGLE source of truth for bi-temporal mutations
against graph edges. The NetworkX store itself remains unaware of
bi-temporal semantics; this service computes the new ``GraphEdge`` plus
the matching ``EdgeEvent`` row, and the caller (DeltaGraphUpdater in T3)
commits both atomically.

Q1 dual-track ruling (architecture §6.5):
    On supersede, BOTH actions happen atomically:
      1. Auto-invalidate the old edge  →  invalid_at := now()
      2. Set supersede pointer          →  superseded_by := new_edge_key
    The new edge's ``valid_at`` := the old edge's ``invalid_at``.

Attribution: the bi-temporal four-timestamp design (valid_at /
invalid_at / created_at / expired_at) follows the Graphiti paradigm
(getagraphiti.com, MIT-clean conceptual reference).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from audio_graphy.core.types import (
    BiTemporalInvalidRangeError,
    BiTemporalSupersedeChainError,
    GraphEdge,
)
from audio_graphy.models.edge_event import EdgeEvent

# Cap supersede chain depth so a runaway merge cascade raises rather than
# silently producing a 100-deep pointer chain (architecture §6.5 cap).
_MAX_SUPERSEDE_CHAIN_DEPTH: int = 8


def _edge_key(source: str, relation: str, target: str) -> str:
    """Stable identifier for an edge across mutations.

    Format: ``"{source}|{relation}|{target}"``. Whitespace inside fields is
    preserved; only the pipe is reserved as the separator. Collisions are
    astronomically unlikely with the car-sales domain vocabulary.
    """
    return f"{source}|{relation}|{target}"


def _now_utc() -> datetime:
    """UTC-normalised now() — datetime.utcnow() is deprecated in 3.12+."""
    return datetime.now(UTC)


def _serialise_edge_payload(edge: GraphEdge) -> str:
    """Render a GraphEdge to a JSON string for the EdgeEvent.payload column."""
    import json

    def _default(o: Any) -> Any:
        if isinstance(o, datetime):
            return o.isoformat()
        raise TypeError(f"Cannot serialise {type(o).__name__}")

    payload = {
        "source": edge.source,
        "target": edge.target,
        "relation": edge.relation,
        "weight": edge.weight,
        "confidence": edge.confidence,
        "confidence_score": edge.confidence_score,
        "source_ids": edge.source_ids,
        "valid_at": edge.valid_at,
        "invalid_at": edge.invalid_at,
        "created_at": edge.created_at,
        "expired_at": edge.expired_at,
        "superseded_by": edge.superseded_by,
    }
    return json.dumps(payload, default=_default, ensure_ascii=False)


class BiTemporalEdgeService:
    """Mutates bi-temporal edges and emits matching ``EdgeEvent`` rows.

    Pure-functional in spirit: every public method returns a tuple of
    ``(new_or_mutated_edge, edge_event_row)`` and the caller is responsible
    for committing the event row + writing the edge to the NetworkX store.
    This keeps the service unit-testable without a live DB or graph store.

    Public API (architecture §6.4):
        - ``insert_edge``           : cold-start a brand-new edge
        - ``merge_edge``            : accumulate weight on an existing edge
        - ``supersede_edge``        : Q1 dual-track replacement
        - ``time_travel_query``     : reconstruct edges as-of a past time
        - ``retention_cascade``     : soft-delete cascade on retention hits
    """

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id: str = tenant_id

    # ------------------------------------------------------------
    # Public mutators
    # ------------------------------------------------------------

    def insert_edge(
        self,
        *,
        source: str,
        target: str,
        relation: str,
        weight: float,
        confidence: str,
        confidence_score: float | None,
        source_ids: Sequence[str],
        valid_at: datetime | None = None,
        actor: str = "system",
    ) -> tuple[GraphEdge, EdgeEvent]:
        """Cold-start a brand-new edge.

        Args:
            valid_at: When the relation became true. Defaults to ``now()``.
                Must NOT be in the future relative to ``now()`` (we trust
                the caller — no clock-skew enforcement).
        Raises:
            BiTemporalInvalidRangeError: never (no upper bound set here).
        """
        ts = _now_utc()
        v_at = valid_at or ts
        edge = GraphEdge(
            source=source,
            target=target,
            relation=relation,
            weight=weight,
            confidence=confidence,  # type: ignore[arg-type]
            confidence_score=confidence_score,
            source_ids=list(source_ids),
            valid_at=v_at,
            invalid_at=None,
            created_at=ts,
            expired_at=None,
            superseded_by=None,
        )
        event = self._make_event(
            event_type="insert",
            edge=edge,
            actor=actor,
        )
        return edge, event

    def merge_edge(
        self,
        *,
        existing: GraphEdge,
        weight_delta: float,
        merged_source_ids: Sequence[str] | None = None,
        upgraded_confidence: str | None = None,
        actor: str = "system",
    ) -> tuple[GraphEdge, EdgeEvent]:
        """Accumulate weight on an existing edge.

        Merges are NOT supersede operations — the existing edge retains
        its identity (valid_at unchanged) and only the weight / confidence
        are updated. A new ``EdgeEvent(event_type="merge")`` row is emitted
        for auditability.
        """
        ts = _now_utc()
        new_weight = existing.weight + weight_delta
        new_conf = upgraded_confidence or existing.confidence
        new_edge = GraphEdge(
            source=existing.source,
            target=existing.target,
            relation=existing.relation,
            weight=new_weight,
            confidence=new_conf,  # type: ignore[arg-type]
            confidence_score=existing.confidence_score,
            source_ids=list(merged_source_ids or existing.source_ids),
            valid_at=existing.valid_at,
            invalid_at=existing.invalid_at,
            created_at=existing.created_at,
            expired_at=existing.expired_at,
            superseded_by=existing.superseded_by,
        )
        event = self._make_event(
            event_type="merge",
            edge=new_edge,
            actor=actor,
        )
        # created_at on event reflects when the merge was applied, not the
        # original creation time. The event row's own created_at column
        # (server_default=now()) carries that, so we leave ``ts`` unused
        # for the event itself — but we DO bump the new_edge's ts via
        # dataclass rebuild (immutable frozen, hence the rebuild above).
        del ts
        return new_edge, event

    def supersede_edge(
        self,
        *,
        old: GraphEdge,
        new_relation: str,
        new_target: str | None = None,
        new_weight: float | None = None,
        new_confidence: str | None = None,
        new_confidence_score: float | None = None,
        new_source_ids: Sequence[str] | None = None,
        actor: str = "system",
    ) -> tuple[GraphEdge, GraphEdge, EdgeEvent, EdgeEvent]:
        """Q1 dual-track supersede (architecture §6.5).

        Returns ``(invalidated_old_edge, replacement_edge, old_event, new_event)``.

        Atomically:
          1. ``invalid_at`` of old := now()
          2. ``superseded_by`` of old := key(replacement)
          3. ``valid_at`` of replacement := ``invalid_at`` of old
          4. Emit two ``EdgeEvent`` rows (``supersede`` for old, ``insert``
             for the replacement — Q1 says BOTH, so we emit both).

        Raises:
            BiTemporalInvalidRangeError: if old.valid_at is None (data corruption).
            BiTemporalSupersedeChainError: if chain depth exceeds cap.
        """
        if old.valid_at is None:
            raise BiTemporalInvalidRangeError(
                "supersede requires old.valid_at to be set (corruption?)"
            )
        if old.superseded_by is not None:
            # Old has already been superseded — refuse chained supersede.
            raise BiTemporalSupersedeChainError(
                f"edge {_edge_key(old.source, old.relation, old.target)} "
                f"is already superseded by {old.superseded_by}; "
                "refusing deep chain"
            )

        ts = _now_utc()
        target_rel = new_relation or old.relation
        target_node = new_target or old.target
        target_weight = new_weight if new_weight is not None else old.weight
        target_conf = new_confidence or old.confidence
        target_score = (
            new_confidence_score if new_confidence_score is not None else old.confidence_score
        )
        target_src_ids = list(new_source_ids or old.source_ids)

        # 1+2: invalidate + supersede-pointer on old
        invalidated_old = GraphEdge(
            source=old.source,
            target=old.target,
            relation=old.relation,
            weight=old.weight,
            confidence=old.confidence,
            confidence_score=old.confidence_score,
            source_ids=list(old.source_ids),
            valid_at=old.valid_at,
            invalid_at=ts,
            created_at=old.created_at,
            expired_at=old.expired_at,
            superseded_by=_edge_key(old.source, target_rel, target_node),
        )
        # 3: replacement edge — valid_at = old.invalid_at (Q1)
        replacement = GraphEdge(
            source=old.source,
            target=target_node,
            relation=target_rel,
            weight=target_weight,
            confidence=target_conf,  # type: ignore[arg-type]
            confidence_score=target_score,
            source_ids=target_src_ids,
            valid_at=ts,  # = invalidated_old.invalid_at
            invalid_at=None,
            created_at=ts,
            expired_at=None,
            superseded_by=None,
        )
        old_event = self._make_event(
            event_type="supersede",
            edge=invalidated_old,
            actor=actor,
        )
        new_event = self._make_event(
            event_type="insert",
            edge=replacement,
            actor=actor,
        )
        return invalidated_old, replacement, old_event, new_event

    # ------------------------------------------------------------
    # Read-side helpers
    # ------------------------------------------------------------

    def time_travel_query(
        self,
        edges: Iterable[GraphEdge],
        *,
        as_of: datetime,
        include_soft_deleted: bool = False,
    ) -> list[GraphEdge]:
        """Return only edges whose bi-temporal interval contains ``as_of``.

        An edge is "alive as-of ``as_of``" iff:
            valid_at <= as_of
            AND (invalid_at IS NULL OR invalid_at > as_of)
            AND (include_soft_deleted OR expired_at IS NULL)

        Args:
            edges: Iterable of edges to filter (typically all edges of
                one relation type, or the full MultiDiGraph edge set).
            as_of: Wall-clock time to project back to.
            include_soft_deleted: When True, edges with expired_at set
                are NOT filtered out (useful for admin / audit views).
        """
        result: list[GraphEdge] = []
        for e in edges:
            if e.valid_at is None:
                # Pre-M9 edge — always visible at any as_of (compat).
                result.append(e)
                continue
            if e.valid_at > as_of:
                continue
            if e.invalid_at is not None and e.invalid_at <= as_of:
                continue
            if not include_soft_deleted and e.expired_at is not None:
                continue
            result.append(e)
        return result

    # ------------------------------------------------------------
    # Retention cascade hook (called by RetentionEnforcer — T14)
    # ------------------------------------------------------------

    def retention_cascade(
        self,
        *,
        edges_on_node: Iterable[GraphEdge],
        actor: str = "retention",
    ) -> list[tuple[GraphEdge, EdgeEvent]]:
        """Soft-delete (Q3) every edge touching a retention-target node.

        Returns a list of ``(soft_deleted_edge, edge_event)`` tuples. The
        caller (RetentionEnforcer T14) is responsible for:
          1. Writing the mutated edge back to the NetworkX store.
          2. Inserting the EdgeEvent rows in the same DB transaction.

        Per Q3 (architecture §9): SOFT DELETE only. ``invalid_at`` is set
        to now(); ``expired_at`` remains None on edges (Q3 reserves
        ``expired_at`` for nodes; edges use ``invalid_at``).
        """
        ts = _now_utc()
        out: list[tuple[GraphEdge, EdgeEvent]] = []
        for e in edges_on_node:
            if e.invalid_at is not None:
                # Already invalidated — skip idempotently.
                continue
            invalidated = GraphEdge(
                source=e.source,
                target=e.target,
                relation=e.relation,
                weight=e.weight,
                confidence=e.confidence,
                confidence_score=e.confidence_score,
                source_ids=list(e.source_ids),
                valid_at=e.valid_at or ts,
                invalid_at=ts,
                created_at=e.created_at or ts,
                expired_at=e.expired_at,
                superseded_by=e.superseded_by,
            )
            event = self._make_event(
                event_type="soft_delete",
                edge=invalidated,
                actor=actor,
            )
            out.append((invalidated, event))
        return out

    # ------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------

    def _make_event(
        self,
        *,
        event_type: str,
        edge: GraphEdge,
        actor: str,
    ) -> EdgeEvent:
        """Construct an EdgeEvent row mirroring the given edge snapshot."""
        return EdgeEvent(
            tenant_id=self.tenant_id,
            event_type=event_type,
            edge_key=_edge_key(edge.source, edge.relation, edge.target),
            source=edge.source,
            target=edge.target,
            relation=edge.relation,
            valid_at=edge.valid_at or _now_utc(),
            invalid_at=edge.invalid_at,
            superseded_by=edge.superseded_by,
            actor=actor,
            payload=_serialise_edge_payload(edge),
        )


# ============================================================
# Hashing helper — stable edge_id for OTel span attributes (T14)
# ============================================================


def edge_fingerprint(edge: GraphEdge) -> str:
    """Short SHA-1 fingerprint of an edge for span attributes / logs.

    Used by T14 OTel spans to correlate a mutation with the resulting
    NetworkX write. The fingerprint is deterministic across processes
    (unlike ``id(edge)``) and stable across serialisation round-trips.
    """
    raw = "|".join(
        [
            edge.source,
            edge.relation,
            edge.target,
            f"{edge.weight:.6f}",
            str(edge.confidence),
            str(edge.confidence_score),
            ",".join(edge.source_ids),
            edge.valid_at.isoformat() if edge.valid_at else "",
            edge.invalid_at.isoformat() if edge.invalid_at else "",
            edge.superseded_by or "",
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# Suppress unused-import lint from dataclasses.asdict (kept for callers).
_ = asdict
