"""Durable, tenant-scoped outbox for irreversible privacy erasure work."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from audio_graphy.models.base import TenantScopedBase, _utcnow


class ErasureOutbox(TenantScopedBase):
    """Deletion intent that survives removal of its source aggregate.

    The row deliberately has no foreign key to ``recordings``: committing the
    database erasure and this intent in one transaction is what makes external
    GraphML, FileIndex, cache and file cleanup recoverable after a crash.
    """

    __tablename__ = "erasure_outbox"

    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
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
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'succeeded', 'failed', 'dead_letter')",
            name="ck_erasure_outbox_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_erasure_outbox_attempts"),
        Index(
            "ux_erasure_outbox_subject",
            "tenant_id",
            "subject_type",
            "subject_id",
            unique=True,
        ),
        Index(
            "ix_erasure_outbox_claim",
            "status",
            "available_at",
            "lease_expires_at",
        ),
    )


__all__ = ["ErasureOutbox"]
