"""MySQL integration coverage for cross-process LLM-cache leasing."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from urllib.parse import quote_plus

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from audio_graphy.adapters.protocols import LLMResponse
from audio_graphy.core.llm_cache_crypto import LLMCacheCrypto
from audio_graphy.models.llm_cache import (
    LLMCacheEntry,
    LLMCachePurge,
    LLMCacheRef,
    LLMCacheSourceGuard,
)
from audio_graphy.services.llm_cache_coordinator import LLMCacheCoordinator
from audio_graphy.services.llm_gateway import LLMGateway, LLMRequest
from audio_graphy.storage.llm_cache_store import CacheReference, LLMCacheStore
from audio_graphy.storage.llm_hot_cache import CacheIdentity, LocalHotCache


@pytest_asyncio.fixture
async def mysql_cache_factories(
    tmp_path: Path,
) -> AsyncIterator[
    tuple[
        async_sessionmaker[AsyncSession],
        async_sessionmaker[AsyncSession],
        LLMCacheCrypto,
    ]
]:
    """Create two independent pools against one isolated MySQL database."""

    host = os.environ.get("MODEL_TEST_MYSQL_HOST", "127.0.0.1")
    port = os.environ.get("MODEL_TEST_MYSQL_PORT", "3307")
    user = quote_plus(os.environ.get("MODEL_TEST_MYSQL_USER", "audiography"))
    password = quote_plus(os.environ.get("MODEL_TEST_MYSQL_PASSWORD", "change-me"))
    database = f"llm_cache_store_test_{os.getpid()}"
    server_url = f"mysql+aiomysql://{user}:{password}@{host}:{port}/"
    database_url = f"{server_url}{database}"
    admin = create_async_engine(server_url)
    engines: list[AsyncEngine] = []
    database_created = False
    try:
        try:
            async with admin.begin() as connection:
                await connection.execute(text(f"DROP DATABASE IF EXISTS `{database}`"))
                await connection.execute(text(f"CREATE DATABASE `{database}`"))
                database_created = True
        except Exception as exc:
            pytest.skip(f"MySQL integration database is unavailable: {exc}")

        engines = [
            create_async_engine(database_url, pool_size=5, max_overflow=20),
            create_async_engine(database_url, pool_size=5, max_overflow=20),
        ]
        async with engines[0].begin() as connection:
            await connection.run_sync(LLMCacheEntry.__table__.create)
            await connection.run_sync(LLMCacheRef.__table__.create)
            await connection.run_sync(LLMCacheSourceGuard.__table__.create)
            await connection.run_sync(LLMCachePurge.__table__.create)

        key_path = tmp_path / "master.key"
        key_path.write_bytes(Fernet.generate_key())
        crypto = LLMCacheCrypto(key_path)
        yield (
            async_sessionmaker(
                engines[0],
                class_=AsyncSession,
                expire_on_commit=False,
            ),
            async_sessionmaker(
                engines[1],
                class_=AsyncSession,
                expire_on_commit=False,
            ),
            crypto,
        )
    finally:
        for engine in engines:
            await engine.dispose()
        if database_created:
            async with admin.begin() as connection:
                await connection.execute(text(f"DROP DATABASE IF EXISTS `{database}`"))
        await admin.dispose()


@pytest.mark.integration
async def test_two_mysql_pools_and_fifty_claims_have_one_leader(
    mysql_cache_factories: tuple[
        async_sessionmaker[AsyncSession],
        async_sessionmaker[AsyncSession],
        LLMCacheCrypto,
    ],
) -> None:
    factory_a, factory_b, crypto = mysql_cache_factories
    stores = [
        LLMCacheStore(factory_a, crypto),
        LLMCacheStore(factory_b, crypto),
    ]
    identity = CacheIdentity("tenant-a", "tag_extract", "a" * 64)

    claims = await asyncio.gather(
        *(
            stores[index % 2].claim(
                identity,
                model="weak-v1",
                model_epoch="epoch-1",
                ttl_seconds=60,
            )
            for index in range(50)
        )
    )
    leaders = [claim for claim in claims if claim.state == "leader"]

    assert len(leaders) == 1
    assert sum(claim.state == "follower" for claim in claims) == 49
    assert await stores[0].publish(
        identity,
        lease_token=leaders[0].lease_token or "",
        payload=b'{"validated":true}',
        usage={"prompt_tokens": 7, "completion_tokens": 2},
        ttl_seconds=60,
    )

    restarted_process = LLMCacheStore(factory_b, crypto)
    ready = await restarted_process.get_ready(identity)
    assert ready is not None
    assert ready.payload == b'{"validated":true}'


@pytest.mark.integration
async def test_two_gateways_share_mysql_singleflight_and_restart_without_provider(
    mysql_cache_factories: tuple[
        async_sessionmaker[AsyncSession],
        async_sessionmaker[AsyncSession],
        LLMCacheCrypto,
    ],
) -> None:
    factory_a, factory_b, crypto = mysql_cache_factories

    class _SharedProvider:
        model = "shared-provider-v1"
        provider = "test"
        model_epoch = "epoch-1"
        calls = 0

        async def complete(
            self,
            messages: Sequence[dict[str, str]],
            *,
            temperature: float = 0.0,
            max_tokens: int | None = None,
            cache_key: str | None = None,
        ) -> LLMResponse:
            del messages, temperature, max_tokens, cache_key
            type(self).calls += 1
            await asyncio.sleep(0.05)
            return LLMResponse(
                text='{"ok":true}',
                model=self.model,
                prompt_hash="transport",
                usage={"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
            )

    def _coordinator(
        factory: async_sessionmaker[AsyncSession],
    ) -> LLMCacheCoordinator:
        return LLMCacheCoordinator(
            LocalHotCache(
                max_entries=8,
                max_bytes=64 * 1024,
                max_item_bytes=8 * 1024,
            ),
            LLMCacheStore(factory, crypto, lease_seconds=5),
            lease_poll_seconds=0.01,
        )

    request = LLMRequest(
        tenant_id="tenant-a",
        purpose="entity_extract",
        model_tier="strong",
        provider="test",
        model_epoch="epoch-1",
        messages=({"role": "user", "content": "same logical work"},),
        prompt_version="v1",
        schema_version="v1",
        parser_version="v1",
        business_snapshot={"chunk_sha256": "b" * 64},
        ttl_seconds=60,
        response_validator=lambda response: response.text == '{"ok":true}',
    )
    coordinators = [_coordinator(factory_a), _coordinator(factory_b)]
    await asyncio.gather(*(coordinator.start() for coordinator in coordinators))
    gateways = [
        LLMGateway(_SharedProvider(), cache=coordinator, retry_base_seconds=0)
        for coordinator in coordinators
    ]

    try:
        responses = await asyncio.gather(
            *(gateways[index % 2].execute(request) for index in range(50))
        )
        assert _SharedProvider.calls == 1
        assert sum(response.provider_called for response in responses) == 1
        assert all(response.text == '{"ok":true}' for response in responses)
    finally:
        await asyncio.gather(*(coordinator.aclose() for coordinator in coordinators))

    restarted = _coordinator(factory_b)
    await restarted.start()
    try:
        response = await LLMGateway(
            _SharedProvider(),
            cache=restarted,
            retry_base_seconds=0,
        ).execute(request)
        assert response.cache_source == "mysql"
        assert response.provider_called is False
        assert _SharedProvider.calls == 1
    finally:
        await restarted.aclose()


@pytest.mark.integration
async def test_mysql_source_tombstone_rejects_stale_and_future_leaders(
    mysql_cache_factories: tuple[
        async_sessionmaker[AsyncSession],
        async_sessionmaker[AsyncSession],
        LLMCacheCrypto,
    ],
) -> None:
    factory_a, factory_b, crypto = mysql_cache_factories
    leader_store = LLMCacheStore(factory_a, crypto)
    eraser_store = LLMCacheStore(factory_b, crypto)
    identity = CacheIdentity("tenant-a", "entity_extract", "c" * 64)
    reference = CacheReference("recording", "recording-1")

    claim = await leader_store.claim(
        identity,
        model="strong-v1",
        model_epoch="epoch-1",
        ttl_seconds=60,
        references=(reference,),
    )
    assert claim.state == "leader"
    assert claim.lease_token is not None

    erased = await eraser_store.delete_by_provenance(
        "tenant-a",
        (reference,),
    )
    assert erased == [identity]
    assert not await leader_store.publish(
        identity,
        lease_token=claim.lease_token,
        payload=b'{"stale":true}',
        usage={},
        ttl_seconds=60,
    )
    blocked = await leader_store.claim(
        identity,
        model="strong-v1",
        model_epoch="epoch-1",
        ttl_seconds=60,
        references=(reference,),
    )
    assert blocked.state == "blocked"
    assert await eraser_store.list_pending_purges() == [identity]


@pytest.mark.integration
async def test_mysql_cleanup_removes_only_orphan_active_source_guards(
    mysql_cache_factories: tuple[
        async_sessionmaker[AsyncSession],
        async_sessionmaker[AsyncSession],
        LLMCacheCrypto,
    ],
) -> None:
    factory_a, _factory_b, crypto = mysql_cache_factories
    store = LLMCacheStore(factory_a, crypto)
    identity = CacheIdentity("tenant-a", "entity_extract", "d" * 64)
    reference = CacheReference("recording", "orphan-recording")
    claim = await store.claim(
        identity,
        model="strong-v1",
        model_epoch="epoch-1",
        ttl_seconds=60,
        references=(reference,),
    )
    assert claim.state == "leader"
    assert await store.release(
        identity,
        lease_token=claim.lease_token or "",
    )

    stats = await store.cleanup(batch_size=10)

    assert stats.metadata_deleted == 1
