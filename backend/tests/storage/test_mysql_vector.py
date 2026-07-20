"""Unit + integration tests for MySQLVectorStore — brute-force cosine search.

Tests cover:
    - float32 ↔ BLOB serialization round-trip
    - Entity/chunk vector upsert + search
    - Cosine similarity correctness (compare with manual calculation)
    - Top-k ordering
    - Tenant isolation
    - Empty table handling
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

from audio_graphy.storage.mysql_vector import MySQLVectorStore


@pytest.mark.unit
class TestSerialization:
    """float32 ↔ BLOB serialization."""

    def test_serialize_deserialize_roundtrip(self) -> None:
        """_serialize → _deserialize produces the same vector."""
        original = tuple(float(v) for v in np.random.randn(1024))
        blob = MySQLVectorStore._serialize(original)
        restored = MySQLVectorStore._deserialize(blob)
        assert len(restored) == 1024
        for orig, rest in zip(original, restored, strict=True):
            assert math.isclose(orig, rest, abs_tol=1e-6)

    def test_serialize_blob_size(self) -> None:
        """1024-dim float32 → 4096 bytes."""
        vec = tuple(1.0 for _ in range(1024))
        blob = MySQLVectorStore._serialize(vec)
        assert len(blob) == 4096

    def test_serialize_empty_vector(self) -> None:
        """Empty vector produces empty blob."""
        blob = MySQLVectorStore._serialize(())
        assert len(blob) == 0

    def test_deserialize_known_values(self) -> None:
        """Deserialise a known blob produces expected values."""
        vec = (1.0, 2.0, 3.0, 4.0)
        blob = MySQLVectorStore._serialize(vec)
        restored = MySQLVectorStore._deserialize(blob)
        assert math.isclose(restored[0], 1.0)
        assert math.isclose(restored[1], 2.0)
        assert math.isclose(restored[2], 3.0)
        assert math.isclose(restored[3], 4.0)


@pytest.mark.integration
class TestBruteCosineSearch:
    """Brute-force cosine similarity search with real MySQL."""

    @staticmethod
    def _make_vector(seed: int, dim: int = 1024) -> tuple[float, ...]:
        """Create a deterministic unit vector from a seed."""
        rng = np.random.RandomState(seed)
        vec = rng.randn(dim).astype(np.float32)
        norm = np.linalg.norm(vec)
        return tuple(float(v / norm) for v in vec)

    async def test_upsert_and_search_entity(self, vector_store: MySQLVectorStore) -> None:
        """Upsert entity vectors and search returns correct top-k."""
        vec1 = self._make_vector(1)
        vec2 = self._make_vector(2)
        vec3 = self._make_vector(3)

        await vector_store.upsert_entity_vector("default", "entity_1", vec1)
        await vector_store.upsert_entity_vector("default", "entity_2", vec2)
        await vector_store.upsert_entity_vector("default", "entity_3", vec3)

        # Search with vec1 → entity_1 should be top hit (cosine=1.0)
        hits = await vector_store.search_entities("default", vec1, top_k=3)
        assert len(hits) == 3
        assert hits[0].id == "entity_1"
        assert math.isclose(hits[0].score, 1.0, abs_tol=1e-5)

    async def test_upsert_and_search_chunk(
        self, vector_store: MySQLVectorStore, async_session_factory: Any
    ) -> None:
        """Upsert chunk vectors and search returns correct top-k."""
        from audio_graphy.models.chunk import Chunk
        from audio_graphy.models.recording import Recording

        # Create a recording + chunks first (FK constraint)
        chunk_ids: list[int] = []
        async with async_session_factory() as session:
            rec = Recording(
                tenant_id="default",
                store_id="test",
                path="/tmp/test.wav",
                status="indexed",
            )
            session.add(rec)
            await session.flush()
            rec_id = rec.id
            for i in range(2):
                chunk = Chunk(
                    tenant_id="default",
                    recording_id=rec_id,
                    segment_ids=[i],
                    text=f"chunk {i}",
                    token_n=10,
                    content_hash=f"hash_{i}",
                )
                session.add(chunk)
                await session.flush()
                chunk_ids.append(chunk.id)
            await session.commit()

        vec1 = self._make_vector(10)
        vec2 = self._make_vector(20)

        await vector_store.upsert_chunk_vector("default", chunk_ids[0], vec1)
        await vector_store.upsert_chunk_vector("default", chunk_ids[1], vec2)

        hits = await vector_store.search_chunks("default", vec1, top_k=2)
        assert len(hits) == 2
        assert hits[0].id == chunk_ids[0]
        assert math.isclose(hits[0].score, 1.0, abs_tol=1e-5)

    async def test_search_empty_table(self, vector_store: MySQLVectorStore) -> None:
        """Searching an empty table returns empty list."""
        query = self._make_vector(99)
        hits = await vector_store.search_entities("empty_tenant", query, top_k=5)
        assert hits == []

    async def test_top_k_ordering(self, vector_store: MySQLVectorStore) -> None:
        """Results are sorted by descending cosine similarity."""
        query = self._make_vector(42)
        # Create vectors with varying similarity to query
        for i in range(5):
            vec = self._make_vector(42 + i)
            await vector_store.upsert_entity_vector("default", f"e_{i}", vec)

        hits = await vector_store.search_entities("default", query, top_k=5)
        assert len(hits) == 5
        # Scores should be in descending order
        for i in range(len(hits) - 1):
            assert hits[i].score >= hits[i + 1].score

    async def test_top_k_fewer_than_available(self, vector_store: MySQLVectorStore) -> None:
        """top_k larger than available rows returns all rows."""
        vec = self._make_vector(50)
        await vector_store.upsert_entity_vector("default", "only_entity", vec)

        hits = await vector_store.search_entities("default", vec, top_k=10)
        assert len(hits) == 1
        assert hits[0].id == "only_entity"

    async def test_tenant_isolation(self, vector_store: MySQLVectorStore) -> None:
        """Vectors from different tenants are isolated."""
        vec = self._make_vector(60)
        await vector_store.upsert_entity_vector("tenant_a", "entity_a", vec)
        await vector_store.upsert_entity_vector("tenant_b", "entity_b", vec)

        hits_a = await vector_store.search_entities("tenant_a", vec, top_k=10)
        hits_b = await vector_store.search_entities("tenant_b", vec, top_k=10)

        assert len(hits_a) == 1
        assert hits_a[0].id == "entity_a"
        assert len(hits_b) == 1
        assert hits_b[0].id == "entity_b"

    async def test_upsert_overwrite(self, vector_store: MySQLVectorStore) -> None:
        """Upserting the same entity_id overwrites the embedding."""
        vec1 = self._make_vector(70)
        vec2 = self._make_vector(71)

        await vector_store.upsert_entity_vector("default", "entity_x", vec1)
        await vector_store.upsert_entity_vector("default", "entity_x", vec2)

        hits = await vector_store.search_entities("default", vec2, top_k=1)
        assert len(hits) == 1
        assert hits[0].id == "entity_x"
        # Should match vec2 (the overwritten vector), not vec1
        assert math.isclose(hits[0].score, 1.0, abs_tol=1e-5)

    async def test_cosine_correctness(self, vector_store: MySQLVectorStore) -> None:
        """Cosine similarity matches manual numpy calculation."""
        vec_a = self._make_vector(80)
        vec_b = self._make_vector(81)

        await vector_store.upsert_entity_vector("default", "ent_a", vec_a)

        # Manual cosine
        a = np.array(vec_a, dtype=np.float32)
        b = np.array(vec_b, dtype=np.float32)
        manual_cosine = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))

        hits = await vector_store.search_entities("default", vec_b, top_k=1)
        assert len(hits) == 1
        assert math.isclose(hits[0].score, manual_cosine, abs_tol=1e-4)
