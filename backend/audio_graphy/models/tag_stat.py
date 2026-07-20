"""TagStat ORM model — tag statistics aggregation (Layer 3).

Stores multi-level tag counts/distributions for reporting and dashboards.
Refreshed incrementally by the application layer (delta aggregation: -old +new).

Table: tag_stats
Inherits: TenantScopedBase
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from audio_graphy.models.base import TenantScopedBase


class TagStat(TenantScopedBase):
    """标签统计聚合表 | TagStat — multi-level tag count distribution.

    Aggregates tag counts by (store_id, agent_name, tag_path, tag_value)
    dimensions. The ``tag_count`` column (renamed from ``count`` to avoid
    MySQL reserved word conflict) stores the occurrence count.

    Key constraints:
        - UNIQUE(tenant_id, store_id, agent_name, tag_path, tag_value):
          dimension combination uniqueness.
        - CHECK(tag_count >= 0): count must be non-negative.
        - INDEX(tenant_id, store_id): drill-down by store.
    """

    __tablename__ = "tag_stats"

    store_id: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tag_path: Mapped[str] = mapped_column(String(100), nullable=False)
    tag_value: Mapped[str] = mapped_column(String(100), nullable=False)
    tag_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint("tag_count >= 0", name="ck_tag_stats_count"),
        Index(
            "ux_tag_stats_dim",
            "tenant_id",
            "store_id",
            "agent_name",
            "tag_path",
            "tag_value",
            unique=True,
        ),
        Index("ix_tag_stats_tenant_store", "tenant_id", "store_id"),
    )
