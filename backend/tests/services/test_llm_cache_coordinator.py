"""Integration contracts for hot-cache + durable-cache coordination."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import update
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from audio_graphy.adapters.protocols import EmbeddingResult, LLMResponse
from audio_graphy.core.llm_cache_crypto import LLMCacheCrypto
from audio_graphy.models.base import Base
from audio_graphy.models.llm_cache import LLMCacheEntry
from audio_graphy.services.llm_cache_coordinator import LLMCacheCoordinator
from audio_graphy.services.llm_gateway import (
    CachedLLMValue,
    CachePolicy,
    LLMCacheIdentity,
    LLMGateway,
    LLMProvenance,
    LLMRequest,
)
from audio_graphy.storage.llm_cache_store import CacheReference, LLMCacheStore
from audio_graphy.storage.llm_hot_cache import CacheIdentity, LocalHotCache


@pytest_asyncio.fixture
async def cache_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'coordinator.sqlite3'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


def _crypto(tmp_path: Path) -> LLMCacheCrypto:
    path = tmp_path / "master.key"
    path.write_bytes(Fernet.generate_key())
    return LLMCacheCrypto(path, max_plaintext_bytes=2 * 1024 * 1024)


def _request() -> LLMRequest:
    return LLMRequest(
        tenant_id="tenant-a",
        purpose="entity_extract",
        model_tier="strong",
        messages=({"role": "user", "content": "extract this"},),
        model_epoch="epoch-1",
        prompt_version="prompt-v1",
        parser_version="parser-v1",
        business_snapshot={"chunk_sha256": "a" * 64},
        provenance=(
            LLMProvenance("recording", "42"),
            LLMProvenance("chunk", "7"),
        ),
        cache_policy=CachePolicy.EXACT,
        ttl_seconds=60,
    )


def _identity(request: LLMRequest) -> LLMCacheIdentity:
    return LLMCacheIdentity(
        request.tenant_id,
        request.purpose,
        request.recipe_sha256(model="strong-v1"),
    )


def _value(identity: LLMCacheIdentity) -> CachedLLMValue:
    return CachedLLMValue(
        text='{"validated":true}',
        model="strong-v1",
        prompt_hash=identity.recipe_sha256,
        usage={"prompt_tokens": 9, "completion_tokens": 3},
    )


async def test_mysql_publish_precedes_hot_fill_and_restart_hits_mysql(
    cache_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    request = _request()
    identity = _identity(request)
    store = LLMCacheStore(cache_factory, _crypto(tmp_path))
    first_hot = LocalHotCache(max_entries=8, max_bytes=4096, max_item_bytes=2048)
    first = LLMCacheCoordinator(first_hot, store, lease_poll_seconds=0.01)
    await first.start()

    claim = await first.acquire(identity, request=request, model="strong-v1")
    assert claim.state == "leader" and claim.lease_token
    assert await first.publish(
        identity,
        lease_token=claim.lease_token,
        value=_value(identity),
        request=request,
        model="strong-v1",
    )
    assert (await first.get(identity, request=request)).cache_source == "local"  # type: ignore[union-attr]
    await first.aclose()

    # A fresh process starts with an empty hot cache and must reuse MySQL.
    second_hot = LocalHotCache(max_entries=8, max_bytes=4096, max_item_bytes=2048)
    second = LLMCacheCoordinator(second_hot, store, lease_poll_seconds=0.01)
    await second.start()
    mysql_hit = await second.get(identity, request=request)
    assert mysql_hit is not None
    assert mysql_hit.text == '{"validated":true}'
    assert mysql_hit.cache_source == "mysql"
    assert (await second.get(identity, request=request)).cache_source == "local"  # type: ignore[union-attr]
    await second.aclose()


async def test_mysql_backfill_uses_only_durable_remaining_ttl(
    cache_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    request = _request()
    identity = _identity(request)
    store = LLMCacheStore(cache_factory, _crypto(tmp_path))
    initial = LLMCacheCoordinator(
        LocalHotCache(max_entries=8, max_bytes=4096, max_item_bytes=2048),
        store,
    )
    claim = await initial.acquire(identity, request=request, model="strong-v1")
    assert await initial.publish(
        identity,
        lease_token=claim.lease_token,
        value=_value(identity),
        request=request,
        model="strong-v1",
    )
    await initial.aclose()
    async with cache_factory() as session:
        await session.execute(
            update(LLMCacheEntry)
            .where(LLMCacheEntry.recipe_sha256 == identity.recipe_sha256)
            .values(expires_at=datetime.now(UTC) + timedelta(seconds=2))
        )
        await session.commit()

    hot_clock = 10.0
    hot = LocalHotCache(
        max_entries=8,
        max_bytes=4096,
        max_item_bytes=2048,
        clock=lambda: hot_clock,
    )
    restarted = LLMCacheCoordinator(hot, store)
    assert await restarted.get(identity, request=request) is not None
    hot_clock = 13.0

    assert (
        await hot.get(
            CacheIdentity(
                identity.tenant_id,
                identity.namespace,
                identity.recipe_sha256,
            )
        )
        is None
    )
    await restarted.aclose()


async def test_provenance_hot_hit_revalidates_mysql_and_dsar_clears_both_layers(
    cache_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    request = _request()
    identity = _identity(request)
    store = LLMCacheStore(cache_factory, _crypto(tmp_path))
    hot = LocalHotCache(max_entries=8, max_bytes=4096, max_item_bytes=2048)
    coordinator = LLMCacheCoordinator(hot, store, lease_poll_seconds=0.01)
    await coordinator.start()

    claim = await coordinator.acquire(identity, request=request, model="strong-v1")
    assert claim.lease_token
    assert await coordinator.publish(
        identity,
        lease_token=claim.lease_token,
        value=_value(identity),
        request=request,
        model="strong-v1",
    )

    # Simulate another process performing DSAR while this process still has a
    # provenance-bearing local/Redis value.
    deleted = await store.delete_by_provenance(
        "tenant-a",
        [CacheReference("recording", "42")],
    )
    assert len(deleted) == 1
    assert await coordinator.get(identity, request=request) is None
    blocked = await coordinator.acquire(identity, request=request, model="strong-v1")
    assert blocked.state == "bypass"
    assert await coordinator.drain_pending_purges() == 1

    # The coordinator-level path deletes persistence and every known hot key.
    second_request = replace(
        request,
        messages=({"role": "user", "content": "extract other source"},),
        business_snapshot={"chunk_sha256": "b" * 64},
        provenance=(
            LLMProvenance("recording", "43"),
            LLMProvenance("chunk", "8"),
        ),
    )
    second_identity = _identity(second_request)
    second_claim = await coordinator.acquire(
        second_identity,
        request=second_request,
        model="strong-v1",
    )
    assert second_claim.lease_token
    assert await coordinator.publish(
        second_identity,
        lease_token=second_claim.lease_token,
        value=_value(second_identity),
        request=second_request,
        model="strong-v1",
    )
    removed = await coordinator.delete_by_provenance(
        "tenant-a",
        [LLMProvenance("recording", "43")],
    )
    assert removed == 1
    assert await coordinator.get(second_identity, request=second_request) is None
    assert await store.list_pending_purges() == []
    await coordinator.aclose()


async def test_v2_shared_generation_attaches_each_new_provenance_before_hot_reuse(
    cache_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    source_request = _request()
    target_request = replace(
        source_request,
        business_snapshot={"chunk_sha256": "different"},
        provenance=(LLMProvenance("recording", "target"),),
    )
    source_identity = _identity(source_request)
    target_identity = _identity(target_request)
    assert target_identity == source_identity

    store = LLMCacheStore(cache_factory, _crypto(tmp_path))
    coordinator = LLMCacheCoordinator(
        LocalHotCache(max_entries=8, max_bytes=4096, max_item_bytes=2048),
        store,
        lease_poll_seconds=0.01,
    )
    await coordinator.start()
    claim = await coordinator.acquire(
        source_identity,
        request=source_request,
        model="strong-v1",
    )
    assert claim.lease_token
    assert await coordinator.publish(
        source_identity,
        lease_token=claim.lease_token,
        value=_value(source_identity),
        request=source_request,
        model="strong-v1",
    )

    assert await coordinator.get(target_identity, request=target_request) is not None
    assert (
        await coordinator.delete_by_provenance(
            "tenant-a",
            [LLMProvenance("recording", "target")],
        )
        == 1
    )
    assert await coordinator.get(source_identity, request=source_request) is None
    await coordinator.aclose()


async def test_failed_hot_erasure_stays_in_durable_queue_until_retry(
    cache_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    class _DeferredErasureHotCache(LocalHotCache):
        erase_available = False

        async def erase_many(self, keys: Sequence[CacheIdentity]) -> bool:
            if not self.erase_available:
                return False
            await self.delete_many(keys)
            return True

    request = _request()
    identity = _identity(request)
    hot = _DeferredErasureHotCache(
        max_entries=8,
        max_bytes=4096,
        max_item_bytes=2048,
    )
    store = LLMCacheStore(cache_factory, _crypto(tmp_path))
    coordinator = LLMCacheCoordinator(hot, store)
    claim = await coordinator.acquire(identity, request=request, model="strong-v1")
    assert await coordinator.publish(
        identity,
        lease_token=claim.lease_token,
        value=_value(identity),
        request=request,
        model="strong-v1",
    )

    assert (
        await coordinator.delete_by_provenance(
            request.tenant_id,
            [LLMProvenance("recording", "42")],
        )
        == 1
    )
    assert await store.list_pending_purges() == [
        CacheIdentity(identity.tenant_id, identity.namespace, identity.recipe_sha256)
    ]

    hot.erase_available = True
    assert await coordinator.drain_pending_purges() == 1
    assert await store.list_pending_purges() == []
    await coordinator.aclose()


async def test_two_gateway_processes_share_mysql_lease_one_provider_call(
    cache_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    class _CountingAdapter:
        model = "strong-v1"

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, **kwargs) -> LLMResponse:
            del messages, kwargs
            self.calls += 1
            # Leave enough time for the second coordinator to observe pending.
            await asyncio.sleep(0.05)
            return LLMResponse("provider", self.model, "transport")

    store = LLMCacheStore(cache_factory, _crypto(tmp_path), lease_seconds=2)
    first_cache = LLMCacheCoordinator(
        LocalHotCache(max_entries=8, max_bytes=4096, max_item_bytes=2048),
        store,
        lease_poll_seconds=0.01,
    )
    second_cache = LLMCacheCoordinator(
        LocalHotCache(max_entries=8, max_bytes=4096, max_item_bytes=2048),
        store,
        lease_poll_seconds=0.01,
    )
    await first_cache.start()
    await second_cache.start()
    adapter = _CountingAdapter()
    first_gateway = LLMGateway(adapter, cache=first_cache, retry_base_seconds=0)
    second_gateway = LLMGateway(adapter, cache=second_cache, retry_base_seconds=0)

    first, second = await asyncio.gather(
        first_gateway.execute(_request()),
        second_gateway.execute(_request()),
    )

    assert adapter.calls == 1
    assert sum(result.provider_called for result in (first, second)) == 1
    assert sum(result.cache_source == "mysql_singleflight" for result in (first, second)) == 1
    assert {first.text, second.text} == {"provider"}
    assert {first.cache_source, second.cache_source} <= {
        "provider",
        "mysql",
        "mysql_singleflight",
    }
    await first_cache.aclose()
    await second_cache.aclose()


async def test_invalid_decrypted_json_envelope_is_a_miss_and_is_deleted(
    cache_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    request = _request()
    identity = _identity(request)
    store = LLMCacheStore(cache_factory, _crypto(tmp_path))
    claim = await store.claim(
        CacheIdentity(
            identity.tenant_id,
            identity.namespace,
            identity.recipe_sha256,
        ),
        model="strong-v1",
        model_epoch=request.model_epoch,
        ttl_seconds=60,
    )
    assert claim.lease_token
    assert await store.publish(
        CacheIdentity(
            identity.tenant_id,
            identity.namespace,
            identity.recipe_sha256,
        ),
        lease_token=claim.lease_token,
        payload=b'{"version":1,"text":',
        usage={},
        ttl_seconds=60,
    )
    coordinator = LLMCacheCoordinator(
        LocalHotCache(max_entries=8, max_bytes=4096, max_item_bytes=2048),
        store,
    )

    assert await coordinator.get(identity, request=request) is None
    assert (
        await store.get_ready(
            CacheIdentity(
                identity.tenant_id,
                identity.namespace,
                identity.recipe_sha256,
            )
        )
        is None
    )


async def test_mysql_semantic_candidate_round_trip_and_new_reference(
    cache_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    class _SameEmbedding:
        model = "embed-v1"
        dim = 2

        async def embed_texts(
            self,
            texts: Sequence[str],
        ) -> Sequence[EmbeddingResult]:
            return [
                EmbeddingResult(vector=(1.0, 0.0), dim=self.dim, model=self.model) for _ in texts
            ]

    source = replace(
        _request(),
        purpose="keyword_extract",
        messages=({"role": "user", "content": "如何申请退款"},),
        business_snapshot={"query_sha256": "1" * 64},
        provenance=(LLMProvenance("query", "source"),),
        cache_policy=CachePolicy.QUERY_SEMANTIC,
        semantic_text="如何申请退款",
        semantic_language="zh-CN",
    )
    target = replace(
        source,
        messages=({"role": "user", "content": "怎么办理退货退款"},),
        business_snapshot={"query_sha256": "2" * 64},
        provenance=(LLMProvenance("query", "target"),),
        semantic_text="怎么办理退货退款",
    )
    store = LLMCacheStore(cache_factory, _crypto(tmp_path))
    coordinator = LLMCacheCoordinator(
        LocalHotCache(max_entries=8, max_bytes=4096, max_item_bytes=2048),
        store,
        embed_adapter=_SameEmbedding(),
        semantic_enabled=True,
    )
    source_identity = _identity(source)
    source_claim = await coordinator.acquire(
        source_identity,
        request=source,
        model="strong-v1",
    )
    assert source_claim.state == "leader"
    assert await coordinator.publish(
        source_identity,
        lease_token=source_claim.lease_token,
        value=_value(source_identity),
        request=source,
        model="strong-v1",
    )

    target_claim = await coordinator.acquire(
        _identity(target),
        request=target,
        model="strong-v1",
    )

    assert target_claim.state == "ready"
    assert target_claim.value is not None
    assert target_claim.value.cache_source == "mysql_semantic"
    deleted = await store.delete_by_provenance(
        source.tenant_id,
        [CacheReference("query", "target")],
    )
    assert deleted == [
        CacheIdentity(
            source_identity.tenant_id,
            source_identity.namespace,
            source_identity.recipe_sha256,
        )
    ]
