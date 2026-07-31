"""SQLite-portable contract tests for the persistent LLM cache store."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from audio_graphy.core.llm_cache_crypto import LLMCacheCrypto
from audio_graphy.models.base import Base
from audio_graphy.models.llm_cache import (
    LLMCacheEntry,
    LLMCachePurge,
    LLMCacheRef,
    LLMCacheSourceGuard,
)
from audio_graphy.storage.llm_cache_store import (
    CacheReference,
    LLMCacheStore,
)
from audio_graphy.storage.llm_hot_cache import CacheIdentity


@pytest_asyncio.fixture
async def cache_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cache.sqlite3'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def cache_crypto(tmp_path: Path) -> LLMCacheCrypto:
    key_path = tmp_path / "master.key"
    key_path.write_bytes(Fernet.generate_key())
    return LLMCacheCrypto(key_path, max_plaintext_bytes=2 * 1024 * 1024)


def _identity(
    character: str = "a",
    tenant_id: str = "tenant-a",
    namespace: str = "tag_extract",
) -> CacheIdentity:
    return CacheIdentity(tenant_id, namespace, character * 64)


def _ref(source_id: str = "42") -> CacheReference:
    return CacheReference("recording", source_id)


async def test_claim_publish_get_ready_roundtrip_and_refs(
    cache_factory: async_sessionmaker[AsyncSession],
    cache_crypto: LLMCacheCrypto,
) -> None:
    store = LLMCacheStore(cache_factory, cache_crypto)

    claim = await store.claim(
        _identity(),
        model="weak-v1",
        model_epoch="epoch-1",
        ttl_seconds=60,
        references=[_ref()],
    )
    follower = await store.claim(
        _identity(),
        model="weak-v1",
        model_epoch="epoch-1",
        ttl_seconds=60,
        references=[_ref()],
    )

    assert claim.state == "leader"
    assert claim.lease_token
    assert follower.state == "follower"
    assert await store.publish(
        _identity(),
        lease_token=claim.lease_token,
        payload=b'{"text":"validated"}',
        usage={"prompt_tokens": 10, "completion_tokens": 2},
        ttl_seconds=60,
    )

    ready = await store.get_ready(_identity())
    assert ready is not None
    assert ready.payload == b'{"text":"validated"}'
    assert ready.usage == {"prompt_tokens": 10, "completion_tokens": 2}
    assert ready.has_provenance
    assert await store.contains_ready(_identity())
    async with cache_factory() as session:
        assert len((await session.execute(select(LLMCacheRef))).scalars().all()) == 1


async def test_expired_lease_is_reclaimed_and_stale_leader_cannot_publish(
    cache_factory: async_sessionmaker[AsyncSession],
    cache_crypto: LLMCacheCrypto,
) -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    store = LLMCacheStore(
        cache_factory,
        cache_crypto,
        lease_seconds=5,
        clock=lambda: now,
    )
    first = await store.claim(
        _identity(),
        model="weak-v1",
        model_epoch="epoch-1",
        ttl_seconds=60,
    )
    now += timedelta(seconds=6)
    second = await store.claim(
        _identity(),
        model="weak-v1",
        model_epoch="epoch-1",
        ttl_seconds=60,
    )

    assert first.state == second.state == "leader"
    assert first.reclaimed is False
    assert second.reclaimed is True
    assert first.lease_token != second.lease_token
    assert not await store.publish(
        _identity(),
        lease_token=first.lease_token or "",
        payload=b"stale",
        usage={},
        ttl_seconds=60,
    )
    assert await store.publish(
        _identity(),
        lease_token=second.lease_token or "",
        payload=b"fresh",
        usage={},
        ttl_seconds=60,
    )
    assert (await store.get_ready(_identity())).payload == b"fresh"  # type: ignore[union-attr]


async def test_renewed_lease_survives_original_expiry_and_cleanup(
    cache_factory: async_sessionmaker[AsyncSession],
    cache_crypto: LLMCacheCrypto,
) -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    store = LLMCacheStore(
        cache_factory,
        cache_crypto,
        lease_seconds=5,
        clock=lambda: now,
    )
    claim = await store.claim(
        _identity(),
        model="weak-v1",
        model_epoch="epoch-1",
        ttl_seconds=60,
    )
    now += timedelta(seconds=4)
    assert await store.renew(
        _identity(),
        lease_token=claim.lease_token or "",
    )

    now += timedelta(seconds=2)
    stats = await store.cleanup(batch_size=10)
    follower = await store.claim(
        _identity(),
        model="weak-v1",
        model_epoch="epoch-1",
        ttl_seconds=60,
    )

    assert stats.expired_deleted == 0
    assert follower.state == "follower"


async def test_fifty_concurrent_claims_across_store_instances_have_one_leader(
    cache_factory: async_sessionmaker[AsyncSession],
    cache_crypto: LLMCacheCrypto,
) -> None:
    stores = [
        LLMCacheStore(cache_factory, cache_crypto),
        LLMCacheStore(cache_factory, cache_crypto),
    ]

    claims = await asyncio.gather(
        *(
            stores[index % 2].claim(
                _identity(),
                model="weak-v1",
                model_epoch="epoch-1",
                ttl_seconds=60,
            )
            for index in range(50)
        )
    )

    assert sum(claim.state == "leader" for claim in claims) == 1
    assert sum(claim.state == "follower" for claim in claims) == 49


async def test_delete_by_provenance_removes_entry_and_blocks_inflight_publish(
    cache_factory: async_sessionmaker[AsyncSession],
    cache_crypto: LLMCacheCrypto,
) -> None:
    store = LLMCacheStore(cache_factory, cache_crypto)
    claim = await store.claim(
        _identity(),
        model="weak-v1",
        model_epoch="epoch-1",
        ttl_seconds=60,
        references=[_ref()],
    )

    deleted = await store.delete_by_provenance("tenant-a", [_ref()])

    assert deleted == [_identity()]
    assert not await store.publish(
        _identity(),
        lease_token=claim.lease_token or "",
        payload=b"must-not-return",
        usage={},
        ttl_seconds=60,
    )
    assert not await store.contains_ready(_identity())
    assert (
        await store.claim(
            _identity(),
            model="weak-v1",
            model_epoch="epoch-1",
            ttl_seconds=60,
            references=[_ref()],
        )
    ).state == "blocked"
    assert await store.list_pending_purges() == [_identity()]

    assert await store.acknowledge_purges([_identity()]) == 1
    assert await store.list_pending_purges() == []
    async with cache_factory() as session:
        guard = (await session.execute(select(LLMCacheSourceGuard))).scalar_one()
        assert guard.state == "erased"
        assert guard.erased_at is not None
        assert (await session.execute(select(LLMCachePurge))).scalar_one_or_none() is None


async def test_erasing_absent_source_still_blocks_later_cache_claim(
    cache_factory: async_sessionmaker[AsyncSession],
    cache_crypto: LLMCacheCrypto,
) -> None:
    store = LLMCacheStore(cache_factory, cache_crypto)

    assert await store.delete_by_provenance("tenant-a", [_ref("not-yet-cached")]) == []
    claim = await store.claim(
        _identity(),
        model="weak-v1",
        model_epoch="epoch-1",
        ttl_seconds=60,
        references=[_ref("not-yet-cached")],
    )

    assert claim.state == "blocked"


async def test_delete_removes_exact_entry_and_refs_and_is_idempotent(
    cache_factory: async_sessionmaker[AsyncSession],
    cache_crypto: LLMCacheCrypto,
) -> None:
    store = LLMCacheStore(cache_factory, cache_crypto)
    claim = await store.claim(
        _identity(),
        model="weak-v1",
        model_epoch="epoch-1",
        ttl_seconds=60,
        references=[_ref()],
    )
    assert await store.publish(
        _identity(),
        lease_token=claim.lease_token or "",
        payload=b"invalid-after-validation",
        usage={},
        ttl_seconds=60,
    )

    assert await store.delete(_identity())
    assert not await store.delete(_identity())
    assert not await store.contains_ready(_identity())
    async with cache_factory() as session:
        assert (await session.execute(select(LLMCacheEntry))).scalars().all() == []
        assert (await session.execute(select(LLMCacheRef))).scalars().all() == []

    replacement = await store.claim(
        _identity(),
        model="weak-v1",
        model_epoch="epoch-1",
        ttl_seconds=60,
    )
    assert replacement.state == "leader"


async def test_corrupt_ciphertext_is_deleted_and_treated_as_miss(
    cache_factory: async_sessionmaker[AsyncSession],
    cache_crypto: LLMCacheCrypto,
) -> None:
    store = LLMCacheStore(cache_factory, cache_crypto)
    claim = await store.claim(
        _identity(),
        model="weak-v1",
        model_epoch="epoch-1",
        ttl_seconds=60,
    )
    assert await store.publish(
        _identity(),
        lease_token=claim.lease_token or "",
        payload=b"valid",
        usage={},
        ttl_seconds=60,
    )
    async with cache_factory() as session:
        await session.execute(
            update(LLMCacheEntry)
            .where(LLMCacheEntry.recipe_sha256 == "a" * 64)
            .values(payload_encrypted=b"tampered")
        )
        await session.commit()

    assert await store.get_ready(_identity()) is None
    async with cache_factory() as session:
        assert (await session.execute(select(LLMCacheEntry))).scalar_one_or_none() is None


async def test_ready_hit_can_attach_new_provenance(
    cache_factory: async_sessionmaker[AsyncSession],
    cache_crypto: LLMCacheCrypto,
) -> None:
    store = LLMCacheStore(cache_factory, cache_crypto)
    claim = await store.claim(
        _identity(),
        model="weak-v1",
        model_epoch="epoch-1",
        ttl_seconds=60,
    )
    await store.publish(
        _identity(),
        lease_token=claim.lease_token or "",
        payload=b"shared",
        usage={},
        ttl_seconds=60,
    )

    ready = await store.get_ready(_identity(), references=[_ref("99")])

    assert ready is not None and ready.has_provenance
    async with cache_factory() as session:
        ref = (await session.execute(select(LLMCacheRef))).scalar_one()
        assert ref.source_id == "99"


async def test_release_allows_a_new_leader(
    cache_factory: async_sessionmaker[AsyncSession],
    cache_crypto: LLMCacheCrypto,
) -> None:
    store = LLMCacheStore(cache_factory, cache_crypto)
    first = await store.claim(
        _identity(),
        model="weak-v1",
        model_epoch="epoch-1",
        ttl_seconds=60,
    )

    assert await store.release(_identity(), lease_token=first.lease_token or "")
    second = await store.claim(
        _identity(),
        model="weak-v1",
        model_epoch="epoch-1",
        ttl_seconds=60,
    )
    assert second.state == "leader"


async def test_budget_cleanup_does_not_evict_an_active_lease(
    cache_factory: async_sessionmaker[AsyncSession],
    cache_crypto: LLMCacheCrypto,
) -> None:
    store = LLMCacheStore(cache_factory, cache_crypto)
    claim = await store.claim(
        _identity(),
        model="weak-v1",
        model_epoch="epoch-1",
        ttl_seconds=60,
    )

    stats = await store.cleanup(
        max_entries_per_tenant=1,
        max_bytes_per_tenant=1,
        batch_size=10,
    )

    assert stats.budget_deleted == 0
    assert not await store.publish(
        _identity(),
        lease_token="0" * 64,
        payload=b"stale",
        usage={},
        ttl_seconds=60,
    )
    assert await store.publish(
        _identity(),
        lease_token=claim.lease_token or "",
        payload=b"ready",
        usage={},
        ttl_seconds=60,
    )


async def test_semantic_candidates_are_strictly_scoped_and_bounded(
    cache_factory: async_sessionmaker[AsyncSession],
    cache_crypto: LLMCacheCrypto,
) -> None:
    store = LLMCacheStore(cache_factory, cache_crypto)
    for index, guard in enumerate(("2" * 64, "2" * 64, "3" * 64)):
        identity = _identity(
            chr(ord("a") + index),
            namespace="keyword_extract",
        )
        claim = await store.claim(
            identity,
            model="weak-v1",
            model_epoch="epoch-1",
            ttl_seconds=60,
            semantic_scope_hash="1" * 64,
            semantic_guard_hash=guard,
            semantic_embedding=b"\x00\x00\x80?" * 2,
            semantic_dim=2,
            language="zh",
        )
        await store.publish(
            identity,
            lease_token=claim.lease_token or "",
            payload=f"value-{index}".encode(),
            usage={},
            ttl_seconds=60,
        )

    candidates = await store.find_semantic_candidates(
        tenant_id="tenant-a",
        namespace="keyword_extract",
        semantic_scope_hash="1" * 64,
        semantic_guard_hash="2" * 64,
        language="zh",
        limit=1,
    )

    assert len(candidates) == 1
    assert candidates[0].semantic_dim == 2
    assert candidates[0].payload in {b"value-0", b"value-1"}


async def test_cleanup_removes_expired_and_oldest_budget_rows(
    cache_factory: async_sessionmaker[AsyncSession],
    cache_crypto: LLMCacheCrypto,
) -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    store = LLMCacheStore(cache_factory, cache_crypto, clock=lambda: now)
    for character in ("a", "b", "c"):
        identity = _identity(character)
        claim = await store.claim(
            identity,
            model="weak-v1",
            model_epoch="epoch-1",
            ttl_seconds=1 if character == "a" else 60,
        )
        await store.publish(
            identity,
            lease_token=claim.lease_token or "",
            payload=character.encode() * 64,
            usage={},
            ttl_seconds=1 if character == "a" else 60,
        )
    now += timedelta(seconds=2)

    stats = await store.cleanup(
        max_entries_per_tenant=1,
        max_bytes_per_tenant=1024 * 1024,
        batch_size=10,
    )

    assert stats.expired_deleted == 1
    assert stats.budget_deleted == 1
    async with cache_factory() as session:
        rows = (await session.execute(select(LLMCacheEntry))).scalars().all()
        assert len(rows) == 1


async def test_cleanup_counts_semantic_vector_bytes_toward_tenant_budget(
    cache_factory: async_sessionmaker[AsyncSession],
    cache_crypto: LLMCacheCrypto,
) -> None:
    store = LLMCacheStore(cache_factory, cache_crypto)
    for character in ("a", "b"):
        identity = _identity(character, namespace="keyword_extract")
        claim = await store.claim(
            identity,
            model="weak-v1",
            model_epoch="epoch-1",
            ttl_seconds=60,
            semantic_scope_hash="1" * 64,
            semantic_guard_hash="2" * 64,
            semantic_embedding=b"\x00\x00\x80?" * 256,
            semantic_dim=256,
            language="zh",
        )
        assert await store.publish(
            identity,
            lease_token=claim.lease_token or "",
            payload=b"x",
            usage={},
            ttl_seconds=60,
        )

    async with cache_factory() as session:
        rows = (await session.execute(select(LLMCacheEntry))).scalars().all()
        encrypted_payload_bytes = sum(row.payload_size_bytes for row in rows)

    stats = await store.cleanup(
        max_entries_per_tenant=10,
        max_bytes_per_tenant=encrypted_payload_bytes + 1,
        batch_size=10,
    )

    assert stats.budget_deleted >= 1
    assert stats.bytes_reclaimed > encrypted_payload_bytes


async def test_cleanup_bounds_orphan_guards_and_aged_purges(
    cache_factory: async_sessionmaker[AsyncSession],
    cache_crypto: LLMCacheCrypto,
) -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    store = LLMCacheStore(cache_factory, cache_crypto, clock=lambda: now)
    claim = await store.claim(
        _identity(),
        model="weak-v1",
        model_epoch="epoch-1",
        ttl_seconds=60,
        references=(_ref("orphan"),),
    )
    assert await store.release(
        _identity(),
        lease_token=claim.lease_token or "",
    )
    assert (
        await store.delete_by_provenance(
            "tenant-a",
            (_ref("erased"),),
        )
        == []
    )

    async with cache_factory() as session:
        session.add_all(
            (
                LLMCachePurge(
                    tenant_id="tenant-a",
                    namespace="tag_extract",
                    recipe_sha256="d" * 64,
                    created_at=now - timedelta(hours=2),
                    updated_at=now - timedelta(hours=2),
                ),
                LLMCachePurge(
                    tenant_id="tenant-a",
                    namespace="tag_extract",
                    recipe_sha256="e" * 64,
                    created_at=now - timedelta(minutes=30),
                    updated_at=now - timedelta(minutes=30),
                ),
            )
        )
        await session.commit()

    stats = await store.cleanup(
        batch_size=10,
        max_hot_ttl_seconds=3600,
    )

    assert stats.metadata_deleted == 2
    async with cache_factory() as session:
        guards = (await session.execute(select(LLMCacheSourceGuard))).scalars().all()
        purges = (await session.execute(select(LLMCachePurge))).scalars().all()
    assert [(guard.source_id, guard.state) for guard in guards] == [("erased", "erased")]
    assert [purge.recipe_sha256 for purge in purges] == ["e" * 64]


async def test_store_rejects_unbounded_reference_fanout(
    cache_factory: async_sessionmaker[AsyncSession],
    cache_crypto: LLMCacheCrypto,
) -> None:
    store = LLMCacheStore(cache_factory, cache_crypto)

    with pytest.raises(ValueError, match="references cannot exceed 64"):
        await store.claim(
            _identity(),
            model="weak-v1",
            model_epoch="epoch-1",
            ttl_seconds=60,
            references=tuple(_ref(str(index)) for index in range(65)),
        )
