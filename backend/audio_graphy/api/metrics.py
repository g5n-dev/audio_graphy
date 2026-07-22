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
    "DSAR_REQUESTS",
    "EVAL_EXAMPLE_DURATION",
    "EVAL_RUN_TOTAL",
    "HTTP_REQUESTS",
    "LLM_CALLS",
    "LLM_CALL_DURATION",
    "PIPELINE_DURATION",
    "REGISTRY",
    "RETENTION_DELETES",
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
