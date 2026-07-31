"""Generation-isolated recording pipeline and durable projection outbox models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from audio_graphy.models.base import TenantScopedBase

PIPELINE_RUN_TERMINAL_STATES = frozenset(
    {
        "ready",
        "ready_no_speech",
        "partial",
        "failed_retryable",
        "failed_terminal",
        "superseded",
    }
)

PIPELINE_RUN_IN_PROGRESS_STATES = frozenset(
    {
        "claimed",
        "vad",
        "asr",
        "segments",
        "chunks",
        "projections",
        "verifying",
    }
)

PIPELINE_RUN_CLAIMABLE_STATES = frozenset(
    {
        "queued",
        *PIPELINE_RUN_IN_PROGRESS_STATES,
        "failed_retryable",
    }
)

PIPELINE_RUN_STATES = frozenset(
    {
        "queued",
        *PIPELINE_RUN_IN_PROGRESS_STATES,
        *PIPELINE_RUN_TERMINAL_STATES,
    }
)

PROJECTION_OUTBOX_STATES = frozenset(
    {"pending", "processing", "succeeded", "failed", "dead_letter"}
)

DEFAULT_REQUIRED_PROJECTIONS = ("file_index", "vector", "graph")

_PIPELINE_RUN_FORWARD_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"claimed"}),
    "claimed": frozenset({"vad"}),
    "vad": frozenset({"asr"}),
    # Verified silence may skip material Segment/Chunk projections, but still
    # passes the explicit VERIFYING gate.
    "asr": frozenset({"segments", "verifying"}),
    "segments": frozenset({"chunks"}),
    "chunks": frozenset({"projections"}),
    "projections": frozenset({"verifying"}),
    "verifying": frozenset({"ready", "ready_no_speech"}),
    "failed_retryable": frozenset({"claimed"}),
}


def pipeline_run_transition_allowed(current: str, target: str) -> bool:
    """Return whether a state change is legal independent of lease ownership.

    Expired in-progress runs may be reclaimed at ``claimed`` and restart their
    generation. Failure and supersession edges are legal from every non-final
    state; the caller must still enforce its lease/CAS predicates.
    """

    if current not in PIPELINE_RUN_STATES or target not in PIPELINE_RUN_STATES:
        return False
    if current == target:
        return current == "claimed"
    if target == "superseded":
        return current != "superseded"
    if target in {"partial", "failed_retryable", "failed_terminal"}:
        return current not in {
            "ready",
            "ready_no_speech",
            "partial",
            "failed_terminal",
            "superseded",
        }
    if target == "claimed" and current in PIPELINE_RUN_IN_PROGRESS_STATES:
        return True
    return target in _PIPELINE_RUN_FORWARD_TRANSITIONS.get(current, frozenset())


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RecordingPipelineRun(TenantScopedBase):
    """One immutable attempt/generation of a recording processing pipeline.

    Rows are append-only except for state, lease and checkpoint fields. A
    successful generation becomes visible only when ``Recording`` points at
    it through ``active_pipeline_run_id``.
    """

    __tablename__ = "recording_pipeline_runs"

    recording_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("recordings.id", ondelete="CASCADE"),
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="queued",
        server_default="queued",
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    required_projections: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    completed_projections: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("generation >= 1", name="ck_pipeline_runs_generation"),
        CheckConstraint("attempt_count >= 0", name="ck_pipeline_runs_attempt_count"),
        CheckConstraint(
            "state IN ('queued', 'claimed', 'vad', 'asr', 'segments', 'chunks', "
            "'projections', 'verifying', 'ready', 'ready_no_speech', 'partial', "
            "'failed_retryable', 'failed_terminal', 'superseded')",
            name="ck_pipeline_runs_state",
        ),
        Index(
            "ux_pipeline_runs_recording_generation",
            "recording_id",
            "generation",
            unique=True,
        ),
        Index(
            "ux_pipeline_runs_tenant_idempotency",
            "tenant_id",
            "idempotency_key",
            unique=True,
        ),
        Index(
            "ix_pipeline_runs_claim",
            "state",
            "lease_expires_at",
            "created_at",
        ),
        Index("ix_pipeline_runs_tenant_recording", "tenant_id", "recording_id"),
    )

    def projections_complete(self) -> bool:
        """Return whether every required durable projection has succeeded."""
        return set(self.required_projections).issubset(self.completed_projections)


class ProjectionOutbox(TenantScopedBase):
    """Durable request to build one external or denormalized projection."""

    __tablename__ = "projection_outbox"

    recording_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("recordings.id", ondelete="CASCADE"),
        nullable=False,
    )
    pipeline_run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("recording_pipeline_runs.id", ondelete="CASCADE"),
        nullable=True,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    projection_type: Mapped[str] = mapped_column(String(32), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(32), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("generation >= 1", name="ck_projection_outbox_generation"),
        CheckConstraint("attempts >= 0", name="ck_projection_outbox_attempts"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'succeeded', 'failed', 'dead_letter')",
            name="ck_projection_outbox_status",
        ),
        Index(
            "ux_projection_outbox_tenant_idempotency",
            "tenant_id",
            "idempotency_key",
            unique=True,
        ),
        Index(
            "ix_projection_outbox_claim",
            "status",
            "available_at",
            "lease_expires_at",
            "created_at",
        ),
        Index(
            "ix_projection_outbox_run",
            "pipeline_run_id",
            "projection_type",
            "aggregate_type",
            "aggregate_id",
        ),
        Index("ix_projection_outbox_tenant_recording", "tenant_id", "recording_id"),
    )
