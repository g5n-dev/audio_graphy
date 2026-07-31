"""Contracts for the opt-in, query-helper-only semantic LLM cache."""

from __future__ import annotations

import asyncio
import hashlib
import math
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from audio_graphy.adapters.protocols import EmbeddingResult
from audio_graphy.api.metrics import LLM_LEASE_EVENTS
from audio_graphy.services.llm_cache_coordinator import LLMCacheCoordinator
from audio_graphy.services.llm_gateway import (
    CachedLLMValue,
    CachePolicy,
    LLMCacheIdentity,
    LLMProvenance,
    LLMRequest,
)
from audio_graphy.storage.llm_cache_store import (
    CacheReference,
    ClaimResult,
    ReadyCacheValue,
    SemanticCacheCandidate,
)
from audio_graphy.storage.llm_hot_cache import CacheIdentity, LocalHotCache


@dataclass(frozen=True, slots=True)
class _StoredSemantic:
    scope_hash: str
    guard_hash: str
    language: str
    candidate: SemanticCacheCandidate


class _FakeSemanticStore:
    """In-memory contract fake with the same semantic filtering as MySQL."""

    def __init__(self) -> None:
        self.ready: dict[CacheIdentity, ReadyCacheValue] = {}
        self.semantic_rows: list[_StoredSemantic] = []
        self.claims: dict[str, tuple[CacheIdentity, dict[str, Any]]] = {}
        self.claim_calls: list[dict[str, Any]] = []
        self.find_calls: list[dict[str, Any]] = []
        self.reference_calls: list[tuple[CacheIdentity, tuple[CacheReference, ...]]] = []
        self._next_token = 0

    async def get_ready(
        self,
        identity: CacheIdentity,
        *,
        references: Sequence[CacheReference] = (),
    ) -> ReadyCacheValue | None:
        value = self.ready.get(identity)
        if value is None:
            return None
        normalized = tuple(references)
        if normalized:
            self.reference_calls.append((identity, normalized))
            value = replace(value, has_provenance=True)
            self.ready[identity] = value
        return value

    async def contains_ready(self, identity: CacheIdentity) -> bool:
        return identity in self.ready

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
    ) -> ClaimResult:
        del ttl_seconds
        ready = await self.get_ready(identity, references=references)
        if ready is not None:
            return ClaimResult("hit", value=ready)
        details = {
            "identity": identity,
            "model": model,
            "model_epoch": model_epoch,
            "references": tuple(references),
            "semantic_scope_hash": semantic_scope_hash,
            "semantic_guard_hash": semantic_guard_hash,
            "semantic_embedding": semantic_embedding,
            "semantic_dim": semantic_dim,
            "language": language,
        }
        self.claim_calls.append(details)
        self._next_token += 1
        token = f"lease-{self._next_token}"
        self.claims[token] = (identity, details)
        return ClaimResult("leader", lease_token=token)

    async def publish(
        self,
        identity: CacheIdentity,
        *,
        lease_token: str,
        payload: bytes,
        usage: Mapping[str, int],
        ttl_seconds: int,
    ) -> bool:
        claimed = self.claims.pop(lease_token, None)
        if claimed is None or claimed[0] != identity:
            return False
        details = claimed[1]
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        ready = ReadyCacheValue(
            payload=payload,
            model=details["model"],
            model_epoch=details["model_epoch"],
            usage=dict(usage),
            expires_at=expires_at,
            has_provenance=bool(details["references"]),
        )
        self.ready[identity] = ready
        semantic_values = (
            details["semantic_scope_hash"],
            details["semantic_guard_hash"],
            details["semantic_embedding"],
            details["semantic_dim"],
            details["language"],
        )
        if all(value is not None for value in semantic_values):
            self.semantic_rows.append(
                _StoredSemantic(
                    scope_hash=details["semantic_scope_hash"],
                    guard_hash=details["semantic_guard_hash"],
                    language=details["language"],
                    candidate=SemanticCacheCandidate(
                        identity=identity,
                        payload=payload,
                        model=details["model"],
                        model_epoch=details["model_epoch"],
                        semantic_embedding=details["semantic_embedding"],
                        semantic_dim=details["semantic_dim"],
                        expires_at=expires_at,
                        has_provenance=bool(details["references"]),
                    ),
                )
            )
        return True

    async def release(self, identity: CacheIdentity, *, lease_token: str) -> bool:
        claimed = self.claims.pop(lease_token, None)
        return claimed is not None and claimed[0] == identity

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
        call = {
            "tenant_id": tenant_id,
            "namespace": namespace,
            "semantic_scope_hash": semantic_scope_hash,
            "semantic_guard_hash": semantic_guard_hash,
            "language": language,
            "limit": limit,
        }
        self.find_calls.append(call)
        return [
            row.candidate
            for row in self.semantic_rows
            if row.candidate.identity.tenant_id == tenant_id
            and row.candidate.identity.namespace == namespace
            and row.scope_hash == semantic_scope_hash
            and row.guard_hash == semantic_guard_hash
            and row.language == language
        ][:limit]

    async def delete(self, identity: CacheIdentity) -> bool:
        return self.ready.pop(identity, None) is not None


class _EmbeddingAdapter:
    dim = 2

    def __init__(
        self,
        vectors: Mapping[str, tuple[float, ...]],
        *,
        model: str = "embed-v1",
        result_dim: int | None = None,
        error: Exception | None = None,
    ) -> None:
        self.model = model
        self._vectors = vectors
        self._result_dim = result_dim
        self._error = error
        self.calls: list[tuple[str, ...]] = []

    async def embed_texts(self, texts: Sequence[str]) -> Sequence[EmbeddingResult]:
        normalized = tuple(texts)
        self.calls.append(normalized)
        if self._error is not None:
            raise self._error
        return [
            EmbeddingResult(
                vector=self._vectors[text],
                dim=self._result_dim if self._result_dim is not None else len(self._vectors[text]),
                model=self.model,
            )
            for text in normalized
        ]


def _request(
    semantic_text: str,
    *,
    tenant_id: str = "tenant-a",
    purpose: str = "keyword_extract",
    language: str = "zh-CN",
    protected_values: Sequence[str] = (),
    cache_policy: CachePolicy = CachePolicy.QUERY_SEMANTIC,
) -> LLMRequest:
    query_sha256 = hashlib.sha256(semantic_text.encode()).hexdigest()
    return LLMRequest(
        tenant_id=tenant_id,
        purpose=purpose,
        model_tier="weak",
        provider="openai-compatible",
        model_epoch="llm-epoch-1",
        messages=(
            {"role": "system", "content": "extract keywords"},
            {"role": "user", "content": semantic_text},
        ),
        prompt_version="prompt-v1",
        schema_version="schema-v1",
        parser_version="parser-v1",
        postprocessor_version="post-v1",
        response_schema={"type": "string"},
        response_format={"type": "text"},
        business_snapshot={"query_sha256": query_sha256},
        permission_scope={"role": "reader", "region": "cn"},
        provenance=(LLMProvenance("query", query_sha256),),
        cache_policy=cache_policy,
        ttl_seconds=60,
        semantic_text=semantic_text,
        semantic_language=language,
        semantic_protected_values=tuple(protected_values),
    )


def _identity(request: LLMRequest, *, model: str = "llm-v1") -> LLMCacheIdentity:
    return LLMCacheIdentity(
        request.tenant_id,
        request.purpose,
        request.recipe_sha256(model=model),
    )


def _coordinator(
    store: _FakeSemanticStore,
    embed: _EmbeddingAdapter | None,
    *,
    semantic_enabled: bool = True,
    semantic_threshold: float = 0.985,
    semantic_candidate_limit: int = 256,
) -> LLMCacheCoordinator:
    return LLMCacheCoordinator(  # type: ignore[arg-type]
        LocalHotCache(max_entries=16, max_bytes=16_384, max_item_bytes=4096),
        store,
        lease_poll_seconds=0.001,
        embed_adapter=embed,
        semantic_enabled=semantic_enabled,
        semantic_threshold=semantic_threshold,
        semantic_candidate_limit=semantic_candidate_limit,
    )


async def _seed(
    coordinator: LLMCacheCoordinator,
    request: LLMRequest,
    *,
    model: str = "llm-v1",
) -> None:
    identity = _identity(request, model=model)
    claim = await coordinator.acquire(identity, request=request, model=model)
    assert claim.state == "leader"
    assert await coordinator.publish(
        identity,
        lease_token=claim.lease_token,
        value=CachedLLMValue(
            text="seed-result",
            model=model,
            prompt_hash=identity.recipe_sha256,
            usage={"total_tokens": 7},
        ),
        request=request,
        model=model,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["keyword_extract", "query_rewrite"])
async def test_semantic_hit_uses_mysql_candidate_and_attaches_new_provenance(
    purpose: str,
) -> None:
    source_text = "如何办理退款"
    target_text = "怎么申请退货退款"
    embed = _EmbeddingAdapter({source_text: (1.0, 0.0), target_text: (1.0, 0.0)})
    store = _FakeSemanticStore()
    coordinator = _coordinator(store, embed)
    source = _request(source_text, purpose=purpose)
    target = _request(target_text, purpose=purpose)
    await _seed(coordinator, source)

    acquired = await coordinator.acquire(
        _identity(target),
        request=target,
        model="llm-v1",
    )

    assert acquired.state == "ready"
    assert acquired.value is not None
    assert acquired.value.text == "seed-result"
    assert acquired.value.cache_source == "mysql_semantic"
    assert len(embed.calls) == 2
    assert store.reference_calls[-1][0] == CacheIdentity(
        source.tenant_id,
        source.purpose,
        source.recipe_sha256(model="llm-v1"),
    )
    assert store.reference_calls[-1][1][0].source_id == target.provenance[0].source_id
    assert store.find_calls[-1]["limit"] == 256


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "model"),
    [
        ("tenant_id", "tenant-b", "llm-v1"),
        ("semantic_language", "en-US", "llm-v1"),
        ("prompt_version", "prompt-v2", "llm-v1"),
        ("schema_version", "schema-v2", "llm-v1"),
        ("parser_version", "parser-v2", "llm-v1"),
        ("postprocessor_version", "post-v2", "llm-v1"),
        ("provider", "different-provider", "llm-v1"),
        ("model_epoch", "llm-epoch-2", "llm-v1"),
        ("permission_scope", {"role": "admin", "region": "cn"}, "llm-v1"),
        ("response_schema", {"type": "array"}, "llm-v1"),
        ("response_format", {"type": "json_object"}, "llm-v1"),
        ("model_tier", "strong", "llm-v1"),
        ("model", None, "llm-v2"),
    ],
)
async def test_semantic_scope_rejects_cross_boundary_reuse(
    field: str,
    value: Any,
    model: str,
) -> None:
    source_text = "查询退款进度"
    target_text = "退款现在到哪一步"
    embed = _EmbeddingAdapter({source_text: (1.0, 0.0), target_text: (1.0, 0.0)})
    store = _FakeSemanticStore()
    coordinator = _coordinator(store, embed)
    await _seed(coordinator, _request(source_text))
    target = _request(target_text)
    if field != "model":
        target = replace(target, **{field: value})

    acquired = await coordinator.acquire(
        _identity(target, model=model),
        request=target,
        model=model,
    )

    assert acquired.state == "leader"


@pytest.mark.asyncio
async def test_embedding_model_is_part_of_semantic_scope() -> None:
    source_text = "查询退款进度"
    target_text = "退款进度查询"
    store = _FakeSemanticStore()
    source_embed = _EmbeddingAdapter({source_text: (1.0, 0.0)}, model="embed-v1")
    await _seed(_coordinator(store, source_embed), _request(source_text))
    target_embed = _EmbeddingAdapter({target_text: (1.0, 0.0)}, model="embed-v2")

    acquired = await _coordinator(store, target_embed).acquire(
        _identity(_request(target_text)),
        request=_request(target_text),
        model="llm-v1",
    )

    assert acquired.state == "leader"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_text", "protected_values", "expected_state"),
    [
        (
            "88.5 元的 A12 订单日期 2026-07-25 如何处理",
            ("segment-blue",),
            "ready",
        ),
        (
            "89.5 元的 A12 订单日期 2026-07-25 如何处理",
            ("segment-blue",),
            "leader",
        ),
        (
            "88.5 元的 A12 订单日期 2026-07-26 如何处理",
            ("segment-blue",),
            "leader",
        ),
        (
            "88.5 元的 A13 订单日期 2026-07-25 如何处理",
            ("segment-blue",),
            "leader",
        ),
        (
            "88.5 元的 A12 订单日期 2026-07-25 如何处理",
            ("segment-red",),
            "leader",
        ),
    ],
)
async def test_semantic_guard_requires_exact_numbers_dates_ids_and_explicit_values(
    target_text: str,
    protected_values: Sequence[str],
    expected_state: str,
) -> None:
    source_text = "订单 A12 在 2026-07-25 支付 88.5，怎么办"
    embed = _EmbeddingAdapter(
        {
            source_text: (1.0, 0.0),
            target_text: (1.0, 0.0),
        }
    )
    store = _FakeSemanticStore()
    coordinator = _coordinator(store, embed)
    await _seed(
        coordinator,
        _request(source_text, protected_values=("segment-blue",)),
    )
    target = _request(target_text, protected_values=protected_values)

    acquired = await coordinator.acquire(
        _identity(target),
        request=target,
        model="llm-v1",
    )

    assert acquired.state == expected_state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("similarity", "expected_state"),
    [(0.984, "leader"), (0.986, "ready")],
)
async def test_semantic_threshold_is_strict(
    similarity: float,
    expected_state: str,
) -> None:
    source_text = "退款说明"
    target_text = "退款帮助"
    target_vector = (similarity, math.sqrt(1.0 - similarity**2))
    embed = _EmbeddingAdapter({source_text: (1.0, 0.0), target_text: target_vector})
    store = _FakeSemanticStore()
    coordinator = _coordinator(store, embed)
    await _seed(coordinator, _request(source_text))
    target = _request(target_text)

    acquired = await coordinator.acquire(
        _identity(target),
        request=target,
        model="llm-v1",
    )

    assert acquired.state == expected_state


@pytest.mark.parametrize("threshold", [0.9849, 1.0001, math.nan, math.inf])
def test_semantic_threshold_cannot_weaken_safety_floor(threshold: float) -> None:
    with pytest.raises(ValueError):
        _coordinator(
            _FakeSemanticStore(),
            _EmbeddingAdapter({}),
            semantic_threshold=threshold,
        )


@pytest.mark.parametrize("candidate_limit", [0, 257])
def test_semantic_candidate_limit_is_bounded(candidate_limit: int) -> None:
    with pytest.raises(ValueError):
        _coordinator(
            _FakeSemanticStore(),
            _EmbeddingAdapter({}),
            semantic_candidate_limit=candidate_limit,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("semantic_enabled", "purpose", "policy"),
    [
        (False, "keyword_extract", CachePolicy.QUERY_SEMANTIC),
        (True, "entity_extract", CachePolicy.QUERY_SEMANTIC),
        (True, "keyword_extract", CachePolicy.EXACT),
    ],
)
async def test_semantic_cache_is_default_off_and_restricted_to_whitelist(
    semantic_enabled: bool,
    purpose: str,
    policy: CachePolicy,
) -> None:
    text = "查询退款"
    embed = _EmbeddingAdapter({text: (1.0, 0.0)})
    store = _FakeSemanticStore()
    coordinator = _coordinator(store, embed, semantic_enabled=semantic_enabled)
    request = _request(text, purpose=purpose, cache_policy=policy)

    acquired = await coordinator.acquire(
        _identity(request),
        request=request,
        model="llm-v1",
    )

    assert acquired.state == "leader"
    assert embed.calls == []
    assert store.find_calls == []
    assert store.claim_calls[-1]["semantic_embedding"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("vector", "result_dim"),
    [
        ((math.nan, 0.0), 2),
        ((math.inf, 0.0), 2),
        ((1.0, 0.0), 3),
        ((0.0, 0.0), 2),
    ],
)
async def test_invalid_query_embedding_degrades_to_exact_cache(
    vector: tuple[float, ...],
    result_dim: int,
) -> None:
    text = "查询退款"
    embed = _EmbeddingAdapter({text: vector}, result_dim=result_dim)
    store = _FakeSemanticStore()
    request = _request(text)

    acquired = await _coordinator(store, embed).acquire(
        _identity(request),
        request=request,
        model="llm-v1",
    )

    assert acquired.state == "leader"
    assert store.find_calls == []
    assert store.claim_calls[-1]["semantic_embedding"] is None


@pytest.mark.asyncio
async def test_embedding_failure_is_a_miss_and_never_persists_raw_query() -> None:
    raw_query = "绝不能写入 MySQL 的原始问题 991"
    embed = _EmbeddingAdapter({}, error=OSError("embedding unavailable"))
    store = _FakeSemanticStore()
    request = _request(raw_query)

    acquired = await _coordinator(store, embed).acquire(
        _identity(request),
        request=request,
        model="llm-v1",
    )

    assert acquired.state == "leader"
    assert raw_query not in repr(store.claim_calls)
    assert raw_query not in repr(store.find_calls)
    assert store.claim_calls[-1]["semantic_embedding"] is None


@pytest.mark.asyncio
async def test_invalid_candidate_embedding_is_ignored() -> None:
    source_text = "退款说明"
    target_text = "退款帮助"
    embed = _EmbeddingAdapter({source_text: (1.0, 0.0), target_text: (1.0, 0.0)})
    store = _FakeSemanticStore()
    coordinator = _coordinator(store, embed)
    await _seed(coordinator, _request(source_text))
    stored = store.semantic_rows[0]
    store.semantic_rows[0] = replace(
        stored,
        candidate=replace(
            stored.candidate,
            semantic_embedding=struct.pack("<2f", math.nan, 0.0),
        ),
    )
    target = _request(target_text)

    acquired = await coordinator.acquire(
        _identity(target),
        request=target,
        model="llm-v1",
    )

    assert acquired.state == "leader"


def _lease_count(event: str) -> float:
    return float(LLM_LEASE_EVENTS.labels(event)._value.get())  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_coordinator_records_only_bounded_lease_lifecycle_events() -> None:
    class _FollowerThenLeaderStore(_FakeSemanticStore):
        def __init__(self) -> None:
            super().__init__()
            self.follow_once = True

        async def claim(self, *args: Any, **kwargs: Any) -> ClaimResult:
            if self.follow_once:
                self.follow_once = False
                return ClaimResult("follower")
            return await super().claim(*args, **kwargs)

    event_names = (
        "acquired",
        "follower",
        "reclaimed",
        "published",
        "stale_rejected",
        "released",
    )
    before = {event: _lease_count(event) for event in event_names}
    request = _request("lease metrics", cache_policy=CachePolicy.EXACT)

    normal_store = _FakeSemanticStore()
    normal = _coordinator(normal_store, None, semantic_enabled=False)
    acquired = await normal.acquire(
        _identity(request),
        request=request,
        model="llm-v1",
    )
    assert await normal.publish(
        _identity(request),
        lease_token=acquired.lease_token,
        value=CachedLLMValue("ok", "llm-v1", _identity(request).recipe_sha256),
        request=request,
        model="llm-v1",
    )

    release_request = replace(request, messages=({"role": "user", "content": "other"},))
    released = await normal.acquire(
        _identity(release_request),
        request=release_request,
        model="llm-v1",
    )
    assert await normal.release(
        _identity(release_request),
        lease_token=released.lease_token,
    )
    assert not await normal.publish(
        _identity(release_request),
        lease_token="stale-token",
        value=CachedLLMValue(
            "stale",
            "llm-v1",
            _identity(release_request).recipe_sha256,
        ),
        request=release_request,
        model="llm-v1",
    )

    follower_store = _FollowerThenLeaderStore()
    follower = _coordinator(follower_store, None, semantic_enabled=False)
    reclaimed = await follower.acquire(
        _identity(request),
        request=request,
        model="llm-v1",
    )
    assert reclaimed.state == "leader"

    after = {event: _lease_count(event) for event in event_names}
    assert after["acquired"] - before["acquired"] == 2
    assert after["follower"] - before["follower"] == 1
    assert after["reclaimed"] - before["reclaimed"] == 1
    assert after["published"] - before["published"] == 1
    assert after["stale_rejected"] - before["stale_rejected"] == 1
    assert after["released"] - before["released"] == 1


@pytest.mark.asyncio
async def test_direct_expired_lease_takeover_is_recorded_as_reclaimed() -> None:
    class _DirectReclaimStore(_FakeSemanticStore):
        async def claim(self, *args: Any, **kwargs: Any) -> ClaimResult:
            del args, kwargs
            return ClaimResult("leader", lease_token="reclaimed", reclaimed=True)

    acquired_before = _lease_count("acquired")
    reclaimed_before = _lease_count("reclaimed")
    request = _request("direct reclaim", cache_policy=CachePolicy.EXACT)

    result = await _coordinator(
        _DirectReclaimStore(),
        None,
        semantic_enabled=False,
    ).acquire(
        _identity(request),
        request=request,
        model="llm-v1",
    )

    assert result.state == "leader"
    assert _lease_count("acquired") == acquired_before
    assert _lease_count("reclaimed") - reclaimed_before == 1


@pytest.mark.asyncio
async def test_corrupt_envelope_delete_failure_bypasses_without_looping() -> None:
    class _UndeletableStore(_FakeSemanticStore):
        async def delete(self, identity: CacheIdentity) -> bool:
            del identity
            raise OSError("database unavailable")

    store = _UndeletableStore()
    request = _request("corrupt exact", cache_policy=CachePolicy.EXACT)
    identity = CacheIdentity(
        request.tenant_id,
        request.purpose,
        request.recipe_sha256(model="llm-v1"),
    )
    store.ready[identity] = ReadyCacheValue(
        payload=b"not-json",
        model="llm-v1",
        model_epoch=request.model_epoch,
        usage={},
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
        has_provenance=False,
    )

    acquired = await _coordinator(
        store,
        None,
        semantic_enabled=False,
    ).acquire(
        _identity(request),
        request=request,
        model="llm-v1",
    )

    assert acquired.state == "bypass"


@pytest.mark.asyncio
async def test_coordinator_readonly_lookup_does_not_claim_and_store_uses_cas() -> None:
    source_text = "如何申请退款"
    target_text = "怎么申请退款"
    embed = _EmbeddingAdapter({source_text: (1.0, 0.0), target_text: (1.0, 0.0)})
    store = _FakeSemanticStore()
    coordinator = _coordinator(store, embed)
    source = _request(source_text)
    target = _request(target_text)
    await _seed(coordinator, source)
    claims_after_seed = len(store.claim_calls)

    hit = await coordinator.lookup(
        _identity(target),
        request=target,
        model="llm-v1",
    )

    assert hit is not None and hit.cache_source == "mysql_semantic"
    assert len(store.claim_calls) == claims_after_seed

    exact = replace(
        target,
        cache_policy=CachePolicy.EXACT,
        messages=({"role": "user", "content": "new exact item"},),
    )
    exact_identity = _identity(exact)
    stored = await coordinator.store(
        exact_identity,
        CachedLLMValue(
            "batch-item",
            "llm-v1",
            exact_identity.recipe_sha256,
        ),
        request=exact,
        model="llm-v1",
    )

    assert stored
    assert len(store.claim_calls) == claims_after_seed + 1
    assert (
        CacheIdentity(
            exact_identity.tenant_id,
            exact_identity.namespace,
            exact_identity.recipe_sha256,
        )
        in store.ready
    )


@pytest.mark.asyncio
async def test_coordinator_validated_store_releases_lease_when_cancelled() -> None:
    class _CancelledPublishStore(_FakeSemanticStore):
        async def publish(self, *args: Any, **kwargs: Any) -> bool:
            del args, kwargs
            raise asyncio.CancelledError

    store = _CancelledPublishStore()
    coordinator = _coordinator(store, None, semantic_enabled=False)
    request = _request("cancel store", cache_policy=CachePolicy.EXACT)
    identity = _identity(request)

    with pytest.raises(asyncio.CancelledError):
        await coordinator.store(
            identity,
            CachedLLMValue("batch-item", "llm-v1", identity.recipe_sha256),
            request=request,
            model="llm-v1",
        )

    assert store.claims == {}
