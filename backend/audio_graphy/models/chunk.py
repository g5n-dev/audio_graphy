"""Chunk ORM model — text chunks for graph extraction and vector search.

Multiple segments are packed into chunks based on token budget. Chunks are
the basic unit for knowledge-graph entity extraction and vector embedding.

Table: chunks
Inherits: TenantScopedBase (denormalized tenant_id for query efficiency)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import JSON, BigInteger, CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from audio_graphy.models.base import TenantScopedBase

if TYPE_CHECKING:
    from audio_graphy.models.recording import Recording
    from audio_graphy.models.vector_chunk import VectorChunk


class Chunk(TenantScopedBase):
    """文本块表 | Chunk — packed text block for extraction and embedding.

    Groups multiple segments into a token-budget-bounded text block.
    The ``segment_ids`` JSON array traces back to source segments.
    ``content_hash`` enables idempotent deduplication.

    Key constraints:
        - FK(recording_id -> recordings.id) ON DELETE CASCADE.
        - UNIQUE(tenant_id, content_hash): content deduplication per tenant.
        - CHECK(token_n > 0): token count must be positive.
        - JSON(segment_ids): MySQL 8 native JSON array.
        - Denormalized tenant_id for middleware-level filtering.
    """

    __tablename__ = "chunks"

    recording_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("recordings.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Nullable only for bootstrap/legacy rows. New pipeline and streaming
    # writes always bind a chunk to its immutable processing generation.
    pipeline_run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("recording_pipeline_runs.id", ondelete="CASCADE"),
        nullable=True,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Legacy rows may be NULL until the resumable backfill assigns a stable
    # per-generation ordering.
    ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    segment_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_n: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    recording: Mapped[Recording] = relationship(back_populates="chunks")
    vector_chunks: Mapped[list[VectorChunk]] = relationship(
        back_populates="chunk", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("token_n > 0", name="ck_chunks_token_n"),
        CheckConstraint("generation >= 0", name="ck_chunks_generation"),
        CheckConstraint("ordinal IS NULL OR ordinal >= 0", name="ck_chunks_ordinal"),
        Index(
            "ux_chunks_recording_generation_ordinal",
            "recording_id",
            "generation",
            "ordinal",
            unique=True,
        ),
        # A content hash is a cache hint, never the identity of a provenance
        # row: equal text in two recordings must coexist.
        Index("ix_chunks_content_hash", "tenant_id", "content_hash"),
        Index("ix_chunks_pipeline_run_id", "pipeline_run_id"),
        Index("ix_chunks_recording_id", "recording_id"),
    )


class ChunkSegment(TenantScopedBase):
    """Normalized, generation-safe provenance between chunks and segments."""

    __tablename__ = "chunk_segments"

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
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("chunks.id", ondelete="CASCADE"),
        nullable=False,
    )
    segment_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("segments.id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint("generation >= 0", name="ck_chunk_segments_generation"),
        CheckConstraint("ordinal >= 0", name="ck_chunk_segments_ordinal"),
        Index(
            "ux_chunk_segments_chunk_ordinal",
            "chunk_id",
            "ordinal",
            unique=True,
        ),
        Index(
            "ux_chunk_segments_chunk_segment",
            "chunk_id",
            "segment_id",
            unique=True,
        ),
        Index(
            "ix_chunk_segments_recording_generation",
            "recording_id",
            "generation",
        ),
        Index("ix_chunk_segments_segment_id", "segment_id"),
        Index("ix_chunk_segments_pipeline_run_id", "pipeline_run_id"),
    )
