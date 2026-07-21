"""VectorAudio ORM — CLAP 512-d segment embedding (M7 P0-13).

Stores CLAP audio embeddings (laion_clap, 512-dim float32) as plaintext
binary BLOB. CLAP vectors are NOT biometric (PRD §6.4); PIPL §14.3 does
not apply. Plaintext storage avoids per-decrypt overhead during retrieval.

Table: ``vectors_audio``. Inherits ``TenantScopedBase``.

See: M7 architecture §13.1.2.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Float, ForeignKey, Index, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from audio_graphy.models.base import TenantScopedBase

if TYPE_CHECKING:
    from audio_graphy.models.recording import Recording


class VectorAudio(TenantScopedBase):
    """CLAP audio embedding row.

    Attributes:
        recording_id: Source recording (FK ON DELETE CASCADE).
        segment_id: Segment index within the recording.
        chunk_id: Optional chunk_id linkage when segment → chunk is 1:1.
        vector: 512-d float32 little-endian bytes (plaintext).
        dim: Always 512 (L1 locked).
        model: Model identifier reported by the service.
        duration_sec: Audio duration in seconds.
    """

    __tablename__ = "vectors_audio"

    recording_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("recordings.id", ondelete="CASCADE"),
        nullable=False,
    )
    segment_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunk_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    vector: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False, default=512)
    model: Mapped[str] = mapped_column(
        String(64), nullable=False, default="clap-htsat-base-2022"
    )
    duration_sec: Mapped[float] = mapped_column(Float, default=0.0)

    recording: Mapped[Recording] = relationship()

    __table_args__ = (
        Index("ix_va_tenant_recording", "tenant_id", "recording_id"),
        Index("ix_va_segment", "segment_id"),
    )


__all__ = ["VectorAudio"]
