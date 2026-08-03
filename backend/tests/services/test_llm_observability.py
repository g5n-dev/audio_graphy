"""Prometheus and LLMCallLog integration for gateway observations."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from audio_graphy.adapters.protocols import LLMResponse
from audio_graphy.api import metrics
from audio_graphy.models.llm_call_log import LLMCallLog
from audio_graphy.services.llm_gateway import (
    LLMGateway,
    LLMObservation,
    LLMPriceSnapshot,
    LLMRequest,
)
from audio_graphy.services.llm_observability import LLMCallObserver


@pytest_asyncio.fixture
async def log_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'observability.sqlite3'}")
    async with engine.begin() as connection:
        await connection.run_sync(LLMCallLog.__table__.create)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def test_gateway_observer_writes_provider_and_logical_rows(
    log_factory: async_sessionmaker[AsyncSession],
) -> None:
    actual_input_before = metrics.LLM_TOKENS.labels(
        "weak",
        "keyword_extract",
        "actual",
        "input",
    )._value.get()
    actual_output_before = metrics.LLM_TOKENS.labels(
        "weak",
        "keyword_extract",
        "actual",
        "output",
    )._value.get()
    saved_input_before = metrics.LLM_TOKENS.labels(
        "weak",
        "keyword_extract",
        "saved",
        "input",
    )._value.get()

    class _Adapter:
        model = "weak-v1"

        async def complete(self, messages, **kwargs) -> LLMResponse:
            del messages, kwargs
            return LLMResponse(
                "keywords",
                self.model,
                "transport",
                usage={"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13},
            )

    gateway = LLMGateway(
        _Adapter(),
        model_tier="weak",
        observer=LLMCallObserver(log_factory, model_tier="weak"),
        retry_base_seconds=0,
        price_snapshot=LLMPriceSnapshot(
            version="provider-price-2026-07",
            input_microunits_per_million_tokens=1_000_000,
            output_microunits_per_million_tokens=2_000_000,
            cached_prefill_microunits_per_million_tokens=250_000,
        ),
    )
    response = await gateway.execute(
        LLMRequest(
            tenant_id="tenant-a",
            purpose="keyword_extract",
            model_tier="weak",
            messages=({"role": "user", "content": "hello"},),
            prompt_version="v1",
            parser_version="v1",
        )
    )

    assert response.provider_called
    async with log_factory() as session:
        rows = list((await session.execute(select(LLMCallLog))).scalars())
    assert len(rows) == 2
    provider_row = next(row for row in rows if row.event_kind == "provider_attempt")
    logical_row = next(row for row in rows if row.event_kind == "logical_request")
    assert provider_row.tenant_id == "tenant-a"
    assert provider_row.purpose == "keyword_extract"
    assert provider_row.prompt_hash == response.prompt_hash
    assert provider_row.tokens_in == 11
    assert provider_row.tokens_out == 2
    assert provider_row.cache_source == "provider"
    assert provider_row.provider_called is True
    assert provider_row.cached is False
    assert provider_row.outcome == "success"
    assert provider_row.attempt == 1
    assert provider_row.error_type is None
    assert provider_row.model_tier == "weak"
    assert provider_row.counterfactual_saved_tokens == 0
    assert provider_row.cost_microunits == 15
    assert provider_row.price_version == "provider-price-2026-07"
    assert logical_row.outcome == "success"
    assert logical_row.attempt is None
    assert logical_row.tokens_in == 0
    assert logical_row.tokens_out == 0
    assert logical_row.counterfactual_saved_tokens == 0
    assert (
        metrics.LLM_TOKENS.labels(
            "weak",
            "keyword_extract",
            "actual",
            "input",
        )._value.get()
        - actual_input_before
        == 11
    )
    assert (
        metrics.LLM_TOKENS.labels(
            "weak",
            "keyword_extract",
            "actual",
            "output",
        )._value.get()
        - actual_output_before
        == 2
    )
    assert (
        metrics.LLM_TOKENS.labels(
            "weak",
            "keyword_extract",
            "saved",
            "input",
        )._value.get()
        - saved_input_before
        == 0
    )


async def test_provider_error_attempt_is_durably_distinguishable(
    log_factory: async_sessionmaker[AsyncSession],
) -> None:
    observer = LLMCallObserver(log_factory, model_tier="strong")
    actual_input_before = metrics.LLM_TOKENS.labels(
        "strong",
        "query_rewrite",
        "actual",
        "input",
    )._value.get()

    await observer.observe(
        LLMObservation(
            kind="provider_attempt",
            outcome="error",
            tenant_id="tenant-a",
            purpose="query_rewrite",
            model="strong-v1",
            recipe_sha256="a" * 64,
            elapsed_seconds=0.125,
            cache_source="provider",
            provider_called=True,
            attempt=2,
            provider_attempt_id="attempt-rate-limit-2",
            error_type="LLMRateLimitError",
            usage={"prompt_tokens": 99, "completion_tokens": 4},
        )
    )

    async with log_factory() as session:
        [row] = list((await session.execute(select(LLMCallLog))).scalars())
    assert row.event_kind == "provider_attempt"
    assert row.outcome == "error"
    assert row.attempt == 2
    assert row.error_type == "LLMRateLimitError"
    assert row.latency_ms == 125
    assert row.retry_class == "rate_limit"
    assert row.unknown_billed is False
    assert row.tokens_in == 0
    assert row.tokens_out == 0
    assert (
        metrics.LLM_TOKENS.labels(
            "strong",
            "query_rewrite",
            "actual",
            "input",
        )._value.get()
        - actual_input_before
        == 0
    )


async def test_request_memo_is_recorded_as_a_logical_cache_hit(
    log_factory: async_sessionmaker[AsyncSession],
) -> None:
    observer = LLMCallObserver(log_factory, model_tier="weak")
    actual_input_before = metrics.LLM_TOKENS.labels(
        "weak",
        "keyword_extract",
        "actual",
        "input",
    )._value.get()
    saved_input_before = metrics.LLM_TOKENS.labels(
        "weak",
        "keyword_extract",
        "saved",
        "input",
    )._value.get()
    saved_output_before = metrics.LLM_TOKENS.labels(
        "weak",
        "keyword_extract",
        "saved",
        "output",
    )._value.get()

    await observer.observe(
        LLMObservation(
            kind="logical_request",
            outcome="success",
            tenant_id="tenant-a",
            purpose="keyword_extract",
            model="weak-v1",
            recipe_sha256="b" * 64,
            elapsed_seconds=0.001,
            cache_source="request_memo",
            provider_called=False,
            usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        )
    )

    async with log_factory() as session:
        [row] = list((await session.execute(select(LLMCallLog))).scalars())
    assert row.event_kind == "logical_request"
    assert row.cache_source == "request_memo"
    assert row.cached is True
    assert row.provider_called is False
    assert row.tokens_in == 0
    assert row.tokens_out == 0
    assert row.counterfactual_saved_input_tokens == 5
    assert row.counterfactual_saved_output_tokens == 2
    assert row.counterfactual_saved_tokens == 7
    assert (
        metrics.LLM_TOKENS.labels(
            "weak",
            "keyword_extract",
            "actual",
            "input",
        )._value.get()
        - actual_input_before
        == 0
    )
    assert (
        metrics.LLM_TOKENS.labels(
            "weak",
            "keyword_extract",
            "saved",
            "input",
        )._value.get()
        - saved_input_before
        == 5
    )
    assert (
        metrics.LLM_TOKENS.labels(
            "weak",
            "keyword_extract",
            "saved",
            "output",
        )._value.get()
        - saved_output_before
        == 2
    )


async def test_optional_usage_ledger_dimensions_are_persisted_when_available(
    log_factory: async_sessionmaker[AsyncSession],
) -> None:
    observer = LLMCallObserver(log_factory, model_tier="strong")
    event = SimpleNamespace(
        kind="provider_attempt",
        outcome="success",
        tenant_id="tenant-a",
        purpose="tag_extract",
        model="strong-v2",
        recipe_sha256="c" * 64,
        elapsed_seconds=0.25,
        cache_source="provider",
        provider_called=True,
        attempt=3,
        error_type=None,
        usage={
            "prompt_tokens": 101,
            "completion_tokens": 17,
            "cached_prefill_tokens": 80,
        },
        logical_request_id="logical-001",
        provider_attempt_id="attempt-003",
        requested_max_tokens=256,
        tagger_version_id=11,
        deployment_id=12,
        evaluation_run_id=13,
        optimization_run_id=14,
        optimization_trial_id=15,
        cost_microunits=1234,
        price_version="2026-07",
        finish_reason="stop",
        provider_request_id="provider-req-1",
        retry_class="retry_success",
        cache_lookup_reason=None,
        cache_miss_reason="not_found",
        unknown_billed=False,
    )

    await observer.observe(event)

    async with log_factory() as session:
        [row] = list((await session.execute(select(LLMCallLog))).scalars())
    assert row.logical_request_id == "logical-001"
    assert row.provider_attempt_id == "attempt-003"
    assert row.model_tier == "strong"
    assert row.requested_max_tokens == 256
    assert row.tagger_version_id == 11
    assert row.deployment_id == 12
    assert row.evaluation_run_id == 13
    assert row.optimization_run_id == 14
    assert row.optimization_trial_id == 15
    assert row.cached_prefill_tokens == 80
    assert row.cost_microunits == 1234
    assert row.price_version == "2026-07"
    assert row.finish_reason == "stop"
    assert row.provider_request_id == "provider-req-1"
    assert row.retry_class == "retry_success"
    assert row.cache_lookup_reason is None
    assert row.cache_miss_reason == "not_found"
    assert row.unknown_billed is False


async def test_provider_attempt_id_is_idempotent_but_distinct_retries_are_kept(
    log_factory: async_sessionmaker[AsyncSession],
) -> None:
    observer = LLMCallObserver(log_factory, model_tier="weak")

    def event(*, attempt: int, attempt_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            kind="provider_attempt",
            outcome="success",
            tenant_id="tenant-a",
            purpose="keyword_extract",
            model="weak-v1",
            recipe_sha256="d" * 64,
            elapsed_seconds=0.01,
            cache_source="provider",
            provider_called=True,
            attempt=attempt,
            error_type=None,
            usage={"prompt_tokens": 3, "completion_tokens": 1},
            logical_request_id="logical-retry",
            provider_attempt_id=attempt_id,
        )

    await observer.observe(event(attempt=1, attempt_id="attempt-1"))
    await observer.observe(event(attempt=1, attempt_id="attempt-1"))
    await observer.observe(event(attempt=2, attempt_id="attempt-2"))

    async with log_factory() as session:
        row_count = await session.scalar(select(func.count()).select_from(LLMCallLog))
    assert row_count == 2


async def test_provider_attempt_without_identity_is_rejected_before_persistence(
    log_factory: async_sessionmaker[AsyncSession],
) -> None:
    observer = LLMCallObserver(log_factory, model_tier="strong")

    with pytest.raises(
        ValueError,
        match="provider_attempt observations require provider_attempt_id",
    ):
        await observer.observe(
            LLMObservation(
                kind="provider_attempt",
                outcome="success",
                tenant_id="tenant-a",
                purpose="tag_extract",
                model="strong-v1",
                recipe_sha256="d" * 64,
                elapsed_seconds=0.01,
                cache_source="provider",
                provider_called=True,
                attempt=1,
                usage={"prompt_tokens": 3, "completion_tokens": 1},
            )
        )

    async with log_factory() as session:
        row_count = await session.scalar(select(func.count()).select_from(LLMCallLog))
    assert row_count == 0


async def test_timeout_attempt_is_marked_as_unknown_billed(
    log_factory: async_sessionmaker[AsyncSession],
) -> None:
    observer = LLMCallObserver(log_factory, model_tier="strong")

    await observer.observe(
        LLMObservation(
            kind="provider_attempt",
            outcome="error",
            tenant_id="tenant-a",
            purpose="tag_extract",
            model="strong-v1",
            recipe_sha256="e" * 64,
            elapsed_seconds=30,
            cache_source="provider",
            provider_called=True,
            attempt=1,
            provider_attempt_id="attempt-timeout-1",
            error_type="LLMTimeoutError",
        )
    )

    async with log_factory() as session:
        [row] = list((await session.execute(select(LLMCallLog))).scalars())
    assert row.retry_class == "timeout"
    assert row.unknown_billed is True


async def test_truncated_attempt_with_provider_usage_is_recorded_as_actual_consumption(
    log_factory: async_sessionmaker[AsyncSession],
) -> None:
    observer = LLMCallObserver(log_factory, model_tier="strong")

    await observer.observe(
        LLMObservation(
            kind="provider_attempt",
            outcome="error",
            tenant_id="tenant-a",
            purpose="tag_extract",
            model="strong-v1",
            recipe_sha256="f" * 64,
            elapsed_seconds=1.2,
            cache_source="provider",
            provider_called=True,
            attempt=1,
            provider_attempt_id="attempt-truncated-known-1",
            error_type="LLMTruncatedResponseError",
            usage={"prompt_tokens": 120, "completion_tokens": 64, "total_tokens": 184},
            requested_max_tokens=64,
            finish_reason="length",
            provider_request_id="provider-request-8",
            billed_usage_known=True,
            unknown_billed=False,
        )
    )

    async with log_factory() as session:
        [row] = list((await session.execute(select(LLMCallLog))).scalars())
    assert row.tokens_in == 120
    assert row.tokens_out == 64
    assert row.finish_reason == "length"
    assert row.provider_request_id == "provider-request-8"
    assert row.unknown_billed is False


async def test_truncated_attempt_without_usage_keeps_unknown_billed_upper_bound(
    log_factory: async_sessionmaker[AsyncSession],
) -> None:
    observer = LLMCallObserver(log_factory, model_tier="weak")

    await observer.observe(
        LLMObservation(
            kind="provider_attempt",
            outcome="error",
            tenant_id="tenant-a",
            purpose="tag_extract",
            model="weak-v1",
            recipe_sha256="a" * 64,
            elapsed_seconds=1.2,
            cache_source="provider",
            provider_called=True,
            attempt=1,
            provider_attempt_id="attempt-truncated-unknown-1",
            error_type="LLMTruncatedResponseError",
            requested_max_tokens=256,
            finish_reason="length",
            provider_request_id="provider-request-9",
            billed_usage_known=False,
            unknown_billed=True,
        )
    )

    async with log_factory() as session:
        [row] = list((await session.execute(select(LLMCallLog))).scalars())
    assert row.tokens_in == 0
    assert row.tokens_out == 0
    assert row.requested_max_tokens == 256
    assert row.finish_reason == "length"
    assert row.unknown_billed is True


async def test_observer_persistence_failure_increments_drop_counter() -> None:
    class _BrokenSession:
        async def __aenter__(self) -> _BrokenSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def add(self, row: object) -> None:
            del row

        async def commit(self) -> None:
            raise RuntimeError("database unavailable")

    observer = LLMCallObserver(
        lambda: _BrokenSession(),  # type: ignore[arg-type]
        model_tier="weak",
    )
    dropped_before = metrics.LLM_OBSERVER_DROPPED.labels("persistence_error")._value.get()

    with pytest.raises(RuntimeError, match="database unavailable"):
        await observer.observe(
            LLMObservation(
                kind="logical_request",
                outcome="error",
                tenant_id="tenant-a",
                purpose="keyword_extract",
                model="weak-v1",
                recipe_sha256="f" * 64,
                elapsed_seconds=0.01,
            )
        )

    assert (
        metrics.LLM_OBSERVER_DROPPED.labels("persistence_error")._value.get() - dropped_before == 1
    )
