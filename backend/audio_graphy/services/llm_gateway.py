"""Centralized LLM execution primitives.

The gateway deliberately keeps the existing ``LLMAdapter.complete`` surface
available while adding a richer, immutable request contract for production
callers.  Cache storage is injected behind a small protocol so the execution
pipeline does not depend on Redis, MySQL, or a process-local implementation.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import time
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Final, Literal, Protocol, cast, runtime_checkable

from audio_graphy.adapters.exceptions import (
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
)
from audio_graphy.adapters.protocols import LLMAdapter, LLMResponse

logger = logging.getLogger(__name__)

RecipeVersion = Literal["llm-recipe-v1", "llm-recipe-v2"]
RecipeMigrationMode = Literal["shadow", "dual_read", "v2"]
_RECIPE_VERSION_V1: Final[RecipeVersion] = "llm-recipe-v1"
_RECIPE_VERSION_V2: Final[RecipeVersion] = "llm-recipe-v2"
_TRANSIENT_ERRORS = (LLMRateLimitError, LLMServerError, LLMTimeoutError)


class CachePolicy(StrEnum):
    """Result-cache policy for one logical LLM request."""

    BYPASS = "bypass"
    EXACT = "exact"
    QUERY_SEMANTIC = "query_semantic"


@dataclass(frozen=True, slots=True)
class LLMProvenance:
    """A source reference used for retention and DSAR invalidation."""

    source_type: str
    source_id: str

    def __post_init__(self) -> None:
        if not self.source_type or not self.source_id:
            raise ValueError("LLM provenance source_type/source_id must not be empty")

    def as_mapping(self) -> dict[str, str]:
        return {"source_type": self.source_type, "source_id": self.source_id}


@dataclass(frozen=True, slots=True)
class LLMCacheIdentity:
    """Tenant-scoped identity used by any gateway cache implementation."""

    tenant_id: str
    namespace: str
    recipe_sha256: str


@dataclass(frozen=True, slots=True)
class CachedLLMValue:
    """Storage-neutral representation of a validated cached response."""

    text: str
    model: str
    prompt_hash: str
    usage: Mapping[str, int] = field(default_factory=dict)
    cache_source: str = "cache"


@dataclass(frozen=True, slots=True)
class LLMPriceSnapshot:
    """Immutable provider price card for one model tier.

    Rates are expressed in micro-currency-units per one million tokens. Input
    usage reported by providers normally includes cached-prefill tokens, so
    cached prefill is removed from regular input before applying its own rate.
    Every provider attempt is rounded up independently to keep the durable
    ledger conservative and exactly additive across retries.
    """

    version: str
    input_microunits_per_million_tokens: int
    output_microunits_per_million_tokens: int
    cached_prefill_microunits_per_million_tokens: int

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("LLM price snapshot version must not be empty")
        rates = (
            self.input_microunits_per_million_tokens,
            self.output_microunits_per_million_tokens,
            self.cached_prefill_microunits_per_million_tokens,
        )
        if any(
            not isinstance(rate, int) or isinstance(rate, bool) or rate < 0
            for rate in rates
        ):
            raise ValueError("LLM price snapshot rates must be non-negative integers")
        if (
            self.cached_prefill_microunits_per_million_tokens
            > self.input_microunits_per_million_tokens
        ):
            raise ValueError("cached-prefill rate cannot exceed the regular input rate")

    def cost_microunits(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cached_prefill_tokens: int = 0,
    ) -> int:
        """Return the conservatively rounded cost for one provider attempt."""

        token_counts = (input_tokens, output_tokens, cached_prefill_tokens)
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in token_counts
        ):
            raise ValueError("LLM priced token counts must be non-negative integers")
        cached_input = min(input_tokens, cached_prefill_tokens)
        uncached_input = input_tokens - cached_input
        numerator = (
            uncached_input * self.input_microunits_per_million_tokens
            + cached_input * self.cached_prefill_microunits_per_million_tokens
            + output_tokens * self.output_microunits_per_million_tokens
        )
        return (numerator + 999_999) // 1_000_000 if numerator else 0

    def cost_for_usage(self, usage: Mapping[str, int]) -> int:
        """Price a provider-native usage mapping without trusting total_tokens."""

        return self.cost_microunits(
            input_tokens=_usage_token_value(usage, "prompt_tokens", "input_tokens"),
            output_tokens=_usage_token_value(
                usage,
                "completion_tokens",
                "output_tokens",
            ),
            cached_prefill_tokens=_usage_token_value(
                usage,
                "cached_prefill_tokens",
                "cached_prompt_tokens",
                "cache_read_input_tokens",
                "cached_tokens",
            ),
        )


_REQUEST_MEMO: ContextVar[dict[LLMCacheIdentity, LLMResponse] | None] = ContextVar(
    "audio_graphy_llm_request_memo",
    default=None,
)


@dataclass(frozen=True, slots=True)
class LLMUsageContext:
    """Correlation dimensions for the usage ledger.

    These values describe the business operation that caused generation. They
    are intentionally excluded from the generation recipe hash so attaching
    observability never invalidates an otherwise reusable model result.
    """

    logical_request_id: str | None = None
    tagger_version_id: int | None = None
    deployment_id: int | None = None
    evaluation_run_id: int | None = None
    optimization_run_id: int | None = None
    optimization_trial_id: int | None = None
    require_durable_ledger: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.require_durable_ledger, bool):
            raise ValueError("require_durable_ledger must be boolean")
        if self.logical_request_id is not None and not (
            1 <= len(self.logical_request_id) <= 64
        ):
            raise ValueError("logical_request_id must contain 1 to 64 characters")
        identifiers = (
            self.tagger_version_id,
            self.deployment_id,
            self.evaluation_run_id,
            self.optimization_run_id,
            self.optimization_trial_id,
        )
        if any(
            value is not None
            and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 1
            )
            for value in identifiers
        ):
            raise ValueError("LLM usage context identifiers must be positive integers")


@dataclass(frozen=True, slots=True)
class _LLMExecutionCorrelation:
    """Stable IDs scoped to one ``LLMGateway.execute`` invocation."""

    logical_request_id: str
    execution_id: str


_EXECUTION_CORRELATION: ContextVar[_LLMExecutionCorrelation | None] = ContextVar(
    "audio_graphy_llm_execution_correlation",
    default=None,
)


@contextmanager
def llm_request_memo_scope() -> Iterator[None]:
    """Create one bounded-lifetime memo for a logical business request.

    The memo only holds validated responses and is discarded when the request
    scope exits. It deliberately has no process-global fallback: cross-request
    reuse belongs to the bounded hot cache and MySQL persistence layers.
    """

    token = _REQUEST_MEMO.set({})
    try:
        yield
    finally:
        _REQUEST_MEMO.reset(token)


@dataclass(frozen=True, slots=True)
class LLMObservation:
    """One low-cardinality gateway lifecycle event."""

    kind: Literal["logical_request", "provider_attempt"]
    outcome: Literal["success", "error", "cancelled"]
    tenant_id: str
    purpose: str
    model: str
    recipe_sha256: str
    elapsed_seconds: float
    recipe_version: RecipeVersion = _RECIPE_VERSION_V2
    shadow_recipe_sha256: str | None = None
    shadow_cache_hit: bool | None = None
    cache_source: str | None = None
    provider_called: bool | None = None
    attempt: int | None = None
    error_type: str | None = None
    usage: Mapping[str, int] = field(default_factory=dict)
    requested_max_tokens: int | None = None
    finish_reason: str | None = None
    provider_request_id: str | None = None
    billed_usage_known: bool | None = None
    unknown_billed: bool | None = None
    cost_microunits: int = 0
    price_version: str | None = None
    logical_request_id: str | None = None
    provider_attempt_id: str | None = None
    tagger_version_id: int | None = None
    deployment_id: int | None = None
    evaluation_run_id: int | None = None
    optimization_run_id: int | None = None
    optimization_trial_id: int | None = None
    ledger_required: bool = False


@runtime_checkable
class LLMObserver(Protocol):
    """Observer object accepted by :class:`LLMGateway`."""

    def observe(self, event: LLMObservation) -> Awaitable[None] | None: ...


LLMObserverCallback = Callable[[LLMObservation], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class _ExecutionCacheBypass:
    state: Literal["bypass"] = "bypass"


@runtime_checkable
class LLMCache(Protocol):
    """Abstract exact-cache interface consumed by :class:`LLMGateway`.

    A composite implementation may internally perform local/Redis hot-cache
    lookup followed by MySQL lookup and lease coordination.  Cache failures
    must remain non-fatal to the model call path.
    """

    async def get(self, identity: LLMCacheIdentity) -> CachedLLMValue | None: ...

    async def put(
        self,
        identity: LLMCacheIdentity,
        value: CachedLLMValue,
        *,
        ttl_seconds: int,
        provenance: Sequence[Mapping[str, str]] = (),
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """Complete logical recipe for one LLM request.

    ``cache_policy`` and ``ttl_seconds`` control reuse but intentionally do not
    participate in the recipe hash: changing where/how long an identical
    result is cached must not change the result identity.
    """

    tenant_id: str
    purpose: str
    messages: Sequence[Mapping[str, Any]]
    model_tier: str = "unspecified"
    provider: str = "openai-compatible"
    model_epoch: str = ""
    prompt_version: str = ""
    schema_version: str = ""
    parser_version: str = ""
    postprocessor_version: str = ""
    temperature: float = 0.0
    top_p: float = 1.0
    max_tokens: int | None = None
    seed: int | None = None
    stop: Sequence[str] = ()
    tools: Sequence[Mapping[str, Any]] = ()
    response_format: Mapping[str, Any] | None = None
    response_schema: Mapping[str, Any] | None = None
    recipe_version: RecipeVersion = _RECIPE_VERSION_V2
    business_snapshot: Any = field(default_factory=dict)
    permission_scope: Mapping[str, Any] = field(default_factory=dict)
    semantic_text: str | None = None
    semantic_language: str | None = None
    semantic_protected_values: Sequence[str] = ()
    provenance: Sequence[LLMProvenance | Mapping[str, str]] = ()
    cache_policy: CachePolicy = CachePolicy.EXACT
    ttl_seconds: int = 7 * 24 * 60 * 60
    usage_context: LLMUsageContext = field(
        default_factory=LLMUsageContext,
        compare=False,
        hash=False,
        repr=False,
    )
    response_validator: Callable[[LLMResponse], bool] | None = field(
        default=None,
        compare=False,
        hash=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("tenant_id must not be empty")
        if not self.purpose:
            raise ValueError("purpose must not be empty")
        if not self.model_tier:
            raise ValueError("model_tier must not be empty")
        if not math.isfinite(self.temperature) or self.temperature < 0:
            raise ValueError("temperature must be finite and non-negative")
        if not math.isfinite(self.top_p) or not 0 < self.top_p <= 1:
            raise ValueError("top_p must be finite and in (0, 1]")
        if self.max_tokens is not None and self.max_tokens < 1:
            raise ValueError("max_tokens must be positive when supplied")
        if self.ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        if (self.semantic_text is None) != (self.semantic_language is None):
            raise ValueError("semantic_text and semantic_language must be supplied together")
        if self.semantic_text is not None and not self.semantic_text.strip():
            raise ValueError("semantic_text must not be blank")
        if self.semantic_language is not None and not (1 <= len(self.semantic_language) <= 16):
            raise ValueError("semantic_language must contain 1 to 16 characters")
        if len(self.semantic_protected_values) > 64:
            raise ValueError("semantic_protected_values cannot exceed 64 items")
        if len(self.provenance) > 64:
            raise ValueError("provenance cannot exceed 64 items")
        if self.recipe_version not in {_RECIPE_VERSION_V1, _RECIPE_VERSION_V2}:
            raise ValueError("recipe_version must be llm-recipe-v1 or llm-recipe-v2")
        for protected_value in self.semantic_protected_values:
            if not isinstance(protected_value, str) or not (1 <= len(protected_value) <= 256):
                raise ValueError("semantic protected values must contain 1 to 256 characters")

    @property
    def cacheable(self) -> bool:
        """Whether deterministic result reuse is allowed."""

        if self.cache_policy is CachePolicy.BYPASS:
            return False
        return self.temperature == 0 or self.seed is not None

    def recipe_sha256(
        self,
        *,
        model: str,
        version: RecipeVersion | None = None,
    ) -> str:
        """Return a canonical generation identity for the selected recipe version."""

        selected = version or self.recipe_version
        if selected == _RECIPE_VERSION_V1:
            return canonical_sha256(self._v1_recipe_payload(model=model))
        if selected != _RECIPE_VERSION_V2:
            raise ValueError("unsupported LLM recipe version")
        payload = {
            "version": _RECIPE_VERSION_V2,
            "tenant_id": self.tenant_id,
            "provider": self.provider,
            "model": model,
            "model_epoch": self.model_epoch,
            "messages": self.messages,
            "parser_version": self.parser_version,
            "postprocessor_version": self.postprocessor_version,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "seed": self.seed,
            "stop": self.stop,
            "tools": self.tools,
            "response_format": self.response_format,
            "response_schema": self.response_schema,
            "permission_scope": self.permission_scope,
        }
        return canonical_sha256(payload)

    def _v1_recipe_payload(self, *, model: str) -> dict[str, Any]:
        """Reproduce the original recipe byte-for-byte for migration reads."""

        return {
            "version": _RECIPE_VERSION_V1,
            "tenant_id": self.tenant_id,
            "purpose": self.purpose,
            "provider": self.provider,
            "model_tier": self.model_tier,
            "model": model,
            "model_epoch": self.model_epoch,
            "messages": self.messages,
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "parser_version": self.parser_version,
            "postprocessor_version": self.postprocessor_version,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "seed": self.seed,
            "stop": self.stop,
            "tools": self.tools,
            "response_format": self.response_format,
            "response_schema": self.response_schema,
            "business_snapshot": self.business_snapshot,
            "permission_scope": self.permission_scope,
            "semantic_text": self.semantic_text,
            "semantic_language": self.semantic_language,
            "semantic_protected_values": self.semantic_protected_values,
            "provenance": self.provenance,
        }


def _canonicalize(value: Any) -> Any:
    """Convert supported recipe values to stable JSON-compatible values."""

    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("recipe contains a non-finite float")
        return value
    if isinstance(value, str):
        normalized_newlines = value.replace("\r\n", "\n").replace("\r", "\n")
        return unicodedata.normalize("NFC", normalized_newlines)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, LLMProvenance):
        return value.as_mapping()
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("recipe mapping keys must be strings")
            result[_canonicalize(key)] = _canonicalize(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, memoryview)):
        return [_canonicalize(item) for item in value]
    raise TypeError(f"unsupported recipe value type: {type(value).__name__}")


def canonical_sha256(value: Any) -> str:
    """Hash canonical JSON without persisting its potentially sensitive input."""

    canonical = json.dumps(
        _canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class LLMGateway:
    """Cache-aware, retry-bounded, process-singleflight LLM client."""

    model: str

    def __init__(
        self,
        adapter: LLMAdapter,
        *,
        cache: LLMCache | None = None,
        model_tier: str = "strong",
        max_retries: int = 2,
        retry_base_seconds: float = 0.1,
        max_concurrency: int = 0,
        observer: LLMObserver | LLMObserverCallback | None = None,
        recipe_migration_mode: RecipeMigrationMode = "dual_read",
        price_snapshot: LLMPriceSnapshot | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if not math.isfinite(retry_base_seconds) or retry_base_seconds < 0:
            raise ValueError("retry_base_seconds must be finite and non-negative")
        if recipe_migration_mode not in {"shadow", "v2", "dual_read"}:
            raise ValueError("recipe_migration_mode must be shadow, dual_read, or v2")
        self.model = adapter.model
        provider = getattr(adapter, "provider", "openai-compatible")
        self._provider = provider if isinstance(provider, str) and provider else "openai-compatible"
        model_epoch = getattr(adapter, "model_epoch", self.model)
        self._model_epoch = (
            model_epoch if isinstance(model_epoch, str) and model_epoch else self.model
        )
        self._adapter = adapter
        self._cache = cache
        self._model_tier = model_tier
        self._max_retries = max_retries
        self._retry_base_seconds = retry_base_seconds
        self._observer = observer
        self._recipe_migration_mode = recipe_migration_mode
        self._price_snapshot = price_snapshot
        self._provider_semaphore = (
            asyncio.Semaphore(max_concurrency) if max_concurrency > 0 else None
        )
        self._inflight: dict[LLMCacheIdentity, asyncio.Task[LLMResponse]] = {}
        self._inflight_lock = asyncio.Lock()

    @property
    def inflight_count(self) -> int:
        """Number of provider/cache-fill tasks currently shared by callers."""

        return len(self._inflight)

    @property
    def provider(self) -> str:
        """Read-only provider identity of the wrapped transport."""

        return self._provider

    @property
    def model_epoch(self) -> str:
        """Read-only model epoch of the wrapped transport."""

        return self._model_epoch

    @property
    def price_snapshot(self) -> LLMPriceSnapshot | None:
        """Immutable price card used for provider-attempt settlement."""

        return self._price_snapshot

    @property
    def max_provider_attempts(self) -> int:
        """Maximum attempts one logical request may spend, including retries."""

        return self._max_retries + 1

    def estimate_cost_microunits(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cached_prefill_tokens: int = 0,
    ) -> int:
        """Price a planned call, failing closed when no snapshot is configured."""

        if self._price_snapshot is None:
            raise RuntimeError("LLM price snapshot is not configured")
        return self._price_snapshot.cost_microunits(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_prefill_tokens=cached_prefill_tokens,
        )

    def price_usage_cost_microunits(self, usage: Mapping[str, int]) -> int:
        """Price one usage record for cold-cache counterfactual reporting."""

        if self._price_snapshot is None:
            raise RuntimeError("LLM price snapshot is not configured")
        return self._price_snapshot.cost_for_usage(usage)

    async def execute(self, request: LLMRequest) -> LLMResponse:
        """Execute one logical request through cache, singleflight, and retry."""

        request = _request_with_logical_request_id(request)
        usage_context = request.usage_context
        assert usage_context.logical_request_id is not None
        correlation_token = _EXECUTION_CORRELATION.set(
            _LLMExecutionCorrelation(
                logical_request_id=usage_context.logical_request_id,
                execution_id=uuid.uuid4().hex,
            )
        )
        started_at = time.perf_counter()
        recipe_sha256 = ""
        serving_version = self._serving_recipe_version(request)
        shadow_recipe_sha256 = self._shadow_recipe_sha256(request)
        shadow_cache_hit: bool | None = None
        try:
            recipe_sha256 = request.recipe_sha256(
                model=self.model,
                version=serving_version,
            )
            response = await self._execute(request, recipe_sha256)
            self._request_memo_put(request, recipe_sha256, response)
            if shadow_recipe_sha256 is not None:
                shadow_cache_hit = await self._shadow_v2_cache_hit(
                    request,
                    shadow_recipe_sha256,
                )
                if (
                    not shadow_cache_hit
                    and self._response_is_valid(request, response)
                ):
                    await self._promote_v1_cache_value(
                        LLMCacheIdentity(
                            tenant_id=request.tenant_id,
                            namespace=request.purpose,
                            recipe_sha256=shadow_recipe_sha256,
                        ),
                        request,
                        replace(
                            response,
                            prompt_hash=shadow_recipe_sha256,
                        ),
                    )
        except BaseException as exc:
            await self._observe(
                LLMObservation(
                    kind="logical_request",
                    outcome=("cancelled" if isinstance(exc, asyncio.CancelledError) else "error"),
                    tenant_id=request.tenant_id,
                    purpose=request.purpose,
                    model=self.model,
                    recipe_sha256=recipe_sha256,
                    elapsed_seconds=time.perf_counter() - started_at,
                    recipe_version=serving_version,
                    shadow_recipe_sha256=shadow_recipe_sha256,
                    shadow_cache_hit=shadow_cache_hit,
                    error_type=type(exc).__name__,
                    **_usage_observation_fields(request),
                )
            )
            raise
        else:
            await self._observe(
                LLMObservation(
                    kind="logical_request",
                    outcome="success",
                    tenant_id=request.tenant_id,
                    purpose=request.purpose,
                    model=self.model,
                    recipe_sha256=recipe_sha256,
                    elapsed_seconds=time.perf_counter() - started_at,
                    recipe_version=serving_version,
                    shadow_recipe_sha256=shadow_recipe_sha256,
                    shadow_cache_hit=shadow_cache_hit,
                    cache_source=response.cache_source,
                    provider_called=response.provider_called,
                    usage=dict(response.usage),
                    **_usage_observation_fields(request),
                )
            )
            return response
        finally:
            _EXECUTION_CORRELATION.reset(correlation_token)

    async def lookup_validated(self, request: LLMRequest) -> LLMResponse | None:
        """Read an existing validated cache value without provider or lease work."""

        request = _request_with_logical_request_id(request)
        started_at = time.perf_counter()
        if not request.cacheable:
            return None
        recipe_sha256 = request.recipe_sha256(
            model=self.model,
            version=self._serving_recipe_version(request),
        )
        identity = LLMCacheIdentity(
            tenant_id=request.tenant_id,
            namespace=request.purpose,
            recipe_sha256=recipe_sha256,
        )
        memoized = self._request_memo_response(identity, request, recipe_sha256)
        if memoized is not None:
            await self._observe_lookup_hit(
                request,
                recipe_sha256,
                memoized,
                started_at,
            )
            return memoized
        if self._cache is None:
            return None
        lookup = getattr(self._cache, "lookup", None)
        if not callable(lookup):
            response = await self._validated_cache_response(
                identity,
                request,
                recipe_sha256,
            )
            if response is not None:
                await self._observe_lookup_hit(
                    request,
                    recipe_sha256,
                    response,
                    started_at,
                )
            return response
        try:
            cached = await lookup(
                identity,
                request=request,
                model=self.model,
            )
        except Exception:
            if request.usage_context.require_durable_ledger:
                raise
            logger.warning(
                "LLM cache readonly lookup failed open namespace=%s hash=%s",
                identity.namespace,
                identity.recipe_sha256[:12],
                exc_info=True,
            )
            return None
        if not isinstance(cached, CachedLLMValue):
            return await self._validated_v1_cache_response(
                identity,
                request,
                recipe_sha256,
            )
        response = _response_from_cache(cached, recipe_sha256)
        if self._response_is_valid(request, response):
            await self._observe_lookup_hit(
                request,
                recipe_sha256,
                response,
                started_at,
            )
            return response
        await self._cache_delete(identity)
        return await self._validated_v1_cache_response(
            identity,
            request,
            recipe_sha256,
        )

    async def _observe_lookup_hit(
        self,
        request: LLMRequest,
        recipe_sha256: str,
        response: LLMResponse,
        started_at: float,
    ) -> None:
        """Count an external read-only hit as one completed logical call."""

        await self._observe(
            LLMObservation(
                kind="logical_request",
                outcome="success",
                tenant_id=request.tenant_id,
                purpose=request.purpose,
                model=self.model,
                recipe_sha256=recipe_sha256,
                elapsed_seconds=time.perf_counter() - started_at,
                recipe_version=self._serving_recipe_version(request),
                shadow_recipe_sha256=self._shadow_recipe_sha256(request),
                cache_source=response.cache_source,
                provider_called=False,
                usage=dict(response.usage),
                **_usage_observation_fields(request),
            )
        )

    async def store_validated(
        self,
        request: LLMRequest,
        response: LLMResponse,
    ) -> bool:
        """Validate and store one externally-produced result via durable CAS."""

        if not request.cacheable:
            return False
        recipe_sha256 = request.recipe_sha256(
            model=self.model,
            version=self._serving_recipe_version(request),
        )
        identity = LLMCacheIdentity(
            tenant_id=request.tenant_id,
            namespace=request.purpose,
            recipe_sha256=recipe_sha256,
        )
        normalized = replace(
            response,
            model=self.model,
            prompt_hash=recipe_sha256,
            cached=False,
            cache_source="provider",
            provider_called=True,
        )
        if not self._response_is_valid(request, normalized):
            return False
        if self._cache is None:
            self._request_memo_put(request, recipe_sha256, normalized)
            return False
        store = getattr(self._cache, "store", None)
        if not callable(store):
            stored = await self._cache_put(identity, request, normalized)
            if stored:
                self._request_memo_put(request, recipe_sha256, normalized)
            return stored
        try:
            stored = bool(
                await store(
                    identity,
                    _cached_value(normalized),
                    request=request,
                    model=self.model,
                )
            )
            if stored:
                self._request_memo_put(request, recipe_sha256, normalized)
            return stored
        except Exception:
            logger.warning(
                "LLM validated cache store failed open namespace=%s hash=%s",
                identity.namespace,
                identity.recipe_sha256[:12],
                exc_info=True,
            )
            return False

    async def _execute(
        self,
        request: LLMRequest,
        recipe_sha256: str,
    ) -> LLMResponse:
        identity = LLMCacheIdentity(
            tenant_id=request.tenant_id,
            namespace=request.purpose,
            recipe_sha256=recipe_sha256,
        )
        if not request.cacheable:
            return await self._call_provider_with_retry(request, recipe_sha256)

        memoized = self._request_memo_response(identity, request, recipe_sha256)
        if memoized is not None:
            return memoized

        cached_response = await self._validated_cache_response(identity, request, recipe_sha256)
        if cached_response is not None:
            return cached_response

        async with self._inflight_lock:
            task = self._inflight.get(identity)
            is_leader = task is None
            if task is None:
                task = asyncio.create_task(
                    self._execute_leader(identity, request, recipe_sha256),
                    name=f"llm:{request.purpose}:{recipe_sha256[:12]}",
                )
                self._inflight[identity] = task
        result = await asyncio.shield(task)
        if is_leader:
            return result
        return replace(
            result,
            cached=True,
            cache_source="singleflight",
            provider_called=False,
            provider_request_id=None,
            provider_attempts=0,
            unknown_billed_tokens=0,
        )

    async def _execute_leader(
        self,
        identity: LLMCacheIdentity,
        request: LLMRequest,
        recipe_sha256: str,
    ) -> LLMResponse:
        try:
            # Close the race where another process populated persistence after
            # this process's initial miss but before it won local singleflight.
            cached_response = await self._validated_cache_response(
                identity,
                request,
                recipe_sha256,
            )
            if cached_response is not None:
                return cached_response

            claim = await self._cache_acquire(identity, request)
            if claim is not None:
                return await self._execute_claim(
                    identity,
                    request,
                    recipe_sha256,
                    claim,
                )

            response = await self._call_provider_with_retry(request, recipe_sha256)
            if self._response_is_valid(request, response):
                await self._cache_put(identity, request, response)
            else:
                logger.warning(
                    "LLM provider response failed validation; not caching namespace=%s hash=%s",
                    identity.namespace,
                    identity.recipe_sha256[:12],
                )
            return response
        finally:
            current = asyncio.current_task()
            async with self._inflight_lock:
                if self._inflight.get(identity) is current:
                    self._inflight.pop(identity, None)

    async def _cache_get(
        self,
        identity: LLMCacheIdentity,
        request: LLMRequest,
    ) -> CachedLLMValue | None:
        if self._cache is None:
            return None
        try:
            get = self._cache.get
            try:
                return await get(identity, request=request)  # type: ignore[call-arg]
            except TypeError as exc:
                if not _is_unexpected_keyword_error(exc, "request"):
                    raise
                return await get(identity)
        except Exception:
            logger.warning(
                "LLM cache read failed; treating as miss namespace=%s hash=%s",
                identity.namespace,
                identity.recipe_sha256[:12],
                exc_info=True,
            )
            return None

    async def _validated_cache_response(
        self,
        identity: LLMCacheIdentity,
        request: LLMRequest,
        recipe_sha256: str,
    ) -> LLMResponse | None:
        cached = await self._cache_get(identity, request)
        if cached is None:
            return await self._validated_v1_cache_response(
                identity,
                request,
                recipe_sha256,
            )
        response = _response_from_cache(cached, recipe_sha256)
        if self._response_is_valid(request, response):
            return response
        logger.warning(
            "LLM cached response failed validation; evicting namespace=%s hash=%s",
            identity.namespace,
            identity.recipe_sha256[:12],
        )
        await self._cache_delete(identity)
        return await self._validated_v1_cache_response(
            identity,
            request,
            recipe_sha256,
        )

    async def _validated_v1_cache_response(
        self,
        primary_identity: LLMCacheIdentity,
        request: LLMRequest,
        primary_recipe_sha256: str,
    ) -> LLMResponse | None:
        """Dual-read a legacy v1 entry and best-effort promote it to v2.

        Reading through ``_cache_get`` passes the current request to durable
        caches, which attaches any newly observed provenance references before
        reuse. Promotion also writes the full current provenance set, so
        removing provenance from the generation identity does not weaken DSAR.
        """

        if (
            self._recipe_migration_mode != "dual_read"
            or request.recipe_version != _RECIPE_VERSION_V2
            or self._cache is None
        ):
            return None
        legacy_recipe_sha256 = request.recipe_sha256(
            model=self.model,
            version=_RECIPE_VERSION_V1,
        )
        if legacy_recipe_sha256 == primary_recipe_sha256:
            return None
        legacy_identity = LLMCacheIdentity(
            tenant_id=request.tenant_id,
            namespace=request.purpose,
            recipe_sha256=legacy_recipe_sha256,
        )
        cached = await self._cache_get(legacy_identity, request)
        if cached is None:
            return None
        response = replace(
            _response_from_cache(cached, primary_recipe_sha256),
            cache_source=f"{cached.cache_source}_v1",
        )
        if not self._response_is_valid(request, response):
            logger.warning(
                "Legacy v1 LLM cached response failed validation; evicting "
                "namespace=%s hash=%s",
                legacy_identity.namespace,
                legacy_identity.recipe_sha256[:12],
            )
            await self._cache_delete(legacy_identity)
            return None
        await self._promote_v1_cache_value(primary_identity, request, response)
        return response

    async def _promote_v1_cache_value(
        self,
        identity: LLMCacheIdentity,
        request: LLMRequest,
        response: LLMResponse,
    ) -> bool:
        """Write a validated v1 hit through the richest available v2 store."""

        if self._cache is None:
            return False
        store = getattr(self._cache, "store", None)
        if not callable(store):
            return await self._cache_put(identity, request, response)
        try:
            return bool(
                await store(
                    identity,
                    _cached_value(response),
                    request=request,
                    model=self.model,
                )
            )
        except Exception:
            logger.warning(
                "LLM v1-to-v2 cache promotion failed open namespace=%s hash=%s",
                identity.namespace,
                identity.recipe_sha256[:12],
                exc_info=True,
            )
            return False

    def _serving_recipe_version(self, request: LLMRequest) -> RecipeVersion:
        if (
            self._recipe_migration_mode == "shadow"
            and request.recipe_version == _RECIPE_VERSION_V2
        ):
            return _RECIPE_VERSION_V1
        return request.recipe_version

    def _shadow_recipe_sha256(self, request: LLMRequest) -> str | None:
        if (
            self._recipe_migration_mode != "shadow"
            or request.recipe_version != _RECIPE_VERSION_V2
        ):
            return None
        return request.recipe_sha256(
            model=self.model,
            version=_RECIPE_VERSION_V2,
        )

    async def _shadow_v2_cache_hit(
        self,
        request: LLMRequest,
        recipe_sha256: str,
    ) -> bool:
        """Probe v2 read-only while v1 remains authoritative in shadow mode."""

        identity = LLMCacheIdentity(
            tenant_id=request.tenant_id,
            namespace=request.purpose,
            recipe_sha256=recipe_sha256,
        )
        cached = await self._cache_get(identity, request)
        if cached is None:
            return False
        response = _response_from_cache(cached, recipe_sha256)
        if self._response_is_valid(request, response):
            return True
        await self._cache_delete(identity)
        return False

    def _response_is_valid(self, request: LLMRequest, response: LLMResponse) -> bool:
        validator = request.response_validator
        if validator is None:
            return True
        try:
            return bool(validator(response))
        except Exception:
            logger.warning(
                "LLM response validator raised purpose=%s hash=%s",
                request.purpose,
                response.prompt_hash[:12],
                exc_info=True,
            )
            return False

    def _request_memo_response(
        self,
        identity: LLMCacheIdentity,
        request: LLMRequest,
        recipe_sha256: str,
    ) -> LLMResponse | None:
        memo = _REQUEST_MEMO.get()
        if memo is None:
            return None
        response = memo.get(identity)
        if response is None:
            return None
        memoized = replace(
            response,
            prompt_hash=recipe_sha256,
            cached=True,
            cache_source="request_memo",
            provider_called=False,
            cost_microunits=0,
            price_version=None,
            provider_request_id=None,
            provider_attempts=0,
            unknown_billed_tokens=0,
        )
        if self._response_is_valid(request, memoized):
            return memoized
        memo.pop(identity, None)
        return None

    def _request_memo_put(
        self,
        request: LLMRequest,
        recipe_sha256: str,
        response: LLMResponse,
    ) -> None:
        memo = _REQUEST_MEMO.get()
        if memo is None or not request.cacheable:
            return
        if not self._response_is_valid(request, response):
            return
        memo[
            LLMCacheIdentity(
                tenant_id=request.tenant_id,
                namespace=request.purpose,
                recipe_sha256=recipe_sha256,
            )
        ] = replace(response, prompt_hash=recipe_sha256)

    async def _cache_acquire(
        self,
        identity: LLMCacheIdentity,
        request: LLMRequest,
    ) -> object | None:
        """Acquire an optional durable execution lease.

        ``None`` means the injected cache only implements ordinary
        ``get``/``put``. An acquire failure becomes an explicit bypass so an
        uncertain lease is never followed by an unsafe ordinary cache write.
        """

        if self._cache is None:
            return None
        acquire = getattr(self._cache, "acquire", None)
        if not callable(acquire):
            return None
        try:
            claim = await acquire(identity, request=request, model=self.model)
        except Exception:
            logger.warning(
                "LLM execution-cache acquire failed; bypassing cache namespace=%s hash=%s",
                identity.namespace,
                identity.recipe_sha256[:12],
                exc_info=True,
            )
            return _ExecutionCacheBypass()
        state = getattr(claim, "state", None)
        if state not in {"ready", "leader", "bypass"}:
            logger.warning(
                "LLM execution-cache returned invalid state=%r; bypassing namespace=%s hash=%s",
                state,
                identity.namespace,
                identity.recipe_sha256[:12],
            )
            return _ExecutionCacheBypass()
        return cast(object, claim)

    async def _execute_claim(
        self,
        identity: LLMCacheIdentity,
        request: LLMRequest,
        recipe_sha256: str,
        claim: object,
    ) -> LLMResponse:
        state = getattr(claim, "state", "bypass")
        if state == "ready":
            value = getattr(claim, "value", None)
            if isinstance(value, CachedLLMValue):
                response = _response_from_cache(value, recipe_sha256)
                if self._response_is_valid(request, response):
                    return response
                logger.warning(
                    "LLM leased cache value failed validation; evicting namespace=%s hash=%s",
                    identity.namespace,
                    identity.recipe_sha256[:12],
                )
                await self._cache_delete(identity)
            else:
                logger.warning(
                    "LLM execution-cache ready claim has no compatible value; "
                    "bypassing namespace=%s hash=%s",
                    identity.namespace,
                    identity.recipe_sha256[:12],
                )
            return await self._call_provider_with_retry(request, recipe_sha256)

        if state == "bypass":
            return await self._call_provider_with_retry(request, recipe_sha256)

        lease_token = getattr(claim, "lease_token", None)
        if not isinstance(lease_token, str) or not lease_token:
            logger.warning(
                "LLM execution-cache leader has no lease token; bypassing namespace=%s hash=%s",
                identity.namespace,
                identity.recipe_sha256[:12],
            )
            return await self._call_provider_with_retry(request, recipe_sha256)

        heartbeat_task = self._start_lease_heartbeat(identity, lease_token)
        try:
            try:
                response = await self._call_provider_with_retry(request, recipe_sha256)
            except BaseException:
                await self._cache_release(identity, lease_token)
                raise

            if not self._response_is_valid(request, response):
                logger.warning(
                    "LLM provider response failed validation; releasing lease namespace=%s hash=%s",
                    identity.namespace,
                    identity.recipe_sha256[:12],
                )
                await self._cache_release(identity, lease_token)
                return response

            try:
                published = await self._cache_publish(
                    identity,
                    lease_token,
                    request,
                    response,
                )
            except asyncio.CancelledError:
                await self._cache_release(identity, lease_token)
                raise
            if not published:
                logger.warning(
                    "LLM execution-cache rejected stale or failed publish namespace=%s hash=%s",
                    identity.namespace,
                    identity.recipe_sha256[:12],
                )
            return response
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task

    def _start_lease_heartbeat(
        self,
        identity: LLMCacheIdentity,
        lease_token: str,
    ) -> asyncio.Task[None] | None:
        cache = self._cache
        if cache is None or not callable(getattr(cache, "renew", None)):
            return None
        interval = getattr(cache, "lease_heartbeat_seconds", None)
        if not isinstance(interval, int | float) or not math.isfinite(interval) or interval <= 0:
            return None
        return asyncio.create_task(
            self._lease_heartbeat_loop(
                identity,
                lease_token,
                float(interval),
            ),
            name=f"llm-lease-heartbeat:{identity.recipe_sha256[:12]}",
        )

    async def _lease_heartbeat_loop(
        self,
        identity: LLMCacheIdentity,
        lease_token: str,
        interval_seconds: float,
    ) -> None:
        assert self._cache is not None
        renew = cast(Any, self._cache).renew
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                renewed = bool(
                    await renew(
                        identity,
                        lease_token=lease_token,
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "LLM lease heartbeat failed namespace=%s hash=%s",
                    identity.namespace,
                    identity.recipe_sha256[:12],
                    exc_info=True,
                )
                continue
            if not renewed:
                logger.warning(
                    "LLM lease heartbeat lost ownership namespace=%s hash=%s",
                    identity.namespace,
                    identity.recipe_sha256[:12],
                )
                return

    async def _cache_publish(
        self,
        identity: LLMCacheIdentity,
        lease_token: str,
        request: LLMRequest,
        response: LLMResponse,
    ) -> bool:
        assert self._cache is not None
        publish = getattr(self._cache, "publish", None)
        if not callable(publish):
            await self._cache_release(identity, lease_token)
            return False
        try:
            return bool(
                await publish(
                    identity,
                    lease_token=lease_token,
                    value=_cached_value(response),
                    request=request,
                    model=self.model,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "LLM execution-cache publish failed; returning provider result "
                "namespace=%s hash=%s",
                identity.namespace,
                identity.recipe_sha256[:12],
                exc_info=True,
            )
            await self._cache_release(identity, lease_token)
            return False

    async def _cache_release(
        self,
        identity: LLMCacheIdentity,
        lease_token: str,
    ) -> None:
        if self._cache is None:
            return
        release = getattr(self._cache, "release", None)
        if not callable(release):
            return
        try:
            await release(identity, lease_token=lease_token)
        except Exception:
            logger.warning(
                "LLM execution-cache lease release failed namespace=%s hash=%s",
                identity.namespace,
                identity.recipe_sha256[:12],
                exc_info=True,
            )

    async def _cache_delete(self, identity: LLMCacheIdentity) -> None:
        if self._cache is None:
            return
        delete = getattr(self._cache, "delete", None)
        if not callable(delete):
            return
        try:
            await delete(identity)
        except Exception:
            logger.warning(
                "LLM invalid-cache eviction failed namespace=%s hash=%s",
                identity.namespace,
                identity.recipe_sha256[:12],
                exc_info=True,
            )

    async def _cache_put(
        self,
        identity: LLMCacheIdentity,
        request: LLMRequest,
        response: LLMResponse,
    ) -> bool:
        if self._cache is None:
            return False
        value = _cached_value(response)
        try:
            return bool(
                await self._cache.put(
                    identity,
                    value,
                    ttl_seconds=request.ttl_seconds,
                    provenance=_provenance_mappings(request.provenance),
                )
            )
        except Exception:
            logger.warning(
                "LLM cache write failed; returning provider result namespace=%s hash=%s",
                identity.namespace,
                identity.recipe_sha256[:12],
                exc_info=True,
            )
            return False

    async def _call_provider_with_retry(
        self,
        request: LLMRequest,
        recipe_sha256: str,
    ) -> LLMResponse:
        settled_cost_microunits = 0
        settled_input_tokens = 0
        settled_output_tokens = 0
        unknown_billed_tokens = 0
        for attempt in range(self._max_retries + 1):
            provider_attempt_id = _provider_attempt_id(
                recipe_sha256=recipe_sha256,
                attempt=attempt + 1,
            )
            started_at = time.perf_counter()
            try:
                if self._provider_semaphore is None:
                    raw = await _invoke_adapter(self._adapter, request, cache_key=None)
                else:
                    async with self._provider_semaphore:
                        raw = await _invoke_adapter(self._adapter, request, cache_key=None)
                response = replace(
                    raw,
                    prompt_hash=recipe_sha256,
                    cached=False,
                    cache_source="provider",
                    provider_called=True,
                )
            except BaseException as exc:
                exception_usage = getattr(exc, "usage", {})
                if not isinstance(exception_usage, Mapping):
                    exception_usage = {}
                billed_usage_known = _exception_bool(exc, "billed_usage_known")
                unknown_billed = _exception_bool(exc, "unknown_billed")
                if unknown_billed is None and isinstance(exc, LLMTimeoutError):
                    unknown_billed = True
                if unknown_billed is True:
                    # At minimum reserve the requested output ceiling. When no
                    # ceiling exists, retain a non-zero sentinel so upstream
                    # optimization and promotion gates fail closed.
                    unknown_billed_tokens += request.max_tokens or 1
                if billed_usage_known is True:
                    settled_input_tokens += _usage_token_value(
                        exception_usage,
                        "prompt_tokens",
                        "input_tokens",
                    )
                    settled_output_tokens += _usage_token_value(
                        exception_usage,
                        "completion_tokens",
                        "output_tokens",
                    )
                attempt_cost_microunits = (
                    self._price_snapshot.cost_for_usage(exception_usage)
                    if self._price_snapshot is not None and billed_usage_known is True
                    else 0
                )
                settled_cost_microunits += attempt_cost_microunits
                await self._observe(
                    LLMObservation(
                        kind="provider_attempt",
                        outcome=(
                            "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
                        ),
                        tenant_id=request.tenant_id,
                        purpose=request.purpose,
                        model=self.model,
                        recipe_sha256=recipe_sha256,
                        elapsed_seconds=time.perf_counter() - started_at,
                        recipe_version=self._serving_recipe_version(request),
                        shadow_recipe_sha256=self._shadow_recipe_sha256(request),
                        cache_source="provider",
                        provider_called=True,
                        attempt=attempt + 1,
                        error_type=type(exc).__name__,
                        usage=dict(exception_usage),
                        requested_max_tokens=request.max_tokens,
                        finish_reason=_exception_text(exc, "finish_reason"),
                        provider_request_id=_exception_text(
                            exc,
                            "provider_request_id",
                        ),
                        billed_usage_known=billed_usage_known,
                        unknown_billed=unknown_billed,
                        cost_microunits=attempt_cost_microunits,
                        price_version=(
                            self._price_snapshot.version
                            if self._price_snapshot is not None
                            and billed_usage_known is True
                            else None
                        ),
                        provider_attempt_id=provider_attempt_id,
                        **_usage_observation_fields(request),
                    )
                )
                if isinstance(exc, _TRANSIENT_ERRORS) and attempt < self._max_retries:
                    delay = self._retry_base_seconds * (2**attempt)
                    if delay:
                        await asyncio.sleep(delay)
                    continue
                raise
            attempt_usage = dict(response.usage)
            attempt_cost_microunits = (
                self._price_snapshot.cost_for_usage(attempt_usage)
                if self._price_snapshot is not None
                else 0
            )
            settled_cost_microunits += attempt_cost_microunits
            settled_input_tokens += _usage_token_value(
                attempt_usage,
                "prompt_tokens",
                "input_tokens",
            )
            settled_output_tokens += _usage_token_value(
                attempt_usage,
                "completion_tokens",
                "output_tokens",
            )
            response = replace(
                response,
                usage={
                    **dict(response.usage),
                    "prompt_tokens": settled_input_tokens,
                    "completion_tokens": settled_output_tokens,
                },
                cost_microunits=settled_cost_microunits,
                price_version=(
                    self._price_snapshot.version
                    if self._price_snapshot is not None
                    else None
                ),
                provider_attempts=attempt + 1,
                unknown_billed_tokens=(
                    unknown_billed_tokens + max(0, response.unknown_billed_tokens)
                ),
            )
            await self._observe(
                LLMObservation(
                    kind="provider_attempt",
                    outcome="success",
                    tenant_id=request.tenant_id,
                    purpose=request.purpose,
                    model=self.model,
                    recipe_sha256=recipe_sha256,
                    elapsed_seconds=time.perf_counter() - started_at,
                    recipe_version=self._serving_recipe_version(request),
                    shadow_recipe_sha256=self._shadow_recipe_sha256(request),
                    cache_source="provider",
                    provider_called=True,
                    attempt=attempt + 1,
                    usage=attempt_usage,
                    requested_max_tokens=request.max_tokens,
                    cost_microunits=attempt_cost_microunits,
                    price_version=(
                        self._price_snapshot.version
                        if self._price_snapshot is not None
                        else None
                    ),
                    provider_request_id=response.provider_request_id,
                    provider_attempt_id=provider_attempt_id,
                    **_usage_observation_fields(request),
                )
            )
            return response
        raise AssertionError("retry loop exhausted without returning or raising")

    async def _observe(self, event: LLMObservation) -> None:
        observer = self._observer
        if observer is None:
            if event.ledger_required:
                raise RuntimeError(
                    "durable LLM usage ledger is required but no observer is configured"
                )
            return
        callback = getattr(observer, "observe", None)
        if not callable(callback):
            callback = observer if callable(observer) else None
        if callback is None:
            if event.ledger_required:
                raise RuntimeError(
                    "durable LLM usage ledger is required but the observer is invalid"
                )
            logger.warning("Ignoring invalid LLM observer %r", observer)
            return
        try:
            result = callback(event)
            if inspect.isawaitable(result):
                await result
        except Exception:
            if event.ledger_required:
                raise
            logger.warning(
                "LLM observer failed open kind=%s purpose=%s",
                event.kind,
                event.purpose,
                exc_info=True,
            )

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
        """Compatibility facade for the existing :class:`LLMAdapter` contract."""

        if cache_key is not None:
            logger.debug(
                "Ignoring caller-provided LLM cache key; canonical recipe is authoritative"
            )
        request = LLMRequest(
            tenant_id="default",
            purpose="legacy",
            model_tier=self._model_tier,
            provider=self.provider,
            model_epoch=self.model_epoch,
            messages=messages,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            seed=seed,
            stop=stop,
            tools=tools,
            response_format=response_format,
            response_schema=response_schema,
        )
        return await self.execute(request)

    async def aclose(self) -> None:
        """Close the wrapped transport if it owns async resources."""

        close = getattr(self._adapter, "aclose", None)
        if callable(close):
            await close()


def _response_from_cache(value: CachedLLMValue, recipe_sha256: str) -> LLMResponse:
    return LLMResponse(
        text=value.text,
        model=value.model,
        prompt_hash=recipe_sha256,
        cached=True,
        usage=dict(value.usage),
        cache_source=value.cache_source,
        provider_called=False,
        provider_request_id=None,
        provider_attempts=0,
        unknown_billed_tokens=0,
    )


def _cached_value(response: LLMResponse) -> CachedLLMValue:
    return CachedLLMValue(
        text=response.text,
        model=response.model,
        prompt_hash=response.prompt_hash,
        usage=dict(response.usage),
    )


def _is_unexpected_keyword_error(exc: TypeError, keyword: str) -> bool:
    message = str(exc)
    return "unexpected keyword argument" in message and f"'{keyword}'" in message


def _exception_text(exc: BaseException, name: str) -> str | None:
    value = getattr(exc, name, None)
    return value if isinstance(value, str) and value else None


def _exception_bool(exc: BaseException, name: str) -> bool | None:
    value = getattr(exc, name, None)
    return value if isinstance(value, bool) else None


def _request_with_logical_request_id(request: LLMRequest) -> LLMRequest:
    """Attach one server-generated logical ID when the caller omitted it."""

    if request.usage_context.logical_request_id is not None:
        return request
    return replace(
        request,
        usage_context=replace(
            request.usage_context,
            logical_request_id=uuid.uuid4().hex,
        ),
    )


def _usage_observation_fields(request: LLMRequest) -> dict[str, Any]:
    context = request.usage_context
    return {
        "logical_request_id": context.logical_request_id,
        "tagger_version_id": context.tagger_version_id,
        "deployment_id": context.deployment_id,
        "evaluation_run_id": context.evaluation_run_id,
        "optimization_run_id": context.optimization_run_id,
        "optimization_trial_id": context.optimization_trial_id,
        "ledger_required": context.require_durable_ledger,
    }


def _provider_attempt_id(*, recipe_sha256: str, attempt: int) -> str:
    """Return a retry-distinct, observer-idempotent attempt identifier."""

    correlation = _EXECUTION_CORRELATION.get()
    if correlation is None:
        # This helper is only reachable under ``execute``. Fail closed rather
        # than writing a nullable attempt that defeats the ledger constraint.
        raise RuntimeError("LLM provider attempt has no execution correlation")
    digest = hashlib.sha256(
        (
            f"{correlation.execution_id}\x1f{correlation.logical_request_id}"
            f"\x1f{recipe_sha256}\x1f{attempt}"
        ).encode()
    ).hexdigest()
    return f"pa_{digest}"


def _usage_token_value(usage: Mapping[str, int], *keys: str) -> int:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


def _provenance_mappings(
    provenance: Sequence[LLMProvenance | Mapping[str, str]],
) -> tuple[Mapping[str, str], ...]:
    return tuple(
        item.as_mapping() if isinstance(item, LLMProvenance) else item for item in provenance
    )


async def execute_llm(adapter: LLMAdapter, request: LLMRequest) -> LLMResponse:
    """Use a rich gateway when available, else preserve the legacy adapter API."""

    execute = getattr(adapter, "execute", None)
    if callable(execute):
        return cast(LLMResponse, await execute(request))
    cache_key = request.recipe_sha256(model=adapter.model) if request.cacheable else None
    return await _invoke_adapter(adapter, request, cache_key=cache_key)


async def lookup_llm_cache(
    adapter: object,
    request: LLMRequest,
) -> LLMResponse | None:
    """Use a gateway's read-only cache API when available."""

    lookup = getattr(adapter, "lookup_validated", None)
    if not callable(lookup):
        return None
    try:
        result = await lookup(request)
    except Exception:
        logger.warning(
            "LLM cache lookup helper failed open purpose=%s",
            request.purpose,
            exc_info=True,
        )
        return None
    return result if isinstance(result, LLMResponse) else None


async def store_validated_llm_cache(
    adapter: object,
    request: LLMRequest,
    response: LLMResponse,
) -> bool:
    """Use a gateway's validated durable-store API when available."""

    store = getattr(adapter, "store_validated", None)
    if not callable(store):
        return False
    try:
        return bool(await store(request, response))
    except Exception:
        logger.warning(
            "LLM cache store helper failed open purpose=%s",
            request.purpose,
            exc_info=True,
        )
        return False


async def _invoke_adapter(
    adapter: LLMAdapter,
    request: LLMRequest,
    *,
    cache_key: str | None,
) -> LLMResponse:
    """Forward supported generation options without breaking legacy fakes."""

    base_kwargs: dict[str, Any] = {
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "cache_key": cache_key,
    }
    extended_kwargs: dict[str, Any] = {
        "top_p": request.top_p,
        "seed": request.seed,
        "stop": request.stop,
        "tools": request.tools,
        "response_format": request.response_format,
        "response_schema": request.response_schema,
    }
    complete = adapter.complete
    try:
        signature = inspect.signature(complete)
    except (TypeError, ValueError):
        kwargs = base_kwargs
    else:
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        candidates = {**base_kwargs, **extended_kwargs}
        kwargs = {
            key: value
            for key, value in candidates.items()
            if accepts_kwargs or key in signature.parameters
        }

    if request.response_schema is not None and "response_schema" not in kwargs:
        raise TypeError(
            f"LLM adapter {type(adapter).__name__} does not declare response_schema capability"
        )

    while True:
        try:
            return await complete(
                messages=request.messages,  # type: ignore[arg-type]
                **kwargs,
            )
        except TypeError as exc:
            rejected_keyword = next(
                (
                    key
                    for key in tuple(kwargs)
                    if _is_unexpected_keyword_error(exc, key)
                ),
                None,
            )
            if rejected_keyword is None:
                raise
            if rejected_keyword == "response_schema" and request.response_schema is not None:
                raise TypeError(
                    f"LLM adapter {type(adapter).__name__} rejected required "
                    "response_schema capability"
                ) from exc
            if rejected_keyword not in extended_kwargs:
                raise
            logger.debug(
                "LLM adapter rejected optional generation kwarg=%s; retrying without it",
                rejected_keyword,
            )
            kwargs.pop(rejected_keyword)


__all__ = [
    "CachePolicy",
    "CachedLLMValue",
    "LLMCache",
    "LLMCacheIdentity",
    "LLMGateway",
    "LLMObservation",
    "LLMObserver",
    "LLMObserverCallback",
    "LLMPriceSnapshot",
    "LLMProvenance",
    "LLMRequest",
    "LLMUsageContext",
    "RecipeMigrationMode",
    "RecipeVersion",
    "canonical_sha256",
    "execute_llm",
    "llm_request_memo_scope",
    "lookup_llm_cache",
    "store_validated_llm_cache",
]
