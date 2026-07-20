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
        Index("ux_chunks_content_hash", "tenant_id", "content_hash", unique=True),
        Index("ix_chunks_recording_id", "recording_id"),
    )
