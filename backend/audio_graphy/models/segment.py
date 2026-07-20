"""Segment ORM model — VAD-split audio segments.

Each segment contains ASR transcript text, speaker label, time offsets,
and VAD confidence. Segments are the finest granularity of audio data
before being grouped into chunks.

Table: segments
Inherits: TenantScopedBase (denormalized tenant_id for query efficiency)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, CheckConstraint, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from audio_graphy.models.base import TenantScopedBase

if TYPE_CHECKING:
    from audio_graphy.models.recording import Recording


class Segment(TenantScopedBase):
    """录音分段表 | Segment — VAD-split audio segment.

    Represents a contiguous segment of audio after VAD processing.
    Contains ASR transcript, speaker label, and time offsets.

    Key constraints:
        - FK(recording_id -> recordings.id) ON DELETE CASCADE.
        - UNIQUE(recording_id, idx): segment index uniqueness per recording.
        - CHECK(end_sec > start_sec): time ordering validation.
        - Denormalized tenant_id for middleware-level filtering.
    """

    __tablename__ = "segments"

    recording_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("recordings.id", ondelete="CASCADE"),
        nullable=False,
    )
    idx: Mapped[int] = mapped_column(Integer, nullable=False)
    start_sec: Mapped[float] = mapped_column(Float, nullable=False)
    end_sec: Mapped[float] = mapped_column(Float, nullable=False)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    speaker: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vad_conf: Mapped[float | None] = mapped_column(Float, nullable=True)

    recording: Mapped[Recording] = relationship(back_populates="segments")

    __table_args__ = (
        CheckConstraint("end_sec > start_sec", name="ck_segments_time_order"),
        Index("ux_segments_recording_idx", "recording_id", "idx", unique=True),
        Index("ix_segments_recording_id", "recording_id"),
    )
