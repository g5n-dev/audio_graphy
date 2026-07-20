"""VectorChunk ORM model — chunk embedding storage for brute-force cosine search.

Stores chunk embeddings (bge-m3, 1024-dim float32) as binary BLOB. Each chunk
has exactly one embedding (UNIQUE per tenant). Cascades on chunk deletion.

Table: vectors_chunk
Inherits: TenantScopedBase
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey, Index, LargeBinary
from sqlalchemy.orm import Mapped, mapped_column, relationship

from audio_graphy.models.base import TenantScopedBase

if TYPE_CHECKING:
    from audio_graphy.models.chunk import Chunk


class VectorChunk(TenantScopedBase):
    """文本块向量表 | VectorChunk — chunk embedding for cosine search.

    Phase 1 uses brute-force cosine similarity search. Each row stores
    a 1024-dimensional bge-m3 embedding as a 4096-byte binary BLOB.
    One embedding per chunk per tenant (UNIQUE constraint).

    Key constraints:
        - FK(chunk_id -> chunks.id) ON DELETE CASCADE.
        - UNIQUE(tenant_id, chunk_id): one embedding per chunk per tenant.
        - INDEX(chunk_id): lookup by chunk.
    """

    __tablename__ = "vectors_chunk"

    chunk_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("chunks.id", ondelete="CASCADE"),
        nullable=False,
    )
    embedding: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    chunk: Mapped[Chunk] = relationship(back_populates="vector_chunks")

    __table_args__ = (
        Index("ux_vectors_chunk_chunk_id", "tenant_id", "chunk_id", unique=True),
        Index("ix_vectors_chunk_chunk_id", "chunk_id"),
    )
