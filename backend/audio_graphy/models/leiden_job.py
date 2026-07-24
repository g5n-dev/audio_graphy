"""LeidenJob ORM model — one Leiden community-detection run (M9 §11.3).

Records the lifecycle + partition snapshot location for each Leiden run
so incremental runs (HIT-Leiden L2) can load the prior partition and
compute the diff against current graph state.

Schema source of truth: ``docs/m9-architecture.md`` §11.3, §21.2.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from audio_graphy.models.base import TenantScopedBase


class LeidenJob(TenantScopedBase):
    """One Leiden community-detection run.

    Attributes:
        job_type: ``full`` (cold-start or threshold exceeded) or
            ``incremental`` (HIT-Leiden within 30% threshold).
        status: ``pending`` / ``running`` / ``succeeded`` / ``failed``.
        triggered_by: ``"manual"`` / ``"scheduled"`` / ``"threshold"`` /
            ``"backfill"``.
        node_count_snapshot: Number of nodes in the graph at run start.
        edge_count_snapshot: Number of edges in the graph at run start.
        diff_percent: For incremental runs: |Δ| / N as a percentage.
            NULL for full runs.
        modularity: Q score achieved (NULL if computation failed).
        levels: Number of hierarchy levels actually computed (0..3, capped
            at 2 in practice because level 3 is dropped per Q2).
        snapshot_path: Relative path under ``storage/`` to the pickled
            ``PartitionSnapshot`` (architecture §7.2).
        error_message: Populated only on status=failed.
        started_at: When the worker picked up the job.
        finished_at: When the worker finished (success or failure).
    """

    __tablename__ = "leiden_jobs"

    job_type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    triggered_by: Mapped[str] = mapped_column(String(32), nullable=False)
    node_count_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    edge_count_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    diff_percent: Mapped[float | None] = mapped_column(nullable=True)
    modularity: Mapped[float | None] = mapped_column(nullable=True)
    levels: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshot_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "job_type IN ('full', 'incremental')",
            name="ck_leiden_jobs_job_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_leiden_jobs_status",
        ),
        Index("ix_leiden_jobs_tenant_status", "tenant_id", "status"),
        Index("ix_leiden_jobs_finished", "finished_at"),
    )
