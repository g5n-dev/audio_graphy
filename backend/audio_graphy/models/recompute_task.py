"""RecomputeTask ORM model — tracks prompt-version-switch recompute jobs.

Stores progress counters (total/processed/changed/cached_hits/llm_calls)
and lifecycle status (pending/running/done/failed).

Table: recompute_tasks
Inherits: TenantScopedBase

See: docs/m3-architecture.md §1.1 (C7), docs/m3-prd.md TAG-07.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from audio_graphy.models.base import TenantScopedBase


class RecomputeTask(TenantScopedBase):
    """重算任务表 | RecomputeTask — tracks batch tag re-computation progress.

    Created when a prompt version is activated (or POST /tags/recompute).
    Updated by the pipeline worker as each recording is re-tagged.

    Key constraints:
        - task_id is a string PK alternative (unique per tenant).
        - CHECK(status): valid lifecycle values.
    """

    __tablename__ = "recompute_tasks"

    task_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
    )
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    changed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'done', 'failed')",
            name="ck_recompute_tasks_status",
        ),
        Index("ix_recompute_tasks_tenant_status", "tenant_id", "status"),
        Index("ix_recompute_tasks_prompt_version", "prompt_version"),
    )
