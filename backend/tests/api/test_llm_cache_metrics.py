"""Low-cardinality observability contracts for the centralized LLM gateway."""

from __future__ import annotations

from prometheus_client import generate_latest

from audio_graphy.api import metrics


def test_llm_cache_metrics_are_registered_without_sensitive_labels() -> None:
    collectors = {
        metric.name: metric
        for metric in metrics.REGISTRY.collect()
        if metric.name.startswith("audiography_llm_")
    }

    expected = {
        "audiography_llm_logical_calls",
        "audiography_llm_provider_calls",
        "audiography_llm_cache_events",
        "audiography_llm_tokens",
        "audiography_llm_singleflight_followers",
        "audiography_llm_cache_evictions",
        "audiography_llm_redis_fallbacks",
        "audiography_llm_lease_events",
        "audiography_llm_cached_prefill_tokens",
        "audiography_llm_observer_dropped",
    }
    assert expected <= set(collectors)
    counters = (
        metrics.LLM_LOGICAL_CALLS,
        metrics.LLM_PROVIDER_CALLS,
        metrics.LLM_CACHE_EVENTS,
        metrics.LLM_TOKENS,
        metrics.LLM_SINGLEFLIGHT_FOLLOWERS,
        metrics.LLM_CACHE_EVICTIONS,
        metrics.LLM_REDIS_FALLBACKS,
        metrics.LLM_LEASE_EVENTS,
        metrics.LLM_CACHED_PREFILL_TOKENS,
        metrics.LLM_OBSERVER_DROPPED,
    )
    for counter in counters:
        label_names = set(counter._labelnames)  # type: ignore[attr-defined]
        assert not {"tenant", "tenant_id", "prompt", "recipe", "recipe_sha256"} & label_names


def test_llm_cache_metrics_can_record_sources_tokens_and_lease_events() -> None:
    metrics.LLM_LOGICAL_CALLS.labels("weak", "keyword_extract", "ok").inc()
    metrics.LLM_PROVIDER_CALLS.labels("weak", "keyword_extract", "ok").inc()
    metrics.LLM_CACHE_EVENTS.labels("weak", "keyword_extract", "redis", "hit").inc()
    metrics.LLM_TOKENS.labels("weak", "keyword_extract", "actual", "input").inc(10)
    metrics.LLM_TOKENS.labels("weak", "keyword_extract", "actual", "output").inc(2)
    metrics.LLM_TOKENS.labels("weak", "keyword_extract", "saved", "input").inc(6)
    metrics.LLM_TOKENS.labels("weak", "keyword_extract", "saved", "output").inc(1)
    metrics.LLM_CACHED_PREFILL_TOKENS.labels("weak", "keyword_extract").inc(4)
    metrics.LLM_OBSERVER_DROPPED.labels("persistence_error").inc()
    metrics.LLM_SINGLEFLIGHT_FOLLOWERS.labels("mysql").inc()
    metrics.LLM_CACHE_EVICTIONS.labels("local").inc()
    metrics.LLM_REDIS_FALLBACKS.labels("operation_failure").inc()
    metrics.LLM_LEASE_EVENTS.labels("reclaimed").inc()

    exposition = generate_latest(metrics.REGISTRY).decode()
    assert (
        'audiography_llm_cache_events_total{model_tier="weak",'
        'purpose="keyword_extract",result="hit",source="redis"}'
    ) in exposition
    assert (
        'audiography_llm_tokens_total{accounting="saved",direction="input",'
        'model_tier="weak",purpose="keyword_extract"}'
    ) in exposition
