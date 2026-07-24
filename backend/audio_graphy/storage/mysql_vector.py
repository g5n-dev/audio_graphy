"""MySQL brute-force cosine vector store (Phase 1).

Stores entity/chunk embeddings as float32 BLOBs in MySQL and performs
brute-force cosine similarity search via numpy matrix multiplication.

Complexity: O(N × dim) per query — suitable for < 100k vectors (DESIGN.md §7.4).
No ANN index; equivalent to NanoVectorDB at small scale.

Tables:
    - vectors_entity: (tenant_id, entity_id, embedding BLOB)
    - vectors_chunk:  (tenant_id, chunk_id, embedding BLOB)
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.core.types import VectorSearchHit
from audio_graphy.models.vector_chunk import VectorChunk
from audio_graphy.models.vector_entity import VectorEntity
from audio_graphy.storage.vector_index_cache import (
    NormalizedVectorIndex,
    TenantVectorIndexCache,
    VectorCacheStats,
    VectorLoadResult,
    build_normalized_index,
    search_normalized_index,
    stream_normalized_index,
    validate_vector_load_options,
)

if TYPE_CHECKING:
    pass


class MySQLVectorStore:
    """Phase 1 brute-force cosine vector store backed by MySQL.

    Vectors are stored as float32 BLOBs (1024 dim × 4 bytes = 4096 bytes/row).
    Search loads all vectors for a tenant into a numpy matrix, then performs
    a single matrix-vector multiplication for cosine similarity.

    Args:
        session_factory: SQLAlchemy ``async_sessionmaker`` for DB access.
        dim: Vector dimension (default 1024, bge-m3).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        dim: int = 1024,
        cache_ttl_seconds: float = 60.0,
        cache_max_entries: int = 32,
        cache_max_bytes: int = 512 * 1024 * 1024,
        load_batch_rows: int = 512,
        load_max_rows: int = 100_000,
        load_max_source_bytes: int = 512 * 1024 * 1024,
        load_max_memory_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        validate_vector_load_options(
            batch_rows=load_batch_rows,
            max_rows=load_max_rows,
            max_source_bytes=load_max_source_bytes,
            max_memory_bytes=load_max_memory_bytes,
        )
        self._session_factory = session_factory
        self._dim = dim
        self._load_batch_rows = load_batch_rows
        self._load_max_rows = load_max_rows
        self._load_max_source_bytes = load_max_source_bytes
        self._load_max_memory_bytes = load_max_memory_bytes
        self._index_cache = TenantVectorIndexCache(
            ttl_seconds=cache_ttl_seconds,
            max_entries=cache_max_entries,
            max_bytes=cache_max_bytes,
        )

    @property
    def dim(self) -> int:
        """Vector dimension."""
        return self._dim

    @property
    def cache_stats(self) -> VectorCacheStats:
        """Process-local normalized-index cache counters."""

        return self._index_cache.stats

    def invalidate_tenant(self, tenant_id: str) -> None:
        """Invalidate both vector channels for a tenant.

        Bulk importers that bypass this store should call this method after
        committing their transaction.
        """

        self._index_cache.invalidate(("entity", tenant_id))
        self._index_cache.invalidate(("chunk", tenant_id))

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _serialize(vec: tuple[float, ...]) -> bytes:
        """Convert a float tuple to a float32 little-endian BLOB.

        Args:
            vec: Input vector as a tuple of floats.

        Returns:
            4 × len(vec) bytes (float32 little-endian).
        """
        arr = np.asarray(vec, dtype=np.float32)
        return arr.tobytes()

    @staticmethod
    def _deserialize(blob: bytes) -> tuple[float, ...]:
        """Convert a float32 BLOB back to a float tuple.

        Args:
            blob: Binary blob from MySQL.

        Returns:
            Tuple of floats.
        """
        arr = np.frombuffer(blob, dtype=np.float32)
        return tuple(float(v) for v in arr)

    # ------------------------------------------------------------------
    # Entity vectors
    # ------------------------------------------------------------------

    async def upsert_entity_vector(
        self,
        tenant_id: str,
        entity_id: str,
        embedding: tuple[float, ...],
    ) -> None:
        """Insert or update an entity embedding.

        Args:
            tenant_id: Tenant scope.
            entity_id: Entity identifier (normalised name).
            embedding: Embedding vector.
        """
        blob = self._serialize(embedding)
        async with self._session_factory() as session:
            # Check existing
            stmt = select(VectorEntity).where(
                VectorEntity.tenant_id == tenant_id,
                VectorEntity.entity_id == entity_id,
            )
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()
            if existing is not None:
                existing.embedding = blob
            else:
                session.add(
                    VectorEntity(
                        tenant_id=tenant_id,
                        entity_id=entity_id,
                        embedding=blob,
                    )
                )
            await session.commit()
        self._index_cache.invalidate(("entity", tenant_id))

    async def search_entities(
        self,
        tenant_id: str,
        query_vec: tuple[float, ...],
        *,
        top_k: int = 10,
    ) -> list[VectorSearchHit]:
        """Brute-force cosine top-k search over entity vectors.

        Args:
            tenant_id: Tenant scope.
            query_vec: Query embedding vector.
            top_k: Number of results to return.

        Returns:
            List of VectorSearchHit sorted by descending cosine similarity.
        """

        async def load() -> VectorLoadResult:
            return await self._load_vectors(
                VectorEntity,
                VectorEntity.tenant_id,
                tenant_id,
                "entity_id",
            )

        index = await self._index_cache.get_or_load(
            ("entity", tenant_id),
            load,
            dim=self._dim,
        )
        return await asyncio.to_thread(
            search_normalized_index,
            index,
            query_vec,
            top_k=top_k,
        )

    # ------------------------------------------------------------------
    # Chunk vectors
    # ------------------------------------------------------------------

    async def upsert_chunk_vector(
        self,
        tenant_id: str,
        chunk_id: int,
        embedding: tuple[float, ...],
    ) -> None:
        """Insert or update a chunk embedding.

        Args:
            tenant_id: Tenant scope.
            chunk_id: Chunk database ID.
            embedding: Embedding vector.
        """
        blob = self._serialize(embedding)
        async with self._session_factory() as session:
            stmt = select(VectorChunk).where(
                VectorChunk.tenant_id == tenant_id,
                VectorChunk.chunk_id == chunk_id,
            )
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()
            if existing is not None:
                existing.embedding = blob
            else:
                session.add(
                    VectorChunk(
                        tenant_id=tenant_id,
                        chunk_id=chunk_id,
                        embedding=blob,
                    )
                )
            await session.commit()
        self._index_cache.invalidate(("chunk", tenant_id))

    async def search_chunks(
        self,
        tenant_id: str,
        query_vec: tuple[float, ...],
        *,
        top_k: int = 10,
    ) -> list[VectorSearchHit]:
        """Brute-force cosine top-k search over chunk vectors.

        Args:
            tenant_id: Tenant scope.
            query_vec: Query embedding vector.
            top_k: Number of results to return.

        Returns:
            List of VectorSearchHit sorted by descending cosine similarity.
        """

        async def load() -> VectorLoadResult:
            return await self._load_vectors(
                VectorChunk,
                VectorChunk.tenant_id,
                tenant_id,
                "chunk_id",
            )

        index = await self._index_cache.get_or_load(
            ("chunk", tenant_id),
            load,
            dim=self._dim,
        )
        return await asyncio.to_thread(
            search_normalized_index,
            index,
            query_vec,
            top_k=top_k,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _load_vectors(
        self,
        model_cls: type[VectorEntity] | type[VectorChunk],
        tenant_col: Any,
        tenant_id: str,
        id_attr: str,
    ) -> NormalizedVectorIndex:
        """Stream one tenant index after a count/byte resource preflight.

        Args:
            model_cls: ORM model class (VectorEntity or VectorChunk).
            tenant_col: The tenant_id column attribute.
            tenant_id: Tenant scope value.
            id_attr: Name of the ID attribute ("entity_id" or "chunk_id").

        Returns:
            A preallocated, row-normalized index.
        """
        id_column: Any = getattr(model_cls, id_attr)
        return await stream_normalized_index(
            self._session_factory,
            id_column=id_column,
            blob_column=model_cls.embedding,
            tenant_column=tenant_col,
            tenant_id=tenant_id,
            dim=self._dim,
            batch_rows=self._load_batch_rows,
            max_rows=self._load_max_rows,
            max_source_bytes=self._load_max_source_bytes,
            max_memory_bytes=self._load_max_memory_bytes,
            log_label=f"{model_cls.__tablename__}/{tenant_id}",
            max_id_bytes_per_row=1020 if id_attr == "entity_id" else 0,
        )

    def _brute_cosine(
        self,
        rows: list[tuple[str | int, bytes]],
        query_vec: tuple[float, ...],
        top_k: int,
    ) -> list[VectorSearchHit]:
        """Numpy matrix cosine similarity + argpartition top-k.

        Args:
            rows: List of (id, embedding_blob) tuples.
            query_vec: Query embedding.
            top_k: Number of top results.

        Returns:
            Sorted list of VectorSearchHit (highest similarity first).
        """
        index = build_normalized_index(rows, dim=self._dim, log_label="vector")
        return search_normalized_index(index, query_vec, top_k=top_k)


__all__ = ["MySQLVectorStore"]
