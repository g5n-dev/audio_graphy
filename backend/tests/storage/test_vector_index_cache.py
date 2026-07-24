"""Unit tests for the reusable in-memory normalized vector index."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

import numpy as np
import pytest

from audio_graphy.storage.mysql_audio_vector import MySQLAudioVectorStore
from audio_graphy.storage.mysql_vector import MySQLVectorStore
from audio_graphy.storage.vector_index_cache import (
    TenantVectorIndexCache,
    build_normalized_index,
    search_normalized_index,
)


def _blob(values: tuple[float, ...]) -> bytes:
    return np.asarray(values, dtype=np.float32).tobytes()


@pytest.mark.unit
class TestNormalizedVectorIndex:
    def test_build_filters_corrupt_and_wrong_dimension_rows(self) -> None:
        index = build_normalized_index(
            [
                ("good", _blob((3.0, 4.0))),
                ("wrong-dim", _blob((1.0,))),
                ("corrupt", b"\x00\x00\x00"),
            ],
            dim=2,
            log_label="test",
        )

        assert index.ids == ("good",)
        assert index.matrix.shape == (1, 2)
        assert np.allclose(index.matrix[0], np.asarray((0.6, 0.8), dtype=np.float32))
        assert not index.matrix.flags.writeable

    def test_search_returns_descending_top_k(self) -> None:
        index = build_normalized_index(
            [
                ("x", _blob((1.0, 0.0))),
                ("diagonal", _blob((1.0, 1.0))),
                ("y", _blob((0.0, 1.0))),
            ],
            dim=2,
            log_label="test",
        )

        hits = search_normalized_index(index, (1.0, 0.0), top_k=2)

        assert [hit.id for hit in hits] == ["x", "diagonal"]
        assert hits[0].score == pytest.approx(1.0)
        assert hits[0].score >= hits[1].score

    def test_search_rejects_query_dimension_mismatch(self) -> None:
        index = build_normalized_index(
            [("x", _blob((1.0, 0.0)))],
            dim=2,
            log_label="test",
        )

        with pytest.raises(ValueError, match="expected 2"):
            search_normalized_index(index, (1.0,), top_k=1)

    def test_zero_query_is_safe(self) -> None:
        index = build_normalized_index(
            [("x", _blob((1.0, 0.0)))],
            dim=2,
            log_label="test",
        )

        hits = search_normalized_index(index, (0.0, 0.0), top_k=1)

        assert len(hits) == 1
        assert hits[0].score == pytest.approx(0.0)


@pytest.mark.unit
class TestTenantVectorIndexCache:
    async def test_reuses_fresh_index_without_reloading(self) -> None:
        calls = 0
        cache = TenantVectorIndexCache(ttl_seconds=60.0, max_entries=4)

        async def loader() -> list[tuple[str | int, bytes]]:
            nonlocal calls
            calls += 1
            return [("x", _blob((1.0, 0.0)))]

        first = await cache.get_or_load(("entity", "tenant-a"), loader, dim=2)
        second = await cache.get_or_load(("entity", "tenant-a"), loader, dim=2)

        assert first is second
        assert calls == 1
        assert cache.stats.loads == 1
        assert cache.stats.hits == 1

    async def test_concurrent_miss_is_single_flight(self) -> None:
        calls = 0
        release = asyncio.Event()
        cache = TenantVectorIndexCache(ttl_seconds=60.0, max_entries=4)

        async def loader() -> list[tuple[str | int, bytes]]:
            nonlocal calls
            calls += 1
            await release.wait()
            return [("x", _blob((1.0, 0.0)))]

        tasks = [
            asyncio.create_task(cache.get_or_load(("entity", "tenant-a"), loader, dim=2))
            for _ in range(5)
        ]
        await asyncio.sleep(0)
        release.set()
        indexes = await asyncio.gather(*tasks)

        assert calls == 1
        assert all(index is indexes[0] for index in indexes)

    async def test_invalidate_forces_reload(self) -> None:
        calls = 0
        cache = TenantVectorIndexCache(ttl_seconds=60.0, max_entries=4)

        async def loader() -> list[tuple[str | int, bytes]]:
            nonlocal calls
            calls += 1
            return [("x", _blob((float(calls), 0.0)))]

        await cache.get_or_load(("entity", "tenant-a"), loader, dim=2)
        cache.invalidate(("entity", "tenant-a"))
        await cache.get_or_load(("entity", "tenant-a"), loader, dim=2)

        assert calls == 2
        assert cache.stats.invalidations == 1

    async def test_invalidation_during_load_cannot_publish_stale_index(self) -> None:
        calls = 0
        first_load_started = asyncio.Event()
        release_first_load = asyncio.Event()
        cache = TenantVectorIndexCache(ttl_seconds=60.0, max_entries=4)

        async def loader() -> list[tuple[str | int, bytes]]:
            nonlocal calls
            calls += 1
            if calls == 1:
                first_load_started.set()
                await release_first_load.wait()
            return [(f"version-{calls}", _blob((1.0, 0.0)))]

        pending = asyncio.create_task(cache.get_or_load(("entity", "tenant-a"), loader, dim=2))
        await first_load_started.wait()
        cache.invalidate(("entity", "tenant-a"))
        release_first_load.set()
        index = await pending

        assert calls == 2
        assert index.ids == ("version-2",)

    async def test_ttl_expiry_forces_reload(self) -> None:
        now = 10.0
        calls = 0

        def clock() -> float:
            return now

        cache = TenantVectorIndexCache(
            ttl_seconds=5.0,
            max_entries=4,
            clock=clock,
        )

        async def loader() -> list[tuple[str | int, bytes]]:
            nonlocal calls
            calls += 1
            return [("x", _blob((1.0, 0.0)))]

        await cache.get_or_load(("entity", "tenant-a"), loader, dim=2)
        now = 16.0
        await cache.get_or_load(("entity", "tenant-a"), loader, dim=2)

        assert calls == 2

    async def test_lru_capacity_evicts_oldest_entry(self) -> None:
        calls: dict[str, int] = {}
        cache = TenantVectorIndexCache(ttl_seconds=60.0, max_entries=2)

        def make_loader(
            tenant: str,
        ) -> Callable[[], Awaitable[list[tuple[str | int, bytes]]]]:
            async def loader() -> list[tuple[str | int, bytes]]:
                calls[tenant] = calls.get(tenant, 0) + 1
                return [(tenant, _blob((1.0, 0.0)))]

            return loader

        await cache.get_or_load(("entity", "a"), make_loader("a"), dim=2)
        await cache.get_or_load(("entity", "b"), make_loader("b"), dim=2)
        await cache.get_or_load(("entity", "a"), make_loader("a"), dim=2)
        await cache.get_or_load(("entity", "c"), make_loader("c"), dim=2)
        await cache.get_or_load(("entity", "b"), make_loader("b"), dim=2)

        assert calls == {"a": 1, "b": 2, "c": 1}
        assert cache.stats.evictions == 2

    async def test_byte_budget_evicts_oldest_matrix(self) -> None:
        cache = TenantVectorIndexCache(
            ttl_seconds=60.0,
            max_entries=10,
            max_bytes=96,
        )

        async def loader(value: float) -> list[tuple[str | int, bytes]]:
            return [(f"{value}-{index}", _blob((value, 0.0))) for index in range(3)]

        first = await cache.get_or_load(
            ("entity", "a"),
            lambda: loader(1.0),
            dim=2,
        )
        await cache.get_or_load(
            ("entity", "b"),
            lambda: loader(2.0),
            dim=2,
        )

        assert first.matrix.nbytes == 24
        assert cache.entry_count == 1
        assert cache.cached_bytes <= cache.max_bytes
        assert cache.stats.evictions == 1

    async def test_index_larger_than_byte_budget_is_not_retained(self) -> None:
        calls = 0
        cache = TenantVectorIndexCache(
            ttl_seconds=60.0,
            max_entries=10,
            max_bytes=32,
        )

        async def loader() -> list[tuple[str | int, bytes]]:
            nonlocal calls
            calls += 1
            return [(f"id-{index}", _blob((1.0, 0.0))) for index in range(3)]

        await cache.get_or_load(("entity", "a"), loader, dim=2)
        await cache.get_or_load(("entity", "a"), loader, dim=2)

        assert calls == 2
        assert cache.entry_count == 0
        assert cache.cached_bytes == 0
        assert cache.stats.oversized_skips == 2


@pytest.mark.unit
class TestVectorStoreCacheIntegration:
    async def test_entity_search_reuses_loaded_tenant_matrix(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = MySQLVectorStore(cast(Any, None), dim=2)
        calls = 0

        async def load_vectors(*_args: object) -> list[tuple[str | int, bytes]]:
            nonlocal calls
            calls += 1
            return [("x", _blob((1.0, 0.0)))]

        monkeypatch.setattr(store, "_load_vectors", load_vectors)

        first = await store.search_entities("tenant-a", (1.0, 0.0), top_k=1)
        second = await store.search_entities("tenant-a", (1.0, 0.0), top_k=1)

        assert first == second
        assert calls == 1
        assert store.cache_stats.hits == 1

    async def test_entity_cache_can_be_invalidated_by_tenant(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = MySQLVectorStore(cast(Any, None), dim=2)
        calls = 0

        async def load_vectors(*_args: object) -> list[tuple[str | int, bytes]]:
            nonlocal calls
            calls += 1
            return [("x", _blob((1.0, 0.0)))]

        monkeypatch.setattr(store, "_load_vectors", load_vectors)

        await store.search_entities("tenant-a", (1.0, 0.0), top_k=1)
        store.invalidate_tenant("tenant-a")
        await store.search_entities("tenant-a", (1.0, 0.0), top_k=1)

        assert calls == 2

    async def test_audio_search_reuses_loaded_tenant_matrix(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = MySQLAudioVectorStore(cast(Any, None), dim=2)
        calls = 0

        async def load_vectors(_tenant_id: str) -> list[tuple[int, bytes]]:
            nonlocal calls
            calls += 1
            return [(7, _blob((1.0, 0.0)))]

        async def load_metadata(
            _tenant_id: str,
            _vector_ids: list[int],
        ) -> dict[int, tuple[int, int, int | None]]:
            return {7: (4, 5, 6)}

        monkeypatch.setattr(store, "_load_vectors", load_vectors)
        monkeypatch.setattr(store, "_load_hit_metadata", load_metadata)

        first = await store.search_audio("tenant-a", (1.0, 0.0), top_k=1)
        second = await store.search_audio("tenant-a", (1.0, 0.0), top_k=1)

        assert first == second
        assert calls == 1
        assert store.cache_stats.hits == 1
