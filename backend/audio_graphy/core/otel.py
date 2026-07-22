"""OpenTelemetry helpers for the M8 streaming path (WS-3 / T11) and
the M9 advanced-graph subsystem (R1 T14).

The ``opentelemetry-sdk`` package is an OPTIONAL dependency — when it is
not installed every helper in this module degrades to a no-op so the
streaming pipeline never fails on observability alone.

Span chain (architecture §15.12 / §17):

    ws_recv → vad → asr → extractor → merger → db_write

M9 R1 T14 adds four new span helpers for the advanced-graph subsystem:

    bitemporal_supersede_span
    leiden_run_span
    community_summary_span
    compression_apply_span
    speaker_fuzzy_match_span

The trace_id is generated per session at init time and included in every
span + the ``session_opened`` event so clients can correlate.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)

try:  # pragma: no cover — exercised only when otel-sdk installed
    from opentelemetry import trace as _otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
    )

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _otel_trace = None
    TracerProvider = None
    BatchSpanProcessor = None
    ConsoleSpanExporter = None
    _OTEL_AVAILABLE = False

OTEL_AVAILABLE: bool = _OTEL_AVAILABLE


def init_otel(service_name: str = "audiography-streaming", *, console_export: bool = False) -> bool:
    """Initialise a global TracerProvider (idempotent).

    Args:
        service_name: OTel resource service name.
        console_export: When True, attach a ConsoleSpanExporter (dev/debug).

    Returns:
        True when the SDK is available and the provider was set up.
    """
    if not _OTEL_AVAILABLE:
        logger.debug("opentelemetry-sdk not installed — OTel disabled")
        return False
    assert _otel_trace is not None
    if isinstance(_otel_trace.get_tracer_provider(), TracerProvider):
        return True  # already initialised
    provider = TracerProvider()
    if console_export:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    _otel_trace.set_tracer_provider(provider)
    logger.info("OTel tracer provider initialised (service=%s)", service_name)
    return True


def new_trace_id() -> str:
    """Generate a per-session trace id (hex, OTel-compatible 128-bit)."""
    return uuid.uuid4().hex


@contextlib.contextmanager
def streaming_span(
    name: str,
    *,
    session_id: str = "",
    tenant_id: str = "",
    trace_id: str = "",
    **attrs: Any,
) -> Iterator[None]:
    """Context manager yielding one OTel span (no-op without the SDK).

    Common attributes (``session_id`` / ``tenant_id`` / ``trace_id``) are
    attached to every span for correlation.
    """
    if not _OTEL_AVAILABLE:
        yield
        return
    assert _otel_trace is not None
    tracer = _otel_trace.get_tracer("audiography.streaming")
    with tracer.start_as_current_span(name) as span:
        span.set_attribute("session_id", session_id)
        span.set_attribute("tenant_id", tenant_id)
        span.set_attribute("trace_id", trace_id)
        for key, value in attrs.items():
            if isinstance(value, (str, int, float, bool)):
                span.set_attribute(key, value)
        yield


# ============================================================
# M9 R1 T14 — advanced-graph span helpers
# ============================================================
#
# All five helpers follow the same shape: contextual span + tenant
# correlation + subsystem-specific attributes. They are intentionally
# tiny so callers (BiTemporalEdgeService, IncrementalLeidenService, etc.)
# can use them with minimal boilerplate.


@contextlib.contextmanager
def bitemporal_supersede_span(
    *,
    tenant_id: str,
    edge_key: str,
    replacement_key: str,
    chain_depth: int = 1,
) -> Iterator[None]:
    """Span for one Q1 dual-track supersede operation."""
    with streaming_span(
        "bitemporal.supersede",
        tenant_id=tenant_id,
        edge_key=edge_key,
        replacement_key=replacement_key,
        chain_depth=chain_depth,
    ):
        yield


@contextlib.contextmanager
def leiden_run_span(
    *,
    tenant_id: str,
    job_type: str,
    diff_percent: float,
    node_count: int,
) -> Iterator[None]:
    """Span for one Leiden run (full or incremental)."""
    with streaming_span(
        "leiden.run",
        tenant_id=tenant_id,
        job_type=job_type,
        diff_percent=diff_percent,
        node_count=node_count,
    ):
        yield


@contextlib.contextmanager
def community_summary_span(
    *,
    tenant_id: str,
    level: int,
    community_id: int,
    strategy: str,
) -> Iterator[None]:
    """Span for one community summary LLM call."""
    with streaming_span(
        "community_summary.generate",
        tenant_id=tenant_id,
        level=level,
        community_id=community_id,
        strategy=strategy,
    ):
        yield


@contextlib.contextmanager
def compression_apply_span(
    *,
    tenant_id: str,
    candidate_count: int,
) -> Iterator[None]:
    """Span for one CompressionService.apply batch."""
    with streaming_span(
        "compression.apply",
        tenant_id=tenant_id,
        candidate_count=candidate_count,
    ):
        yield


@contextlib.contextmanager
def speaker_fuzzy_match_span(
    *,
    tenant_id: str,
    query_name: str,
    candidate_count: int,
) -> Iterator[None]:
    """Span for one SpeakerFuzzyMatcher.match call."""
    with streaming_span(
        "speaker_fuzzy.match",
        tenant_id=tenant_id,
        query_name=query_name,
        candidate_count=candidate_count,
    ):
        yield


__all__ = [
    "OTEL_AVAILABLE",
    "bitemporal_supersede_span",
    "community_summary_span",
    "compression_apply_span",
    "init_otel",
    "leiden_run_span",
    "new_trace_id",
    "speaker_fuzzy_match_span",
    "streaming_span",
]
