"""StreamingSession ORM model — one row per WebSocket session (M8 P0-11).

Tracks each ``/ws/stream`` connection's lifecycle: started_at / ended_at /
last_chunk_at, segment counts, byte throughput, error counts, end reason.
Used for audit, observability, and crash-recovery postmortems.

Table: streaming_sessions
Inherits: TenantScopedBase (tenant_id denormalized for query efficiency).

Schema source of truth: ``docs/m8-architecture.md`` §14.1.1.
"""

from __future__ import annotations

from datetime import datetime
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
)
from sqlalchemy.orm import Mapped, mapped_column

from audio_graphy.models.base import TenantScopedBase


class StreamingSession(TenantScopedBase):
    """M8 streaming_sessions row — one per ``/ws/stream`` WebSocket connection.

    Attributes mirror ``core/stream_session.py:StreamSession.stats()`` plus
    DB-specific provenance (FK to recordings, audit timestamps).

    Key constraints:
        - UNIQUE(session_id): client-supplied UUID v4, used for reconnect.
        - FK(recording_id -> recordings.id) ON DELETE CASCADE.
        - CHECK(status): valid session lifecycle values.
        - INDEX(tenant_id, started_at): per-tenant session history queries.
        - INDEX(recording_id): trace sessions by recording.
    """

    __tablename__ = "streaming_sessions"

    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="RESERVING")
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pipeline_run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("recording_pipeline_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    ack_seq_high_watermark: Mapped[int] = mapped_column(Integer, nullable=False, default=-1)
    durable_segment_high_watermark: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recording_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("recordings.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_chunk_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    seg_confirmed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    seg_realtime_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bytes_in: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    end_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    consent_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # M8 P0-12 metrics payload (optional; populated on session_closed).
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('RESERVING', 'ACTIVE', 'DRAINING', 'COMMITTING', "
            "'CLOSED', 'INCOMPLETE', 'FAILED')",
            name="ck_streaming_sessions_status",
        ),
        CheckConstraint(
            "epoch >= 1 AND generation >= 0 AND ack_seq_high_watermark >= -1",
            name="ck_streaming_sessions_watermarks",
        ),
        CheckConstraint(
            "end_reason IS NULL OR end_reason IN "
            "('normal', 'client_disconnect', 'server_shutdown', 'error', 'backpressure', 'timeout')",
            name="ck_streaming_sessions_end_reason",
        ),
        Index("ix_streaming_sessions_tenant_started", "tenant_id", "started_at"),
        Index("ix_streaming_sessions_recording", "recording_id"),
        Index("ix_streaming_sessions_pipeline_run", "pipeline_run_id"),
        Index(
            "ux_streaming_sessions_tenant_session_epoch",
            "tenant_id",
            "session_id",
            "epoch",
            unique=True,
        ),
    )
