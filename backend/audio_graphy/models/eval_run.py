"""EvalRun ORM — async evaluation run state (M6 WS-2).

Tracks one async evaluation run from ``POST /api/v1/eval/runs`` through
completion. APScheduler picks up rows with ``status='pending'`` and
transitions them through ``running`` → ``completed`` | ``failed``.

Key design:
    - PK = UUID hex string (36 chars) returned to the API client.
    - INDEX(tenant_id, status) for scheduler queue lookups.
    - JSON(aggregate_metrics): 8 metrics mean values + ``entity_f1_strict``
      / ``entity_f1_fuzzy``.
    - JSON(config): snapshot of pipeline / k / judge / position_debias.

See: docs/m6-architecture.md §4.1.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from audio_graphy.models.base import TenantScopedBase

_VALID_PIPELINE = ("mock", "rag")
_VALID_STATUS = ("pending", "running", "completed", "failed")


class EvalRunORM(TenantScopedBase):
    """EvalRun — one async evaluation run.

    Table: ``eval_runs``. Inherits ``created_at`` / ``updated_at`` /
    ``tenant_id`` from ``TenantScopedBase``.

    Attributes:
        id: UUID4 hex string (primary key).
        gold_set_path: Absolute path to the gold set YAML.
        pipeline: ``"mock"`` | ``"rag"``.
        judge_enabled: Whether LLM-as-judge metrics were enabled.
        k_value: Cutoff ``k`` for ``context_precision_at_k``.
        status: ``pending`` | ``running`` | ``completed`` | ``failed``.
        config: Snapshot of input config (metadata, position_debias, etc.).
        aggregate_metrics: Mean metric values populated on completion.
        report_markdown_path: Path to generated Markdown report (or None).
        report_json_path: Path to generated JSON report (or None).
        error_message: Failure detail (only set when status=failed).
        started_at: When the run was created (queued).
        finished_at: When the run reached completed | failed.
    """

    __tablename__ = "eval_runs"

    # UUID-string PK overrides the default BigInteger id from Base.
    id: Mapped[str] = mapped_column(  # type: ignore[assignment]
        String(36), primary_key=True, comment="UUID4 hex"
    )
    gold_set_path: Mapped[str] = mapped_column(String(512), nullable=False)
    pipeline: Mapped[str] = mapped_column(String(32), nullable=False, comment="mock|rag")
    judge_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    k_value: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
        comment="pending|running|completed|failed",
    )
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    aggregate_metrics: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    report_markdown_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    report_json_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(f"pipeline IN {_VALID_PIPELINE}", name="ck_eval_runs_pipeline"),
        CheckConstraint(f"status IN {_VALID_STATUS}", name="ck_eval_runs_status"),
        Index("ix_eval_runs_tenant_status", "tenant_id", "status"),
        Index("ix_eval_runs_started_at", "started_at"),
    )

    def to_dict(self) -> dict[str, Any]:
        """Public serialization (excludes internal fields)."""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "gold_set_path": self.gold_set_path,
            "pipeline": self.pipeline,
            "judge_enabled": bool(self.judge_enabled),
            "k_value": int(self.k_value),
            "status": self.status,
            "config": dict(self.config or {}),
            "aggregate_metrics": dict(self.aggregate_metrics or {})
            if self.aggregate_metrics is not None
            else None,
            "report_markdown_path": self.report_markdown_path,
            "report_json_path": self.report_json_path,
            "error_message": self.error_message,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


__all__ = ["EvalRunORM"]
