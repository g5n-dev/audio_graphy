"""Unit tests for the centralized LLM execution gateway."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

import pytest

from audio_graphy.adapters.exceptions import (
    LLMBadRequest,
    LLMServerError,
    LLMTimeoutError,
    LLMTruncatedResponseError,
)
from audio_graphy.adapters.protocols import LLMResponse
from audio_graphy.services.llm_gateway import (
    CachedLLMValue,
    CachePolicy,
    LLMCache,
    LLMCacheIdentity,
    LLMGateway,
    LLMObservation,
    LLMPriceSnapshot,
    LLMProvenance,
    LLMRequest,
    LLMUsageContext,
    execute_llm,
    llm_request_memo_scope,
    lookup_llm_cache,
    store_validated_llm_cache,
)


def _price_snapshot() -> LLMPriceSnapshot:
    return LLMPriceSnapshot(
        version="provider-price-2026-07",
        input_microunits_per_million_tokens=2_000_000,
        output_microunits_per_million_tokens=4_000_000,
        cached_prefill_microunits_per_million_tokens=500_000,
    )


class _MemoryCache(LLMCache):
    def __init__(self) -> None:
        self.values: dict[LLMCacheIdentity, CachedLLMValue] = {}
        self.get_calls = 0
        self.put_calls = 0
        self.delete_calls = 0

    async def get(self, identity: LLMCacheIdentity) -> CachedLLMValue | None:
        self.get_calls += 1
        return self.values.get(identity)

    async def put(
        self,
        identity: LLMCacheIdentity,
        value: CachedLLMValue,
        *,
        ttl_seconds: int,
        provenance: Sequence[Mapping[str, str]] = (),
    ) -> bool:
        del ttl_seconds, provenance
        self.put_calls += 1
        self.values[identity] = value
        return True

    async def delete(self, identity: LLMCacheIdentity) -> bool:
        self.delete_calls += 1
        return self.values.pop(identity, None) is not None


class _ScriptedAdapter:
    model = "provider-model-v1"

    def __init__(
        self,
        outcomes: Sequence[LLMResponse | Exception] | None = None,
        *,
        block: asyncio.Event | None = None,
    ) -> None:
        self._outcomes = list(outcomes or [])
        self._block = block
        self.calls = 0
        self.received_cache_keys: list[str | None] = []

    async def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        cache_key: str | None = None,
    ) -> LLMResponse:
        del messages, temperature, max_tokens
        self.calls += 1
        self.received_cache_keys.append(cache_key)
        if self._block is not None:
            await self._block.wait()
        if self._outcomes:
            outcome = self._outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return LLMResponse(
            text="ok",
            model=self.model,
            prompt_hash="transport-hash",
            usage={"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        )


def _request(**overrides: Any) -> LLMRequest:
    base = LLMRequest(
        tenant_id="tenant-a",
        purpose="entity_extract",
        model_tier="strong",
        messages=(
            {"role": "system", "content": "stable system prompt"},
            {"role": "user", "content": "动态输入\r\n第二行"},
        ),
        provider="openai-compatible",
        model_epoch="epoch-7",
        prompt_version="prompt-v3",
        schema_version="schema-v4",
        parser_version="parser-v2",
        postprocessor_version="post-v1",
        temperature=0.0,
        top_p=1.0,
        max_tokens=512,
        seed=None,
        stop=("END",),
        tools=({"type": "function", "function": {"name": "extract"}},),
        response_format={"type": "json_schema", "name": "entities"},
        business_snapshot={"chunk_sha256": "abc", "recording_version": 9},
        permission_scope={"agent_ids": [3, 7], "role": "inspector"},
        provenance=(
            LLMProvenance(source_type="recording", source_id="42"),
            LLMProvenance(source_type="chunk", source_id="91"),
        ),
        cache_policy=CachePolicy.EXACT,
        ttl_seconds=3600,
    )
    return replace(base, **overrides)


def test_price_snapshot_separates_cached_prefill_and_rounds_per_attempt() -> None:
    snapshot = _price_snapshot()

    assert snapshot.cost_microunits(
        input_tokens=100,
        output_tokens=10,
        cached_prefill_tokens=80,
    ) == 120
    assert snapshot.cost_microunits(input_tokens=1, output_tokens=0) == 2


def test_usage_context_is_not_part_of_generation_hash() -> None:
    base = _request()
    correlated = replace(
        base,
        usage_context=LLMUsageContext(
            logical_request_id="logical-fixed",
            tagger_version_id=11,
            deployment_id=12,
            evaluation_run_id=13,
            optimization_run_id=14,
            optimization_trial_id=15,
        ),
    )

    assert correlated.recipe_sha256(model="provider-model-v1") == base.recipe_sha256(
        model="provider-model-v1"
    )


@pytest.mark.asyncio
async def test_gateway_correlates_logical_request_and_retry_distinct_attempts() -> None:
    observations: list[LLMObservation] = []
    gateway = LLMGateway(
        _ScriptedAdapter([LLMTimeoutError("retry")]),
        retry_base_seconds=0,
        observer=observations.append,
    )
    context = LLMUsageContext(
        logical_request_id="logical-fixed",
        tagger_version_id=11,
        deployment_id=12,
        evaluation_run_id=13,
        optimization_run_id=14,
        optimization_trial_id=15,
    )

    response = await gateway.execute(
        _request(
            cache_policy=CachePolicy.BYPASS,
            usage_context=context,
        )
    )

    attempts = [event for event in observations if event.kind == "provider_attempt"]
    logical = [event for event in observations if event.kind == "logical_request"]
    assert len(attempts) == 2
    assert len({event.provider_attempt_id for event in attempts}) == 2
    assert all(event.provider_attempt_id for event in attempts)
    assert {event.logical_request_id for event in attempts + logical} == {
        "logical-fixed"
    }
    assert all(event.tagger_version_id == 11 for event in attempts + logical)
    assert all(event.deployment_id == 12 for event in attempts + logical)
    assert all(event.evaluation_run_id == 13 for event in attempts + logical)
    assert all(event.optimization_run_id == 14 for event in attempts + logical)
    assert all(event.optimization_trial_id == 15 for event in attempts + logical)
    assert response.provider_attempts == 2


@pytest.mark.asyncio
async def test_gateway_generates_logical_id_and_cache_hit_has_no_provider_attempt() -> None:
    observations: list[LLMObservation] = []
    gateway = LLMGateway(
        _ScriptedAdapter(),
        cache=_MemoryCache(),
        retry_base_seconds=0,
        observer=observations.append,
    )

    await gateway.execute(_request())
    await gateway.execute(_request())

    logical = [event for event in observations if event.kind == "logical_request"]
    attempts = [event for event in observations if event.kind == "provider_attempt"]
    assert len(logical) == 2
    assert all(event.logical_request_id for event in logical)
    assert logical[0].logical_request_id != logical[1].logical_request_id
    assert len(attempts) == 1
    assert attempts[0].provider_attempt_id


@pytest.mark.asyncio
async def test_unknown_billed_retry_is_returned_but_cache_hit_resets_uncertainty() -> None:
    timeout = LLMTimeoutError("provider outcome unknown")
    observations: list[LLMObservation] = []
    gateway = LLMGateway(
        _ScriptedAdapter([timeout]),
        cache=_MemoryCache(),
        retry_base_seconds=0,
        observer=observations.append,
    )
    request = _request(max_tokens=256)

    first = await gateway.execute(request)
    second = await gateway.execute(request)

    assert first.unknown_billed_tokens == 256
    assert first.provider_attempts == 2
    assert second.cached
    assert second.provider_attempts == 0
    assert second.unknown_billed_tokens == 0
    attempts = [event for event in observations if event.kind == "provider_attempt"]
    assert [event.unknown_billed for event in attempts] == [True, None]


@pytest.mark.asyncio
async def test_gateway_prices_success_and_cache_hit_resets_actual_cost() -> None:
    observations: list[LLMObservation] = []
    cache = _MemoryCache()
    gateway = LLMGateway(
        _ScriptedAdapter(),
        cache=cache,
        observer=observations.append,
        price_snapshot=_price_snapshot(),
        retry_base_seconds=0,
    )

    first = await gateway.execute(_request())
    second = await gateway.execute(_request())

    assert first.cost_microunits == 10
    assert first.price_version == "provider-price-2026-07"
    assert second.cached
    assert not second.provider_called
    assert second.cost_microunits == 0
    assert second.price_version is None
    assert second.usage == first.usage
    [provider_attempt] = [
        event for event in observations if event.kind == "provider_attempt"
    ]
    assert provider_attempt.cost_microunits == 10
    assert provider_attempt.price_version == "provider-price-2026-07"


@pytest.mark.asyncio
async def test_gateway_response_cost_includes_known_billed_retry() -> None:
    retry = LLMServerError("retry")
    retry.usage = {"prompt_tokens": 2, "completion_tokens": 1}  # type: ignore[attr-defined]
    retry.billed_usage_known = True  # type: ignore[attr-defined]
    retry.unknown_billed = False  # type: ignore[attr-defined]
    observations: list[LLMObservation] = []
    gateway = LLMGateway(
        _ScriptedAdapter(
            outcomes=(
                retry,
                LLMResponse(
                    "ok",
                    _ScriptedAdapter.model,
                    "raw",
                    usage={"prompt_tokens": 3, "completion_tokens": 1},
                ),
            )
        ),
        observer=observations.append,
        price_snapshot=_price_snapshot(),
        retry_base_seconds=0,
    )

    response = await gateway.execute(_request(cache_policy=CachePolicy.BYPASS))

    assert response.cost_microunits == 18
    assert response.provider_attempts == 2
    assert response.usage["prompt_tokens"] == 5
    assert response.usage["completion_tokens"] == 2
    attempts = [event for event in observations if event.kind == "provider_attempt"]
    assert [event.cost_microunits for event in attempts] == [8, 10]
    assert [event.usage["prompt_tokens"] for event in attempts] == [2, 3]
    assert [event.usage["completion_tokens"] for event in attempts] == [1, 1]
    assert all(event.price_version == "provider-price-2026-07" for event in attempts)


def test_recipe_v1_remains_canonical_for_dual_read_migration() -> None:
    request = _request()
    digest = request.recipe_sha256(
        model="provider-model-v1",
        version="llm-recipe-v1",
    )

    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")
    assert digest == request.recipe_sha256(
        model="provider-model-v1",
        version="llm-recipe-v1",
    )

    variants = (
        replace(request, tenant_id="tenant-b"),
        replace(request, purpose="final_answer"),
        replace(request, model_tier="weak"),
        replace(request, provider="another-provider"),
        replace(request, model_epoch="epoch-8"),
        replace(request, messages=({"role": "user", "content": "different"},)),
        replace(request, prompt_version="prompt-v4"),
        replace(request, schema_version="schema-v5"),
        replace(request, parser_version="parser-v3"),
        replace(request, postprocessor_version="post-v2"),
        replace(request, temperature=0.1),
        replace(request, top_p=0.9),
        replace(request, max_tokens=513),
        replace(request, seed=7),
        replace(request, stop=("STOP",)),
        replace(request, tools=({"type": "function", "function": {"name": "other"}},)),
        replace(request, response_format={"type": "json_object"}),
        replace(
            request,
            response_schema={
                "type": "object",
                "properties": {"answer": {"type": "string"}},
            },
        ),
        replace(request, business_snapshot={"chunk_sha256": "def"}),
        replace(request, permission_scope={"role": "admin"}),
        replace(
            request,
            provenance=(LLMProvenance(source_type="recording", source_id="different"),),
        ),
    )
    for variant in variants:
        assert (
            variant.recipe_sha256(
                model="provider-model-v1",
                version="llm-recipe-v1",
            )
            != digest
        )
    assert (
        request.recipe_sha256(
            model="provider-model-v2",
            version="llm-recipe-v1",
        )
        != digest
    )

    reordered = replace(
        request,
        business_snapshot={"recording_version": 9, "chunk_sha256": "abc"},
        permission_scope={"role": "inspector", "agent_ids": [3, 7]},
    )
    assert (
        reordered.recipe_sha256(
            model="provider-model-v1",
            version="llm-recipe-v1",
        )
        == digest
    )
    with_validator = replace(request, response_validator=lambda response: bool(response.text))
    assert (
        with_validator.recipe_sha256(
            model="provider-model-v1",
            version="llm-recipe-v1",
        )
        == digest
    )


def test_recipe_v2_excludes_non_generation_metadata_but_keeps_security_boundary() -> None:
    request = _request(
        response_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
        }
    )
    digest = request.recipe_sha256(model="provider-model-v1")

    non_generation_variants = (
        replace(request, purpose="same-generation-different-namespace"),
        replace(request, model_tier="weak"),
        replace(request, prompt_version="renamed-prompt"),
        replace(request, schema_version="renamed-schema"),
        replace(request, business_snapshot={"chunk_sha256": "different"}),
        replace(
            request,
            provenance=(LLMProvenance(source_type="recording", source_id="different"),),
        ),
    )
    for variant in non_generation_variants:
        assert variant.recipe_sha256(model="provider-model-v1") == digest

    generation_or_boundary_variants = (
        replace(request, tenant_id="tenant-b"),
        replace(request, permission_scope={"role": "admin"}),
        replace(request, provider="another-provider"),
        replace(request, model_epoch="epoch-8"),
        replace(request, messages=({"role": "user", "content": "different"},)),
        replace(request, parser_version="parser-v3"),
        replace(request, postprocessor_version="post-v2"),
        replace(request, max_tokens=513),
        replace(request, response_schema={"type": "array"}),
    )
    for variant in generation_or_boundary_variants:
        assert variant.recipe_sha256(model="provider-model-v1") != digest


@pytest.mark.asyncio
async def test_recipe_v2_reuses_generation_across_provenance_and_attaches_new_reference() -> None:
    class _ReferenceAwareCache(_MemoryCache):
        def __init__(self) -> None:
            super().__init__()
            self.references: set[tuple[str, str]] = set()

        async def get(
            self,
            identity: LLMCacheIdentity,
            *,
            request: LLMRequest | None = None,
        ) -> CachedLLMValue | None:
            if request is not None:
                self.references.update(
                    (item.source_type, item.source_id)
                    if isinstance(item, LLMProvenance)
                    else (str(item["source_type"]), str(item["source_id"]))
                    for item in request.provenance
                )
            return await super().get(identity)

        async def put(
            self,
            identity: LLMCacheIdentity,
            value: CachedLLMValue,
            *,
            ttl_seconds: int,
            provenance: Sequence[Mapping[str, str]] = (),
        ) -> bool:
            self.references.update(
                (str(item["source_type"]), str(item["source_id"])) for item in provenance
            )
            return await super().put(
                identity,
                value,
                ttl_seconds=ttl_seconds,
                provenance=provenance,
            )

    cache = _ReferenceAwareCache()
    adapter = _ScriptedAdapter()
    gateway = LLMGateway(adapter, cache=cache, retry_base_seconds=0)
    source = _request(provenance=(LLMProvenance("recording", "source"),))
    target = replace(
        source,
        business_snapshot={"chunk_sha256": "different"},
        provenance=(LLMProvenance("recording", "target"),),
    )

    first = await gateway.execute(source)
    second = await gateway.execute(target)

    assert first.provider_called
    assert second.cached
    assert adapter.calls == 1
    assert cache.references == {("recording", "source"), ("recording", "target")}


@pytest.mark.asyncio
async def test_recipe_v2_dual_reads_v1_and_promotes_without_provider_call() -> None:
    class _DurablePromotionCache(_MemoryCache):
        def __init__(self) -> None:
            super().__init__()
            self.store_calls: list[tuple[LLMCacheIdentity, LLMRequest, str]] = []

        async def store(
            self,
            identity: LLMCacheIdentity,
            value: CachedLLMValue,
            *,
            request: LLMRequest,
            model: str,
        ) -> bool:
            self.store_calls.append((identity, request, model))
            self.values[identity] = value
            return True

    cache = _DurablePromotionCache()
    adapter = _ScriptedAdapter()
    request = _request()
    v1_sha256 = request.recipe_sha256(model=adapter.model, version="llm-recipe-v1")
    v2_sha256 = request.recipe_sha256(model=adapter.model)
    cache.values[
        LLMCacheIdentity(request.tenant_id, request.purpose, v1_sha256)
    ] = CachedLLMValue(
        text="legacy-hit",
        model=adapter.model,
        prompt_hash=v1_sha256,
        usage={"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    )
    gateway = LLMGateway(
        adapter,
        cache=cache,
        retry_base_seconds=0,
        recipe_migration_mode="dual_read",
    )

    response = await gateway.execute(request)

    assert response.text == "legacy-hit"
    assert response.cached
    assert response.cache_source == "cache_v1"
    assert response.prompt_hash == v2_sha256
    assert adapter.calls == 0
    assert LLMCacheIdentity(request.tenant_id, request.purpose, v2_sha256) in cache.values
    assert cache.store_calls == [
        (
            LLMCacheIdentity(request.tenant_id, request.purpose, v2_sha256),
            request,
            adapter.model,
        )
    ]


@pytest.mark.asyncio
async def test_recipe_shadow_serves_v1_and_observes_v2_without_changing_result() -> None:
    cache = _MemoryCache()
    adapter = _ScriptedAdapter()
    request = _request()
    v1_sha256 = request.recipe_sha256(model=adapter.model, version="llm-recipe-v1")
    v2_sha256 = request.recipe_sha256(model=adapter.model)
    cache.values[
        LLMCacheIdentity(request.tenant_id, request.purpose, v1_sha256)
    ] = CachedLLMValue(
        text="served-v1",
        model=adapter.model,
        prompt_hash=v1_sha256,
    )
    cache.values[
        LLMCacheIdentity(request.tenant_id, request.purpose, v2_sha256)
    ] = CachedLLMValue(
        text="shadow-v2",
        model=adapter.model,
        prompt_hash=v2_sha256,
    )
    observations: list[LLMObservation] = []
    gateway = LLMGateway(
        adapter,
        cache=cache,
        retry_base_seconds=0,
        recipe_migration_mode="shadow",
        observer=observations.append,
    )

    response = await gateway.execute(request)

    assert response.text == "served-v1"
    assert response.prompt_hash == v1_sha256
    assert adapter.calls == 0
    logical = [event for event in observations if event.kind == "logical_request"]
    assert len(logical) == 1
    assert logical[0].recipe_version == "llm-recipe-v1"
    assert logical[0].shadow_recipe_sha256 == v2_sha256
    assert logical[0].shadow_cache_hit is True


@pytest.mark.asyncio
async def test_recipe_shadow_backfills_v2_after_observing_a_miss() -> None:
    cache = _MemoryCache()
    adapter = _ScriptedAdapter()
    request = _request()
    v1_sha256 = request.recipe_sha256(model=adapter.model, version="llm-recipe-v1")
    v2_sha256 = request.recipe_sha256(model=adapter.model)
    cache.values[
        LLMCacheIdentity(request.tenant_id, request.purpose, v1_sha256)
    ] = CachedLLMValue(
        text="served-v1",
        model=adapter.model,
        prompt_hash=v1_sha256,
        usage={"prompt_tokens": 9, "completion_tokens": 2},
    )
    observations: list[LLMObservation] = []
    gateway = LLMGateway(
        adapter,
        cache=cache,
        retry_base_seconds=0,
        recipe_migration_mode="shadow",
        observer=observations.append,
    )

    response = await gateway.execute(request)

    assert response.text == "served-v1"
    assert adapter.calls == 0
    assert LLMCacheIdentity(request.tenant_id, request.purpose, v2_sha256) in cache.values
    logical = [event for event in observations if event.kind == "logical_request"]
    assert logical[0].shadow_cache_hit is False


def test_request_rejects_unbounded_provenance() -> None:
    with pytest.raises(ValueError, match="provenance cannot exceed 64"):
        _request(
            provenance=tuple(
                LLMProvenance(source_type="recording", source_id=str(index)) for index in range(65)
            )
        )


def test_semantic_inputs_are_separate_from_v2_generation_identity() -> None:
    request = _request(
        cache_policy=CachePolicy.QUERY_SEMANTIC,
        semantic_text="订单 A-123 在 2026-07-25 的金额是 88.5",
        semantic_language="zh-CN",
        semantic_protected_values=("customer-tier-gold",),
    )
    digest = request.recipe_sha256(model="provider-model-v1")

    variants = (
        replace(request, semantic_text="订单 A-123 怎么退款"),
        replace(request, semantic_language="en-US"),
        replace(
            request,
            semantic_protected_values=("customer-tier-silver",),
        ),
    )
    for variant in variants:
        assert variant.recipe_sha256(model="provider-model-v1") == digest
        assert variant.recipe_sha256(
            model="provider-model-v1",
            version="llm-recipe-v1",
        ) != request.recipe_sha256(
            model="provider-model-v1",
            version="llm-recipe-v1",
        )


@pytest.mark.asyncio
async def test_cache_identity_is_tenant_isolated() -> None:
    cache = _MemoryCache()
    adapter = _ScriptedAdapter()
    gateway = LLMGateway(adapter, cache=cache, retry_base_seconds=0)

    first = await gateway.execute(_request(tenant_id="tenant-a"))
    second = await gateway.execute(_request(tenant_id="tenant-b"))

    assert first.text == second.text == "ok"
    assert adapter.calls == 2
    assert len(cache.values) == 2
    assert {key.tenant_id for key in cache.values} == {"tenant-a", "tenant-b"}


@pytest.mark.asyncio
async def test_fifty_concurrent_identical_requests_singleflight_one_provider_call() -> None:
    cache = _MemoryCache()
    release = asyncio.Event()
    adapter = _ScriptedAdapter(block=release)
    gateway = LLMGateway(adapter, cache=cache, retry_base_seconds=0)

    tasks = [asyncio.create_task(gateway.execute(_request())) for _ in range(50)]
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*tasks)

    assert adapter.calls == 1
    assert {result.text for result in results} == {"ok"}
    assert all(result.prompt_hash == results[0].prompt_hash for result in results)
    assert sum(result.provider_called for result in results) == 1
    assert sum(result.cache_source == "singleflight" for result in results) == 49
    assert gateway.inflight_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [LLMTimeoutError, LLMServerError])
async def test_transient_failures_retry_at_most_two_additional_times(
    error_type: type[Exception],
) -> None:
    errors = [error_type("temporary-1"), error_type("temporary-2")]
    success = LLMResponse(text="recovered", model="provider-model-v1", prompt_hash="raw")
    adapter = _ScriptedAdapter([*errors, success])
    gateway = LLMGateway(adapter, retry_base_seconds=0, max_retries=2)

    result = await gateway.execute(_request())

    assert result.text == "recovered"
    assert adapter.calls == 3

    always_fails = _ScriptedAdapter([error_type("x"), error_type("y"), error_type("z")])
    failing_gateway = LLMGateway(always_fails, retry_base_seconds=0, max_retries=2)
    with pytest.raises(error_type):
        await failing_gateway.execute(_request())
    assert always_fails.calls == 3


@pytest.mark.asyncio
async def test_permanent_error_is_not_retried() -> None:
    adapter = _ScriptedAdapter([LLMBadRequest("invalid request")])
    gateway = LLMGateway(adapter, retry_base_seconds=0, max_retries=2)

    with pytest.raises(LLMBadRequest):
        await gateway.execute(_request())

    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_non_deterministic_request_without_seed_bypasses_cache_and_singleflight() -> None:
    cache = _MemoryCache()
    adapter = _ScriptedAdapter()
    gateway = LLMGateway(adapter, cache=cache, retry_base_seconds=0)
    request = _request(temperature=0.7, seed=None)

    first = await gateway.execute(request)
    second = await gateway.execute(request)

    assert first.cached is False
    assert second.cached is False
    assert adapter.calls == 2
    assert cache.get_calls == 0
    assert cache.put_calls == 0


@pytest.mark.asyncio
async def test_request_scope_memo_reuses_sequential_calls_without_shared_cache() -> None:
    adapter = _ScriptedAdapter()
    gateway = LLMGateway(adapter, retry_base_seconds=0)
    request = _request()

    with llm_request_memo_scope():
        first = await gateway.execute(request)
        second = await gateway.execute(request)

    assert first.provider_called is True
    assert second.cached is True
    assert second.provider_called is False
    assert second.cache_source == "request_memo"
    assert second.usage == first.usage
    assert adapter.calls == 1

    with llm_request_memo_scope():
        await gateway.execute(request)
    assert adapter.calls == 2


@pytest.mark.asyncio
async def test_request_scope_memo_never_reuses_invalid_or_nondeterministic_results() -> None:
    adapter = _ScriptedAdapter(
        [
            LLMResponse(text="", model="provider-model-v1", prompt_hash="bad"),
            LLMResponse(text="valid", model="provider-model-v1", prompt_hash="good"),
        ]
    )
    gateway = LLMGateway(adapter, retry_base_seconds=0)
    validated = _request(response_validator=lambda response: bool(response.text.strip()))

    with llm_request_memo_scope():
        first = await gateway.execute(validated)
        second = await gateway.execute(validated)
    assert first.text == ""
    assert second.text == "valid"
    assert adapter.calls == 2

    nondeterministic = _request(temperature=0.7, seed=None)
    with llm_request_memo_scope():
        await gateway.execute(nondeterministic)
        await gateway.execute(nondeterministic)
    assert adapter.calls == 4


def test_llm_response_new_fields_preserve_old_constructor_compatibility() -> None:
    response = LLMResponse(text="old-style", model="m", prompt_hash="h")

    assert response.cached is False
    assert response.cache_source == "provider"
    assert response.provider_called is True


@pytest.mark.asyncio
async def test_execute_llm_falls_back_to_legacy_complete_signature() -> None:
    adapter = _ScriptedAdapter()
    request = _request()

    response = await execute_llm(adapter, request)

    assert response.text == "ok"
    assert adapter.calls == 1
    assert adapter.received_cache_keys == [request.recipe_sha256(model=adapter.model)]


@pytest.mark.asyncio
async def test_exact_cache_hit_preserves_usage_and_reports_no_provider_call() -> None:
    cache = _MemoryCache()
    adapter = _ScriptedAdapter()
    gateway = LLMGateway(adapter, cache=cache, retry_base_seconds=0)

    first = await gateway.execute(_request())
    second = await gateway.execute(_request())

    assert first.provider_called is True
    assert second.cached is True
    assert second.provider_called is False
    assert second.cache_source == "cache"
    assert second.usage == first.usage
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_cache_read_and_write_failures_degrade_to_provider_success() -> None:
    class _BrokenCache(_MemoryCache):
        async def get(self, identity: LLMCacheIdentity) -> CachedLLMValue | None:
            del identity
            raise OSError("cache read unavailable")

        async def put(
            self,
            identity: LLMCacheIdentity,
            value: CachedLLMValue,
            *,
            ttl_seconds: int,
            provenance: Sequence[Mapping[str, str]] = (),
        ) -> bool:
            del identity, value, ttl_seconds, provenance
            raise OSError("cache write unavailable")

    adapter = _ScriptedAdapter()
    gateway = LLMGateway(adapter, cache=_BrokenCache(), retry_base_seconds=0)

    result = await gateway.execute(_request())

    assert result.text == "ok"
    assert result.provider_called is True
    assert gateway.inflight_count == 0


@pytest.mark.asyncio
async def test_compatibility_complete_and_aclose_delegate_without_transport_cache() -> None:
    class _ClosableAdapter(_ScriptedAdapter):
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    adapter = _ClosableAdapter()
    gateway = LLMGateway(adapter, retry_base_seconds=0, model_tier="weak")

    result = await gateway.complete(
        [{"role": "user", "content": "legacy"}],
        max_tokens=7,
        cache_key="ignored-legacy-key",
    )
    await gateway.aclose()

    assert result.text == "ok"
    assert adapter.received_cache_keys == [None]
    assert adapter.closed is True


@pytest.mark.asyncio
async def test_invalid_cache_value_is_deleted_and_treated_as_miss() -> None:
    cache = _MemoryCache()
    request = _request(response_validator=lambda response: response.text == "valid")
    identity = LLMCacheIdentity(
        tenant_id=request.tenant_id,
        namespace=request.purpose,
        recipe_sha256=request.recipe_sha256(model=_ScriptedAdapter.model),
    )
    cache.values[identity] = CachedLLMValue(
        text="invalid",
        model=_ScriptedAdapter.model,
        prompt_hash=identity.recipe_sha256,
    )
    adapter = _ScriptedAdapter(
        [LLMResponse(text="valid", model=_ScriptedAdapter.model, prompt_hash="raw")]
    )
    gateway = LLMGateway(adapter, cache=cache, retry_base_seconds=0)

    result = await gateway.execute(request)

    assert result.text == "valid"
    assert result.provider_called is True
    assert adapter.calls == 1
    assert cache.delete_calls == 1
    assert cache.put_calls == 1


@pytest.mark.asyncio
async def test_invalid_provider_response_is_returned_but_never_cached() -> None:
    cache = _MemoryCache()
    request = _request(response_validator=lambda response: response.text == "valid")
    adapter = _ScriptedAdapter(
        [LLMResponse(text="invalid", model=_ScriptedAdapter.model, prompt_hash="raw")]
    )
    gateway = LLMGateway(adapter, cache=cache, retry_base_seconds=0)

    result = await gateway.execute(request)

    assert result.text == "invalid"
    assert cache.put_calls == 0
    assert cache.values == {}


@pytest.mark.asyncio
async def test_rich_generation_parameters_are_forwarded_to_supporting_adapter() -> None:
    class _RichAdapter:
        model = "rich-model"

        def __init__(self) -> None:
            self.received: dict[str, Any] = {}

        async def complete(
            self,
            messages: Sequence[dict[str, str]],
            *,
            temperature: float = 0.0,
            max_tokens: int | None = None,
            cache_key: str | None = None,
            top_p: float = 1.0,
            seed: int | None = None,
            stop: Sequence[str] = (),
            tools: Sequence[Mapping[str, Any]] = (),
            response_format: Mapping[str, Any] | None = None,
            response_schema: Mapping[str, Any] | None = None,
        ) -> LLMResponse:
            self.received = {
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "cache_key": cache_key,
                "top_p": top_p,
                "seed": seed,
                "stop": stop,
                "tools": tools,
                "response_format": response_format,
                "response_schema": response_schema,
            }
            return LLMResponse(text="rich", model=self.model, prompt_hash="raw")

    adapter = _RichAdapter()
    request = _request(
        temperature=0.4,
        top_p=0.8,
        seed=17,
        response_schema={"type": "object"},
        cache_policy=CachePolicy.BYPASS,
    )
    gateway = LLMGateway(adapter, retry_base_seconds=0)  # type: ignore[arg-type]

    await gateway.execute(request)

    assert adapter.received["temperature"] == pytest.approx(0.4)
    assert adapter.received["top_p"] == pytest.approx(0.8)
    assert adapter.received["max_tokens"] == 512
    assert adapter.received["seed"] == 17
    assert adapter.received["stop"] == ("END",)
    assert adapter.received["tools"] == request.tools
    assert adapter.received["response_format"] == request.response_format
    assert adapter.received["response_schema"] == request.response_schema
    assert adapter.received["cache_key"] is None


@pytest.mark.asyncio
async def test_response_schema_is_never_silently_dropped_for_legacy_adapter() -> None:
    class _LegacyAdapter:
        model = "legacy-without-schema"

        def __init__(self) -> None:
            self.calls = 0

        async def complete(
            self,
            messages: Sequence[dict[str, str]],
            *,
            temperature: float = 0.0,
            max_tokens: int | None = None,
            cache_key: str | None = None,
        ) -> LLMResponse:
            del messages, temperature, max_tokens, cache_key
            self.calls += 1
            return LLMResponse(text="{}", model=self.model, prompt_hash="raw")

    adapter = _LegacyAdapter()
    gateway = LLMGateway(adapter, retry_base_seconds=0)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="response_schema capability"):
        await gateway.execute(
            _request(
                response_schema={"type": "object"},
                cache_policy=CachePolicy.BYPASS,
            )
        )

    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_unexpected_extended_keyword_typeerror_falls_back_to_base_signature() -> None:
    class _CompatibilityAdapter:
        model = "compat-model"

        def __init__(self) -> None:
            self.attempts: list[dict[str, Any]] = []
            self.provider_calls = 0

        async def complete(
            self,
            messages: Sequence[dict[str, str]],
            **kwargs: Any,
        ) -> LLMResponse:
            del messages
            self.attempts.append(dict(kwargs))
            if "top_p" in kwargs:
                raise TypeError("complete() got an unexpected keyword argument 'top_p'")
            self.provider_calls += 1
            return LLMResponse(text="compat", model=self.model, prompt_hash="raw")

    adapter = _CompatibilityAdapter()
    gateway = LLMGateway(adapter, retry_base_seconds=0)  # type: ignore[arg-type]

    result = await gateway.execute(_request(cache_policy=CachePolicy.BYPASS))

    assert result.text == "compat"
    assert len(adapter.attempts) == 2
    assert "top_p" in adapter.attempts[0]
    assert "top_p" not in adapter.attempts[1]
    assert {
        "temperature",
        "max_tokens",
        "cache_key",
        "seed",
        "stop",
        "tools",
        "response_format",
        "response_schema",
    }.issubset(adapter.attempts[1])
    assert adapter.provider_calls == 1


@pytest.mark.asyncio
async def test_instance_semaphore_bounds_unique_provider_requests() -> None:
    class _ConcurrencyAdapter:
        model = "concurrency-model"

        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0

        async def complete(
            self,
            messages: Sequence[dict[str, str]],
            **kwargs: Any,
        ) -> LLMResponse:
            del messages, kwargs
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return LLMResponse(text="ok", model=self.model, prompt_hash="raw")

    adapter = _ConcurrencyAdapter()
    gateway = LLMGateway(  # type: ignore[arg-type]
        adapter,
        retry_base_seconds=0,
        max_concurrency=2,
    )
    requests = [
        _request(
            messages=({"role": "user", "content": f"unique-{index}"},),
            cache_policy=CachePolicy.BYPASS,
        )
        for index in range(8)
    ]

    await asyncio.gather(*(gateway.execute(request) for request in requests))

    assert adapter.max_active == 2


class _SharedLeaseCache:
    """Minimal execution-cache fake shared by independent gateway instances."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._published = asyncio.Event()
        self._leader_claimed = False
        self.value: CachedLLMValue | None = None
        self.acquire_calls = 0
        self.publish_calls = 0
        self.put_calls = 0
        self.get_requests: list[LLMRequest] = []

    async def get(
        self,
        identity: LLMCacheIdentity,
        *,
        request: LLMRequest,
    ) -> CachedLLMValue | None:
        del identity
        self.get_requests.append(request)
        return self.value

    async def acquire(
        self,
        identity: LLMCacheIdentity,
        *,
        request: LLMRequest,
        model: str,
    ) -> object:
        del identity, request, model
        self.acquire_calls += 1
        async with self._lock:
            if self.value is not None:
                return SimpleNamespace(state="ready", value=self.value)
            if not self._leader_claimed:
                self._leader_claimed = True
                return SimpleNamespace(state="leader", lease_token="lease-1")
        await self._published.wait()
        assert self.value is not None
        return SimpleNamespace(state="ready", value=self.value)

    async def publish(
        self,
        identity: LLMCacheIdentity,
        *,
        lease_token: str,
        value: CachedLLMValue,
        request: LLMRequest,
        model: str,
    ) -> bool:
        del identity, request, model
        assert lease_token == "lease-1"
        self.publish_calls += 1
        self.value = value
        self._published.set()
        return True

    async def release(
        self,
        identity: LLMCacheIdentity,
        *,
        lease_token: str,
    ) -> bool:
        del identity, lease_token
        self._published.set()
        return True

    async def put(
        self,
        identity: LLMCacheIdentity,
        value: CachedLLMValue,
        *,
        ttl_seconds: int,
        provenance: Sequence[Mapping[str, str]] = (),
    ) -> bool:
        del identity, value, ttl_seconds, provenance
        self.put_calls += 1
        return True


@pytest.mark.asyncio
async def test_two_gateways_and_fifty_callers_share_one_cross_process_lease() -> None:
    cache = _SharedLeaseCache()
    release_provider = asyncio.Event()
    adapter = _ScriptedAdapter(block=release_provider)
    observations: list[LLMObservation] = []

    async def observer(event: LLMObservation) -> None:
        observations.append(event)

    gateways = (
        LLMGateway(adapter, cache=cache, retry_base_seconds=0, observer=observer),
        LLMGateway(adapter, cache=cache, retry_base_seconds=0, observer=observer),
    )
    tasks = [asyncio.create_task(gateways[index % 2].execute(_request())) for index in range(50)]
    for _ in range(100):
        if adapter.calls == 1:
            break
        await asyncio.sleep(0)
    assert adapter.calls == 1
    release_provider.set()

    results = await asyncio.gather(*tasks)

    assert adapter.calls == 1
    assert cache.publish_calls == 1
    assert cache.put_calls == 0
    assert len(cache.get_requests) >= 2
    assert sum(event.kind == "logical_request" for event in observations) == 50
    assert sum(event.kind == "provider_attempt" for event in observations) == 1
    assert sum(result.provider_called for result in results) == 1
    assert all(result.text == "ok" for result in results)


@pytest.mark.asyncio
async def test_leader_renews_lease_while_waiting_for_provider() -> None:
    class _HeartbeatCache(_MemoryCache):
        lease_heartbeat_seconds = 0.01

        def __init__(self) -> None:
            super().__init__()
            self.renew_calls = 0

        async def acquire(
            self,
            identity: LLMCacheIdentity,
            *,
            request: LLMRequest,
            model: str,
        ) -> object:
            del identity, request, model
            return SimpleNamespace(state="leader", lease_token="owned")

        async def renew(
            self,
            identity: LLMCacheIdentity,
            *,
            lease_token: str,
        ) -> bool:
            del identity
            assert lease_token == "owned"
            self.renew_calls += 1
            return True

        async def publish(self, *args: Any, **kwargs: Any) -> bool:
            del args, kwargs
            return True

        async def release(self, *args: Any, **kwargs: Any) -> bool:
            del args, kwargs
            return True

    release_provider = asyncio.Event()
    cache = _HeartbeatCache()
    gateway = LLMGateway(
        _ScriptedAdapter(block=release_provider),
        cache=cache,
        retry_base_seconds=0,
    )

    task = asyncio.create_task(gateway.execute(_request()))
    for _ in range(100):
        if cache.renew_calls >= 2:
            break
        await asyncio.sleep(0.005)
    release_provider.set()
    result = await task

    assert result.provider_called
    assert cache.renew_calls >= 2


@pytest.mark.asyncio
async def test_stale_lease_leader_cannot_overwrite_newer_published_value() -> None:
    class _StaleLeaseCache:
        def __init__(self) -> None:
            self.claims = 0
            self.value: CachedLLMValue | None = None
            self.publish_results: list[tuple[str, str, bool]] = []
            self.first_claimed = asyncio.Event()

        async def get(
            self,
            identity: LLMCacheIdentity,
            *,
            request: LLMRequest,
        ) -> None:
            del identity, request
            return None

        async def acquire(
            self,
            identity: LLMCacheIdentity,
            *,
            request: LLMRequest,
            model: str,
        ) -> object:
            del identity, request, model
            self.claims += 1
            if self.claims == 1:
                self.first_claimed.set()
                return SimpleNamespace(state="leader", lease_token="stale")
            return SimpleNamespace(state="leader", lease_token="fresh")

        async def publish(
            self,
            identity: LLMCacheIdentity,
            *,
            lease_token: str,
            value: CachedLLMValue,
            request: LLMRequest,
            model: str,
        ) -> bool:
            del identity, request, model
            accepted = lease_token == "fresh"
            if accepted:
                self.value = value
            self.publish_results.append((lease_token, value.text, accepted))
            return accepted

        async def release(
            self,
            identity: LLMCacheIdentity,
            *,
            lease_token: str,
        ) -> bool:
            del identity, lease_token
            return False

    stale_cache = _StaleLeaseCache()
    release_stale_provider = asyncio.Event()
    stale_adapter = _ScriptedAdapter(
        [
            LLMResponse(
                text="stale-result",
                model=_ScriptedAdapter.model,
                prompt_hash="raw",
            )
        ],
        block=release_stale_provider,
    )
    fresh_adapter = _ScriptedAdapter(
        [
            LLMResponse(
                text="fresh-result",
                model=_ScriptedAdapter.model,
                prompt_hash="raw",
            )
        ]
    )
    stale_gateway = LLMGateway(stale_adapter, cache=stale_cache, retry_base_seconds=0)
    fresh_gateway = LLMGateway(fresh_adapter, cache=stale_cache, retry_base_seconds=0)

    stale_task = asyncio.create_task(stale_gateway.execute(_request()))
    await stale_cache.first_claimed.wait()
    fresh_result = await fresh_gateway.execute(_request())
    release_stale_provider.set()
    stale_result = await stale_task

    assert fresh_result.text == "fresh-result"
    assert stale_result.text == "stale-result"
    assert stale_cache.value is not None
    assert stale_cache.value.text == "fresh-result"
    assert stale_cache.publish_results == [
        ("fresh", "fresh-result", True),
        ("stale", "stale-result", False),
    ]


@pytest.mark.asyncio
async def test_lease_is_released_when_provider_or_validation_fails() -> None:
    @dataclass
    class _LeaseCache:
        released: list[str]
        publish_calls: int = 0
        put_calls: int = 0

        async def get(
            self,
            identity: LLMCacheIdentity,
            *,
            request: LLMRequest,
        ) -> None:
            del identity, request
            return None

        async def acquire(
            self,
            identity: LLMCacheIdentity,
            *,
            request: LLMRequest,
            model: str,
        ) -> object:
            del identity, request, model
            return SimpleNamespace(state="leader", lease_token="owned")

        async def publish(self, *args: Any, **kwargs: Any) -> bool:
            del args, kwargs
            self.publish_calls += 1
            return True

        async def release(
            self,
            identity: LLMCacheIdentity,
            *,
            lease_token: str,
        ) -> bool:
            del identity
            self.released.append(lease_token)
            return True

        async def put(self, *args: Any, **kwargs: Any) -> bool:
            del args, kwargs
            self.put_calls += 1
            return True

    provider_error_cache = _LeaseCache([])
    failing_gateway = LLMGateway(
        _ScriptedAdapter([LLMBadRequest("bad")]),
        cache=provider_error_cache,
        retry_base_seconds=0,
    )
    with pytest.raises(LLMBadRequest):
        await failing_gateway.execute(_request())

    invalid_cache = _LeaseCache([])
    invalid_gateway = LLMGateway(
        _ScriptedAdapter(
            [LLMResponse(text="invalid", model=_ScriptedAdapter.model, prompt_hash="raw")]
        ),
        cache=invalid_cache,
        retry_base_seconds=0,
    )
    invalid_result = await invalid_gateway.execute(
        _request(response_validator=lambda response: response.text == "valid")
    )

    assert invalid_result.text == "invalid"
    assert provider_error_cache.released == ["owned"]
    assert invalid_cache.released == ["owned"]
    assert invalid_cache.publish_calls == invalid_cache.put_calls == 0


@pytest.mark.asyncio
async def test_execution_cache_bypass_calls_provider_without_any_cache_write() -> None:
    class _BypassCache(_MemoryCache):
        async def acquire(
            self,
            identity: LLMCacheIdentity,
            *,
            request: LLMRequest,
            model: str,
        ) -> object:
            del identity, request, model
            return SimpleNamespace(state="bypass")

        async def get(
            self,
            identity: LLMCacheIdentity,
            *,
            request: LLMRequest,
        ) -> None:
            del identity, request
            return None

    cache = _BypassCache()
    adapter = _ScriptedAdapter()
    result = await LLMGateway(adapter, cache=cache, retry_base_seconds=0).execute(_request())

    assert result.text == "ok"
    assert adapter.calls == 1
    assert cache.put_calls == 0


@pytest.mark.asyncio
async def test_observer_records_retries_logical_errors_and_is_fail_open() -> None:
    observations: list[LLMObservation] = []

    class _Observer:
        async def observe(self, event: LLMObservation) -> None:
            observations.append(event)

    adapter = _ScriptedAdapter([LLMTimeoutError("retry"), LLMBadRequest("permanent")])
    gateway = LLMGateway(
        adapter,
        retry_base_seconds=0,
        observer=_Observer(),
    )

    with pytest.raises(LLMBadRequest):
        await gateway.execute(_request())

    provider_events = [event for event in observations if event.kind == "provider_attempt"]
    logical_events = [event for event in observations if event.kind == "logical_request"]
    assert [event.attempt for event in provider_events] == [1, 2]
    assert [event.outcome for event in provider_events] == ["error", "error"]
    assert len(logical_events) == 1
    assert logical_events[0].outcome == "error"
    assert logical_events[0].error_type == "LLMBadRequest"

    async def broken_observer(event: LLMObservation) -> None:
        del event
        raise RuntimeError("metrics unavailable")

    successful = await LLMGateway(
        _ScriptedAdapter(),
        retry_base_seconds=0,
        observer=broken_observer,
    ).execute(_request(cache_policy=CachePolicy.BYPASS))
    assert successful.text == "ok"

    with pytest.raises(RuntimeError, match="metrics unavailable"):
        await LLMGateway(
            _ScriptedAdapter(),
            retry_base_seconds=0,
            observer=broken_observer,
        ).execute(
            _request(
                cache_policy=CachePolicy.BYPASS,
                usage_context=LLMUsageContext(require_durable_ledger=True),
            )
        )

    with pytest.raises(RuntimeError, match="no observer is configured"):
        await LLMGateway(
            _ScriptedAdapter(),
            retry_base_seconds=0,
        ).execute(
            _request(
                cache_policy=CachePolicy.BYPASS,
                usage_context=LLMUsageContext(require_durable_ledger=True),
            )
        )

    with pytest.raises(RuntimeError, match="observer is invalid"):
        await LLMGateway(
            _ScriptedAdapter(),
            retry_base_seconds=0,
            observer=object(),  # type: ignore[arg-type]
        ).execute(
            _request(
                cache_policy=CachePolicy.BYPASS,
                usage_context=LLMUsageContext(require_durable_ledger=True),
            )
        )


@pytest.mark.asyncio
async def test_truncated_provider_attempt_preserves_billing_metadata_for_ledger() -> None:
    observations: list[LLMObservation] = []
    truncation = LLMTruncatedResponseError(
        "incomplete",
        model="provider-model-v1",
        finish_reason="length",
        provider_request_id="provider-request-7",
        usage={"prompt_tokens": 101, "completion_tokens": 64, "total_tokens": 165},
    )
    gateway = LLMGateway(
        _ScriptedAdapter([truncation]),
        retry_base_seconds=0,
        observer=observations.append,
    )

    with pytest.raises(LLMTruncatedResponseError):
        await gateway.execute(_request(max_tokens=64))

    [attempt] = [event for event in observations if event.kind == "provider_attempt"]
    assert attempt.outcome == "error"
    assert attempt.usage == {
        "prompt_tokens": 101,
        "completion_tokens": 64,
        "total_tokens": 165,
    }
    assert attempt.finish_reason == "length"
    assert attempt.provider_request_id == "provider-request-7"
    assert attempt.requested_max_tokens == 64
    assert attempt.billed_usage_known is True
    assert attempt.unknown_billed is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["raise", "invalid"])
async def test_execution_cache_acquire_faults_fail_open_without_ordinary_put(
    mode: str,
) -> None:
    class _AcquireFaultCache(_MemoryCache):
        async def get(
            self,
            identity: LLMCacheIdentity,
            *,
            request: LLMRequest,
        ) -> None:
            del identity, request
            return None

        async def acquire(
            self,
            identity: LLMCacheIdentity,
            *,
            request: LLMRequest,
            model: str,
        ) -> object:
            del identity, request, model
            if mode == "raise":
                raise OSError("persistent cache unavailable")
            return SimpleNamespace(state="unknown")

    cache = _AcquireFaultCache()
    adapter = _ScriptedAdapter()

    result = await LLMGateway(adapter, cache=cache, retry_base_seconds=0).execute(_request())

    assert result.text == "ok"
    assert adapter.calls == 1
    assert cache.put_calls == 0


@pytest.mark.asyncio
async def test_invalid_ready_lease_value_is_evicted_then_bypasses_writes() -> None:
    class _InvalidReadyCache(_MemoryCache):
        async def get(
            self,
            identity: LLMCacheIdentity,
            *,
            request: LLMRequest,
        ) -> None:
            del identity, request
            return None

        async def acquire(
            self,
            identity: LLMCacheIdentity,
            *,
            request: LLMRequest,
            model: str,
        ) -> object:
            del request, model
            return SimpleNamespace(
                state="ready",
                value=CachedLLMValue(
                    text="invalid",
                    model=_ScriptedAdapter.model,
                    prompt_hash=identity.recipe_sha256,
                ),
            )

    cache = _InvalidReadyCache()
    adapter = _ScriptedAdapter(
        [LLMResponse(text="valid", model=_ScriptedAdapter.model, prompt_hash="raw")]
    )
    request = _request(response_validator=lambda response: response.text == "valid")

    result = await LLMGateway(adapter, cache=cache, retry_base_seconds=0).execute(request)

    assert result.text == "valid"
    assert adapter.calls == 1
    assert cache.delete_calls == 1
    assert cache.put_calls == 0


@pytest.mark.asyncio
async def test_publish_and_release_faults_do_not_replace_provider_result() -> None:
    class _BrokenPublishCache(_MemoryCache):
        def __init__(self) -> None:
            super().__init__()
            self.release_calls = 0

        async def get(
            self,
            identity: LLMCacheIdentity,
            *,
            request: LLMRequest,
        ) -> None:
            del identity, request
            return None

        async def acquire(
            self,
            identity: LLMCacheIdentity,
            *,
            request: LLMRequest,
            model: str,
        ) -> object:
            del identity, request, model
            return SimpleNamespace(state="leader", lease_token="owned")

        async def publish(self, *args: Any, **kwargs: Any) -> bool:
            del args, kwargs
            raise OSError("publish unavailable")

        async def release(
            self,
            identity: LLMCacheIdentity,
            *,
            lease_token: str,
        ) -> bool:
            del identity, lease_token
            self.release_calls += 1
            raise OSError("release unavailable")

    cache = _BrokenPublishCache()
    result = await LLMGateway(
        _ScriptedAdapter(),
        cache=cache,
        retry_base_seconds=0,
    ).execute(_request())

    assert result.text == "ok"
    assert cache.release_calls == 1
    assert cache.put_calls == 0


@pytest.mark.asyncio
async def test_sync_observer_callback_and_cancelled_logical_request_are_supported() -> None:
    observations: list[LLMObservation] = []
    release_provider = asyncio.Event()
    adapter = _ScriptedAdapter(block=release_provider)
    gateway = LLMGateway(
        adapter,
        retry_base_seconds=0,
        observer=observations.append,
    )

    task = asyncio.create_task(gateway.execute(_request(cache_policy=CachePolicy.BYPASS)))
    for _ in range(100):
        if adapter.calls:
            break
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [event.kind for event in observations] == [
        "provider_attempt",
        "logical_request",
    ]
    assert all(event.outcome == "cancelled" for event in observations)


@pytest.mark.asyncio
async def test_cancelled_lease_leader_releases_its_claim() -> None:
    class _CancelledAdapter:
        model = _ScriptedAdapter.model

        async def complete(
            self,
            messages: Sequence[dict[str, str]],
            **kwargs: Any,
        ) -> LLMResponse:
            del messages, kwargs
            raise asyncio.CancelledError

    class _LeaderCache(_MemoryCache):
        def __init__(self) -> None:
            super().__init__()
            self.released: list[str] = []

        async def get(
            self,
            identity: LLMCacheIdentity,
            *,
            request: LLMRequest,
        ) -> None:
            del identity, request
            return None

        async def acquire(
            self,
            identity: LLMCacheIdentity,
            *,
            request: LLMRequest,
            model: str,
        ) -> object:
            del identity, request, model
            return SimpleNamespace(state="leader", lease_token="cancelled")

        async def release(
            self,
            identity: LLMCacheIdentity,
            *,
            lease_token: str,
        ) -> bool:
            del identity
            self.released.append(lease_token)
            return True

    cache = _LeaderCache()
    gateway = LLMGateway(  # type: ignore[arg-type]
        _CancelledAdapter(),
        cache=cache,
        retry_base_seconds=0,
    )

    with pytest.raises(asyncio.CancelledError):
        await gateway.execute(_request())

    assert cache.released == ["cancelled"]


@pytest.mark.asyncio
async def test_public_lookup_and_validated_store_never_call_provider() -> None:
    cache = _MemoryCache()
    adapter = _ScriptedAdapter()
    observations: list[LLMObservation] = []
    gateway = LLMGateway(
        adapter,
        cache=cache,
        retry_base_seconds=0,
        observer=observations.append,
    )
    request = _request(response_validator=lambda response: response.text == "valid")
    raw = LLMResponse(
        text="valid",
        model="batch-model",
        prompt_hash="batch-request",
        usage={"total_tokens": 5},
    )

    assert await gateway.store_validated(request, raw)
    hit = await gateway.lookup_validated(request)

    assert hit is not None
    assert hit.text == "valid"
    assert hit.model == gateway.model
    assert hit.prompt_hash == request.recipe_sha256(model=gateway.model)
    assert hit.cached
    assert adapter.calls == 0
    assert cache.put_calls == 1
    assert len(observations) == 1
    assert observations[0].kind == "logical_request"
    assert observations[0].cache_source == "cache"
    assert observations[0].provider_called is False

    rejected = await gateway.store_validated(
        request,
        replace(raw, text="invalid"),
    )
    assert rejected is False
    assert cache.put_calls == 1


def test_gateway_exposes_wrapped_provider_and_model_epoch_with_safe_defaults() -> None:
    default_gateway = LLMGateway(_ScriptedAdapter())
    assert default_gateway.provider == "openai-compatible"
    assert default_gateway.model_epoch == default_gateway.model

    class _VersionedAdapter(_ScriptedAdapter):
        provider = "private-vllm"
        model_epoch = "weights-2026-07-25"

    versioned = LLMGateway(_VersionedAdapter())
    assert versioned.provider == "private-vllm"
    assert versioned.model_epoch == "weights-2026-07-25"

    with pytest.raises(AttributeError):
        versioned.provider = "mutated"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        versioned.model_epoch = "mutated"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_public_lookup_uses_semantic_duck_without_acquire_or_provider() -> None:
    class _LookupCache(_MemoryCache):
        def __init__(self) -> None:
            super().__init__()
            self.lookup_calls = 0
            self.acquire_calls = 0

        async def lookup(
            self,
            identity: LLMCacheIdentity,
            *,
            request: LLMRequest,
            model: str,
        ) -> CachedLLMValue:
            del request
            self.lookup_calls += 1
            return CachedLLMValue(
                text="semantic",
                model=model,
                prompt_hash=identity.recipe_sha256,
                cache_source="mysql_semantic",
            )

        async def acquire(self, *args: Any, **kwargs: Any) -> object:
            del args, kwargs
            self.acquire_calls += 1
            raise AssertionError("lookup must not acquire a lease")

    cache = _LookupCache()
    adapter = _ScriptedAdapter()
    gateway = LLMGateway(adapter, cache=cache, retry_base_seconds=0)

    hit = await gateway.lookup_validated(_request())

    assert hit is not None and hit.cache_source == "mysql_semantic"
    assert cache.lookup_calls == 1
    assert cache.acquire_calls == 0
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_public_lookup_cache_error_only_fails_closed_for_durable_ledger() -> None:
    class _BrokenLookupCache(_MemoryCache):
        async def lookup(
            self,
            identity: LLMCacheIdentity,
            *,
            request: LLMRequest,
            model: str,
        ) -> CachedLLMValue:
            del identity, request, model
            raise RuntimeError("cache unavailable")

    gateway = LLMGateway(
        _ScriptedAdapter(),
        cache=_BrokenLookupCache(),
        retry_base_seconds=0,
    )

    assert await gateway.lookup_validated(_request()) is None
    with pytest.raises(RuntimeError, match="cache unavailable"):
        await gateway.lookup_validated(
            _request(
                usage_context=LLMUsageContext(require_durable_ledger=True),
            )
        )


@pytest.mark.asyncio
async def test_public_lookup_revalidates_and_evicts_bad_value() -> None:
    cache = _MemoryCache()
    adapter = _ScriptedAdapter()
    gateway = LLMGateway(adapter, cache=cache, retry_base_seconds=0)
    request = _request(response_validator=lambda response: response.text == "valid")
    identity = LLMCacheIdentity(
        request.tenant_id,
        request.purpose,
        request.recipe_sha256(model=gateway.model),
    )
    cache.values[identity] = CachedLLMValue(
        "invalid",
        gateway.model,
        identity.recipe_sha256,
    )

    assert await gateway.lookup_validated(request) is None
    assert cache.delete_calls == 1
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_public_store_prefers_durable_execution_cache_contract() -> None:
    class _DurableStoreCache(_MemoryCache):
        def __init__(self) -> None:
            super().__init__()
            self.store_calls: list[tuple[LLMCacheIdentity, LLMRequest, str]] = []

        async def store(
            self,
            identity: LLMCacheIdentity,
            value: CachedLLMValue,
            *,
            request: LLMRequest,
            model: str,
        ) -> bool:
            self.store_calls.append((identity, request, model))
            self.values[identity] = value
            return True

    cache = _DurableStoreCache()
    gateway = LLMGateway(_ScriptedAdapter(), cache=cache, retry_base_seconds=0)
    request = _request()

    stored = await gateway.store_validated(
        request,
        LLMResponse("batch-result", "batch-model", "batch-hash"),
    )

    assert stored
    assert len(cache.store_calls) == 1
    assert cache.store_calls[0][1] is request
    assert cache.store_calls[0][2] == gateway.model
    assert cache.put_calls == 0


@pytest.mark.asyncio
async def test_cache_helpers_are_safe_for_legacy_adapters_and_cache_faults() -> None:
    request = _request()
    response = LLMResponse("ok", _ScriptedAdapter.model, "raw")
    legacy = _ScriptedAdapter()

    assert await lookup_llm_cache(legacy, request) is None
    assert not await store_validated_llm_cache(legacy, request, response)

    class _FaultyGateway:
        async def lookup_validated(self, request: LLMRequest) -> LLMResponse | None:
            del request
            raise OSError("cache unavailable")

        async def store_validated(
            self,
            request: LLMRequest,
            response: LLMResponse,
        ) -> bool:
            del request, response
            raise OSError("cache unavailable")

    faulty = _FaultyGateway()
    assert await lookup_llm_cache(faulty, request) is None  # type: ignore[arg-type]
    assert not await store_validated_llm_cache(  # type: ignore[arg-type]
        faulty,
        request,
        response,
    )
