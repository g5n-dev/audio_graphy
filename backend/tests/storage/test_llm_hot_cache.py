"""Bounded local cache, Redis cache, and automatic failover tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from cryptography.fernet import Fernet

from audio_graphy.core.llm_cache_crypto import LLMCacheCrypto
from audio_graphy.storage.llm_hot_cache import (
    CacheIdentity,
    FailoverHotCache,
    HotCacheValue,
    LocalHotCache,
    RedisHotCache,
)


def _identity(recipe_character: str = "a", *, tenant_id: str = "tenant-a") -> CacheIdentity:
    return CacheIdentity(
        tenant_id=tenant_id,
        namespace="keyword_extract",
        recipe_sha256=recipe_character * 64,
    )


def _crypto(tmp_path: Path) -> LLMCacheCrypto:
    key_path = tmp_path / "master.key"
    key_path.write_bytes(Fernet.generate_key())
    return LLMCacheCrypto(key_path, max_plaintext_bytes=2 * 1024 * 1024)


async def test_local_cache_enforces_ttl_lru_bytes_and_item_limit() -> None:
    now = 10.0
    cache = LocalHotCache(
        max_entries=2,
        max_bytes=380,
        max_item_bytes=64,
        max_ttl_seconds=5,
        clock=lambda: now,
    )
    await cache.start()

    assert await cache.set(_identity("a"), HotCacheValue(b"a" * 32, False), ttl_seconds=60)
    assert await cache.set(_identity("b"), HotCacheValue(b"b" * 32, True), ttl_seconds=60)
    assert (await cache.get(_identity("a"))).payload == b"a" * 32  # type: ignore[union-attr]
    assert await cache.set(_identity("c"), HotCacheValue(b"c" * 32, False), ttl_seconds=60)

    assert await cache.get(_identity("b")) is None
    assert cache.entry_count <= 2
    assert cache.cached_bytes <= cache.max_bytes
    assert not await cache.set(
        _identity("d"),
        HotCacheValue(b"d" * 65, False),
        ttl_seconds=1,
    )

    now = 16.0
    assert await cache.get(_identity("a")) is None
    assert await cache.get(_identity("c")) is None
    assert cache.cached_bytes == 0


async def test_local_cache_clear_tenant_and_close_release_all_metadata() -> None:
    cache = LocalHotCache(max_entries=10, max_bytes=4096, max_item_bytes=1024)
    await cache.start()
    await cache.set(_identity("a", tenant_id="a"), HotCacheValue(b"a", True), ttl_seconds=60)
    await cache.set(_identity("b", tenant_id="b"), HotCacheValue(b"b", False), ttl_seconds=60)

    assert await cache.clear_tenant("a") == 1
    assert await cache.get(_identity("a", tenant_id="a")) is None
    assert await cache.get(_identity("b", tenant_id="b")) is not None

    await cache.aclose()
    assert cache.entry_count == 0
    assert cache.cached_bytes == 0


async def test_local_capacity_eviction_increments_low_cardinality_metric() -> None:
    from audio_graphy.api.metrics import LLM_CACHE_EVICTIONS

    counter = LLM_CACHE_EVICTIONS.labels("local")
    before = counter._value.get()  # type: ignore[attr-defined]
    cache = LocalHotCache(
        max_entries=1,
        max_bytes=512,
        max_item_bytes=64,
    )

    assert await cache.set(_identity("a"), HotCacheValue(b"a", False), ttl_seconds=60)
    assert await cache.set(_identity("b"), HotCacheValue(b"b", False), ttl_seconds=60)

    assert counter._value.get() == before + 1  # type: ignore[attr-defined]
    await cache.aclose()


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.expiries: dict[str, int] = {}
        self.ping_failures = 0
        self.closed = False

    async def ping(self) -> bool:
        if self.ping_failures:
            self.ping_failures -= 1
            raise ConnectionError("redis unavailable")
        return True

    async def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    async def set(self, key: str, value: bytes, *, ex: int) -> bool:
        self.values[key] = value
        self.expiries[key] = ex
        return True

    async def unlink(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            removed += int(self.values.pop(key, None) is not None)
            self.expiries.pop(key, None)
        return removed

    async def scan_iter(self, *, match: str, count: int) -> AsyncIterator[str]:
        prefix = match.removesuffix("*")
        for key in list(self.values):
            if key.startswith(prefix):
                yield key

    async def aclose(self) -> None:
        self.closed = True


async def test_redis_cache_encrypts_values_hashes_tenant_and_caps_ttl(tmp_path: Path) -> None:
    client = _FakeRedis()
    cache = RedisHotCache(
        "redis://unused",
        crypto=_crypto(tmp_path),
        client=client,
        max_item_bytes=1024,
        max_ttl_seconds=3600,
    )
    await cache.start()
    value = HotCacheValue(payload=b"raw sensitive output", has_provenance=True)

    assert await cache.set(_identity(), value, ttl_seconds=9999)
    [redis_key] = client.values
    assert "tenant-a" not in redis_key
    assert b"raw sensitive output" not in client.values[redis_key]
    assert client.expiries[redis_key] == 3600
    assert await cache.get(_identity()) == value
    assert await cache.clear_tenant("tenant-a") == 1
    assert client.values == {}


async def test_redis_rejects_compressible_raw_payload_over_item_limit(tmp_path: Path) -> None:
    client = _FakeRedis()
    cache = RedisHotCache(
        "redis://unused",
        crypto=_crypto(tmp_path),
        client=client,
        max_item_bytes=1024,
    )
    await cache.start()

    assert not await cache.set(
        _identity(),
        HotCacheValue(payload=b"x" * 1025, has_provenance=False),
        ttl_seconds=60,
    )
    assert client.values == {}


class _FlakyPrimary:
    backend_name = "redis"

    def __init__(self) -> None:
        self.values: dict[CacheIdentity, HotCacheValue] = {}
        self.fail_operations = False
        self.fail_probe = False
        self.ping_succeeds = True

    async def start(self) -> None:
        if not await self.ping():
            raise ConnectionError("ping unsuccessful")

    async def ping(self) -> bool:
        if self.fail_probe:
            raise ConnectionError("probe failed")
        return self.ping_succeeds

    async def get(self, key: CacheIdentity) -> HotCacheValue | None:
        if self.fail_operations:
            raise ConnectionError("get failed")
        return self.values.get(key)

    async def set(
        self,
        key: CacheIdentity,
        value: HotCacheValue,
        *,
        ttl_seconds: int,
    ) -> bool:
        if self.fail_operations:
            raise ConnectionError("set failed")
        self.values[key] = value
        return True

    async def delete(self, key: CacheIdentity) -> bool:
        if self.fail_operations:
            raise ConnectionError("delete failed")
        return self.values.pop(key, None) is not None

    async def delete_many(self, keys: list[CacheIdentity]) -> int:
        return sum([int(await self.delete(key)) for key in keys])

    async def clear_tenant(self, tenant_id: str) -> int:
        keys = [key for key in self.values if key.tenant_id == tenant_id]
        return await self.delete_many(keys)

    async def aclose(self) -> None:
        self.values.clear()


async def test_failover_uses_local_only_on_failure_and_recovers_after_two_probes() -> None:
    from audio_graphy.api.metrics import LLM_REDIS_FALLBACKS

    now = 10.0
    fallback_counter = LLM_REDIS_FALLBACKS.labels("operation_failure")
    fallback_before = fallback_counter._value.get()  # type: ignore[attr-defined]
    primary = _FlakyPrimary()
    local = LocalHotCache(max_entries=8, max_bytes=4096, max_item_bytes=1024)
    cache = FailoverHotCache(
        primary,
        local,
        failure_threshold=3,
        circuit_seconds=30,
        recovery_successes=2,
        probe_interval_seconds=3600,
        clock=lambda: now,
    )
    await cache.start()
    value = HotCacheValue(b"value", True)

    assert await cache.set(_identity(), value, ttl_seconds=60)
    assert local.entry_count == 0
    assert cache.backend_name == "redis"

    primary.fail_operations = True
    for character in ("b", "c", "d"):
        assert await cache.set(_identity(character), value, ttl_seconds=60)
    assert cache.backend_name == "local"
    assert local.entry_count == 3
    assert fallback_counter._value.get() == fallback_before + 1  # type: ignore[attr-defined]

    primary.fail_operations = False
    now = 41.0
    assert not await cache.probe_once()
    assert await cache.probe_once()
    assert cache.backend_name == "redis"
    assert local.entry_count == 0
    assert fallback_counter._value.get() == fallback_before + 1  # type: ignore[attr-defined]
    await cache.aclose()


async def test_failover_cache_never_surfaces_backend_errors() -> None:
    from audio_graphy.api.metrics import LLM_REDIS_FALLBACKS

    fallback_counter = LLM_REDIS_FALLBACKS.labels("startup_failure")
    fallback_before = fallback_counter._value.get()  # type: ignore[attr-defined]
    primary = _FlakyPrimary()
    primary.fail_probe = True
    local = LocalHotCache(max_entries=8, max_bytes=4096, max_item_bytes=1024)
    cache = FailoverHotCache(
        primary,
        local,
        probe_interval_seconds=3600,
    )

    await cache.start()
    assert cache.backend_name == "local"
    assert await cache.get(_identity()) is None
    assert await cache.set(_identity(), HotCacheValue(b"x", False), ttl_seconds=5)
    assert (await cache.get(_identity())) == HotCacheValue(b"x", False)
    assert fallback_counter._value.get() == fallback_before + 1  # type: ignore[attr-defined]
    await cache.aclose()


async def test_erasure_requires_configured_redis_to_confirm_physical_delete() -> None:
    now = 10.0
    primary = _FlakyPrimary()
    local = LocalHotCache(max_entries=8, max_bytes=4096, max_item_bytes=1024)
    cache = FailoverHotCache(
        primary,
        local,
        failure_threshold=1,
        circuit_seconds=30,
        recovery_successes=2,
        probe_interval_seconds=3600,
        clock=lambda: now,
    )
    await cache.start()
    key = _identity()
    value = HotCacheValue(b"private", True)
    assert await cache.set(key, value, ttl_seconds=60)

    primary.fail_operations = True
    assert await cache.get(key) is None
    assert await local.set(key, value, ttl_seconds=60)
    assert not await cache.erase_many([key])
    assert await local.get(key) is None
    assert key in primary.values

    primary.fail_operations = False
    now = 41.0
    assert not await cache.probe_once()
    assert await cache.probe_once()
    assert await cache.erase_many([key])
    assert key not in primary.values
    await cache.aclose()


async def test_unsuccessful_ping_is_not_counted_as_redis_health() -> None:
    primary = _FlakyPrimary()
    primary.ping_succeeds = False
    cache = FailoverHotCache(
        primary,
        LocalHotCache(max_entries=8, max_bytes=4096, max_item_bytes=1024),
        probe_interval_seconds=3600,
    )

    await cache.start()

    assert cache.backend_name == "local"
    assert await cache.set(_identity(), HotCacheValue(b"x", False), ttl_seconds=5)
    await cache.aclose()


async def test_transient_primary_recovery_clears_duplicate_local_values() -> None:
    primary = _FlakyPrimary()
    local = LocalHotCache(max_entries=8, max_bytes=4096, max_item_bytes=1024)
    cache = FailoverHotCache(
        primary,
        local,
        failure_threshold=3,
        probe_interval_seconds=3600,
    )
    await cache.start()

    primary.fail_operations = True
    assert await cache.set(
        _identity("b"),
        HotCacheValue(b"fallback", False),
        ttl_seconds=60,
    )
    assert local.entry_count == 1
    assert cache.backend_name == "redis"

    primary.fail_operations = False
    assert await cache.get(_identity("c")) is None
    assert local.entry_count == 0
    await cache.aclose()


async def test_failover_background_task_is_cancelled_on_close() -> None:
    primary = _FlakyPrimary()
    primary.fail_probe = True
    cache = FailoverHotCache(
        primary,
        LocalHotCache(max_entries=8, max_bytes=4096, max_item_bytes=1024),
        probe_interval_seconds=0.01,
    )
    await cache.start()
    await asyncio.sleep(0)

    await cache.aclose()

    assert cache.closed
