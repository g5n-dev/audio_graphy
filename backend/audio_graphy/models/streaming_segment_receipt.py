"""Idempotency receipts for durable streaming segment publication."""

from __future__ import annotations

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from audio_graphy.models.base import TenantScopedBase


class StreamingSegmentReceipt(TenantScopedBase):
    """Bind one stable WS source event to its persisted Segment and Chunk."""

    __tablename__ = "streaming_segment_receipts"

    streaming_session_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("streaming_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_key: Mapped[str] = mapped_column(String(128), nullable=False)
    source_event_key: Mapped[str] = mapped_column(String(128), nullable=False)
    source_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
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
    segment_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("segments.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("chunks.id", ondelete="CASCADE"),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint("source_seq >= 0", name="ck_stream_receipts_source_seq"),
        CheckConstraint("generation >= 1", name="ck_stream_receipts_generation"),
        Index(
            "ux_stream_receipts_source_event",
            "tenant_id",
            "session_key",
            "source_event_key",
            unique=True,
        ),
        Index(
            "ix_stream_receipts_recording_generation",
            "recording_id",
            "generation",
        ),
        Index("ix_stream_receipts_segment_id", "segment_id"),
        Index("ix_stream_receipts_chunk_id", "chunk_id"),
    )


__all__ = ["StreamingSegmentReceipt"]
