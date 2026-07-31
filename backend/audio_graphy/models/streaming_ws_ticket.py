"""Single-use, tenant-scoped WebSocket admission tickets."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from audio_graphy.models.base import TenantScopedBase


class StreamingWSTicket(TenantScopedBase):
    """Short-lived admission capability; only its SHA-256 hash is stored."""

    __tablename__ = "streaming_ws_tickets"

    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    recording_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("recordings.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    consent_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="ISSUED")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('ISSUED', 'CONSUMED', 'EXPIRED', 'REVOKED')",
            name="ck_streaming_ws_tickets_state",
        ),
        Index("ux_streaming_ws_tickets_hash", "token_hash", unique=True),
        Index(
            "ix_streaming_ws_tickets_tenant_expiry",
            "tenant_id",
            "state",
            "expires_at",
        ),
    )


__all__ = ["StreamingWSTicket"]
