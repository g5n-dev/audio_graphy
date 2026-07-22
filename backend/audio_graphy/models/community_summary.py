"""CommunitySummary ORM model — GraphRAG community summary cache (M9 §11.2).

Stores LLM-generated natural-language summaries of Leiden communities at
multiple hierarchy levels. Populated eagerly for level 0 + leaves (Q2)
and lazily for levels 1-2. Level 3 is dropped entirely (Q2 ruling).

Schema source of truth: ``docs/m9-architecture.md`` §11.2, §21.2, §8 (Q2).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from audio_graphy.models.base import TenantScopedBase


class CommunitySummary(TenantScopedBase):
    """Cache row for one community summary.

    Attributes:
        leiden_job_id: FK to ``leiden_jobs.id`` (the run that produced this
            community). On ``leiden_jobs`` deletion the row is RESTRICT'd
            (deleting a job invalidates its communities; do it explicitly).
        level: Hierarchy depth 0..2 (level 3 dropped per Q2).
        community_id: Leiden-assigned integer community id within the level.
        title: Short LLM-assigned title (≤ 80 chars).
        summary: Full LLM summary text.
        member_count: Number of nodes in the community at write time.
        member_node_ids: JSON list of entity_id strings (for debugging /
            membership reconstruction).
        generated_at: Wall-clock time the LLM produced this summary.
        strategy: ``eager`` (level 0 + leaf) or ``lazy`` (level 1-2), per Q2.
    """

    __tablename__ = "community_summaries"

    leiden_job_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("leiden_jobs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    community_id: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(80), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    member_node_ids: Mapped[str] = mapped_column(Text, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    strategy: Mapped[str] = mapped_column(String(16), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "leiden_job_id", "level", "community_id", name="ux_cs_job_level_comm"
        ),
        Index("ix_community_summaries_tenant_level", "tenant_id", "level"),
    )
