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

import logging
from typing import TYPE_CHECKING, Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.core.types import VectorSearchHit
from audio_graphy.models.vector_chunk import VectorChunk
from audio_graphy.models.vector_entity import VectorEntity

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._session_factory = session_factory
        self._dim = dim

    @property
    def dim(self) -> int:
        """Vector dimension."""
        return self._dim

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
        rows = await self._load_vectors(
            VectorEntity, VectorEntity.tenant_id, tenant_id, "entity_id"
        )
        return self._brute_cosine(rows, query_vec, top_k)

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
        rows = await self._load_vectors(VectorChunk, VectorChunk.tenant_id, tenant_id, "chunk_id")
        return self._brute_cosine(rows, query_vec, top_k)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _load_vectors(
        self,
        model_cls: type[VectorEntity] | type[VectorChunk],
        tenant_col: Any,
        tenant_id: str,
        id_attr: str,
    ) -> list[tuple[str | int, bytes]]:
        """Load all vectors for a tenant from the database.

        Args:
            model_cls: ORM model class (VectorEntity or VectorChunk).
            tenant_col: The tenant_id column attribute.
            tenant_id: Tenant scope value.
            id_attr: Name of the ID attribute ("entity_id" or "chunk_id").

        Returns:
            List of (id, embedding_blob) tuples.
        """
        rows: list[tuple[str | int, bytes]] = []
        async with self._session_factory() as session:
            stmt = select(model_cls).where(tenant_col == tenant_id)
            result = await session.execute(stmt)
            for row in result.scalars():
                try:
                    row_any: Any = row  # Type erased for attr access (VectorEntity | VectorChunk)
                    row_id: str | int = getattr(row_any, id_attr)
                    emb: bytes = row_any.embedding
                    rows.append((row_id, emb))
                except Exception as exc:
                    logger.warning("Skipping corrupted vector row: %s", exc)
        return rows

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
        if not rows:
            return []

        # Build matrix (N × dim)
        ids: list[str | int] = []
        vectors: list[np.ndarray] = []
        for row_id, blob in rows:
            try:
                vec = np.frombuffer(blob, dtype=np.float32)
                if vec.shape[0] != self._dim:
                    logger.warning(
                        "Vector dim mismatch: expected %d, got %d — skipping",
                        self._dim,
                        vec.shape[0],
                    )
                    continue
                ids.append(row_id)
                vectors.append(vec)
            except Exception as exc:
                logger.warning("Failed to deserialize vector for id=%s: %s", row_id, exc)

        if not vectors:
            return []

        matrix = np.stack(vectors)  # (N, dim)
        query = np.asarray(query_vec, dtype=np.float32)  # (dim,)

        # Normalise
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms = np.clip(norms, 1e-12, None)
        normalized = matrix / norms

        query_norm = float(np.linalg.norm(query))
        if query_norm < 1e-12:
            query_norm = 1e-12
        query_normalized = query / query_norm

        # Cosine similarity: (N,)
        scores = normalized @ query_normalized

        # Top-k via argpartition (O(N) selection, then sort the k results)
        k = min(top_k, len(scores))
        if k <= 0:
            return []

        top_k_idx = np.argpartition(scores, -k)[-k:]
        # Sort the k indices by descending score
        top_k_sorted = top_k_idx[np.argsort(scores[top_k_idx])[::-1]]

        return [VectorSearchHit(id=ids[idx], score=float(scores[idx])) for idx in top_k_sorted]


__all__ = ["MySQLVectorStore"]
