"""Durable staging for acknowledged WebSocket PCM frames."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    LargeBinary,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from audio_graphy.models.base import TenantScopedBase


class StreamingPCMFrame(TenantScopedBase):
    """One client sequence frame retained until its Segment is durable."""

    __tablename__ = "streaming_pcm_frames"

    session_key: Mapped[str] = mapped_column(String(128), nullable=False)
    recording_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("recordings.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pcm: Mapped[bytes] = mapped_column(LargeBinary(length=65_536), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="ACCEPTED")
    consumed_segment_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("segments.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint("source_seq >= 0", name="ck_stream_pcm_frames_source_seq"),
        CheckConstraint(
            "state IN ('ACCEPTED', 'CONSUMED', 'ORPHANED')",
            name="ck_stream_pcm_frames_state",
        ),
        CheckConstraint(
            "length(pcm) > 0 AND length(pcm) <= 65536",
            name="ck_stream_pcm_frames_size",
        ),
        Index(
            "ux_stream_pcm_frames_source",
            "tenant_id",
            "session_key",
            "source_seq",
            unique=True,
        ),
        Index(
            "ix_stream_pcm_frames_replay",
            "tenant_id",
            "session_key",
            "recording_id",
            "state",
            "source_seq",
        ),
    )


__all__ = ["StreamingPCMFrame"]
