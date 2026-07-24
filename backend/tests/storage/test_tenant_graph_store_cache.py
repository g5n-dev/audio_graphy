"""Concurrency and eviction gates for the per-tenant NetworkX cache."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from audio_graphy.storage.tenant_graph_cache import TenantGraphStoreCache


@pytest.mark.unit
class TestTenantGraphStoreCache:
    def test_lru_is_bounded_and_reads_refresh_recency(self) -> None:
        cache = TenantGraphStoreCache[object](max_entries=2)
        first = object()
        second = object()
        third = object()

        cache["a"] = first
        cache["b"] = second
        assert cache["a"] is first
        cache["c"] = third

        assert tuple(cache) == ("a", "c")
        assert "b" not in cache
        assert cache.evictions == 1

    def test_eviction_cleans_lock_metadata_after_active_loader_exits(self) -> None:
        cache = TenantGraphStoreCache[object](max_entries=1)

        with cache.load_guard("a"):
            cache["a"] = object()
            cache["b"] = object()
            assert cache.load_lock_count == 1

        assert "a" not in cache
        assert cache.load_lock_count == 0

    def test_invalid_capacity_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            TenantGraphStoreCache[object](max_entries=0)


@pytest.mark.unit
async def test_graph_factory_single_flight_lru_and_cold_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from audio_graphy.main import _build_graph_store_factory

    load_counts: dict[str, int] = {}
    counts_guard = threading.Lock()

    class _StubGraphStore:
        def __init__(self, _working_dir: Path, *, tenant_id: str) -> None:
            self.tenant_id = tenant_id
            self._loaded = False

        def _sync_load(self) -> None:
            with counts_guard:
                load_counts[self.tenant_id] = load_counts.get(self.tenant_id, 0) + 1
            time.sleep(0.01)

        def invalidate_path_projection(self) -> None:
            return None

    monkeypatch.setattr(
        "audio_graphy.storage.graph_networkx.NetworkXGraphStore",
        _StubGraphStore,
    )
    cache = TenantGraphStoreCache[Any](max_entries=2)
    factory = _build_graph_store_factory(cache, tmp_path)

    concurrent = await asyncio.gather(*(factory("a") for _ in range(6)))
    assert all(store is concurrent[0] for store in concurrent)
    assert load_counts == {"a": 1}

    first_b = await factory("b")
    assert await factory("a") is concurrent[0]
    await factory("c")
    assert tuple(cache) == ("a", "c")
    assert cache.load_lock_count == 2

    second_b = await factory("b")
    assert second_b is not first_b
    assert load_counts["b"] == 2
    assert len(cache) == 2
    assert cache.load_lock_count == 2
