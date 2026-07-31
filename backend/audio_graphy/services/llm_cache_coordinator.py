"""Coordinate Redis/local hot caching with the durable MySQL LLM cache.

The coordinator is intentionally the only bridge between ``LLMGateway`` and
storage.  It preserves the required ordering:

* reads: hot cache, then durable exact cache;
* writes: durable lease-CAS publish, then hot-cache fill;
* provenance-bearing hot hits are revalidated against MySQL so DSAR performed
  by another process cannot leave a usable stale Redis/local value.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import struct
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from audio_graphy.adapters.protocols import EmbedAdapter, EmbeddingResult
from audio_graphy.observability.metrics import LLM_LEASE_EVENTS
from audio_graphy.services.llm_gateway import (
    CachedLLMValue,
    CachePolicy,
    LLMCacheIdentity,
    LLMProvenance,
    LLMRequest,
    canonical_sha256,
)
from audio_graphy.storage.llm_cache_store import (
    CacheReference,
    LLMCacheStore,
    ReadyCacheValue,
    SemanticCacheCandidate,
)
from audio_graphy.storage.llm_hot_cache import (
    CacheIdentity,
    HotCache,
    HotCacheValue,
)

_VALUE_VERSION = 1
_SEMANTIC_SCOPE_VERSION = "semantic-scope-v1"
_SEMANTIC_GUARD_VERSION = "semantic-guard-v1"
_SEMANTIC_NAMESPACES = frozenset({"keyword_extract", "query_rewrite"})
_MIN_SEMANTIC_THRESHOLD = 0.985
_MAX_SEMANTIC_CANDIDATES = 256
_MAX_SEMANTIC_DIM = 65_536
_LEASE_EVENT_NAMES = frozenset(
    {
        "acquired",
        "follower",
        "reclaimed",
        "published",
        "stale_rejected",
        "released",
    }
)
_DATE_RE = re.compile(r"(?<!\d)(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{4}年\d{1,2}月\d{1,2}日)(?!\d)")
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])[-+]?\d+(?:[.,]\d+)?%?(?![A-Za-z0-9])")
_IDENTIFIER_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?=[A-Za-z0-9._:/-]*[A-Za-z])"
    r"(?=[A-Za-z0-9._:/-]*\d)"
    r"[A-Za-z0-9][A-Za-z0-9._:/-]*"
    r"(?![A-Za-z0-9])"
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CacheAcquisition:
    """Result returned to a lease-aware gateway leader."""

    state: Literal["ready", "leader", "bypass"]
    value: CachedLLMValue | None = None
    lease_token: str | None = None


@dataclass(frozen=True, slots=True)
class _SemanticContext:
    scope_hash: str
    guard_hash: str
    language: str
    vector: tuple[float, ...]
    packed_vector: bytes
    dim: int


class HotOnlyLLMCache:
    """Exact hot-cache adapter used when durable reuse is rolled back.

    Provenance-bearing values deliberately bypass this mode because a hot
    cache alone cannot provide the durable reverse index required by DSAR.
    """

    def __init__(self, hot_cache: HotCache) -> None:
        self._hot = hot_cache

    @property
    def backend_name(self) -> str:
        return self._hot.backend_name

    async def get(
        self,
        identity: LLMCacheIdentity,
        *,
        request: LLMRequest | None = None,
    ) -> CachedLLMValue | None:
        if request is not None and request.provenance:
            return None
        storage_identity = _storage_identity(identity)
        hot_value = await self._hot.get(storage_identity)
        if hot_value is None:
            return None
        if hot_value.has_provenance:
            await self._hot.delete(storage_identity)
            return None
        decoded = _decode_value(hot_value.payload, source=self._hot.backend_name)
        if decoded is not None:
            return decoded
        await self._hot.delete(storage_identity)
        return None

    async def put(
        self,
        identity: LLMCacheIdentity,
        value: CachedLLMValue,
        *,
        ttl_seconds: int,
        provenance: Sequence[Mapping[str, str]] = (),
    ) -> bool:
        if provenance:
            return False
        return await self._hot.set(
            _storage_identity(identity),
            HotCacheValue(payload=_encode_value(value), has_provenance=False),
            ttl_seconds=ttl_seconds,
        )

    async def delete(self, identity: LLMCacheIdentity) -> bool:
        return await self._hot.delete(_storage_identity(identity))


class LLMCacheCoordinator:
    """Tenant-safe exact cache and cross-process singleflight coordinator."""

    def __init__(
        self,
        hot_cache: HotCache,
        store: LLMCacheStore,
        *,
        lease_poll_seconds: float = 0.1,
        embed_adapter: EmbedAdapter | None = None,
        semantic_enabled: bool = False,
        semantic_threshold: float = _MIN_SEMANTIC_THRESHOLD,
        semantic_candidate_limit: int = _MAX_SEMANTIC_CANDIDATES,
    ) -> None:
        if lease_poll_seconds <= 0:
            raise ValueError("lease_poll_seconds must be positive")
        if (
            not math.isfinite(semantic_threshold)
            or not _MIN_SEMANTIC_THRESHOLD <= semantic_threshold <= 1.0
        ):
            raise ValueError("semantic_threshold must be finite and in [0.985, 1.0]")
        if not 1 <= semantic_candidate_limit <= _MAX_SEMANTIC_CANDIDATES:
            raise ValueError("semantic_candidate_limit must be in [1, 256]")
        self._hot = hot_cache
        self._store = store
        self._lease_poll_seconds = lease_poll_seconds
        self._embed = embed_adapter
        self._semantic_enabled = semantic_enabled
        self._semantic_threshold = semantic_threshold
        self._semantic_candidate_limit = semantic_candidate_limit

    @property
    def backend_name(self) -> str:
        return self._hot.backend_name

    @property
    def lease_heartbeat_seconds(self) -> float:
        """Safe renewal cadence for a leader that may queue behind a semaphore."""

        return max(0.05, self._store.lease_seconds / 3)

    async def start(self) -> None:
        await self._hot.start()

    async def aclose(self) -> None:
        await self._hot.aclose()

    async def get(
        self,
        identity: LLMCacheIdentity,
        *,
        request: LLMRequest | None = None,
    ) -> CachedLLMValue | None:
        storage_identity = _storage_identity(identity)
        hot_value = await self._hot.get(storage_identity)
        if hot_value is not None:
            requested_references = _references(request.provenance if request is not None else ())
            # A v2 generation identity may be shared by multiple source
            # objects. Every hot reuse must attach the current request's
            # provenance to the durable reverse index before returning;
            # otherwise DSAR for a later source could leave the shared result
            # reachable. Provenance-bearing hot hits already require a durable
            # readiness check, so this preserves the existing privacy boundary.
            durable_ready = True
            if hot_value.has_provenance or requested_references:
                durable_ready = (
                    await self._store.get_ready(
                        storage_identity,
                        references=requested_references,
                    )
                    is not None
                )
            if durable_ready:
                decoded = _decode_value(hot_value.payload, source=self._hot.backend_name)
                if decoded is not None:
                    return decoded
            await self._hot.delete(storage_identity)

        ready = await self._store.get_ready(
            storage_identity,
            references=_references(request.provenance if request is not None else ()),
        )
        if ready is None:
            return None
        try:
            value = _from_ready(ready, source="mysql")
        except ValueError:
            await self._delete_corrupt(storage_identity)
            return None
        await self._fill_hot(
            storage_identity,
            value,
            ready.has_provenance,
            request,
            expires_at=ready.expires_at,
        )
        return value

    async def lookup(
        self,
        identity: LLMCacheIdentity,
        *,
        request: LLMRequest,
        model: str,
    ) -> CachedLLMValue | None:
        """Read an existing exact/semantic value without acquiring a lease."""

        if not request.cacheable:
            return None
        try:
            exact = await self.get(identity, request=request)
        except Exception:
            logger.warning(
                "LLM cache readonly exact lookup failed open purpose=%s",
                request.purpose,
                exc_info=True,
            )
            return None
        if exact is not None:
            return exact
        semantic = await self._semantic_context(request, model)
        if semantic is None:
            return None
        return await self._find_semantic_value(
            _storage_identity(identity),
            request,
            model,
            _references(request.provenance),
            semantic,
        )

    async def acquire(
        self,
        identity: LLMCacheIdentity,
        *,
        request: LLMRequest,
        model: str,
    ) -> CacheAcquisition:
        """Get a ready value, own the lease, or wait/reclaim as a follower."""

        storage_identity = _storage_identity(identity)
        references = _references(request.provenance)
        semantic = await self._semantic_context(request, model)
        if semantic is not None:
            semantic_value = await self._find_semantic_value(
                storage_identity,
                request,
                model,
                references,
                semantic,
            )
            if semantic_value is not None:
                return CacheAcquisition("ready", value=semantic_value)

        followed_active_lease = False
        while True:
            claim = await self._store.claim(
                storage_identity,
                model=model,
                model_epoch=request.model_epoch,
                ttl_seconds=request.ttl_seconds,
                references=references,
                semantic_scope_hash=semantic.scope_hash if semantic else None,
                semantic_guard_hash=semantic.guard_hash if semantic else None,
                semantic_embedding=semantic.packed_vector if semantic else None,
                semantic_dim=semantic.dim if semantic else None,
                language=semantic.language if semantic else None,
            )
            if claim.state == "leader":
                _record_lease_event(
                    "reclaimed" if claim.reclaimed or followed_active_lease else "acquired"
                )
                return CacheAcquisition("leader", lease_token=claim.lease_token)
            if claim.state == "hit":
                assert claim.value is not None
                try:
                    value = _from_ready(
                        claim.value,
                        source=("mysql_singleflight" if followed_active_lease else "mysql"),
                    )
                except ValueError:
                    if not await self._delete_corrupt(storage_identity):
                        return CacheAcquisition("bypass")
                    continue
                await self._fill_hot(
                    storage_identity,
                    value,
                    claim.value.has_provenance,
                    request,
                    expires_at=claim.value.expires_at,
                )
                return CacheAcquisition("ready", value=value)
            if claim.state == "blocked":
                logger.warning(
                    "LLM cache write blocked by erased provenance purpose=%s",
                    request.purpose,
                )
                return CacheAcquisition("bypass")

            # No transaction or row lock is held while the provider leader
            # works. Re-claiming after the bounded sleep either observes ready
            # or atomically takes an expired lease.
            if not followed_active_lease:
                _record_lease_event("follower")
            followed_active_lease = True
            await asyncio.sleep(self._lease_poll_seconds)

    async def _semantic_context(
        self,
        request: LLMRequest,
        model: str,
    ) -> _SemanticContext | None:
        if not self._semantic_eligible(request):
            return None
        assert self._embed is not None
        assert request.semantic_text is not None
        assert request.semantic_language is not None
        try:
            results = await self._embed.embed_texts((request.semantic_text,))
            if len(results) != 1:
                raise ValueError("embedding adapter must return exactly one result")
            vector, packed, dim = _validated_embedding(
                results[0],
                expected_model=self._embed.model,
                expected_dim=self._embed.dim,
            )
            scope_hash = _semantic_scope_hash(
                request,
                model=model,
                embedding_model=self._embed.model,
            )
            guard_hash = _semantic_guard_hash(request)
        except Exception as exc:
            logger.warning(
                "Semantic cache embedding failed open purpose=%s error_type=%s",
                request.purpose,
                type(exc).__name__,
            )
            return None
        return _SemanticContext(
            scope_hash=scope_hash,
            guard_hash=guard_hash,
            language=request.semantic_language,
            vector=vector,
            packed_vector=packed,
            dim=dim,
        )

    def _semantic_eligible(self, request: LLMRequest) -> bool:
        return bool(
            self._semantic_enabled
            and self._embed is not None
            and request.cacheable
            and request.cache_policy is CachePolicy.QUERY_SEMANTIC
            and request.purpose in _SEMANTIC_NAMESPACES
            and request.semantic_text is not None
            and request.semantic_language is not None
        )

    async def _find_semantic_value(
        self,
        identity: CacheIdentity,
        request: LLMRequest,
        model: str,
        references: Sequence[CacheReference],
        semantic: _SemanticContext,
    ) -> CachedLLMValue | None:
        try:
            candidates = await self._store.find_semantic_candidates(
                tenant_id=identity.tenant_id,
                namespace=identity.namespace,
                semantic_scope_hash=semantic.scope_hash,
                semantic_guard_hash=semantic.guard_hash,
                language=semantic.language,
                limit=self._semantic_candidate_limit,
            )
        except Exception:
            logger.warning(
                "Semantic cache candidate lookup failed open purpose=%s",
                request.purpose,
                exc_info=True,
            )
            return None

        ranked: list[tuple[float, SemanticCacheCandidate]] = []
        for candidate in candidates[: self._semantic_candidate_limit]:
            if (
                candidate.identity.tenant_id != identity.tenant_id
                or candidate.identity.namespace != identity.namespace
                or candidate.model != model
                or candidate.model_epoch != request.model_epoch
                or candidate.semantic_dim != semantic.dim
            ):
                continue
            try:
                candidate_vector = _unpack_embedding(
                    candidate.semantic_embedding,
                    candidate.semantic_dim,
                )
                similarity = _cosine_similarity(semantic.vector, candidate_vector)
            except (TypeError, ValueError, struct.error):
                continue
            if similarity >= self._semantic_threshold:
                ranked.append((similarity, candidate))

        ranked.sort(key=lambda item: item[0], reverse=True)
        for _, candidate in ranked:
            try:
                ready = await self._store.get_ready(
                    candidate.identity,
                    references=references,
                )
                if ready is None:
                    continue
                if ready.model != model or ready.model_epoch != request.model_epoch:
                    continue
                return _from_ready(ready, source="mysql_semantic")
            except ValueError:
                await self._delete_corrupt(candidate.identity)
            except Exception:
                logger.warning(
                    "Semantic cache candidate hydration failed open purpose=%s",
                    request.purpose,
                    exc_info=True,
                )
        return None

    async def publish(
        self,
        identity: LLMCacheIdentity,
        *,
        lease_token: str | None,
        value: CachedLLMValue,
        request: LLMRequest,
        model: str,
    ) -> bool:
        del model  # model is already bound by the durable claim and recipe.
        if not lease_token:
            _record_lease_event("stale_rejected")
            return False
        storage_identity = _storage_identity(identity)
        payload = _encode_value(value)
        published = await self._store.publish(
            storage_identity,
            lease_token=lease_token,
            payload=payload,
            usage=value.usage,
            ttl_seconds=request.ttl_seconds,
        )
        if not published:
            _record_lease_event("stale_rejected")
            return False
        _record_lease_event("published")
        await self._hot.set(
            storage_identity,
            HotCacheValue(payload=payload, has_provenance=bool(request.provenance)),
            ttl_seconds=request.ttl_seconds,
        )
        return True

    async def release(
        self,
        identity: LLMCacheIdentity,
        *,
        lease_token: str | None,
    ) -> bool:
        if not lease_token:
            return False
        released = await self._store.release(
            _storage_identity(identity),
            lease_token=lease_token,
        )
        if released:
            _record_lease_event("released")
        return released

    async def renew(
        self,
        identity: LLMCacheIdentity,
        *,
        lease_token: str | None,
    ) -> bool:
        if not lease_token:
            return False
        return await self._store.renew(
            _storage_identity(identity),
            lease_token=lease_token,
        )

    async def put(
        self,
        identity: LLMCacheIdentity,
        value: CachedLLMValue,
        *,
        ttl_seconds: int,
        provenance: Sequence[Mapping[str, str]] = (),
    ) -> bool:
        """Compatibility path for non-lease-aware gateway implementations."""

        request = LLMRequest(
            tenant_id=identity.tenant_id,
            purpose=identity.namespace,
            messages=(),
            model_tier="unspecified",
            model_epoch="",
            provenance=tuple(provenance),
            ttl_seconds=ttl_seconds,
        )
        acquired = await self.acquire(identity, request=request, model=value.model)
        if acquired.state == "ready":
            return True
        return await self.publish(
            identity,
            lease_token=acquired.lease_token,
            value=value,
            request=request,
            model=value.model,
        )

    async def store(
        self,
        identity: LLMCacheIdentity,
        value: CachedLLMValue,
        *,
        request: LLMRequest,
        model: str,
    ) -> bool:
        """Durably store a validated value through lease CAS, then fill hot."""

        if not request.cacheable:
            return False
        lease_token: str | None = None
        try:
            acquired = await self.acquire(identity, request=request, model=model)
            if acquired.state == "ready":
                return True
            if acquired.state != "leader":
                return False
            lease_token = acquired.lease_token
            return await self.publish(
                identity,
                lease_token=lease_token,
                value=value,
                request=request,
                model=model,
            )
        except asyncio.CancelledError:
            await self._release_failed_store(identity, lease_token)
            raise
        except Exception:
            await self._release_failed_store(identity, lease_token)
            logger.warning(
                "LLM cache validated store failed open purpose=%s",
                request.purpose,
                exc_info=True,
            )
            return False

    async def _release_failed_store(
        self,
        identity: LLMCacheIdentity,
        lease_token: str | None,
    ) -> None:
        if lease_token is None:
            return
        try:
            await self.release(identity, lease_token=lease_token)
        except Exception:
            logger.warning(
                "LLM validated store lease release failed open namespace=%s",
                identity.namespace,
                exc_info=True,
            )

    async def delete(self, identity: LLMCacheIdentity) -> bool:
        """Evict an invalid exact value from both hot and durable layers."""

        storage_identity = _storage_identity(identity)
        hot_deleted = await self._hot.delete(storage_identity)
        delete_persistent = getattr(self._store, "delete", None)
        if not callable(delete_persistent):
            return hot_deleted
        return bool(await delete_persistent(storage_identity)) or hot_deleted

    async def delete_by_provenance(
        self,
        tenant_id: str,
        provenance: Sequence[LLMProvenance | Mapping[str, str] | CacheReference],
    ) -> int:
        references = _references(provenance)
        identities = await self._store.delete_by_provenance(tenant_id, references)
        if identities and await self._erase_hot(identities):
            await self._store.acknowledge_purges(identities)
        return len(identities)

    async def drain_pending_purges(self, *, limit: int = 500) -> int:
        """Retry durable privacy invalidations after Redis becomes healthy."""

        identities = await self._store.list_pending_purges(limit=limit)
        if not identities or not await self._erase_hot(identities):
            return 0
        return await self._store.acknowledge_purges(identities)

    async def clear_tenant(self, tenant_id: str) -> int:
        """Clear the hot tenant namespace.

        Durable tenant-wide deletion is intentionally not exposed: privacy
        erasure uses exact provenance references rather than an accidental
        broad delete.
        """

        return await self._hot.clear_tenant(tenant_id)

    async def _delete_corrupt(self, identity: CacheIdentity) -> bool:
        try:
            await self._hot.delete(identity)
        except Exception:
            logger.warning(
                "Failed to evict corrupt hot LLM cache envelope namespace=%s",
                identity.namespace,
                exc_info=True,
            )
        try:
            return bool(await self._store.delete(identity))
        except Exception:
            logger.warning(
                "Failed to delete corrupt persistent LLM cache envelope namespace=%s",
                identity.namespace,
                exc_info=True,
            )
            return False

    async def _erase_hot(self, identities: Sequence[CacheIdentity]) -> bool:
        erase_many = getattr(self._hot, "erase_many", None)
        try:
            if callable(erase_many):
                return bool(await erase_many(identities))
            await self._hot.delete_many(identities)
            return True
        except Exception:
            logger.warning(
                "Hot-cache privacy erasure deferred for durable retry",
                exc_info=True,
            )
            return False

    async def _fill_hot(
        self,
        identity: CacheIdentity,
        value: CachedLLMValue,
        has_provenance: bool,
        request: LLMRequest | None,
        *,
        expires_at: datetime,
    ) -> None:
        remaining_seconds = math.ceil(
            (LLMCacheStore._aware(expires_at) - datetime.now(UTC)).total_seconds()
        )
        if remaining_seconds <= 0:
            return
        ttl_seconds = min(
            request.ttl_seconds if request is not None else 300,
            remaining_seconds,
        )
        await self._hot.set(
            identity,
            HotCacheValue(_encode_value(value), has_provenance),
            ttl_seconds=ttl_seconds,
        )


def _validated_embedding(
    result: EmbeddingResult,
    *,
    expected_model: str,
    expected_dim: int,
) -> tuple[tuple[float, ...], bytes, int]:
    if result.model != expected_model:
        raise ValueError("embedding result model does not match configured model")
    if (
        result.dim != expected_dim
        or not 1 <= result.dim <= _MAX_SEMANTIC_DIM
        or len(result.vector) != result.dim
    ):
        raise ValueError("embedding result dimension is invalid")
    vector: list[float] = []
    for value in result.vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("embedding values must be numeric")
        converted = float(value)
        if not math.isfinite(converted):
            raise ValueError("embedding values must be finite")
        vector.append(converted)
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("embedding vector must have a finite non-zero norm")
    normalized = tuple(value / norm for value in vector)
    try:
        packed = struct.pack(f"<{result.dim}f", *normalized)
    except (OverflowError, struct.error) as exc:
        raise ValueError("embedding cannot be encoded as float32") from exc
    float32_vector = _unpack_embedding(packed, result.dim)
    return float32_vector, packed, result.dim


def _unpack_embedding(payload: bytes, dim: int) -> tuple[float, ...]:
    if not 1 <= dim <= _MAX_SEMANTIC_DIM or len(payload) != dim * 4:
        raise ValueError("semantic embedding has invalid float32 dimensions")
    values = struct.unpack(f"<{dim}f", payload)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("semantic embedding contains a non-finite value")
    norm = math.sqrt(sum(value * value for value in values))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("semantic embedding must have a finite non-zero norm")
    return tuple(values)


def _cosine_similarity(
    left: Sequence[float],
    right: Sequence[float],
) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("cosine vectors must have the same non-zero dimension")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if (
        not math.isfinite(left_norm)
        or not math.isfinite(right_norm)
        or left_norm <= 0
        or right_norm <= 0
    ):
        raise ValueError("cosine vectors must have finite non-zero norms")
    similarity = sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)
    if not math.isfinite(similarity):
        raise ValueError("cosine similarity is not finite")
    return max(-1.0, min(1.0, similarity))


def _semantic_scope_hash(
    request: LLMRequest,
    *,
    model: str,
    embedding_model: str,
) -> str:
    return canonical_sha256(
        {
            "version": _SEMANTIC_SCOPE_VERSION,
            "tenant_id": request.tenant_id,
            "namespace": request.purpose,
            "language": request.semantic_language,
            "prompt_version": request.prompt_version,
            "schema_version": request.schema_version,
            "parser_version": request.parser_version,
            "postprocessor_version": request.postprocessor_version,
            "provider": request.provider,
            "model_tier": request.model_tier,
            "model": model,
            "model_epoch": request.model_epoch,
            "permission_scope": request.permission_scope,
            "response_schema": request.response_schema,
            "response_format": request.response_format,
            "embedding_model": embedding_model,
            "temperature": request.temperature,
            "top_p": request.top_p,
            "max_tokens": request.max_tokens,
            "seed": request.seed,
            "stop": request.stop,
            "tools": request.tools,
        }
    )


def _semantic_guard_hash(request: LLMRequest) -> str:
    assert request.semantic_text is not None
    text = unicodedata.normalize("NFC", request.semantic_text)
    values = set(_DATE_RE.findall(text))
    values.update(_NUMBER_RE.findall(text))
    values.update(_IDENTIFIER_RE.findall(text))
    values.update(
        unicodedata.normalize("NFC", value).strip() for value in request.semantic_protected_values
    )
    return canonical_sha256(
        {
            "version": _SEMANTIC_GUARD_VERSION,
            "values": sorted(values),
        }
    )


def _record_lease_event(event: str) -> None:
    if event not in _LEASE_EVENT_NAMES:
        logger.error("Ignoring unsupported LLM lease metric event=%r", event)
        return
    try:
        LLM_LEASE_EVENTS.labels(event).inc()
    except Exception:
        logger.warning("LLM lease metric recording failed open", exc_info=True)


def _storage_identity(identity: LLMCacheIdentity) -> CacheIdentity:
    return CacheIdentity(
        tenant_id=identity.tenant_id,
        namespace=identity.namespace,
        recipe_sha256=identity.recipe_sha256,
    )


def _references(
    provenance: Sequence[LLMProvenance | Mapping[str, str] | CacheReference],
) -> tuple[CacheReference, ...]:
    references: list[CacheReference] = []
    for item in provenance:
        if isinstance(item, CacheReference):
            references.append(item)
        elif isinstance(item, LLMProvenance):
            references.append(CacheReference(item.source_type, item.source_id))
        else:
            references.append(
                CacheReference(
                    str(item.get("source_type", "")),
                    str(item.get("source_id", "")),
                )
            )
    return tuple(dict.fromkeys(references))


def _encode_value(value: CachedLLMValue) -> bytes:
    return json.dumps(
        {
            "model": value.model,
            "prompt_hash": value.prompt_hash,
            "text": value.text,
            "usage": dict(value.usage),
            "version": _VALUE_VERSION,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _decode_value(payload: bytes, *, source: str) -> CachedLLMValue | None:
    try:
        decoded: Any = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict) or decoded.get("version") != _VALUE_VERSION:
        return None
    text = decoded.get("text")
    model = decoded.get("model")
    prompt_hash = decoded.get("prompt_hash")
    usage = decoded.get("usage")
    if not isinstance(text, str) or not isinstance(model, str) or not isinstance(prompt_hash, str):
        return None
    if not isinstance(usage, dict):
        return None
    normalized_usage: dict[str, int] = {}
    for key, count in usage.items():
        if (
            not isinstance(key, str)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
        ):
            return None
        normalized_usage[key] = count
    return CachedLLMValue(
        text=text,
        model=model,
        prompt_hash=prompt_hash,
        usage=normalized_usage,
        cache_source=source,
    )


def _from_ready(value: ReadyCacheValue, *, source: str) -> CachedLLMValue:
    decoded = _decode_value(value.payload, source=source)
    if decoded is None:
        raise ValueError("persistent LLM cache payload has an invalid envelope")
    return decoded


__all__ = ["CacheAcquisition", "HotOnlyLLMCache", "LLMCacheCoordinator"]
