"""Unit + integration tests for MySQLAudioVectorStore — M7 P0-13.

Tests cover:
    - float32 ↔ BLOB serialization round-trip (static helpers)
    - Brute-force cosine search correctness (compare with manual calculation)
    - Top-k ordering + cap
    - Tenant isolation
    - Empty table handling
    - Dim-mismatch warning filter
    - get_audio_vectors_for_recording (segment_id + chunk_id + vector tuple)
    - Cross-recording isolation
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest
import pytest_asyncio

from audio_graphy.storage.mysql_audio_vector import MySQLAudioVectorStore

# ============================================================
# Pure-Python unit tests (no DB) — always run
# ============================================================


@pytest.mark.unit
class TestAudioSerialization:
    """float32 ↔ BLOB serialization for CLAP vectors."""

    def test_serialize_deserialize_roundtrip(self) -> None:
        """_serialize → _deserialize produces the same vector."""
        original = tuple(float(v) for v in np.random.randn(512))
        blob = MySQLAudioVectorStore._serialize(original)
        restored = MySQLAudioVectorStore._deserialize(blob)
        assert len(restored) == 512
        for orig, rest in zip(original, restored, strict=True):
            assert math.isclose(orig, rest, abs_tol=1e-6)

    def test_serialize_blob_size(self) -> None:
        """512-dim float32 → 2048 bytes."""
        vec = tuple(1.0 for _ in range(512))
        blob = MySQLAudioVectorStore._serialize(vec)
        assert len(blob) == 2048

    def test_serialize_empty_vector(self) -> None:
        """Empty vector produces empty blob."""
        blob = MySQLAudioVectorStore._serialize(())
        assert len(blob) == 0

    def test_deserialize_known_values(self) -> None:
        """Deserialise a known blob produces expected values."""
        vec = (1.0, 2.0, 3.0, 4.0)
        blob = MySQLAudioVectorStore._serialize(vec)
        restored = MySQLAudioVectorStore._deserialize(blob)
        assert math.isclose(restored[0], 1.0)
        assert math.isclose(restored[1], 2.0)
        assert math.isclose(restored[2], 3.0)
        assert math.isclose(restored[3], 4.0)


@pytest.mark.unit
class TestBruteCosineInMemory:
    """_brute_cosine() correctness with hand-crafted rows (no DB)."""

    @staticmethod
    def _make_unit_vec(seed: int, dim: int = 512) -> tuple[float, ...]:
        rng = np.random.RandomState(seed)
        vec = rng.randn(dim).astype(np.float32)
        norm = float(np.linalg.norm(vec))
        return tuple(float(v / norm) for v in vec)

    def test_empty_rows_returns_empty(self) -> None:
        store = MySQLAudioVectorStore.__new__(MySQLAudioVectorStore)
        store._dim = 512  # type: ignore[attr-defined]
        assert store._brute_cosine([], self._make_unit_vec(1), top_k=5) == []

    def test_exact_match_returns_cosine_one(self) -> None:
        """Identical query + stored vector → cosine ≈ 1.0."""
        store = MySQLAudioVectorStore.__new__(MySQLAudioVectorStore)
        store._dim = 512  # type: ignore[attr-defined]
        vec = self._make_unit_vec(42)
        blob = MySQLAudioVectorStore._serialize(vec)
        hits = store._brute_cosine([(101, blob)], vec, top_k=1)
        assert len(hits) == 1
        assert hits[0].id == 101
        assert math.isclose(hits[0].score, 1.0, abs_tol=1e-5)

    def test_top_k_sorted_descending(self) -> None:
        """Hits are sorted by descending score."""
        store = MySQLAudioVectorStore.__new__(MySQLAudioVectorStore)
        store._dim = 512  # type: ignore[attr-defined]
        query = self._make_unit_vec(7)
        rows = [
            (i, MySQLAudioVectorStore._serialize(self._make_unit_vec(7 + i)))
            for i in range(5)
        ]
        hits = store._brute_cosine(rows, query, top_k=5)
        assert len(hits) == 5
        for i in range(len(hits) - 1):
            assert hits[i].score >= hits[i + 1].score

    def test_top_k_fewer_than_available(self) -> None:
        """top_k > len(rows) returns all available rows."""
        store = MySQLAudioVectorStore.__new__(MySQLAudioVectorStore)
        store._dim = 512  # type: ignore[attr-defined]
        vec = self._make_unit_vec(99)
        blob = MySQLAudioVectorStore._serialize(vec)
        hits = store._brute_cosine([(1, blob)], vec, top_k=10)
        assert len(hits) == 1

    def test_top_k_zero_returns_empty(self) -> None:
        store = MySQLAudioVectorStore.__new__(MySQLAudioVectorStore)
        store._dim = 512  # type: ignore[attr-defined]
        vec = self._make_unit_vec(99)
        blob = MySQLAudioVectorStore._serialize(vec)
        assert store._brute_cosine([(1, blob)], vec, top_k=0) == []

    def test_dim_mismatch_row_skipped(self) -> None:
        """Rows whose dim ≠ store._dim are skipped, not crashed."""
        store = MySQLAudioVectorStore.__new__(MySQLAudioVectorStore)
        store._dim = 512  # type: ignore[attr-defined]
        query = self._make_unit_vec(1, dim=512)

        # Wrong-dim blob (256 floats = 1024 bytes)
        wrong_vec = tuple(1.0 for _ in range(256))
        wrong_blob = MySQLAudioVectorStore._serialize(wrong_vec)

        # Correct-dim blob
        good_vec = self._make_unit_vec(2)
        good_blob = MySQLAudioVectorStore._serialize(good_vec)

        hits = store._brute_cosine(
            [(1, wrong_blob), (2, good_blob)], query, top_k=5
        )
        assert len(hits) == 1
        assert hits[0].id == 2

    def test_zero_query_vector_does_not_crash(self) -> None:
        """All-zeros query vector is a degenerate case — must not divide-by-zero."""
        store = MySQLAudioVectorStore.__new__(MySQLAudioVectorStore)
        store._dim = 512  # type: ignore[attr-defined]
        zero_query = tuple(0.0 for _ in range(512))
        good_vec = self._make_unit_vec(1)
        blob = MySQLAudioVectorStore._serialize(good_vec)
        hits = store._brute_cosine([(1, blob)], zero_query, top_k=1)
        assert len(hits) == 1  # score will be 0 but no NaN

    def test_corrupt_blob_skipped(self) -> None:
        """Blobs that can't be parsed as float32 are skipped."""
        store = MySQLAudioVectorStore.__new__(MySQLAudioVectorStore)
        store._dim = 512  # type: ignore[attr-defined]
        query = self._make_unit_vec(1)
        good_blob = MySQLAudioVectorStore._serialize(self._make_unit_vec(2))
        # 3-byte blob is invalid float32
        hits = store._brute_cosine(
            [(1, b"\x00\x00\x00"), (2, good_blob)], query, top_k=5
        )
        assert len(hits) == 1
        assert hits[0].id == 2


# ============================================================
# Integration tests — require MySQL (docker-compose port 3307)
# ============================================================


@pytest_asyncio.fixture
async def audio_store(async_session_factory: Any) -> MySQLAudioVectorStore:
    """MySQLAudioVectorStore with dim=512."""
    return MySQLAudioVectorStore(async_session_factory, dim=512)


@pytest.mark.integration
class TestAudioVectorStoreDB:
    """Brute-force cosine similarity search with real MySQL."""

    @staticmethod
    def _make_unit_vec(seed: int, dim: int = 512) -> tuple[float, ...]:
        rng = np.random.RandomState(seed)
        vec = rng.randn(dim).astype(np.float32)
        norm = float(np.linalg.norm(vec))
        return tuple(float(v / norm) for v in vec)

    @staticmethod
    async def _seed_recording(
        async_session_factory: Any,
        tenant_id: str = "default",
    ) -> int:
        """Seed a Recording row to satisfy FK constraint; return its id."""
        from audio_graphy.models.recording import Recording

        async with async_session_factory() as session:
            rec = Recording(
                tenant_id=tenant_id,
                store_id="test",
                path="/tmp/test.wav",
                status="indexed",
            )
            session.add(rec)
            await session.commit()
            return int(rec.id)

    async def test_dim_property(
        self,
        audio_store: MySQLAudioVectorStore,
    ) -> None:
        assert audio_store.dim == 512

    async def test_upsert_and_search_finds_self(
        self,
        audio_store: MySQLAudioVectorStore,
        async_session_factory: Any,
    ) -> None:
        """A vector searched by itself returns cosine ≈ 1.0."""
        rec_id = await self._seed_recording(async_session_factory)
        vec = self._make_unit_vec(1)
        await audio_store.upsert_audio_vector(
            tenant_id="default",
            recording_id=rec_id,
            segment_id=100,
            embedding=vec,
        )
        hits = await audio_store.search_audio("default", vec, top_k=1)
        assert len(hits) == 1
        assert hits[0].id == 100
        assert math.isclose(hits[0].score, 1.0, abs_tol=1e-5)

    async def test_search_empty_tenant_returns_empty(
        self,
        audio_store: MySQLAudioVectorStore,
    ) -> None:
        vec = self._make_unit_vec(99)
        hits = await audio_store.search_audio("empty_tenant", vec, top_k=5)
        assert hits == []

    async def test_top_k_ordering(
        self,
        audio_store: MySQLAudioVectorStore,
        async_session_factory: Any,
    ) -> None:
        rec_id = await self._seed_recording(async_session_factory)
        query = self._make_unit_vec(42)
        for i in range(5):
            await audio_store.upsert_audio_vector(
                tenant_id="default",
                recording_id=rec_id,
                segment_id=200 + i,
                embedding=self._make_unit_vec(42 + i),
            )
        hits = await audio_store.search_audio("default", query, top_k=5)
        assert len(hits) == 5
        for i in range(len(hits) - 1):
            assert hits[i].score >= hits[i + 1].score

    async def test_top_k_cap(
        self,
        audio_store: MySQLAudioVectorStore,
        async_session_factory: Any,
    ) -> None:
        """top_k larger than available returns all rows."""
        rec_id = await self._seed_recording(async_session_factory)
        vec = self._make_unit_vec(7)
        await audio_store.upsert_audio_vector(
            tenant_id="default",
            recording_id=rec_id,
            segment_id=300,
            embedding=vec,
        )
        hits = await audio_store.search_audio("default", vec, top_k=10)
        assert len(hits) == 1

    async def test_tenant_isolation(
        self,
        audio_store: MySQLAudioVectorStore,
        async_session_factory: Any,
    ) -> None:
        """Vectors in tenant_a are invisible from tenant_b."""
        rec_a = await self._seed_recording(async_session_factory, "tenant_a")
        rec_b = await self._seed_recording(async_session_factory, "tenant_b")
        vec = self._make_unit_vec(11)
        await audio_store.upsert_audio_vector(
            "tenant_a", rec_a, 400, vec, chunk_id=401
        )
        await audio_store.upsert_audio_vector(
            "tenant_b", rec_b, 401, vec, chunk_id=402
        )

        hits_a = await audio_store.search_audio("tenant_a", vec, top_k=10)
        hits_b = await audio_store.search_audio("tenant_b", vec, top_k=10)
        assert {h.id for h in hits_a} == {400}
        assert {h.id for h in hits_b} == {401}

    async def test_get_audio_vectors_for_recording(
        self,
        audio_store: MySQLAudioVectorStore,
        async_session_factory: Any,
    ) -> None:
        rec_id = await self._seed_recording(async_session_factory)
        vec1 = self._make_unit_vec(21)
        vec2 = self._make_unit_vec(22)
        await audio_store.upsert_audio_vector(
            "default", rec_id, 500, vec1, chunk_id=11
        )
        await audio_store.upsert_audio_vector(
            "default", rec_id, 501, vec2, chunk_id=None
        )

        rows = await audio_store.get_audio_vectors_for_recording(
            "default", rec_id
        )
        assert len(rows) == 2
        by_seg = {seg_id: (chk, vec) for seg_id, chk, vec in rows}
        assert 500 in by_seg
        assert 501 in by_seg
        assert by_seg[500][0] == 11
        assert by_seg[501][0] is None
        # Vector roundtrip preserved
        assert math.isclose(by_seg[500][1][0], vec1[0], abs_tol=1e-6)

    async def test_get_audio_vectors_for_recording_cross_tenant_empty(
        self,
        audio_store: MySQLAudioVectorStore,
        async_session_factory: Any,
    ) -> None:
        rec_id = await self._seed_recording(async_session_factory, "tenant_a")
        vec = self._make_unit_vec(31)
        await audio_store.upsert_audio_vector(
            "tenant_a", rec_id, 600, vec, chunk_id=601
        )
        # Wrong tenant → empty list
        rows = await audio_store.get_audio_vectors_for_recording(
            "tenant_b", rec_id
        )
        assert rows == []

    async def test_upsert_with_optional_fields(
        self,
        audio_store: MySQLAudioVectorStore,
        async_session_factory: Any,
    ) -> None:
        """Upsert supports chunk_id, model name, and duration_sec."""
        rec_id = await self._seed_recording(async_session_factory)
        vec = self._make_unit_vec(41)
        await audio_store.upsert_audio_vector(
            tenant_id="default",
            recording_id=rec_id,
            segment_id=700,
            embedding=vec,
            chunk_id=701,
            model="custom-clap-v2",
            duration_sec=12.5,
        )
        rows = await audio_store.get_audio_vectors_for_recording(
            "default", rec_id
        )
        assert len(rows) == 1
        seg_id, chunk_id, _vec = rows[0]
        assert seg_id == 700
        assert chunk_id == 701
