"""Immutable reception timeline, audio operation, and artifact records."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from audio_graphy.models.base import TenantScopedBase


class ReceptionTimelineRevision(TenantScopedBase):
    __tablename__ = "reception_timeline_revisions"

    reception_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("receptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_reception_version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="STAGING")
    plan_signature: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_manifest: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    total_duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    physical_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    warnings: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('STAGING', 'ACTIVE', 'SUPERSEDED', 'CANCELLED', 'FAILED')",
            name="ck_reception_timeline_revisions_state",
        ),
        CheckConstraint(
            "revision >= 1 AND expected_reception_version >= 1 "
            "AND total_duration_ms > 0",
            name="ck_reception_timeline_revisions_values",
        ),
        Index(
            "ux_reception_timeline_revisions_revision",
            "reception_id",
            "revision",
            unique=True,
        ),
        Index(
            "ux_reception_timeline_revisions_token",
            "plan_token_hash",
            unique=True,
        ),
        Index(
            "ix_reception_timeline_revisions_active",
            "tenant_id",
            "reception_id",
            "state",
        ),
    )


class ReceptionAudioOperation(TenantScopedBase):
    __tablename__ = "reception_audio_operations"

    reception_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("receptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    timeline_revision_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("reception_timeline_revisions.id", ondelete="CASCADE"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    expected_reception_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "mode IN ('logical', 'physical', 'both')",
            name="ck_reception_audio_operations_mode",
        ),
        CheckConstraint(
            "status IN ('queued', 'claimed', 'probing', 'slicing', "
            "'assembling', 'encrypting', 'verifying', 'committing', "
            "'succeeded', 'failed', 'cancelled')",
            name="ck_reception_audio_operations_status",
        ),
        CheckConstraint(
            "progress >= 0 AND progress <= 1 AND attempt_count >= 0",
            name="ck_reception_audio_operations_progress",
        ),
        Index(
            "ux_reception_audio_operations_idempotency",
            "tenant_id",
            "reception_id",
            "idempotency_key",
            unique=True,
        ),
        Index(
            "ix_reception_audio_operations_claim",
            "tenant_id",
            "status",
            "lease_expires_at",
        ),
    )


class ReceptionAudioArtifact(TenantScopedBase):
    __tablename__ = "reception_audio_artifacts"

    reception_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("receptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    timeline_revision_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("reception_timeline_revisions.id", ondelete="CASCADE"),
        nullable=False,
    )
    operation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("reception_audio_operations.id", ondelete="CASCADE"),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="PREPARING")
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channels: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN ('PREPARING', 'READY', 'ATTACHED', 'RETIRED', "
            "'DELETED', 'FAILED', 'ORPHANED')",
            name="ck_reception_audio_artifacts_state",
        ),
        CheckConstraint(
            "(size_bytes IS NULL OR size_bytes > 0) AND "
            "(duration_ms IS NULL OR duration_ms > 0) AND "
            "(sample_rate IS NULL OR sample_rate > 0) AND "
            "(channels IS NULL OR channels > 0)",
            name="ck_reception_audio_artifacts_media",
        ),
        Index(
            "ix_reception_audio_artifacts_active",
            "tenant_id",
            "reception_id",
            "state",
        ),
        Index("ux_reception_audio_artifacts_path", "path", unique=True),
    )


__all__ = [
    "ReceptionAudioArtifact",
    "ReceptionAudioOperation",
    "ReceptionTimelineRevision",
]
