"""MySQL-backed exact LLM cache with portable lease CAS and provenance."""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from sqlalchemy import CursorResult, and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.core.llm_cache_crypto import (
    EncryptedCachePayload,
    LLMCacheCrypto,
)
from audio_graphy.models.llm_cache import (
    LLMCacheEntry,
    LLMCachePurge,
    LLMCacheRef,
    LLMCacheSourceGuard,
)
from audio_graphy.storage.llm_hot_cache import CacheIdentity

logger = logging.getLogger(__name__)

_SOURCE_TYPE_RE = re.compile(r"^[a-z0-9_.-]{1,32}$")
_SEMANTIC_NAMESPACES = frozenset({"keyword_extract", "query_rewrite"})


@dataclass(frozen=True, slots=True)
class CacheReference:
    """A polymorphic source reference used for privacy invalidation."""

    source_type: str
    source_id: str

    def __post_init__(self) -> None:
        if _SOURCE_TYPE_RE.fullmatch(self.source_type) is None:
            raise ValueError("source_type contains unsupported characters")
        if not isinstance(self.source_id, str) or not 1 <= len(self.source_id) <= 128:
            raise ValueError("source_id must contain 1 to 128 characters")


@dataclass(frozen=True, slots=True)
class ReadyCacheValue:
    payload: bytes
    model: str
    model_epoch: str
    usage: dict[str, int]
    expires_at: datetime
    has_provenance: bool


@dataclass(frozen=True, slots=True)
class ClaimResult:
    state: Literal["hit", "leader", "follower", "blocked"]
    value: ReadyCacheValue | None = None
    lease_token: str | None = None
    reclaimed: bool = False


@dataclass(frozen=True, slots=True)
class SemanticCacheCandidate:
    identity: CacheIdentity
    payload: bytes
    model: str
    model_epoch: str
    semantic_embedding: bytes
    semantic_dim: int
    expires_at: datetime
    has_provenance: bool


@dataclass(frozen=True, slots=True)
class CleanupStats:
    expired_deleted: int = 0
    budget_deleted: int = 0
    bytes_reclaimed: int = 0
    metadata_deleted: int = 0


class LLMCacheStore:
    """Durable exact cache and cross-process singleflight lease manager."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        crypto: LLMCacheCrypto,
        *,
        lease_seconds: float = 120.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._factory = session_factory
        self._crypto = crypto
        self._lease_seconds = lease_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def lease_seconds(self) -> float:
        """Configured lease lifetime used to schedule leader heartbeats."""

        return self._lease_seconds

    async def get_ready(
        self,
        identity: CacheIdentity,
        *,
        references: Sequence[CacheReference] = (),
    ) -> ReadyCacheValue | None:
        """Read one unexpired ready result and attach any new provenance."""

        normalized_refs = self._normalize_references(references)
        now = self._now()
        async with self._factory() as session:
            if normalized_refs and not await self._lock_source_guards(
                session,
                identity.tenant_id,
                normalized_refs,
            ):
                await session.rollback()
                return None
            row = await self._select_entry(session, identity)
            if not self._is_ready(row, now):
                return None
            assert row is not None
            if normalized_refs:
                await self._attach_references(session, row, normalized_refs)
                row.has_provenance = True
            row.hit_count += 1
            row.last_accessed_at = now
            await session.commit()
            ready = await self._decode_ready(row, identity)
        if ready is None:
            await self._delete_entry(row.id)
        return ready

    async def claim(
        self,
        identity: CacheIdentity,
        *,
        model: str,
        model_epoch: str,
        ttl_seconds: int,
        references: Sequence[CacheReference] = (),
        semantic_scope_hash: str | None = None,
        semantic_guard_hash: str | None = None,
        semantic_embedding: bytes | None = None,
        semantic_dim: int | None = None,
        language: str | None = None,
        _attempt: int = 0,
    ) -> ClaimResult:
        """Return a hit, become leader, or follow an active cross-process lease."""

        if _attempt >= 5:
            raise RuntimeError("LLM cache claim did not converge")
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        self._validate_model(model, model_epoch)
        self._validate_semantic(
            identity,
            semantic_scope_hash=semantic_scope_hash,
            semantic_guard_hash=semantic_guard_hash,
            semantic_embedding=semantic_embedding,
            semantic_dim=semantic_dim,
            language=language,
        )
        normalized_refs = self._normalize_references(references)
        now = self._now()
        lease_token = secrets.token_hex(32)
        lease_expires_at = now + timedelta(seconds=self._lease_seconds)

        inserted = LLMCacheEntry(
            tenant_id=identity.tenant_id,
            namespace=identity.namespace,
            recipe_sha256=identity.recipe_sha256,
            status="pending",
            model=model,
            model_epoch=model_epoch,
            usage={},
            payload_size_bytes=0,
            has_provenance=bool(normalized_refs),
            expires_at=None,
            last_accessed_at=now,
            hit_count=0,
            lease_token=lease_token,
            lease_expires_at=lease_expires_at,
            semantic_scope_hash=semantic_scope_hash,
            semantic_guard_hash=semantic_guard_hash,
            semantic_embedding=semantic_embedding,
            semantic_dim=semantic_dim,
            language=language,
        )
        async with self._factory() as session:
            if normalized_refs and not await self._lock_source_guards(
                session,
                identity.tenant_id,
                normalized_refs,
            ):
                await session.rollback()
                return ClaimResult("blocked")
            try:
                session.add(inserted)
                await session.flush()
                await self._attach_references(session, inserted, normalized_refs)
                await session.commit()
                return ClaimResult("leader", lease_token=lease_token)
            except IntegrityError as exc:
                await session.rollback()
                if not self._is_unique_violation(exc):
                    raise

        corrupt_entry_id: int | None = None
        async with self._factory() as session:
            if normalized_refs and not await self._lock_source_guards(
                session,
                identity.tenant_id,
                normalized_refs,
            ):
                await session.rollback()
                return ClaimResult("blocked")
            row = await self._select_entry(session, identity)
            if row is None:
                pass
            elif self._is_ready(row, now):
                if normalized_refs:
                    await self._attach_references(session, row, normalized_refs)
                    row.has_provenance = True
                row.hit_count += 1
                row.last_accessed_at = now
                await session.commit()
                ready = await self._decode_ready(row, identity)
                if ready is not None:
                    return ClaimResult("hit", value=ready)
                corrupt_entry_id = row.id
            else:
                active_lease = (
                    row.status == "pending"
                    and row.lease_token is not None
                    and row.lease_expires_at is not None
                    and self._aware(row.lease_expires_at) > now
                )
                if active_lease:
                    if normalized_refs:
                        await self._attach_references(session, row, normalized_refs)
                        row.has_provenance = True
                    row.last_accessed_at = now
                    await session.commit()
                    return ClaimResult("follower")

                reclaim = (
                    update(LLMCacheEntry)
                    .where(
                        LLMCacheEntry.id == row.id,
                        or_(
                            and_(
                                LLMCacheEntry.status == "ready",
                                or_(
                                    LLMCacheEntry.expires_at.is_(None),
                                    LLMCacheEntry.expires_at <= now,
                                ),
                            ),
                            and_(
                                LLMCacheEntry.status == "pending",
                                or_(
                                    LLMCacheEntry.lease_expires_at.is_(None),
                                    LLMCacheEntry.lease_expires_at <= now,
                                ),
                            ),
                        ),
                    )
                    .values(
                        status="pending",
                        model=model,
                        model_epoch=model_epoch,
                        payload_encrypted=None,
                        encryption_meta=None,
                        usage={},
                        payload_size_bytes=0,
                        has_provenance=bool(row.has_provenance or normalized_refs),
                        expires_at=None,
                        last_accessed_at=now,
                        lease_token=lease_token,
                        lease_expires_at=lease_expires_at,
                        semantic_scope_hash=semantic_scope_hash,
                        semantic_guard_hash=semantic_guard_hash,
                        semantic_embedding=semantic_embedding,
                        semantic_dim=semantic_dim,
                        language=language,
                    )
                    .execution_options(synchronize_session=False)
                )
                result = await session.execute(reclaim)
                if cast(CursorResult[Any], result).rowcount == 1:
                    await self._attach_references(session, row, normalized_refs)
                    await session.commit()
                    return ClaimResult(
                        "leader",
                        lease_token=lease_token,
                        reclaimed=True,
                    )
                await session.rollback()

        if corrupt_entry_id is not None:
            await self._delete_entry(corrupt_entry_id)

        return await self.claim(
            identity,
            model=model,
            model_epoch=model_epoch,
            ttl_seconds=ttl_seconds,
            references=normalized_refs,
            semantic_scope_hash=semantic_scope_hash,
            semantic_guard_hash=semantic_guard_hash,
            semantic_embedding=semantic_embedding,
            semantic_dim=semantic_dim,
            language=language,
            _attempt=_attempt + 1,
        )

    async def publish(
        self,
        identity: CacheIdentity,
        *,
        lease_token: str,
        payload: bytes,
        usage: Mapping[str, int],
        ttl_seconds: int,
    ) -> bool:
        """Publish only if this caller still owns the active pending lease."""

        if len(lease_token) != 64:
            return False
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        normalized_usage = self._normalize_usage(usage)
        encrypted = self._crypto.encrypt(
            tenant_id=identity.tenant_id,
            namespace=identity.namespace,
            recipe_sha256=identity.recipe_sha256,
            plaintext=payload,
        )
        now = self._now()
        async with self._factory() as session:
            statement = (
                update(LLMCacheEntry)
                .where(
                    LLMCacheEntry.tenant_id == identity.tenant_id,
                    LLMCacheEntry.namespace == identity.namespace,
                    LLMCacheEntry.recipe_sha256 == identity.recipe_sha256,
                    LLMCacheEntry.status == "pending",
                    LLMCacheEntry.lease_token == lease_token,
                    LLMCacheEntry.lease_expires_at >= now,
                )
                .values(
                    status="ready",
                    payload_encrypted=encrypted.blob,
                    encryption_meta=encrypted.metadata,
                    usage=normalized_usage,
                    payload_size_bytes=len(encrypted.blob),
                    expires_at=now + timedelta(seconds=ttl_seconds),
                    last_accessed_at=now,
                    lease_token=None,
                    lease_expires_at=None,
                )
                .execution_options(synchronize_session=False)
            )
            result = await session.execute(statement)
            published = cast(CursorResult[Any], result).rowcount == 1
            await session.commit()
            return published

    async def renew(self, identity: CacheIdentity, *, lease_token: str) -> bool:
        """Extend an owned pending lease using a token-scoped CAS update."""

        if len(lease_token) != 64:
            return False
        now = self._now()
        async with self._factory() as session:
            result = await session.execute(
                update(LLMCacheEntry)
                .where(
                    LLMCacheEntry.tenant_id == identity.tenant_id,
                    LLMCacheEntry.namespace == identity.namespace,
                    LLMCacheEntry.recipe_sha256 == identity.recipe_sha256,
                    LLMCacheEntry.status == "pending",
                    LLMCacheEntry.lease_token == lease_token,
                )
                .values(
                    lease_expires_at=now + timedelta(seconds=self._lease_seconds),
                    last_accessed_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            renewed = cast(CursorResult[Any], result).rowcount == 1
            await session.commit()
            return renewed

    async def release(self, identity: CacheIdentity, *, lease_token: str) -> bool:
        """Drop a failed pending claim so the next caller can become leader."""

        async with self._factory() as session:
            entry_id = (
                await session.execute(
                    select(LLMCacheEntry.id).where(
                        LLMCacheEntry.tenant_id == identity.tenant_id,
                        LLMCacheEntry.namespace == identity.namespace,
                        LLMCacheEntry.recipe_sha256 == identity.recipe_sha256,
                        LLMCacheEntry.status == "pending",
                        LLMCacheEntry.lease_token == lease_token,
                    )
                )
            ).scalar_one_or_none()
            if entry_id is None:
                return False
            result = await session.execute(
                delete(LLMCacheEntry).where(
                    LLMCacheEntry.id == entry_id,
                    LLMCacheEntry.status == "pending",
                    LLMCacheEntry.lease_token == lease_token,
                )
            )
            released = cast(CursorResult[Any], result).rowcount == 1
            if released:
                await session.execute(
                    delete(LLMCacheRef).where(LLMCacheRef.cache_entry_id == entry_id)
                )
            await session.commit()
            return released

    async def contains_ready(self, identity: CacheIdentity) -> bool:
        """Lightweight persistent validation for provenance-bearing hot hits."""

        now = self._now()
        async with self._factory() as session:
            value = (
                await session.execute(
                    select(LLMCacheEntry.id)
                    .where(
                        LLMCacheEntry.tenant_id == identity.tenant_id,
                        LLMCacheEntry.namespace == identity.namespace,
                        LLMCacheEntry.recipe_sha256 == identity.recipe_sha256,
                        LLMCacheEntry.status == "ready",
                        LLMCacheEntry.expires_at.is_not(None),
                        LLMCacheEntry.expires_at > now,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            return value is not None

    async def delete(self, identity: CacheIdentity) -> bool:
        """Atomically delete one exact cache entry and all of its provenance."""

        async with self._factory() as session:
            entry_id = (
                await session.execute(
                    select(LLMCacheEntry.id).where(
                        LLMCacheEntry.tenant_id == identity.tenant_id,
                        LLMCacheEntry.namespace == identity.namespace,
                        LLMCacheEntry.recipe_sha256 == identity.recipe_sha256,
                    )
                )
            ).scalar_one_or_none()
            if entry_id is None:
                return False

            result = await session.execute(
                delete(LLMCacheEntry).where(
                    LLMCacheEntry.id == entry_id,
                    LLMCacheEntry.tenant_id == identity.tenant_id,
                    LLMCacheEntry.namespace == identity.namespace,
                    LLMCacheEntry.recipe_sha256 == identity.recipe_sha256,
                )
            )
            deleted = cast(CursorResult[Any], result).rowcount == 1
            if deleted:
                # MySQL removes these through ON DELETE CASCADE. Keep the explicit
                # delete for SQLite deployments where foreign keys may be disabled.
                await session.execute(
                    delete(LLMCacheRef).where(LLMCacheRef.cache_entry_id == entry_id)
                )
            await session.commit()
            return deleted

    async def delete_by_provenance(
        self,
        tenant_id: str,
        references: Sequence[CacheReference],
    ) -> list[CacheIdentity]:
        """Delete entire entries linked to any erased source reference."""

        normalized = self._normalize_references(references)
        if not normalized:
            return []
        conditions = [
            and_(
                LLMCacheRef.source_type == reference.source_type,
                LLMCacheRef.source_id == reference.source_id,
            )
            for reference in normalized
        ]
        async with self._factory() as session:
            await self._lock_source_guards(
                session,
                tenant_id,
                normalized,
                erase=True,
            )
            rows = (
                await session.execute(
                    select(
                        LLMCacheEntry.id,
                        LLMCacheEntry.namespace,
                        LLMCacheEntry.recipe_sha256,
                    )
                    .join(
                        LLMCacheRef,
                        LLMCacheRef.cache_entry_id == LLMCacheEntry.id,
                    )
                    .where(
                        LLMCacheEntry.tenant_id == tenant_id,
                        LLMCacheRef.tenant_id == tenant_id,
                        or_(*conditions),
                    )
                    .distinct()
                )
            ).all()
            identities = [
                CacheIdentity(tenant_id, str(row.namespace), str(row.recipe_sha256)) for row in rows
            ]
            entry_ids = [int(row.id) for row in rows]
            if entry_ids:
                await self._insert_purges(session, identities)
                await session.execute(delete(LLMCacheEntry).where(LLMCacheEntry.id.in_(entry_ids)))
                await session.execute(
                    delete(LLMCacheRef).where(LLMCacheRef.cache_entry_id.in_(entry_ids))
                )
            # Commit the source tombstones even when no current cache entry
            # exists, preventing an in-flight or later claim from recreating
            # data derived from the erased source.
            await session.commit()
            return identities

    async def list_pending_purges(self, *, limit: int = 500) -> list[CacheIdentity]:
        """Return durable hot-cache invalidations in bounded FIFO order."""

        if not 1 <= limit <= 5_000:
            raise ValueError("purge limit must be in [1, 5000]")
        async with self._factory() as session:
            rows = (
                await session.execute(
                    select(
                        LLMCachePurge.tenant_id,
                        LLMCachePurge.namespace,
                        LLMCachePurge.recipe_sha256,
                    )
                    .order_by(LLMCachePurge.created_at, LLMCachePurge.id)
                    .limit(limit)
                )
            ).all()
        return [
            CacheIdentity(
                str(row.tenant_id),
                str(row.namespace),
                str(row.recipe_sha256),
            )
            for row in rows
        ]

    async def acknowledge_purges(self, identities: Sequence[CacheIdentity]) -> int:
        """Remove only invalidations whose hot-cache deletion was confirmed."""

        normalized = tuple(dict.fromkeys(identities))
        if not normalized:
            return 0
        conditions = [
            and_(
                LLMCachePurge.tenant_id == identity.tenant_id,
                LLMCachePurge.namespace == identity.namespace,
                LLMCachePurge.recipe_sha256 == identity.recipe_sha256,
            )
            for identity in normalized
        ]
        async with self._factory() as session:
            result = await session.execute(delete(LLMCachePurge).where(or_(*conditions)))
            await session.commit()
            return int(cast(CursorResult[Any], result).rowcount or 0)

    async def find_semantic_candidates(
        self,
        *,
        tenant_id: str,
        namespace: str,
        semantic_scope_hash: str,
        semantic_guard_hash: str,
        language: str,
        limit: int = 256,
    ) -> list[SemanticCacheCandidate]:
        """Return recent exact values eligible for caller-side cosine comparison."""

        identity_for_validation = CacheIdentity(tenant_id, namespace, "0" * 64)
        self._validate_semantic(
            identity_for_validation,
            semantic_scope_hash=semantic_scope_hash,
            semantic_guard_hash=semantic_guard_hash,
            semantic_embedding=b"\x00\x00\x00\x00",
            semantic_dim=1,
            language=language,
        )
        if not 1 <= limit <= 256:
            raise ValueError("semantic candidate limit must be in [1, 256]")
        now = self._now()
        async with self._factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(LLMCacheEntry)
                        .where(
                            LLMCacheEntry.tenant_id == tenant_id,
                            LLMCacheEntry.namespace == namespace,
                            LLMCacheEntry.status == "ready",
                            LLMCacheEntry.expires_at.is_not(None),
                            LLMCacheEntry.expires_at > now,
                            LLMCacheEntry.semantic_scope_hash == semantic_scope_hash,
                            LLMCacheEntry.semantic_guard_hash == semantic_guard_hash,
                            LLMCacheEntry.language == language,
                            LLMCacheEntry.semantic_embedding.is_not(None),
                            LLMCacheEntry.semantic_dim.is_not(None),
                        )
                        .order_by(LLMCacheEntry.last_accessed_at.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )

        candidates: list[SemanticCacheCandidate] = []
        for row in rows:
            identity = CacheIdentity(
                str(row.tenant_id),
                str(row.namespace),
                str(row.recipe_sha256),
            )
            ready = await self._decode_ready(row, identity)
            if ready is None:
                await self._delete_entry(row.id)
                continue
            assert row.semantic_embedding is not None
            assert row.semantic_dim is not None
            candidates.append(
                SemanticCacheCandidate(
                    identity=identity,
                    payload=ready.payload,
                    model=ready.model,
                    model_epoch=ready.model_epoch,
                    semantic_embedding=bytes(row.semantic_embedding),
                    semantic_dim=int(row.semantic_dim),
                    expires_at=ready.expires_at,
                    has_provenance=ready.has_provenance,
                )
            )
        return candidates

    async def cleanup(
        self,
        *,
        max_entries_per_tenant: int = 50_000,
        max_bytes_per_tenant: int = 256 * 1024 * 1024,
        batch_size: int = 500,
        max_hot_ttl_seconds: int = 3600,
    ) -> CleanupStats:
        """Delete expired/stale rows, then enforce bounded per-tenant budgets."""

        if (
            min(
                max_entries_per_tenant,
                max_bytes_per_tenant,
                batch_size,
                max_hot_ttl_seconds,
            )
            < 1
        ):
            raise ValueError("cleanup resource limits must be positive")
        now = self._now()
        expired_deleted = 0
        budget_deleted = 0
        bytes_reclaimed = 0
        entry_storage_bytes = LLMCacheEntry.payload_size_bytes + func.coalesce(
            func.length(LLMCacheEntry.semantic_embedding), 0
        )

        async with self._factory() as session:
            expired_rows = (
                await session.execute(
                    select(
                        LLMCacheEntry.id,
                        entry_storage_bytes.label("storage_bytes"),
                    )
                    .where(
                        or_(
                            and_(
                                LLMCacheEntry.status == "ready",
                                or_(
                                    LLMCacheEntry.expires_at.is_(None),
                                    LLMCacheEntry.expires_at <= now,
                                ),
                            ),
                            and_(
                                LLMCacheEntry.status == "pending",
                                or_(
                                    LLMCacheEntry.lease_expires_at.is_(None),
                                    LLMCacheEntry.lease_expires_at <= now,
                                ),
                            ),
                        )
                    )
                    .order_by(LLMCacheEntry.updated_at)
                    .limit(batch_size)
                )
            ).all()
            candidate_ids = [int(row.id) for row in expired_rows]
            if candidate_ids:
                # Recheck expiry/status in the DELETE itself. Another process
                # may have reclaimed an expired lease after the initial
                # bounded scan; deleting by id alone would kill its new lease.
                await session.execute(
                    delete(LLMCacheEntry).where(
                        LLMCacheEntry.id.in_(candidate_ids),
                        or_(
                            and_(
                                LLMCacheEntry.status == "ready",
                                or_(
                                    LLMCacheEntry.expires_at.is_(None),
                                    LLMCacheEntry.expires_at <= now,
                                ),
                            ),
                            and_(
                                LLMCacheEntry.status == "pending",
                                or_(
                                    LLMCacheEntry.lease_expires_at.is_(None),
                                    LLMCacheEntry.lease_expires_at <= now,
                                ),
                            ),
                        ),
                    )
                )
                remaining_ids = set(
                    (
                        await session.execute(
                            select(LLMCacheEntry.id).where(LLMCacheEntry.id.in_(candidate_ids))
                        )
                    )
                    .scalars()
                    .all()
                )
                expired_ids = [
                    entry_id for entry_id in candidate_ids if entry_id not in remaining_ids
                ]
                await session.execute(
                    delete(LLMCacheRef).where(LLMCacheRef.cache_entry_id.in_(expired_ids))
                )
                expired_deleted = len(expired_ids)
                size_by_id = {int(row.id): int(row.storage_bytes or 0) for row in expired_rows}
                bytes_reclaimed += sum(size_by_id[entry_id] for entry_id in expired_ids)
                await session.commit()

        remaining = batch_size - expired_deleted
        if remaining > 0:
            async with self._factory() as session:
                tenant_stats = (
                    await session.execute(
                        select(
                            LLMCacheEntry.tenant_id,
                            func.count(LLMCacheEntry.id),
                            func.coalesce(func.sum(entry_storage_bytes), 0),
                        )
                        .where(LLMCacheEntry.status == "ready")
                        .group_by(LLMCacheEntry.tenant_id)
                    )
                ).all()
                budget_ids: list[int] = []
                budget_sizes: list[int] = []
                for tenant_id, row_count_raw, byte_count_raw in tenant_stats:
                    if len(budget_ids) >= remaining:
                        break
                    row_count = int(row_count_raw or 0)
                    byte_count = int(byte_count_raw or 0)
                    excess_rows = max(row_count - max_entries_per_tenant, 0)
                    excess_bytes = max(byte_count - max_bytes_per_tenant, 0)
                    if excess_rows == 0 and excess_bytes == 0:
                        continue
                    candidates = (
                        await session.execute(
                            select(
                                LLMCacheEntry.id,
                                entry_storage_bytes.label("storage_bytes"),
                            )
                            .where(
                                LLMCacheEntry.tenant_id == str(tenant_id),
                                LLMCacheEntry.status == "ready",
                            )
                            .order_by(
                                LLMCacheEntry.last_accessed_at,
                                LLMCacheEntry.created_at,
                            )
                            .limit(remaining - len(budget_ids))
                        )
                    ).all()
                    reclaimed_for_tenant = 0
                    for selected_for_tenant, candidate in enumerate(candidates):
                        if (
                            selected_for_tenant >= excess_rows
                            and reclaimed_for_tenant >= excess_bytes
                        ):
                            break
                        size = int(candidate.storage_bytes or 0)
                        budget_ids.append(int(candidate.id))
                        budget_sizes.append(size)
                        reclaimed_for_tenant += size

                if budget_ids:
                    await session.execute(
                        delete(LLMCacheEntry).where(LLMCacheEntry.id.in_(budget_ids))
                    )
                    await session.execute(
                        delete(LLMCacheRef).where(LLMCacheRef.cache_entry_id.in_(budget_ids))
                    )
                    await session.commit()
                    budget_deleted = len(budget_ids)
                    bytes_reclaimed += sum(budget_sizes)

        metadata_deleted = await self._cleanup_auxiliary_rows(
            now=now,
            max_hot_ttl_seconds=max_hot_ttl_seconds,
            batch_size=batch_size,
        )
        return CleanupStats(
            expired_deleted,
            budget_deleted,
            bytes_reclaimed,
            metadata_deleted,
        )

    async def _cleanup_auxiliary_rows(
        self,
        *,
        now: datetime,
        max_hot_ttl_seconds: int,
        batch_size: int,
    ) -> int:
        """Bound transient privacy metadata without deleting DSAR tombstones."""

        purge_cutoff = now - timedelta(seconds=max_hot_ttl_seconds)
        async with self._factory() as session:
            purge_ids = list(
                (
                    await session.execute(
                        select(LLMCachePurge.id)
                        .where(LLMCachePurge.created_at <= purge_cutoff)
                        .order_by(LLMCachePurge.created_at, LLMCachePurge.id)
                        .limit(batch_size)
                    )
                )
                .scalars()
                .all()
            )
            if purge_ids:
                await session.execute(delete(LLMCachePurge).where(LLMCachePurge.id.in_(purge_ids)))

            remaining = batch_size - len(purge_ids)
            guard_ids: list[int] = []
            if remaining > 0:
                matching_ref = (
                    select(LLMCacheRef.id)
                    .where(
                        LLMCacheRef.tenant_id == LLMCacheSourceGuard.tenant_id,
                        LLMCacheRef.source_type == LLMCacheSourceGuard.source_type,
                        LLMCacheRef.source_id == LLMCacheSourceGuard.source_id,
                    )
                    .exists()
                )
                guard_ids = list(
                    (
                        await session.execute(
                            select(LLMCacheSourceGuard.id)
                            .where(
                                LLMCacheSourceGuard.state == "active",
                                ~matching_ref,
                            )
                            .order_by(
                                LLMCacheSourceGuard.updated_at,
                                LLMCacheSourceGuard.id,
                            )
                            .limit(remaining)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .all()
                )
                if guard_ids:
                    # Recheck the absence of references under the guard row
                    # locks so a concurrent claim cannot lose its barrier.
                    await session.execute(
                        delete(LLMCacheSourceGuard).where(
                            LLMCacheSourceGuard.id.in_(guard_ids),
                            LLMCacheSourceGuard.state == "active",
                            ~matching_ref,
                        )
                    )
            await session.commit()
            return len(purge_ids) + len(guard_ids)

    async def _decode_ready(
        self,
        row: LLMCacheEntry,
        identity: CacheIdentity,
    ) -> ReadyCacheValue | None:
        if (
            row.payload_encrypted is None
            or not isinstance(row.encryption_meta, dict)
            or row.expires_at is None
        ):
            return None
        try:
            payload = self._crypto.decrypt(
                tenant_id=identity.tenant_id,
                namespace=identity.namespace,
                recipe_sha256=identity.recipe_sha256,
                encrypted=EncryptedCachePayload(
                    blob=bytes(row.payload_encrypted),
                    metadata=dict(row.encryption_meta),
                ),
            )
        except ValueError:
            logger.warning(
                "Discarding corrupt MySQL LLM cache entry id=%s namespace=%s",
                row.id,
                identity.namespace,
            )
            return None
        return ReadyCacheValue(
            payload=payload,
            model=str(row.model),
            model_epoch=str(row.model_epoch),
            usage=self._normalize_usage(row.usage or {}),
            expires_at=self._aware(row.expires_at),
            has_provenance=bool(row.has_provenance),
        )

    async def _delete_entry(self, entry_id: int) -> None:
        async with self._factory() as session:
            await session.execute(delete(LLMCacheEntry).where(LLMCacheEntry.id == entry_id))
            await session.execute(delete(LLMCacheRef).where(LLMCacheRef.cache_entry_id == entry_id))
            await session.commit()

    async def _lock_source_guards(
        self,
        session: AsyncSession,
        tenant_id: str,
        references: Sequence[CacheReference],
        *,
        erase: bool = False,
    ) -> bool:
        """Serialize cache claims with source erasure using durable guard rows."""

        ordered = sorted(
            self._normalize_references(references),
            key=lambda reference: (reference.source_type, reference.source_id),
        )
        for reference in ordered:
            guard: LLMCacheSourceGuard | None = None
            for _attempt in range(3):
                guard = (
                    await session.execute(
                        select(LLMCacheSourceGuard)
                        .where(
                            LLMCacheSourceGuard.tenant_id == tenant_id,
                            LLMCacheSourceGuard.source_type == reference.source_type,
                            LLMCacheSourceGuard.source_id == reference.source_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if guard is not None:
                    break
                try:
                    async with session.begin_nested():
                        guard = LLMCacheSourceGuard(
                            tenant_id=tenant_id,
                            source_type=reference.source_type,
                            source_id=reference.source_id,
                            state="erased" if erase else "active",
                            erased_at=self._now() if erase else None,
                        )
                        session.add(guard)
                        await session.flush()
                except IntegrityError as exc:
                    if not self._is_unique_violation(exc):
                        raise
                    guard = None
                    continue
                break
            if guard is None:
                raise RuntimeError("LLM source guard creation did not converge")
            if erase:
                guard.state = "erased"
                if guard.erased_at is None:
                    guard.erased_at = self._now()
            elif guard.state == "erased":
                return False
        return True

    async def _insert_purges(
        self,
        session: AsyncSession,
        identities: Sequence[CacheIdentity],
    ) -> None:
        for identity in dict.fromkeys(identities):
            try:
                async with session.begin_nested():
                    session.add(
                        LLMCachePurge(
                            tenant_id=identity.tenant_id,
                            namespace=identity.namespace,
                            recipe_sha256=identity.recipe_sha256,
                        )
                    )
                    await session.flush()
            except IntegrityError as exc:
                if not self._is_unique_violation(exc):
                    raise

    @staticmethod
    async def _select_entry(
        session: AsyncSession,
        identity: CacheIdentity,
    ) -> LLMCacheEntry | None:
        return (
            await session.execute(
                select(LLMCacheEntry).where(
                    LLMCacheEntry.tenant_id == identity.tenant_id,
                    LLMCacheEntry.namespace == identity.namespace,
                    LLMCacheEntry.recipe_sha256 == identity.recipe_sha256,
                )
            )
        ).scalar_one_or_none()

    @staticmethod
    async def _attach_references(
        session: AsyncSession,
        entry: LLMCacheEntry,
        references: Sequence[CacheReference],
    ) -> None:
        for reference in references:
            try:
                async with session.begin_nested():
                    session.add(
                        LLMCacheRef(
                            tenant_id=str(entry.tenant_id),
                            cache_entry_id=entry.id,
                            source_type=reference.source_type,
                            source_id=reference.source_id,
                        )
                    )
                    await session.flush()
            except IntegrityError as exc:
                # Concurrent followers may attach the same source. The unique
                # index makes this idempotent; the savepoint preserves the
                # surrounding claim/hit transaction.
                if not LLMCacheStore._is_unique_violation(exc):
                    raise
                continue

    @staticmethod
    def _normalize_references(
        references: Sequence[CacheReference],
    ) -> tuple[CacheReference, ...]:
        if len(references) > 64:
            raise ValueError("cache references cannot exceed 64 items")
        return tuple(
            dict.fromkeys(
                CacheReference(reference.source_type, reference.source_id)
                for reference in references
            )
        )

    @staticmethod
    def _normalize_usage(usage: Mapping[str, int]) -> dict[str, int]:
        result: dict[str, int] = {}
        for key, value in usage.items():
            if not isinstance(key, str) or not 1 <= len(key) <= 64:
                raise ValueError("usage keys must contain 1 to 64 characters")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("usage token counts must be non-negative integers")
            result[key] = value
        return result

    @staticmethod
    def _is_unique_violation(exc: IntegrityError) -> bool:
        """Recognize only MySQL/SQLite duplicate-key integrity errors."""

        original = exc.orig
        arguments = getattr(original, "args", ())
        code = arguments[0] if arguments else None
        if code in {1062, 1555, 2067, "23505"}:
            return True
        message = str(original).lower()
        return (
            "duplicate entry" in message
            or "unique constraint failed" in message
            or "unique violation" in message
        )

    @staticmethod
    def _validate_model(model: str, model_epoch: str) -> None:
        if not isinstance(model, str) or not 1 <= len(model) <= 128:
            raise ValueError("model must contain 1 to 128 characters")
        if not isinstance(model_epoch, str) or len(model_epoch) > 128:
            raise ValueError("model_epoch cannot exceed 128 characters")

    @staticmethod
    def _validate_semantic(
        identity: CacheIdentity,
        *,
        semantic_scope_hash: str | None,
        semantic_guard_hash: str | None,
        semantic_embedding: bytes | None,
        semantic_dim: int | None,
        language: str | None,
    ) -> None:
        values = (
            semantic_scope_hash,
            semantic_guard_hash,
            semantic_embedding,
            semantic_dim,
            language,
        )
        if all(value is None for value in values):
            return
        if identity.namespace not in _SEMANTIC_NAMESPACES:
            raise ValueError("semantic cache is restricted to query helper namespaces")
        if any(value is None for value in values):
            raise ValueError("semantic cache fields must be supplied together")
        assert semantic_scope_hash is not None
        assert semantic_guard_hash is not None
        assert semantic_embedding is not None
        assert semantic_dim is not None
        assert language is not None
        for name, digest in {
            "semantic_scope_hash": semantic_scope_hash,
            "semantic_guard_hash": semantic_guard_hash,
        }.items():
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
        if semantic_dim < 1 or len(semantic_embedding) != semantic_dim * 4:
            raise ValueError("semantic embedding must contain semantic_dim float32 values")
        if not 1 <= len(language) <= 16:
            raise ValueError("language must contain 1 to 16 characters")

    @staticmethod
    def _is_ready(row: LLMCacheEntry | None, now: datetime) -> bool:
        return bool(
            row is not None
            and row.status == "ready"
            and row.expires_at is not None
            and LLMCacheStore._aware(row.expires_at) > now
        )

    def _now(self) -> datetime:
        return self._aware(self._clock())

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


__all__ = [
    "CacheReference",
    "ClaimResult",
    "CleanupStats",
    "LLMCacheStore",
    "ReadyCacheValue",
    "SemanticCacheCandidate",
]
