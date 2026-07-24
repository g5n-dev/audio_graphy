"""Cold-load memory and streaming gates for MySQL vector stores."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

import numpy as np
import pytest
from sqlalchemy import Column, Integer, LargeBinary, MetaData, String, Table, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from audio_graphy.models.vector_entity import VectorEntity
from audio_graphy.storage.mysql_audio_vector import MySQLAudioVectorStore
from audio_graphy.storage.mysql_vector import MySQLVectorStore
from audio_graphy.storage.vector_index_cache import (
    NormalizedVectorIndex,
    PreallocatedNormalizedIndexBuilder,
    TenantVectorIndexCache,
    VectorIndexBudgetError,
    build_normalized_index,
    estimate_vector_load_peak_bytes,
    stream_normalized_index,
)


def _blob(values: tuple[float, ...]) -> bytes:
    return np.asarray(values, dtype=np.float32).tobytes()


class _StatsResult:
    def __init__(self, row_count: int, source_bytes: int, max_blob_bytes: int) -> None:
        self._row = (row_count, source_bytes, max_blob_bytes)

    def one(self) -> tuple[int, int, int]:
        return self._row


class _PartitionedResult:
    def __init__(self, rows: Sequence[tuple[str | int, bytes]]) -> None:
        self._rows = rows
        self.partition_sizes: list[int] = []

    async def partitions(
        self,
        size: int,
    ) -> AsyncIterator[list[tuple[str | int, bytes]]]:
        self.partition_sizes.append(size)
        for offset in range(0, len(self._rows), size):
            yield list(self._rows[offset : offset + size])


class _FakeSession:
    def __init__(
        self,
        rows: Sequence[tuple[str | int, bytes]],
        *,
        stats: tuple[int, int, int] | None = None,
    ) -> None:
        self.rows = rows
        source_bytes = sum(len(blob) for _, blob in rows)
        max_blob_bytes = max((len(blob) for _, blob in rows), default=0)
        self.stats = stats or (len(rows), source_bytes, max_blob_bytes)
        self.execute_calls = 0
        self.stream_calls = 0
        self.selected_stream_columns: tuple[str, ...] = ()
        self.stream_result = _PartitionedResult(rows)

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, _statement: object) -> _StatsResult:
        self.execute_calls += 1
        return _StatsResult(*self.stats)

    async def stream(self, statement: Any) -> _PartitionedResult:
        self.stream_calls += 1
        self.selected_stream_columns = tuple(
            str(column.key) for column in statement.selected_columns
        )
        return self.stream_result


class _FakeSessionFactory:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session

    def __call__(self) -> _FakeSession:
        return self.session


@pytest.mark.unit
class TestPreallocatedNormalizedIndex:
    def test_production_100k_bge_preflight_fits_512_mib_cap(self) -> None:
        peak_bytes = estimate_vector_load_peak_bytes(
            row_count=100_000,
            dim=1024,
            batch_rows=512,
            source_bytes=100_000 * 1024 * 4,
            max_blob_bytes=1024 * 4,
            max_id_bytes_per_row=255 * 4,
        )

        assert peak_bytes == 521_032_320
        assert peak_bytes <= 512 * 1024 * 1024

    def test_build_does_not_use_stack_or_full_matrix_norm(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def forbidden(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("cold build must not allocate stack/norm matrices")

        monkeypatch.setattr(np, "stack", forbidden)
        monkeypatch.setattr(np.linalg, "norm", forbidden)

        index = build_normalized_index(
            [("x", _blob((3.0, 4.0))), ("y", _blob((0.0, 2.0)))],
            dim=2,
            log_label="test",
        )

        assert index.ids == ("x", "y")
        assert np.allclose(index.matrix, ((0.6, 0.8), (0.0, 1.0)))
        assert index.matrix_allocation_bytes == 16

    def test_builder_skips_corrupt_rows_without_a_second_matrix(self) -> None:
        builder = PreallocatedNormalizedIndexBuilder(
            row_capacity=4,
            dim=2,
            log_label="test",
        )

        builder.add_rows(
            [
                ("good", _blob((3.0, 4.0))),
                ("wrong", _blob((1.0,))),
                ("nan", _blob((float("nan"), 1.0))),
                ("broken", b"\x00"),
            ]
        )
        index = builder.finish()

        assert index.ids == ("good",)
        assert index.matrix.shape == (1, 2)
        assert index.matrix_allocation_bytes == 4 * 2 * 4
        assert not index.matrix.flags.writeable

    async def test_cache_accepts_prebuilt_index_without_rebuilding(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        index = build_normalized_index(
            [("x", _blob((1.0, 0.0)))],
            dim=2,
            log_label="test",
        )
        cache = TenantVectorIndexCache(ttl_seconds=60.0, max_entries=2)

        def forbidden(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("prebuilt streaming index must not be rebuilt")

        monkeypatch.setattr(
            "audio_graphy.storage.vector_index_cache.build_normalized_index",
            forbidden,
        )

        async def loader() -> NormalizedVectorIndex:
            return index

        loaded = await cache.get_or_load(("entity", "tenant-a"), loader, dim=2)

        assert loaded is index
        assert cache.stats.loads == 1


@pytest.mark.unit
class TestStreamingDatabaseLoad:
    async def test_real_async_sqlalchemy_cursor_path(self) -> None:
        metadata = MetaData()
        vectors = Table(
            "stream_vectors",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("tenant_id", String(64), nullable=False),
            Column("embedding", LargeBinary, nullable=False),
        )
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(metadata.create_all)
            async with session_factory() as session:
                await session.execute(
                    insert(vectors),
                    [
                        {"id": 1, "tenant_id": "tenant-a", "embedding": _blob((3.0, 4.0))},
                        {"id": 2, "tenant_id": "tenant-a", "embedding": _blob((0.0, 2.0))},
                        {"id": 3, "tenant_id": "tenant-b", "embedding": _blob((1.0, 0.0))},
                    ],
                )
                await session.commit()

            index = await stream_normalized_index(
                session_factory,
                id_column=vectors.c.id,
                blob_column=vectors.c.embedding,
                tenant_column=vectors.c.tenant_id,
                tenant_id="tenant-a",
                dim=2,
                batch_rows=1,
                max_rows=10,
                max_source_bytes=1024,
                max_memory_bytes=4096,
                log_label="sqlite-test",
            )
        finally:
            await engine.dispose()

        assert index.ids == (1, 2)
        assert np.allclose(index.matrix, ((0.6, 0.8), (0.0, 1.0)))

    async def test_entity_loader_preflights_then_streams_projected_columns(self) -> None:
        rows = [
            ("entity-a", _blob((3.0, 4.0))),
            ("bad", b"\x00"),
            ("entity-b", _blob((0.0, 2.0))),
        ]
        session = _FakeSession(rows)
        store = MySQLVectorStore(
            cast(Any, _FakeSessionFactory(session)),
            dim=2,
            load_batch_rows=2,
            load_max_rows=10,
            load_max_source_bytes=1024,
            load_max_memory_bytes=4096,
        )

        index = await store._load_vectors(
            VectorEntity,
            VectorEntity.tenant_id,
            "tenant-a",
            "entity_id",
        )

        assert isinstance(index, NormalizedVectorIndex)
        assert index.ids == ("entity-a", "entity-b")
        assert session.execute_calls == 1
        assert session.stream_calls == 1
        assert session.selected_stream_columns == ("entity_id", "embedding")
        assert session.stream_result.partition_sizes == [2]

    async def test_audio_loader_uses_same_bounded_streaming_path(self) -> None:
        rows = [(7, _blob((1.0, 0.0))), (8, _blob((0.0, 1.0)))]
        session = _FakeSession(rows)
        store = MySQLAudioVectorStore(
            cast(Any, _FakeSessionFactory(session)),
            dim=2,
            load_batch_rows=1,
            load_max_rows=10,
            load_max_source_bytes=1024,
            load_max_memory_bytes=4096,
        )

        index = await store._load_vectors("tenant-a")

        assert isinstance(index, NormalizedVectorIndex)
        assert index.ids == (7, 8)
        assert session.selected_stream_columns == ("id", "vector")
        assert session.stream_result.partition_sizes == [1]

    async def test_row_budget_rejects_before_blob_stream(self) -> None:
        session = _FakeSession([], stats=(101, 808, 8))
        store = MySQLVectorStore(
            cast(Any, _FakeSessionFactory(session)),
            dim=2,
            load_batch_rows=2,
            load_max_rows=100,
            load_max_source_bytes=1024,
            load_max_memory_bytes=4096,
        )

        with pytest.raises(VectorIndexBudgetError, match="row count"):
            await store._load_vectors(
                VectorEntity,
                VectorEntity.tenant_id,
                "tenant-a",
                "entity_id",
            )

        assert session.execute_calls == 1
        assert session.stream_calls == 0

    async def test_peak_memory_budget_rejects_before_blob_stream(self) -> None:
        session = _FakeSession([], stats=(2, 16, 8))
        store = MySQLAudioVectorStore(
            cast(Any, _FakeSessionFactory(session)),
            dim=2,
            load_batch_rows=1,
            load_max_rows=10,
            load_max_source_bytes=1024,
            load_max_memory_bytes=200,
        )

        with pytest.raises(VectorIndexBudgetError, match="peak"):
            await store._load_vectors("tenant-a")

        assert session.stream_calls == 0
