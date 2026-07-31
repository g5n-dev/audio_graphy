"""Low-cardinality metrics and durable call logs for ``LLMGateway``."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.models.llm_call_log import LLMCallLog
from audio_graphy.observability.metrics import (
    LLM_CACHE_EVENTS,
    LLM_CACHED_PREFILL_TOKENS,
    LLM_CALL_DURATION,
    LLM_CALLS,
    LLM_LOGICAL_CALLS,
    LLM_OBSERVER_DROPPED,
    LLM_PROVIDER_CALLS,
    LLM_SINGLEFLIGHT_FOLLOWERS,
    LLM_TOKENS,
)
from audio_graphy.services.llm_gateway import LLMObservation

_KNOWN_PURPOSES = frozenset(
    {
        "community_summary",
        "dialogue_tag_assignments",
        "dialogue_tag_assignments_repair",
        "entity_extract",
        "entity_gleaning",
        "entity_relation_extract",
        "entity_relation_gleaning",
        "eval_extract_facts",
        "eval_faithfulness",
        "eval_relevance",
        "extract_facts",
        "final_answer",
        "judge_faithfulness",
        "judge_relevance",
        "keyword_extract",
        "legacy",
        "legacy_tag_batch",
        "query_rewrite",
        "relevance_judge",
        "relevance_judge_batch",
        "tag_extract",
    }
)
_KNOWN_SOURCES = frozenset(
    {
        "cache",
        "cache_v1",
        "local",
        "local_v1",
        "mysql",
        "mysql_v1",
        "mysql_semantic",
        "mysql_singleflight",
        "mysql_singleflight_v1",
        "provider",
        "redis",
        "redis_v1",
        "request_memo",
        "request_memo_v1",
        "singleflight",
        "singleflight_v1",
    }
)


class LLMCallObserver:
    """Record gateway events without exposing tenant or recipe metric labels."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        model_tier: str,
    ) -> None:
        self._factory = session_factory
        self._model_tier = model_tier if model_tier in {"strong", "weak"} else "other"

    async def observe(self, event: LLMObservation) -> None:
        """Persist one event, then expose metrics exactly once.

        A provider attempt owns actual token usage when it succeeds or when a
        terminal provider response (for example ``finish_reason=length``)
        carries authoritative usage. A successful logical cache hit owns only
        counterfactual savings. Persisting before incrementing counters keeps
        duplicate provider-attempt IDs idempotent across both surfaces.
        """

        row = self._ledger_row(event)
        try:
            inserted = await self._persist(row)
        except Exception:
            LLM_OBSERVER_DROPPED.labels("persistence_error").inc()
            raise
        if not inserted:
            return
        self._record_metrics(event)

    def _ledger_row(self, event: LLMObservation) -> LLMCallLog:
        event_kind = str(event.kind)
        outcome = str(event.outcome)
        provider_attempt_id = _optional_text(event, "provider_attempt_id", 96)
        if event_kind == "provider_attempt" and provider_attempt_id is None:
            raise ValueError("provider_attempt observations require provider_attempt_id")
        usage = event.usage if isinstance(event.usage, Mapping) else {}
        raw_tokens_in = _usage_value(usage, "prompt_tokens", "input_tokens")
        raw_tokens_out = _usage_value(usage, "completion_tokens", "output_tokens")
        provider_success = event_kind == "provider_attempt" and outcome == "success"
        provider_usage_known = provider_success or (
            event_kind == "provider_attempt" and getattr(event, "billed_usage_known", None) is True
        )
        cache_hit = (
            event_kind == "logical_request"
            and outcome == "success"
            and event.provider_called is False
        )
        cached_prefill_tokens = (
            _usage_value(
                usage,
                "cached_prefill_tokens",
                "cached_prompt_tokens",
                "cache_read_input_tokens",
                "cached_tokens",
            )
            if provider_usage_known
            else 0
        )
        saved_input = raw_tokens_in if cache_hit else 0
        saved_output = raw_tokens_out if cache_hit else 0
        saved_total = max(_total_tokens(usage), saved_input + saved_output) if cache_hit else 0
        explicit_unknown_billed = getattr(event, "unknown_billed", None)
        unknown_billed = (
            explicit_unknown_billed
            if isinstance(explicit_unknown_billed, bool)
            else _is_unknown_billed(event_kind, outcome, event.error_type)
        )
        retry_class = _optional_text(event, "retry_class", 32) or _retry_class(
            outcome,
            event.error_type,
            event.attempt,
        )

        return LLMCallLog(
            tenant_id=event.tenant_id[:64],
            model=event.model[:64],
            logical_request_id=_optional_text(event, "logical_request_id", 64),
            provider_attempt_id=provider_attempt_id,
            model_tier=self._model_tier,
            requested_max_tokens=_optional_positive_int(event, "requested_max_tokens"),
            tagger_version_id=_optional_positive_int(event, "tagger_version_id"),
            deployment_id=_optional_positive_int(event, "deployment_id"),
            evaluation_run_id=_optional_positive_int(event, "evaluation_run_id"),
            optimization_run_id=_optional_positive_int(event, "optimization_run_id"),
            optimization_trial_id=_optional_positive_int(event, "optimization_trial_id"),
            event_kind=event_kind,
            outcome=outcome,
            attempt=event.attempt,
            error_type=event.error_type[:128] if event.error_type else None,
            purpose=event.purpose[:64] or "legacy",
            prompt_hash=event.recipe_sha256 or ("0" * 64),
            tokens_in=raw_tokens_in if provider_usage_known else 0,
            tokens_out=raw_tokens_out if provider_usage_known else 0,
            cached_prefill_tokens=cached_prefill_tokens,
            counterfactual_saved_input_tokens=saved_input,
            counterfactual_saved_output_tokens=saved_output,
            counterfactual_saved_tokens=saved_total,
            cost_microunits=(
                _optional_nonnegative_int(event, "cost_microunits") or 0
                if provider_usage_known
                else 0
            ),
            price_version=(
                _optional_text(event, "price_version", 64) if provider_usage_known else None
            ),
            finish_reason=(
                _optional_text(event, "finish_reason", 64)
                if event_kind == "provider_attempt"
                else None
            ),
            provider_request_id=(
                _optional_text(event, "provider_request_id", 128)
                if event_kind == "provider_attempt"
                else None
            ),
            retry_class=retry_class,
            cached=cache_hit,
            cache_source=(event.cache_source or outcome)[:32],
            provider_called=event.provider_called is True,
            cache_lookup_reason=_optional_text(event, "cache_lookup_reason", 64),
            cache_miss_reason=_optional_text(event, "cache_miss_reason", 64),
            unknown_billed=unknown_billed,
            latency_ms=max(0, round(event.elapsed_seconds * 1_000)),
            logged_at=datetime.now(UTC),
        )

    async def _persist(self, row: LLMCallLog) -> bool:
        """Return ``False`` only for an already-recorded provider attempt."""

        try:
            async with self._factory() as session:
                session.add(row)
                await session.commit()
            return True
        except IntegrityError:
            if not row.provider_attempt_id:
                raise
            async with self._factory() as session:
                existing = await session.scalar(
                    select(LLMCallLog.id).where(
                        LLMCallLog.tenant_id == row.tenant_id,
                        LLMCallLog.provider_attempt_id == row.provider_attempt_id,
                    )
                )
            if existing is None:
                raise
            return False

    def _record_metrics(self, event: LLMObservation) -> None:
        purpose_label = event.purpose if event.purpose in _KNOWN_PURPOSES else "other"
        if event.kind == "provider_attempt":
            LLM_PROVIDER_CALLS.labels(
                self._model_tier,
                purpose_label,
                event.outcome,
            ).inc()
            LLM_CALLS.labels(event.model[:64], event.outcome).inc()
            LLM_CALL_DURATION.labels(event.model[:64]).observe(event.elapsed_seconds)
            if (
                event.outcome == "success"
                or getattr(
                    event,
                    "billed_usage_known",
                    None,
                )
                is True
            ):
                tokens_in = _usage_value(event.usage, "prompt_tokens", "input_tokens")
                tokens_out = _usage_value(
                    event.usage,
                    "completion_tokens",
                    "output_tokens",
                )
                LLM_TOKENS.labels(
                    self._model_tier,
                    purpose_label,
                    "actual",
                    "input",
                ).inc(tokens_in)
                LLM_TOKENS.labels(
                    self._model_tier,
                    purpose_label,
                    "actual",
                    "output",
                ).inc(tokens_out)
                LLM_CACHED_PREFILL_TOKENS.labels(
                    self._model_tier,
                    purpose_label,
                ).inc(
                    _usage_value(
                        event.usage,
                        "cached_prefill_tokens",
                        "cached_prompt_tokens",
                        "cache_read_input_tokens",
                        "cached_tokens",
                    )
                )
        else:
            LLM_LOGICAL_CALLS.labels(
                self._model_tier,
                purpose_label,
                event.outcome,
            ).inc()
            source = event.cache_source or "none"
            source_label = source if source in _KNOWN_SOURCES else "other"
            if event.outcome == "success":
                cache_result = "hit" if event.provider_called is False else "miss"
                LLM_CACHE_EVENTS.labels(
                    self._model_tier,
                    purpose_label,
                    source_label,
                    cache_result,
                ).inc()
                if event.provider_called is False:
                    saved_input = _usage_value(
                        event.usage,
                        "prompt_tokens",
                        "input_tokens",
                    )
                    saved_output = _usage_value(
                        event.usage,
                        "completion_tokens",
                        "output_tokens",
                    )
                    LLM_TOKENS.labels(
                        self._model_tier,
                        purpose_label,
                        "saved",
                        "input",
                    ).inc(saved_input)
                    LLM_TOKENS.labels(
                        self._model_tier,
                        purpose_label,
                        "saved",
                        "output",
                    ).inc(saved_output)
                normalized_source = source.removesuffix("_v1")
                if normalized_source == "singleflight":
                    LLM_SINGLEFLIGHT_FOLLOWERS.labels("process").inc()
                elif normalized_source == "mysql_singleflight":
                    LLM_SINGLEFLIGHT_FOLLOWERS.labels("mysql").inc()


def _usage_value(usage: Mapping[str, int], *keys: str) -> int:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


def _total_tokens(usage: Mapping[str, int]) -> int:
    total = _usage_value(usage, "total_tokens")
    if total:
        return total
    return _usage_value(usage, "prompt_tokens", "input_tokens") + _usage_value(
        usage,
        "completion_tokens",
        "output_tokens",
    )


def _optional_text(event: object, name: str, limit: int) -> str | None:
    value = getattr(event, name, None)
    if not isinstance(value, str) or not value:
        return None
    return value[:limit]


def _optional_nonnegative_int(event: object, name: str) -> int | None:
    value: Any = getattr(event, name, None)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _optional_positive_int(event: object, name: str) -> int | None:
    value = _optional_nonnegative_int(event, name)
    return value if value is not None and value >= 1 else None


def _retry_class(
    outcome: str,
    error_type: str | None,
    attempt: int | None,
) -> str | None:
    if error_type:
        lowered = error_type.lower()
        if "ratelimit" in lowered or "rate_limit" in lowered:
            return "rate_limit"
        if "timeout" in lowered:
            return "timeout"
        if "server" in lowered:
            return "server_error"
        if "badrequest" in lowered or "bad_request" in lowered:
            return "non_retryable"
        return "other_error"
    if outcome == "cancelled":
        return "cancelled"
    if outcome == "success" and isinstance(attempt, int) and attempt > 1:
        return "retry_success"
    return None


def _is_unknown_billed(
    event_kind: str,
    outcome: str,
    error_type: str | None,
) -> bool:
    if event_kind != "provider_attempt" or outcome == "success":
        return False
    return outcome == "cancelled" or bool(error_type and "timeout" in error_type.lower())


__all__ = ["LLMCallObserver"]
