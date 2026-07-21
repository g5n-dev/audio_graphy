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

import logging
from typing import TYPE_CHECKING

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.core.types import VectorSearchHit
from audio_graphy.models.vector_audio import VectorAudio

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._session_factory = session_factory
        self._dim = dim

    @property
    def dim(self) -> int:
        """Vector dimension."""
        return self._dim

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

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    async def search_audio(
        self,
        tenant_id: str,
        query_vec: tuple[float, ...],
        *,
        top_k: int = 10,
    ) -> list[VectorSearchHit]:
        """Brute-force cosine top-k search over CLAP audio vectors.

        Each hit's ``id`` is the ``segment_id`` (int). Callers reverse-lookup
        the chunk_id / recording_id via the candidate-build helper.

        Args:
            tenant_id: Tenant scope.
            query_vec: Query audio embedding (CLAP 512-d).
            top_k: Number of results to return.

        Returns:
            List of VectorSearchHit sorted by descending cosine similarity.
            ``id`` is ``segment_id`` (int).
        """
        rows = await self._load_vectors(tenant_id)
        return self._brute_cosine(rows, query_vec, top_k)

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
    ) -> list[tuple[int, bytes]]:
        """Load all (segment_id, blob) rows for a tenant."""
        rows: list[tuple[int, bytes]] = []
        async with self._session_factory() as session:
            stmt = select(VectorAudio).where(VectorAudio.tenant_id == tenant_id)
            result = await session.execute(stmt)
            for row in result.scalars():
                try:
                    rows.append((int(row.segment_id), row.vector))
                except Exception as exc:
                    logger.warning(
                        "Skipping corrupted audio vector row: %s", exc
                    )
        return rows

    def _brute_cosine(
        self,
        rows: list[tuple[int, bytes]],
        query_vec: tuple[float, ...],
        top_k: int,
    ) -> list[VectorSearchHit]:
        """Numpy matrix cosine similarity + argpartition top-k."""
        if not rows:
            return []

        ids: list[int] = []
        vectors: list[np.ndarray] = []
        for seg_id, blob in rows:
            try:
                vec = np.frombuffer(blob, dtype=np.float32)
                if vec.shape[0] != self._dim:
                    logger.warning(
                        "Audio vector dim mismatch: expected %d, got %d — skipping",
                        self._dim,
                        vec.shape[0],
                    )
                    continue
                ids.append(seg_id)
                vectors.append(vec)
            except Exception as exc:
                logger.warning(
                    "Failed to deserialize audio vector for segment_id=%s: %s",
                    seg_id,
                    exc,
                )

        if not vectors:
            return []

        matrix = np.stack(vectors)
        query = np.asarray(query_vec, dtype=np.float32)

        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms = np.clip(norms, 1e-12, None)
        normalized = matrix / norms

        query_norm = float(np.linalg.norm(query))
        if query_norm < 1e-12:
            query_norm = 1e-12
        query_normalized = query / query_norm

        scores = normalized @ query_normalized

        k = min(top_k, len(scores))
        if k <= 0:
            return []

        top_k_idx = np.argpartition(scores, -k)[-k:]
        top_k_sorted = top_k_idx[np.argsort(scores[top_k_idx])[::-1]]

        return [
            VectorSearchHit(id=ids[idx], score=float(scores[idx]))
            for idx in top_k_sorted
        ]

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


__all__ = ["MySQLAudioVectorStore"]
