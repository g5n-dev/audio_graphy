"""MySQL brute-force cosine vector store for CLAP audio embeddings (M7 P0-13).

Stores CLAP 512-dim segment embeddings (plaintext BLOB) in MySQL and performs
brute-force cosine similarity search via numpy matrix multiplication.

Architecture §10.1 — ``ThreeChannelRetriever._audio_channel`` calls
``search_audio()`` here. Plaintext storage avoids per-decrypt overhead
(PIPL §14.3 does NOT apply to CLAP vectors — they are not biometric).

Table: ``vectors_audio``. See ``models/vector_audio.py``.

Complexity: O(N × dim) per query — same as ``MySQLVectorStore``. Suitable
for < 100k vectors (M7 small-scale).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.core.types import VectorSearchHit
from audio_graphy.models.vector_audio import VectorAudio
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

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AudioVectorSearchHit:
    """Similarity hit resolved back to its tenant-owned source identity."""

    vector_id: int
    recording_id: int
    segment_id: int
    chunk_id: int | None
    score: float


class MySQLAudioVectorStore:
    """Phase 2 brute-force cosine vector store for CLAP audio embeddings.

    Args:
        session_factory: SQLAlchemy ``async_sessionmaker`` for DB access.
        dim: Vector dimension (default 512, laion_clap HTSAT-base).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        dim: int = 512,
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
        """Invalidate one tenant after a bulk import performed elsewhere."""

        self._index_cache.invalidate(("audio", tenant_id))

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    async def upsert_audio_vector(
        self,
        tenant_id: str,
        recording_id: int,
        segment_id: int,
        embedding: tuple[float, ...],
        *,
        chunk_id: int | None = None,
        model: str = "clap-htsat-base-2022",
        duration_sec: float = 0.0,
    ) -> None:
        """Insert a CLAP audio embedding row.

        Unlike ``MySQLVectorStore.upsert_*_vector``, this ALWAYS inserts —
        segments are append-only (each call to ``embed_audio`` produces a
        fresh vector). Caller is responsible for de-duplication at the
        chunk_id level if needed.

        Args:
            tenant_id: Tenant scope.
            recording_id: Source recording.
            segment_id: Segment index within the recording.
            embedding: 512-d CLAP vector.
            chunk_id: Optional chunk linkage (when segment → chunk is 1:1).
            model: Model identifier reported by the service.
            duration_sec: Audio duration in seconds.
        """
        blob = self._serialize(embedding)
        async with self._session_factory() as session:
            row = VectorAudio(
                tenant_id=tenant_id,
                recording_id=recording_id,
                segment_id=segment_id,
                chunk_id=chunk_id,
                vector=blob,
                dim=len(embedding),
                model=model,
                duration_sec=duration_sec,
            )
            session.add(row)
            await session.commit()
        self._index_cache.invalidate(("audio", tenant_id))

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    async def search_audio(
        self,
        tenant_id: str,
        query_vec: tuple[float, ...],
        *,
        top_k: int = 10,
    ) -> list[AudioVectorSearchHit]:
        """Brute-force cosine top-k search over CLAP audio vectors.

        The normalized matrix is cached by immutable ``vectors_audio.id``.
        Top hits are then resolved in one tenant-scoped metadata query so a
        segment index can never be mistaken for a globally unique chunk ID.

        Args:
            tenant_id: Tenant scope.
            query_vec: Query audio embedding (CLAP 512-d).
            top_k: Number of results to return.

        Returns:
            Metadata-rich hits sorted by descending cosine similarity.
        """

        async def load() -> VectorLoadResult:
            return await self._load_vectors(tenant_id)

        index = await self._index_cache.get_or_load(
            ("audio", tenant_id),
            load,
            dim=self._dim,
        )
        raw_hits = await asyncio.to_thread(
            search_normalized_index,
            index,
            query_vec,
            top_k=top_k,
        )
        vector_ids = [int(hit.id) for hit in raw_hits if isinstance(hit.id, int)]
        metadata = await self._load_hit_metadata(tenant_id, vector_ids)
        return [
            AudioVectorSearchHit(
                vector_id=int(hit.id),
                recording_id=metadata[int(hit.id)][0],
                segment_id=metadata[int(hit.id)][1],
                chunk_id=metadata[int(hit.id)][2],
                score=hit.score,
            )
            for hit in raw_hits
            if isinstance(hit.id, int) and int(hit.id) in metadata
        ]

    async def get_audio_vectors_for_recording(
        self,
        tenant_id: str,
        recording_id: int,
    ) -> list[tuple[int, int | None, tuple[float, ...]]]:
        """Load all (segment_id, chunk_id, vector) rows for a recording.

        Used by ingestion + speaker-linker backfill paths.
        """
        out: list[tuple[int, int | None, tuple[float, ...]]] = []
        async with self._session_factory() as session:
            stmt = select(VectorAudio).where(
                VectorAudio.tenant_id == tenant_id,
                VectorAudio.recording_id == recording_id,
            )
            result = await session.execute(stmt)
            for row in result.scalars():
                try:
                    vec = self._deserialize(row.vector)
                    out.append((row.segment_id, row.chunk_id, vec))
                except Exception as exc:
                    logger.warning(
                        "Failed to deserialize audio vector for recording=%d segment=%d: %s",
                        recording_id,
                        row.segment_id,
                        exc,
                    )
        return out

    # ------------------------------------------------------------------
    # Internal helpers — mirror MySQLVectorStore pattern
    # ------------------------------------------------------------------

    async def _load_vectors(
        self,
        tenant_id: str,
    ) -> NormalizedVectorIndex:
        """Stream immutable row IDs and BLOBs into one bounded tenant index."""

        return await stream_normalized_index(
            self._session_factory,
            id_column=VectorAudio.id,
            blob_column=VectorAudio.vector,
            tenant_column=VectorAudio.tenant_id,
            tenant_id=tenant_id,
            dim=self._dim,
            batch_rows=self._load_batch_rows,
            max_rows=self._load_max_rows,
            max_source_bytes=self._load_max_source_bytes,
            max_memory_bytes=self._load_max_memory_bytes,
            log_label=f"{VectorAudio.__tablename__}/{tenant_id}",
        )

    async def _load_hit_metadata(
        self,
        tenant_id: str,
        vector_ids: list[int],
    ) -> dict[int, tuple[int, int, int | None]]:
        """Resolve top-hit row IDs without crossing a tenant boundary."""
        if not vector_ids:
            return {}

        async with self._session_factory() as session:
            stmt = select(
                VectorAudio.id,
                VectorAudio.recording_id,
                VectorAudio.segment_id,
                VectorAudio.chunk_id,
            ).where(
                VectorAudio.tenant_id == tenant_id,
                VectorAudio.id.in_(vector_ids),
            )
            result = await session.execute(stmt)
            return {
                int(vector_id): (
                    int(recording_id),
                    int(segment_id),
                    int(chunk_id) if chunk_id is not None else None,
                )
                for vector_id, recording_id, segment_id, chunk_id in result.all()
            }

    def _brute_cosine(
        self,
        rows: list[tuple[int, bytes]],
        query_vec: tuple[float, ...],
        top_k: int,
    ) -> list[VectorSearchHit]:
        """Numpy matrix cosine similarity + argpartition top-k."""
        index = build_normalized_index(
            rows,
            dim=self._dim,
            log_label="audio",
        )
        return search_normalized_index(index, query_vec, top_k=top_k)

    @staticmethod
    def _serialize(vec: tuple[float, ...]) -> bytes:
        """Convert a float tuple to a float32 little-endian BLOB."""
        arr = np.asarray(vec, dtype=np.float32)
        return arr.tobytes()

    @staticmethod
    def _deserialize(blob: bytes) -> tuple[float, ...]:
        """Convert a float32 BLOB back to a float tuple."""
        arr = np.frombuffer(blob, dtype=np.float32)
        return tuple(float(v) for v in arr)


__all__ = ["AudioVectorSearchHit", "MySQLAudioVectorStore"]
