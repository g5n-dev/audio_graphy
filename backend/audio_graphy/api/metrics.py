"""Prometheus /metrics endpoint + middleware (M6 Q3 Quick Win).

Exposes the AudioGraphy Prometheus metrics on the main FastAPI app (port 8000
reuse, per Q4 locked decision). The endpoint is unauthenticated so Prometheus
can scrape without setup; sensitive metrics never include PII — only aggregate
counters / histograms.

The metric objects themselves live in ``audio_graphy.observability.metrics``, a
leaf module that lower layers can import without dragging in FastAPI. They are
re-exported here so the historical ``audio_graphy.api.metrics.X`` import path
keeps working, and so the middleware resolves its counters through this
module's globals (which is what makes them monkeypatchable in tests).

The middleware labels ``endpoint`` with the matched route template, so
high-cardinality paths (e.g. /recordings/{id}) collapse into one series.
"""

from __future__ import annotations

import logging
import time
from typing import cast

from fastapi import APIRouter, FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse

from audio_graphy.observability.metrics import (
    AUDIT_LOG_WRITTEN,
    BITEMPORAL_EDGE_EVENTS_TOTAL,
    BITEMPORAL_SUPERSEDE_CHAIN_DEPTH,
    COMMUNITY_SUMMARIES_TOTAL,
    COMMUNITY_SUMMARY_DURATION,
    COMPRESSION_EDGES_DEPRECATED,
    COMPRESSION_EDGES_SOFT_DELETED,
    COMPRESSION_NODES_SOFT_DELETED,
    COMPRESSION_ORPHANS_INVALIDATED,
    COMPRESSION_RUNS_TOTAL,
    DSAR_REQUESTS,
    EVAL_EXAMPLE_DURATION,
    EVAL_RUN_TOTAL,
    GLOBAL_SEARCH_DURATION,
    HTTP_REQUESTS,
    LEIDEN_DIFF_PERCENT,
    LEIDEN_MODULARITY,
    LEIDEN_RUN_DURATION,
    LEIDEN_RUNS_TOTAL,
    LLM_CACHE_EVENTS,
    LLM_CACHE_EVICTIONS,
    LLM_CACHED_PREFILL_TOKENS,
    LLM_CALL_DURATION,
    LLM_CALLS,
    LLM_LEASE_EVENTS,
    LLM_LOGICAL_CALLS,
    LLM_OBSERVER_DROPPED,
    LLM_PROVIDER_CALLS,
    LLM_REDIS_FALLBACKS,
    LLM_SINGLEFLIGHT_FOLLOWERS,
    LLM_TOKENS,
    PIPELINE_DURATION,
    REGISTRY,
    RETENTION_DELETES,
    SPEAKER_FUZZY_MATCHES_TOTAL,
    SPEAKER_RECONFIRM_QUEUE_SIZE,
    STREAMING_ASR_LATENCY,
    STREAMING_SEGMENTS_TOTAL,
    STREAMING_SESSIONS_ACTIVE,
    STREAMING_SESSIONS_TOTAL,
    STREAMING_TAG_RECOMPUTES_TOTAL,
    STREAMING_VAD_RESETS_TOTAL,
    VECTOR_QUERY_DURATION,
)

logger = logging.getLogger(__name__)


# ============================================================
# Middleware
# ============================================================

# Paths that should NOT increment the HTTP_REQUESTS counter. /metrics itself
# is excluded to avoid recursive growth on every scrape.
_SKIP_PATHS: frozenset[str] = frozenset({"/metrics", "/health", "/health/readiness"})


def _endpoint_label(request: Request) -> str:
    """Return a bounded label for the request's endpoint.

    Uses the matched route's template (``/recordings/{recording_id}``) rather
    than the concrete URL. Labelling by concrete path mints a fresh time series
    per id, which is how a Prometheus instance runs out of memory.

    Unmatched requests (404s, including scanner traffic) collapse into a single
    series so they cannot be used to inflate cardinality either.
    """
    route = request.scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    return str(template) if template else "__unmatched__"


class MetricsMiddleware(BaseHTTPMiddleware):
    """Count every HTTP request with method / route-template / status labels.

    Skips ``/metrics`` to avoid inflating its own counter on every scrape, and
    ``/health`` / ``/health/readiness`` to keep probe traffic out of the signal.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: BaseHTTPMiddleware.RequestResponseEndpoint,  # type: ignore[name-defined]
    ) -> StarletteResponse:
        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start

        if request.url.path not in _SKIP_PATHS:
            path = _endpoint_label(request)
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
    "MetricsMiddleware",
    "register_metrics",
    "router",
]
