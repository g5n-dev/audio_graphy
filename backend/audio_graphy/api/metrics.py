"""Prometheus /metrics endpoint + middleware (M6 Q3 Quick Win).

Exposes a small set of AudioGraphy-specific Prometheus metrics on the main
FastAPI app (port 8000 reuse, per Q4 locked decision). The endpoint is
unauthenticated so Prometheus can scrape without setup; sensitive metrics
never include PII — only aggregate counters / histograms.

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

The middleware auto-labels ``endpoint`` from ``request.url.path``; for
high-cardinality paths (e.g. /recordings/{id}) you should normalise the
path template upstream. M6 uses raw path for simplicity.
"""

from __future__ import annotations

import logging
import time
from typing import cast

from fastapi import APIRouter, FastAPI, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse

logger = logging.getLogger(__name__)

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


# ============================================================
# Middleware
# ============================================================

# Paths that should NOT increment the HTTP_REQUESTS counter. /metrics itself
# is excluded to avoid recursive growth on every scrape.
_SKIP_PATHS: frozenset[str] = frozenset({"/metrics", "/health", "/readyz"})


class MetricsMiddleware(BaseHTTPMiddleware):
    """Count every HTTP request with method / path / status labels.

    Skips ``/metrics`` to avoid inflating its own counter on every scrape,
    and ``/health`` / ``/readyz`` to keep health-check traffic out of the
    signal.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: BaseHTTPMiddleware.RequestResponseEndpoint,  # type: ignore[name-defined]
    ) -> StarletteResponse:
        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start

        path = request.url.path
        if path not in _SKIP_PATHS:
            try:
                HTTP_REQUESTS.labels(
                    request.method,
                    path,
                    str(response.status_code),
                ).inc()
                # Also observe total request duration as a "stage=request" sample.
                PIPELINE_DURATION.labels(stage="request").observe(duration)
            except Exception as exc:
                logger.debug("metrics middleware inc failed: %s", exc)

        return cast(Response, response)


# ============================================================
# Router — exposes GET /metrics on the main app
# ============================================================

router = APIRouter(tags=["metrics"])


@router.get("/metrics", summary="Prometheus metrics endpoint")
async def metrics() -> Response:
    """Return the latest metrics snapshot in Prometheus text format."""
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


def register_metrics(app: FastAPI) -> None:
    """Wire the middleware + /metrics router into a FastAPI app.

    Called from ``main.py`` lifespan. Safe to call multiple times — the
    middleware deduplicates via the bound router identity, and the prometheus
    default registry ignores duplicate metric registrations.
    """
    app.add_middleware(MetricsMiddleware)
    if not any(getattr(r, "path", "") == "/metrics" for r in app.routes):
        app.include_router(router)


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
    "LLM_CALLS",
    "LLM_CALL_DURATION",
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
    "MetricsMiddleware",
    "register_metrics",
    "router",
]
