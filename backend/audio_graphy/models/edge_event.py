"""EdgeEvent ORM model — append-only bi-temporal event log (M9 §11.1).

Every mutation against the in-memory MultiDiGraph (insert / merge /
supersede / soft-delete) writes ONE row here for audit + time-travel
queries. The actual graph still lives in NetworkX + GraphML; this table
is the durable, queryable projection of edge state changes.

Schema source of truth: ``docs/m9-architecture.md`` §11.1, §21.1.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from audio_graphy.models.base import TenantScopedBase


class EdgeEvent(TenantScopedBase):
    """Append-only row recording a single mutation against a graph edge.

    Attributes:
        event_type: One of ``insert`` / ``merge`` / ``supersede`` /
            ``soft_delete`` / ``restore``.
        edge_key: Stable key ``"{source}|{relation}|{target}"`` (no whitespace).
        source: Source entity_id of the edge.
        target: Target entity_id of the edge.
        relation: Relation description (free-text Chinese / English).
        valid_at: When the edge became true in the real world.
        invalid_at: When the edge ceased to be true (NULL = open).
        superseded_by: Q1 supersede pointer — edge_key of replacement.
        actor: ``"system"`` / ``"user:{id}"`` / ``"retention"`` / etc.
        payload: JSON blob with full edge snapshot (optional, debug only).
    """

    __tablename__ = "edge_events"

    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    edge_key: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    target: Mapped[str] = mapped_column(String(128), nullable=False)
    relation: Mapped[str] = mapped_column(String(128), nullable=False)
    valid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    invalid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('insert', 'merge', 'supersede', 'soft_delete', 'restore')",
            name="ck_edge_events_event_type",
        ),
        Index("ix_edge_events_tenant_valid", "tenant_id", "valid_at"),
        Index("ix_edge_events_edge_key", "edge_key"),
        Index("ix_edge_events_event_type", "event_type"),
    )
