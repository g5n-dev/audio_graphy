"""Bounded hot-cache implementations with optional Redis and local failover."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from audio_graphy.core.llm_cache_crypto import (
    EncryptedCachePayload,
    LLMCacheCrypto,
)
from audio_graphy.observability.metrics import LLM_CACHE_EVICTIONS, LLM_REDIS_FALLBACKS

logger = logging.getLogger(__name__)

_NAMESPACE_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")
_REDIS_PREFIX = "ag:llm:v1"
_HOT_VALUE_MAGIC = b"AGH1"
_METADATA: Mapping[str, str | int] = {
    "version": 1,
    "algorithm": "AES-256-GCM",
    "kdf": "HKDF-SHA256",
    "compression": "zlib",
}


@dataclass(frozen=True, slots=True)
class CacheIdentity:
    """Tenant-scoped exact-cache address."""

    tenant_id: str
    namespace: str
    recipe_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, str) or not 1 <= len(self.tenant_id) <= 64:
            raise ValueError("tenant_id must contain 1 to 64 characters")
        if not isinstance(self.namespace, str) or _NAMESPACE_RE.fullmatch(self.namespace) is None:
            raise ValueError("namespace contains unsupported characters")
        if (
            not isinstance(self.recipe_sha256, str)
            or len(self.recipe_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.recipe_sha256)
        ):
            raise ValueError("recipe_sha256 must be a lowercase SHA-256 hex digest")

    @property
    def tenant_hash(self) -> str:
        return hashlib.sha256(self.tenant_id.encode("utf-8")).hexdigest()

    @property
    def redis_key(self) -> str:
        return f"{_REDIS_PREFIX}:{self.tenant_hash}:{self.namespace}:{self.recipe_sha256}"


@dataclass(frozen=True, slots=True)
class HotCacheValue:
    """Opaque gateway bytes plus a flag requiring persistent revalidation."""

    payload: bytes
    has_provenance: bool

    def __post_init__(self) -> None:
        if not isinstance(self.payload, bytes):
            raise TypeError("HotCacheValue.payload must be bytes")


@runtime_checkable
class HotCache(Protocol):
    @property
    def backend_name(self) -> str: ...

    async def start(self) -> None: ...

    async def get(self, key: CacheIdentity) -> HotCacheValue | None: ...

    async def set(
        self,
        key: CacheIdentity,
        value: HotCacheValue,
        *,
        ttl_seconds: int,
    ) -> bool: ...

    async def delete(self, key: CacheIdentity) -> bool: ...

    async def delete_many(self, keys: Sequence[CacheIdentity]) -> int: ...

    async def erase_many(self, keys: Sequence[CacheIdentity]) -> bool: ...

    async def clear_tenant(self, tenant_id: str) -> int: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _LocalEntry:
    value: HotCacheValue
    expires_at: float
    size_bytes: int


class LocalHotCache:
    """Process-local TTL/LRU cache with exact logical byte accounting."""

    backend_name = "local"

    def __init__(
        self,
        *,
        max_entries: int = 1024,
        max_bytes: int = 32 * 1024 * 1024,
        max_item_bytes: int = 1024 * 1024,
        max_ttl_seconds: int = 300,
        clock: Any = time.monotonic,
    ) -> None:
        for name, value in {
            "max_entries": max_entries,
            "max_bytes": max_bytes,
            "max_item_bytes": max_item_bytes,
            "max_ttl_seconds": max_ttl_seconds,
        }.items():
            if value < 1:
                raise ValueError(f"{name} must be positive")
        if max_item_bytes > max_bytes:
            raise ValueError("max_item_bytes cannot exceed max_bytes")
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._max_item_bytes = max_item_bytes
        self._max_ttl_seconds = max_ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[CacheIdentity, _LocalEntry] = OrderedDict()
        self._cached_bytes = 0
        self._lock = asyncio.Lock()

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    @property
    def cached_bytes(self) -> int:
        return self._cached_bytes

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    async def start(self) -> None:
        return None

    async def get(self, key: CacheIdentity) -> HotCacheValue | None:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= self._clock():
                self._remove(key)
                return None
            self._entries.move_to_end(key)
            return entry.value

    async def set(
        self,
        key: CacheIdentity,
        value: HotCacheValue,
        *,
        ttl_seconds: int,
    ) -> bool:
        if ttl_seconds < 1:
            return False
        if len(value.payload) > self._max_item_bytes:
            return False
        ttl = min(ttl_seconds, self._max_ttl_seconds)
        size_bytes = len(key.redis_key.encode("utf-8")) + len(value.payload) + 1
        if size_bytes > self._max_bytes:
            return False
        async with self._lock:
            self._remove(key)
            self._entries[key] = _LocalEntry(
                value=value,
                expires_at=self._clock() + ttl,
                size_bytes=size_bytes,
            )
            self._cached_bytes += size_bytes
            self._entries.move_to_end(key)
            self._evict()
            return key in self._entries

    async def delete(self, key: CacheIdentity) -> bool:
        async with self._lock:
            return self._remove(key)

    async def delete_many(self, keys: Sequence[CacheIdentity]) -> int:
        async with self._lock:
            return sum(int(self._remove(key)) for key in keys)

    async def erase_many(self, keys: Sequence[CacheIdentity]) -> bool:
        await self.delete_many(keys)
        return True

    async def clear_tenant(self, tenant_id: str) -> int:
        async with self._lock:
            keys = [key for key in self._entries if key.tenant_id == tenant_id]
            for key in keys:
                self._remove(key)
            return len(keys)

    async def clear_all(self) -> int:
        async with self._lock:
            count = len(self._entries)
            self._entries.clear()
            self._cached_bytes = 0
            return count

    async def aclose(self) -> None:
        await self.clear_all()

    def _remove(self, key: CacheIdentity) -> bool:
        entry = self._entries.pop(key, None)
        if entry is None:
            return False
        self._cached_bytes -= entry.size_bytes
        return True

    def _evict(self) -> None:
        evicted = 0
        while len(self._entries) > self._max_entries or self._cached_bytes > self._max_bytes:
            _, entry = self._entries.popitem(last=False)
            self._cached_bytes -= entry.size_bytes
            evicted += 1
        if evicted:
            LLM_CACHE_EVICTIONS.labels("local").inc(evicted)


class RedisHotCache:
    """Encrypted Redis hot cache. Failures intentionally propagate to its manager."""

    backend_name = "redis"

    def __init__(
        self,
        url: str,
        *,
        crypto: LLMCacheCrypto,
        client: Any | None = None,
        max_item_bytes: int = 1024 * 1024,
        max_ttl_seconds: int = 3600,
        socket_timeout_seconds: float = 1.0,
    ) -> None:
        if not url:
            raise ValueError("Redis URL is required")
        if max_item_bytes < 1 or max_ttl_seconds < 1:
            raise ValueError("Redis cache limits must be positive")
        if socket_timeout_seconds <= 0:
            raise ValueError("socket_timeout_seconds must be positive")
        self._url = url
        self._crypto = crypto
        self._client = client
        self._owns_client = client is None
        self._max_item_bytes = max_item_bytes
        self._max_ttl_seconds = max_ttl_seconds
        self._socket_timeout_seconds = socket_timeout_seconds

    async def start(self) -> None:
        if self._client is None:
            from redis.asyncio import Redis

            self._client = Redis.from_url(
                self._url,
                decode_responses=False,
                socket_connect_timeout=self._socket_timeout_seconds,
                socket_timeout=self._socket_timeout_seconds,
                health_check_interval=30,
            )
        if not await self.ping():
            raise ConnectionError("Redis PING returned an unsuccessful response")

    async def ping(self) -> bool:
        if self._client is None:
            raise RuntimeError("Redis cache has not been started")
        return bool(await self._client.ping())

    async def get(self, key: CacheIdentity) -> HotCacheValue | None:
        client = self._require_client()
        encoded = await client.get(key.redis_key)
        if encoded is None:
            return None
        if not isinstance(encoded, bytes):
            encoded = bytes(encoded)
        if len(encoded) < len(_HOT_VALUE_MAGIC) + 2 or not encoded.startswith(_HOT_VALUE_MAGIC):
            await client.unlink(key.redis_key)
            return None
        provenance_byte = encoded[len(_HOT_VALUE_MAGIC)]
        if provenance_byte not in (0, 1):
            await client.unlink(key.redis_key)
            return None
        encrypted = EncryptedCachePayload(
            blob=encoded[len(_HOT_VALUE_MAGIC) + 1 :],
            metadata=dict(_METADATA),
        )
        try:
            payload = self._crypto.decrypt(
                tenant_id=key.tenant_id,
                namespace=key.namespace,
                recipe_sha256=key.recipe_sha256,
                encrypted=encrypted,
            )
        except ValueError:
            logger.warning(
                "Discarding corrupt Redis LLM cache value namespace=%s recipe=%s",
                key.namespace,
                key.recipe_sha256[:12],
            )
            await client.unlink(key.redis_key)
            return None
        return HotCacheValue(payload, bool(provenance_byte))

    async def set(
        self,
        key: CacheIdentity,
        value: HotCacheValue,
        *,
        ttl_seconds: int,
    ) -> bool:
        if ttl_seconds < 1:
            return False
        if len(value.payload) > self._max_item_bytes:
            return False
        encrypted = self._crypto.encrypt(
            tenant_id=key.tenant_id,
            namespace=key.namespace,
            recipe_sha256=key.recipe_sha256,
            plaintext=value.payload,
        )
        encoded = _HOT_VALUE_MAGIC + bytes([int(value.has_provenance)]) + encrypted.blob
        if len(encoded) > self._max_item_bytes:
            return False
        result = await self._require_client().set(
            key.redis_key,
            encoded,
            ex=min(ttl_seconds, self._max_ttl_seconds),
        )
        return bool(result)

    async def delete(self, key: CacheIdentity) -> bool:
        return bool(await self._require_client().unlink(key.redis_key))

    async def delete_many(self, keys: Sequence[CacheIdentity]) -> int:
        redis_keys = [key.redis_key for key in keys]
        if not redis_keys:
            return 0
        return int(await self._require_client().unlink(*redis_keys))

    async def erase_many(self, keys: Sequence[CacheIdentity]) -> bool:
        await self.delete_many(keys)
        return True

    async def clear_tenant(self, tenant_id: str) -> int:
        tenant_hash = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
        pattern = f"{_REDIS_PREFIX}:{tenant_hash}:*"
        client = self._require_client()
        batch: list[str | bytes] = []
        removed = 0
        async for redis_key in client.scan_iter(match=pattern, count=100):
            batch.append(redis_key)
            if len(batch) >= 100:
                removed += int(await client.unlink(*batch))
                batch.clear()
        if batch:
            removed += int(await client.unlink(*batch))
        return removed

    async def aclose(self) -> None:
        client = self._client
        self._client = None
        if client is not None and self._owns_client:
            await client.aclose()

    def _require_client(self) -> Any:
        if self._client is None:
            raise RuntimeError("Redis cache has not been started")
        return self._client


class _RecoverableHotCache(HotCache, Protocol):
    async def ping(self) -> bool: ...


class FailoverHotCache:
    """Use Redis when healthy and a bounded local cache during outages."""

    def __init__(
        self,
        primary: _RecoverableHotCache | None,
        fallback: LocalHotCache,
        *,
        failure_threshold: int = 3,
        circuit_seconds: float = 30.0,
        recovery_successes: int = 2,
        probe_interval_seconds: float = 5.0,
        clock: Any = time.monotonic,
    ) -> None:
        for name, value in {
            "failure_threshold": failure_threshold,
            "recovery_successes": recovery_successes,
        }.items():
            if value < 1:
                raise ValueError(f"{name} must be positive")
        if circuit_seconds <= 0 or probe_interval_seconds <= 0:
            raise ValueError("circuit and probe intervals must be positive")
        self._primary = primary
        self._fallback = fallback
        self._failure_threshold = failure_threshold
        self._circuit_seconds = circuit_seconds
        self._recovery_successes = recovery_successes
        self._probe_interval_seconds = probe_interval_seconds
        self._clock = clock
        self._healthy = False
        self._consecutive_failures = 0
        self._consecutive_recoveries = 0
        self._open_until = 0.0
        self._fallback_episode_recorded = False
        self._probe_task: asyncio.Task[None] | None = None
        self._state_lock = asyncio.Lock()
        self.closed = False

    @property
    def backend_name(self) -> str:
        return "redis" if self._primary is not None and self._healthy else "local"

    async def start(self) -> None:
        self.closed = False
        await self._fallback.start()
        if self._primary is None:
            return
        try:
            await self._primary.start()
        except Exception as exc:
            self._record_failure(
                exc,
                open_circuit=True,
                reason="startup_failure",
            )
        else:
            self._healthy = True
            self._consecutive_failures = 0
            self._fallback_episode_recorded = False
            await self._fallback.clear_all()
        if self._probe_task is None or self._probe_task.done():
            self._probe_task = asyncio.create_task(self._probe_loop())

    async def get(self, key: CacheIdentity) -> HotCacheValue | None:
        if self._primary is not None and self._healthy:
            try:
                value = await self._primary.get(key)
            except Exception as exc:
                self._record_failure(exc, reason="operation_failure")
            else:
                await self._record_success()
                return value
        return await self._fallback.get(key)

    async def set(
        self,
        key: CacheIdentity,
        value: HotCacheValue,
        *,
        ttl_seconds: int,
    ) -> bool:
        if self._primary is not None and self._healthy:
            try:
                stored = await self._primary.set(
                    key,
                    value,
                    ttl_seconds=ttl_seconds,
                )
            except Exception as exc:
                self._record_failure(exc, reason="operation_failure")
            else:
                await self._record_success()
                return stored
        return await self._fallback.set(
            key,
            value,
            ttl_seconds=ttl_seconds,
        )

    async def delete(self, key: CacheIdentity) -> bool:
        removed = await self._fallback.delete(key)
        if self._primary is not None and self._healthy:
            try:
                removed = await self._primary.delete(key) or removed
            except Exception as exc:
                self._record_failure(exc, reason="operation_failure")
            else:
                await self._record_success()
        return removed

    async def delete_many(self, keys: Sequence[CacheIdentity]) -> int:
        local_removed = await self._fallback.delete_many(keys)
        primary_removed = 0
        if self._primary is not None and self._healthy:
            try:
                primary_removed = await self._primary.delete_many(keys)
            except Exception as exc:
                self._record_failure(exc, reason="operation_failure")
            else:
                await self._record_success()
        return max(local_removed, primary_removed)

    async def erase_many(self, keys: Sequence[CacheIdentity]) -> bool:
        """Physically erase privacy-scoped keys or report that retry is needed.

        Ordinary cache deletion is best effort. DSAR/retention invalidation is
        stricter: local values are always removed, and a configured Redis must
        confirm ``UNLINK`` before the durable purge queue can be acknowledged.
        """

        await self._fallback.delete_many(keys)
        if self._primary is None:
            return True
        if not self._healthy:
            return False
        try:
            await self._primary.delete_many(keys)
        except Exception as exc:
            self._record_failure(exc, reason="operation_failure")
            return False
        await self._record_success()
        return True

    async def clear_tenant(self, tenant_id: str) -> int:
        local_removed = await self._fallback.clear_tenant(tenant_id)
        primary_removed = 0
        if self._primary is not None and self._healthy:
            try:
                primary_removed = await self._primary.clear_tenant(tenant_id)
            except Exception as exc:
                self._record_failure(exc, reason="operation_failure")
            else:
                await self._record_success()
        return max(local_removed, primary_removed)

    async def probe_once(self) -> bool:
        """Run one recovery probe; return True only when Redis is restored."""

        if self._primary is None or self._healthy or self._clock() < self._open_until:
            return self._healthy
        async with self._state_lock:
            try:
                ping_succeeded = await self._primary.ping()
            except Exception as exc:
                self._record_failure(
                    exc,
                    open_circuit=True,
                    reason="probe_failure",
                )
                self._consecutive_recoveries = 0
                return False
            if not ping_succeeded:
                self._record_failure(
                    ConnectionError("Redis PING returned an unsuccessful response"),
                    open_circuit=True,
                    reason="probe_failure",
                )
                self._consecutive_recoveries = 0
                return False
            self._consecutive_recoveries += 1
            if self._consecutive_recoveries < self._recovery_successes:
                return False
            self._healthy = True
            self._consecutive_failures = 0
            self._consecutive_recoveries = 0
            self._open_until = 0.0
            self._fallback_episode_recorded = False
            await self._fallback.clear_all()
            logger.info("Redis LLM hot cache recovered")
            return True

    async def aclose(self) -> None:
        self.closed = True
        task = self._probe_task
        self._probe_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._fallback.aclose()
        if self._primary is not None:
            with contextlib.suppress(Exception):
                await self._primary.aclose()

    async def _probe_loop(self) -> None:
        while not self.closed:
            await asyncio.sleep(self._probe_interval_seconds)
            if not self._healthy:
                await self.probe_once()

    def _record_failure(
        self,
        exc: Exception,
        *,
        open_circuit: bool = False,
        reason: Literal[
            "startup_failure",
            "operation_failure",
            "probe_failure",
        ],
    ) -> None:
        self._consecutive_failures += 1
        logger.warning(
            "Redis LLM hot cache unavailable; using local cache: %s",
            exc.__class__.__name__,
        )
        if open_circuit or self._consecutive_failures >= self._failure_threshold:
            self._healthy = False
            self._open_until = self._clock() + self._circuit_seconds
            self._consecutive_recoveries = 0
            if not self._fallback_episode_recorded:
                # Keep labels finite; never derive them from exception text.
                LLM_REDIS_FALLBACKS.labels(reason).inc()
                self._fallback_episode_recorded = True

    async def _record_success(self) -> None:
        had_transient_failures = self._consecutive_failures > 0
        self._consecutive_failures = 0
        if had_transient_failures:
            # A failed Redis operation may have populated the fallback before
            # the circuit threshold was reached. Once Redis succeeds again it
            # is authoritative, so do not retain a duplicate process cache.
            await self._fallback.clear_all()


__all__ = [
    "CacheIdentity",
    "FailoverHotCache",
    "HotCache",
    "HotCacheValue",
    "LocalHotCache",
    "RedisHotCache",
]
