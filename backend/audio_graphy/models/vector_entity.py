"""VectorEntity ORM model — entity embedding storage for brute-force cosine search.

Stores entity embeddings (bge-m3, 1024-dim float32) as binary BLOB. Entity IDs
reference NetworkX graph files (no physical FK to MySQL). Application layer
handles numpy float32 <-> bytes serialization.

Table: vectors_entity
Inherits: TenantScopedBase
"""

from __future__ import annotations

from sqlalchemy import Index, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from audio_graphy.models.base import TenantScopedBase


class VectorEntity(TenantScopedBase):
    """实体向量表 | VectorEntity — entity embedding for cosine search.

    Phase 1 uses brute-force cosine similarity search. Each row stores
    a 1024-dimensional bge-m3 embedding as a 4096-byte binary BLOB.

    Key constraints:
        - INDEX(tenant_id, entity_id): lookup by entity within tenant.
        - No physical FK: entities stored in NetworkX GraphML files.
    """

    __tablename__ = "vectors_entity"

    entity_id: Mapped[str] = mapped_column(String(255), nullable=False)
    embedding: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    __table_args__ = (Index("ix_vectors_entity_entity_id", "tenant_id", "entity_id"),)
