"""Production lifecycle wiring for the centralized LLM gateways."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.adapters.bundle import AdapterBundle
from audio_graphy.config import Settings
from audio_graphy.core.llm_cache_crypto import LLMCacheCrypto
from audio_graphy.observability.metrics import LLM_CACHE_EVICTIONS
from audio_graphy.services.llm_cache_coordinator import (
    HotOnlyLLMCache,
    LLMCacheCoordinator,
)
from audio_graphy.services.llm_gateway import LLMCache, LLMGateway, LLMPriceSnapshot
from audio_graphy.services.llm_observability import LLMCallObserver
from audio_graphy.storage.llm_cache_store import CleanupStats, LLMCacheStore
from audio_graphy.storage.llm_hot_cache import (
    CacheIdentity,
    FailoverHotCache,
    HotCache,
    HotCacheValue,
    LocalHotCache,
    RedisHotCache,
)

logger = logging.getLogger(__name__)


class _DisabledHotCache:
    """No-op hot layer used by the independent rollout switch."""

    backend_name = "disabled"

    async def start(self) -> None:
        return None

    async def get(self, key: CacheIdentity) -> HotCacheValue | None:
        del key
        return None

    async def set(
        self,
        key: CacheIdentity,
        value: HotCacheValue,
        *,
        ttl_seconds: int,
    ) -> bool:
        del key, value, ttl_seconds
        return False

    async def delete(self, key: CacheIdentity) -> bool:
        del key
        return False

    async def delete_many(self, keys: Sequence[CacheIdentity]) -> int:
        del keys
        return 0

    async def erase_many(self, keys: Sequence[CacheIdentity]) -> bool:
        del keys
        return True

    async def clear_tenant(self, tenant_id: str) -> int:
        del tenant_id
        return 0

    async def aclose(self) -> None:
        return None


@dataclass(slots=True)
class LLMRuntime:
    """Gateway bundle plus cache resources owned by one process."""

    bundle: AdapterBundle
    cache: LLMCacheCoordinator
    store: LLMCacheStore
    _cleanup_task: asyncio.Task[None] | None = None
    _purge_task: asyncio.Task[None] | None = None

    async def cleanup_once(self, settings: Settings) -> CleanupStats:
        return await self.store.cleanup(
            max_entries_per_tenant=settings.llm_cache_max_entries_per_tenant,
            max_bytes_per_tenant=settings.llm_cache_max_bytes_per_tenant,
            batch_size=settings.llm_cache_cleanup_batch_size,
            max_hot_ttl_seconds=settings.llm_redis_cache_ttl_seconds,
        )

    async def aclose(self) -> None:
        tasks = (self._cleanup_task, self._purge_task)
        self._cleanup_task = None
        self._purge_task = None
        for task in tasks:
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        seen: set[int] = set()
        for adapter in (self.bundle.strong_llm, self.bundle.weak_llm):
            if id(adapter) in seen:
                continue
            seen.add(id(adapter))
            close = getattr(adapter, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception:
                    logger.warning("LLM adapter close failed", exc_info=True)
        await self.cache.aclose()


async def build_llm_runtime(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    bundle: AdapterBundle,
) -> LLMRuntime:
    """Build and start hot cache, durable store, and strong/weak gateways."""

    crypto = LLMCacheCrypto(
        Path(settings.master_key_path),
        max_plaintext_bytes=settings.llm_cache_max_payload_bytes,
    )
    crypto.validate_master_key()

    hot: HotCache
    if not settings.enable_llm_hot_cache:
        hot = _DisabledHotCache()
    else:
        local = LocalHotCache(
            max_entries=settings.llm_local_cache_max_entries,
            max_bytes=settings.llm_local_cache_max_bytes,
            max_item_bytes=settings.llm_hot_cache_max_item_bytes,
            max_ttl_seconds=settings.llm_local_cache_ttl_seconds,
        )
        primary: RedisHotCache | None = None
        if settings.llm_hot_cache_backend != "local" and settings.redis_url is not None:
            primary = RedisHotCache(
                settings.redis_url.get_secret_value(),
                crypto=crypto,
                max_item_bytes=settings.llm_hot_cache_max_item_bytes,
                max_ttl_seconds=settings.llm_redis_cache_ttl_seconds,
                deployment_id=settings.deployment_id,
            )
        hot = FailoverHotCache(
            primary,
            local,
            failure_threshold=settings.llm_redis_failure_threshold,
            circuit_seconds=settings.llm_redis_circuit_seconds,
            recovery_successes=settings.llm_redis_recovery_successes,
            probe_interval_seconds=settings.llm_redis_probe_seconds,
        )
    store = LLMCacheStore(
        session_factory,
        crypto,
        lease_seconds=settings.llm_cache_lease_seconds,
    )
    persistent_cache_enabled = (
        settings.enable_llm_exact_cache and settings.enable_llm_persistent_cache
    )
    hot_only_cache_enabled = (
        settings.enable_llm_exact_cache
        and settings.enable_llm_hot_cache
        and not settings.enable_llm_persistent_cache
    )
    if settings.enable_llm_semantic_cache and not persistent_cache_enabled:
        logger.warning(
            "Semantic LLM cache requested while exact persistent caching is disabled; "
            "semantic reuse remains disabled"
        )
    coordinator = LLMCacheCoordinator(
        hot,
        store,
        embed_adapter=bundle.embed,
        semantic_enabled=settings.enable_llm_semantic_cache and persistent_cache_enabled,
    )
    await coordinator.start()
    execution_cache: LLMCache | None
    if persistent_cache_enabled:
        execution_cache = coordinator
    elif hot_only_cache_enabled:
        execution_cache = HotOnlyLLMCache(hot)
    else:
        execution_cache = None
    strong_price_snapshot = _price_snapshot(settings, tier="strong")
    weak_price_snapshot = _price_snapshot(settings, tier="weak")

    strong = LLMGateway(
        bundle.strong_llm,
        cache=execution_cache,
        model_tier="strong",
        max_retries=2,
        max_concurrency=settings.llm_strong_concurrency,
        observer=LLMCallObserver(session_factory, model_tier="strong"),
        recipe_migration_mode=settings.llm_recipe_migration_mode_resolved,
        price_snapshot=strong_price_snapshot,
    )
    weak = LLMGateway(
        bundle.weak_llm,
        cache=execution_cache,
        model_tier="weak",
        max_retries=2,
        max_concurrency=settings.llm_weak_concurrency,
        observer=LLMCallObserver(session_factory, model_tier="weak"),
        recipe_migration_mode=settings.llm_recipe_migration_mode_resolved,
        price_snapshot=weak_price_snapshot,
    )
    wrapped = replace(bundle, strong_llm=strong, weak_llm=weak)
    runtime = LLMRuntime(bundle=wrapped, cache=coordinator, store=store)
    runtime._cleanup_task = asyncio.create_task(
        _cleanup_loop(runtime, settings),
        name="llm-cache-cleanup",
    )
    try:
        await coordinator.drain_pending_purges(
            limit=settings.llm_cache_cleanup_batch_size,
        )
    except Exception:
        logger.warning("Initial LLM hot-cache purge drain failed", exc_info=True)
    runtime._purge_task = asyncio.create_task(
        _purge_loop(runtime, settings),
        name="llm-cache-privacy-purge",
    )
    return runtime


def _price_snapshot(
    settings: Settings,
    *,
    tier: str,
) -> LLMPriceSnapshot | None:
    """Materialize one immutable startup snapshot from validated settings."""

    if not settings.llm_price_version:
        return None
    prefix = f"llm_{tier}"
    input_rate = getattr(
        settings,
        f"{prefix}_input_microunits_per_million_tokens",
    )
    output_rate = getattr(
        settings,
        f"{prefix}_output_microunits_per_million_tokens",
    )
    cached_rate = getattr(
        settings,
        f"{prefix}_cached_prefill_microunits_per_million_tokens",
    )
    if not all(
        isinstance(rate, int) and not isinstance(rate, bool)
        for rate in (
            input_rate,
            output_rate,
            cached_rate,
        )
    ):
        raise RuntimeError("validated LLM price snapshot is incomplete")
    return LLMPriceSnapshot(
        version=settings.llm_price_version,
        input_microunits_per_million_tokens=input_rate,
        output_microunits_per_million_tokens=output_rate,
        cached_prefill_microunits_per_million_tokens=cached_rate,
    )


async def _cleanup_loop(runtime: LLMRuntime, settings: Settings) -> None:
    while True:
        await asyncio.sleep(settings.llm_cache_cleanup_interval_seconds)
        try:
            stats = await runtime.cleanup_once(settings)
            if stats.expired_deleted or stats.budget_deleted or stats.metadata_deleted:
                LLM_CACHE_EVICTIONS.labels("mysql").inc(
                    stats.expired_deleted + stats.budget_deleted
                )
                LLM_CACHE_EVICTIONS.labels("mysql_metadata").inc(stats.metadata_deleted)
                logger.info(
                    "LLM cache cleanup expired=%d budget=%d metadata=%d bytes=%d",
                    stats.expired_deleted,
                    stats.budget_deleted,
                    stats.metadata_deleted,
                    stats.bytes_reclaimed,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Cache maintenance must never take down request serving.
            logger.warning("LLM cache cleanup failed", exc_info=True)


async def _purge_loop(runtime: LLMRuntime, settings: Settings) -> None:
    interval = min(max(settings.llm_redis_probe_seconds, 1.0), 30.0)
    while True:
        await asyncio.sleep(interval)
        try:
            await runtime.cache.drain_pending_purges(
                limit=settings.llm_cache_cleanup_batch_size,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("LLM hot-cache purge drain failed", exc_info=True)


__all__ = ["LLMRuntime", "build_llm_runtime"]
