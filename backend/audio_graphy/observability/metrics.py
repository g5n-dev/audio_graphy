"""Prometheus metric objects and the registry that owns them.

This module is a leaf: it imports only stdlib + ``prometheus_client`` so any
layer (core, storage, services, api) can record a metric without depending on
the API layer. The HTTP surface — ``/metrics`` route and request middleware —
lives in ``audio_graphy.api.metrics``, which re-exports everything here.

Metrics exposed (architecture §15.4):

    Counter
        audiography_http_requests_total{method,endpoint,status}
        audiography_llm_calls_total{adapter,status}
        audiography_retention_deletes_total
        audiography_audit_log_written_total{action}
        audiography_dsar_requests_total{type,status}
        audiography_eval_run_total{status}

    Histogram
        audiography_pipeline_seconds{stage}
        audiography_llm_call_duration_seconds{model}
        audiography_vector_query_duration_seconds
        audiography_eval_example_duration_seconds
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

# Use the global default registry (Q locked: open-source user config simplest).
REGISTRY: CollectorRegistry = CollectorRegistry()

# ============================================================
# Counters
# ============================================================

HTTP_REQUESTS = Counter(
    "audiography_http_requests_total",
    "Total HTTP requests by method / endpoint / status.",
    ["method", "endpoint", "status"],
    registry=REGISTRY,
)

LLM_CALLS = Counter(
    "audiography_llm_calls_total",
    "Total LLM calls by adapter name and outcome status.",
    ["adapter", "status"],
    registry=REGISTRY,
)

# Central gateway metrics deliberately use only bounded operational labels.
# Tenant IDs, prompts, and recipe hashes must never become Prometheus labels.
LLM_LOGICAL_CALLS = Counter(
    "audiography_llm_logical_calls_total",
    "Logical LLM requests, including cache hits and singleflight followers.",
    ["model_tier", "purpose", "status"],
    registry=REGISTRY,
)

LLM_PROVIDER_CALLS = Counter(
    "audiography_llm_provider_calls_total",
    "Physical provider calls made by the centralized LLM gateway.",
    ["model_tier", "purpose", "status"],
    registry=REGISTRY,
)

LLM_CACHE_EVENTS = Counter(
    "audiography_llm_cache_events_total",
    "LLM result-cache events by bounded source and outcome.",
    ["model_tier", "purpose", "source", "result"],
    registry=REGISTRY,
)

LLM_TOKENS = Counter(
    "audiography_llm_tokens_total",
    "LLM input/output tokens by actual provider use or counterfactual cache saving.",
    ["model_tier", "purpose", "accounting", "direction"],
    registry=REGISTRY,
)

LLM_CACHED_PREFILL_TOKENS = Counter(
    "audiography_llm_cached_prefill_tokens_total",
    "Provider-reported cached-prefix tokens within actual input tokens.",
    ["model_tier", "purpose"],
    registry=REGISTRY,
)

LLM_OBSERVER_DROPPED = Counter(
    "audiography_llm_observer_dropped_total",
    "LLM observations that could not be durably recorded.",
    ["reason"],
    registry=REGISTRY,
)

LLM_SINGLEFLIGHT_FOLLOWERS = Counter(
    "audiography_llm_singleflight_followers_total",
    "Requests served as process- or MySQL-level singleflight followers.",
    ["scope"],
    registry=REGISTRY,
)

LLM_CACHE_EVICTIONS = Counter(
    "audiography_llm_cache_evictions_total",
    "Bounded hot-cache evictions by backend.",
    ["backend"],
    registry=REGISTRY,
)

LLM_REDIS_FALLBACKS = Counter(
    "audiography_llm_redis_fallbacks_total",
    "Redis hot-cache degradations to the bounded local cache.",
    ["reason"],
    registry=REGISTRY,
)

LLM_LEASE_EVENTS = Counter(
    "audiography_llm_lease_events_total",
    "Persistent LLM-cache lease events.",
    ["event"],
    registry=REGISTRY,
)

RETENTION_DELETES = Counter(
    "audiography_retention_deletes_total",
    "Total recording rows hard-deleted by the daily retention cron.",
    registry=REGISTRY,
)

AUDIT_LOG_WRITTEN = Counter(
    "audiography_audit_log_written_total",
    "Total audit_log rows written, by action code.",
    ["action"],
    registry=REGISTRY,
)

DSAR_REQUESTS = Counter(
    "audiography_dsar_requests_total",
    "Total DSAR endpoint hits by type (export / erase / audit) and status.",
    ["type", "status"],
    registry=REGISTRY,
)

EVAL_RUN_TOTAL = Counter(
    "audiography_eval_run_total",
    "Total eval runs by final status (pending / running / completed / failed).",
    ["status"],
    registry=REGISTRY,
)

# ============================================================
# M8 Phase 4 (WS-3 / T11) — streaming metrics
# ============================================================

STREAMING_SESSIONS_ACTIVE = Gauge(
    "audiography_streaming_sessions_active",
    "Currently active /ws/stream sessions.",
    registry=REGISTRY,
)

STREAMING_SESSIONS_TOTAL = Counter(
    "audiography_streaming_sessions_total",
    "Total streaming sessions opened, by tenant.",
    ["tenant_id"],
    registry=REGISTRY,
)

STREAMING_SEGMENTS_TOTAL = Counter(
    "audiography_streaming_segments_total",
    "Total ASR segments emitted, by mode (confirmed / realtime).",
    ["mode"],
    registry=REGISTRY,
)

STREAMING_VAD_RESETS_TOTAL = Counter(
    "audiography_streaming_vad_resets_total",
    "Total VAD resets, by reason (seq_gap / client_request).",
    ["reason"],
    registry=REGISTRY,
)

STREAMING_ASR_LATENCY = Histogram(
    "audiography_streaming_asr_latency_seconds",
    "Per-push streaming ASR latency in seconds.",
    registry=REGISTRY,
)

STREAMING_TAG_RECOMPUTES_TOTAL = Counter(
    "audiography_streaming_tag_recomputes_total",
    "Total streaming tag batch recomputes, by outcome (ok / error).",
    ["status"],
    registry=REGISTRY,
)

# ============================================================
# M9 R1 T14 — advanced-graph metrics (13 metrics, architecture §17)
# ============================================================
#
# Naming follows the established "audiography_<subsystem>_<verb>" convention.
# All 13 metrics are scoped to the M9 advanced-graph subsystem. They are
# always registered (even when enable_advanced_graph=False) so dashboards
# don't break on flag toggle; they just stay at zero.

# BiTemporal
BITEMPORAL_EDGE_EVENTS_TOTAL = Counter(
    "audiography_bitemporal_edge_events_total",
    "M9 bi-temporal edge events written, by event_type "
    "(insert / merge / supersede / soft_delete / restore).",
    ["event_type"],
    registry=REGISTRY,
)

BITEMPORAL_SUPERSEDE_CHAIN_DEPTH = Histogram(
    "audiography_bitemporal_supersede_chain_depth",
    "Depth of the supersede chain when a supersede happens (cap=8).",
    buckets=(1, 2, 3, 4, 5, 6, 7, 8),
    registry=REGISTRY,
)

# Leiden
LEIDEN_RUNS_TOTAL = Counter(
    "audiography_leiden_runs_total",
    "M9 Leiden runs, by job_type (full / incremental) and outcome (succeeded / failed).",
    ["job_type", "status"],
    registry=REGISTRY,
)

LEIDEN_RUN_DURATION = Histogram(
    "audiography_leiden_run_duration_seconds",
    "Wall-clock duration of one Leiden run.",
    registry=REGISTRY,
)

LEIDEN_DIFF_PERCENT = Histogram(
    "audiography_leiden_diff_percent",
    "Incremental diff percentage observed at run start (L2 threshold gauge).",
    buckets=(0, 5, 10, 20, 30, 50, 75, 100),
    registry=REGISTRY,
)

LEIDEN_MODULARITY = Histogram(
    "audiography_leiden_modularity",
    "Q modularity score achieved per Leiden run.",
    buckets=(-1.0, -0.5, 0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 1.0),
    registry=REGISTRY,
)

# Community summaries
COMMUNITY_SUMMARIES_TOTAL = Counter(
    "audiography_community_summaries_total",
    "M9 community summaries generated, by level (0/1/2) and strategy (eager / lazy).",
    ["level", "strategy"],
    registry=REGISTRY,
)

COMMUNITY_SUMMARY_DURATION = Histogram(
    "audiography_community_summary_duration_seconds",
    "Per-summary LLM call duration (Q2 level 0/1/2).",
    registry=REGISTRY,
)

# Compression
COMPRESSION_RUNS_TOTAL = Counter(
    "audiography_compression_runs_total",
    "M9 compression runs, by outcome (committed / rolled_back).",
    ["outcome"],
    registry=REGISTRY,
)

COMPRESSION_NODES_SOFT_DELETED = Counter(
    "audiography_compression_nodes_soft_deleted_total",
    "M9 compression: nodes soft-deleted (expired_at set).",
    registry=REGISTRY,
)

COMPRESSION_EDGES_SOFT_DELETED = Counter(
    "audiography_compression_edges_soft_deleted_total",
    "M9 compression: edges soft-deleted (invalid_at set).",
    registry=REGISTRY,
)

COMPRESSION_EDGES_DEPRECATED = Counter(
    "audiography_compression_edges_deprecated_total",
    "M9 compression L7: AMBIGUOUS edges demoted to DEPRECATED.",
    registry=REGISTRY,
)

COMPRESSION_ORPHANS_INVALIDATED = Counter(
    "audiography_compression_orphans_invalidated_total",
    "M9 compression Phase 3: orphan edges invalidated (source/target node already soft-deleted).",
    registry=REGISTRY,
)

# Global search latency (L4 map-reduce).
GLOBAL_SEARCH_DURATION = Histogram(
    "audiography_global_search_duration_seconds",
    "M9 L4 global search (map-reduce) end-to-end latency in seconds.",
    registry=REGISTRY,
)

# Speaker fuzzy
SPEAKER_FUZZY_MATCHES_TOTAL = Counter(
    "audiography_speaker_fuzzy_matches_total",
    "M9 SpeakerFuzzyMatcher verdicts, by verdict (CONFIRMED / AMBIGUOUS / INFERRED / NO_MATCH).",
    ["verdict"],
    registry=REGISTRY,
)

SPEAKER_RECONFIRM_QUEUE_SIZE = Gauge(
    "audiography_speaker_reconfirm_queue_size",
    "Current count of pending SpeakerMergePending rows awaiting reconfirm.",
    registry=REGISTRY,
)

# ============================================================
# Histograms
# ============================================================

PIPELINE_DURATION = Histogram(
    "audiography_pipeline_seconds",
    "Pipeline stage duration in seconds.",
    ["stage"],
    registry=REGISTRY,
)

LLM_CALL_DURATION = Histogram(
    "audiography_llm_call_duration_seconds",
    "LLM call duration in seconds by model name.",
    ["model"],
    registry=REGISTRY,
)

VECTOR_QUERY_DURATION = Histogram(
    "audiography_vector_query_duration_seconds",
    "Vector store query duration in seconds.",
    registry=REGISTRY,
)

EVAL_EXAMPLE_DURATION = Histogram(
    "audiography_eval_example_duration_seconds",
    "Per-example eval duration in seconds.",
    registry=REGISTRY,
)


__all__ = [
    "AUDIT_LOG_WRITTEN",
    "BITEMPORAL_EDGE_EVENTS_TOTAL",
    "BITEMPORAL_SUPERSEDE_CHAIN_DEPTH",
    "COMMUNITY_SUMMARIES_TOTAL",
    "COMMUNITY_SUMMARY_DURATION",
    "COMPRESSION_EDGES_DEPRECATED",
    "COMPRESSION_EDGES_SOFT_DELETED",
    "COMPRESSION_NODES_SOFT_DELETED",
    "COMPRESSION_ORPHANS_INVALIDATED",
    "COMPRESSION_RUNS_TOTAL",
    "DSAR_REQUESTS",
    "EVAL_EXAMPLE_DURATION",
    "EVAL_RUN_TOTAL",
    "GLOBAL_SEARCH_DURATION",
    "HTTP_REQUESTS",
    "LEIDEN_DIFF_PERCENT",
    "LEIDEN_MODULARITY",
    "LEIDEN_RUNS_TOTAL",
    "LEIDEN_RUN_DURATION",
    "LLM_CACHED_PREFILL_TOKENS",
    "LLM_CACHE_EVENTS",
    "LLM_CACHE_EVICTIONS",
    "LLM_CALLS",
    "LLM_CALL_DURATION",
    "LLM_LEASE_EVENTS",
    "LLM_LOGICAL_CALLS",
    "LLM_OBSERVER_DROPPED",
    "LLM_PROVIDER_CALLS",
    "LLM_REDIS_FALLBACKS",
    "LLM_SINGLEFLIGHT_FOLLOWERS",
    "LLM_TOKENS",
    "PIPELINE_DURATION",
    "REGISTRY",
    "RETENTION_DELETES",
    "SPEAKER_FUZZY_MATCHES_TOTAL",
    "SPEAKER_RECONFIRM_QUEUE_SIZE",
    "STREAMING_ASR_LATENCY",
    "STREAMING_SEGMENTS_TOTAL",
    "STREAMING_SESSIONS_ACTIVE",
    "STREAMING_SESSIONS_TOTAL",
    "STREAMING_TAG_RECOMPUTES_TOTAL",
    "STREAMING_VAD_RESETS_TOTAL",
    "VECTOR_QUERY_DURATION",
]
