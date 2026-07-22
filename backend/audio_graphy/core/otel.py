"""OpenTelemetry helpers for the M8 streaming path (WS-3 / T11).

The ``opentelemetry-sdk`` package is an OPTIONAL dependency — when it is
not installed every helper in this module degrades to a no-op so the
streaming pipeline never fails on observability alone.

Span chain (architecture §15.12 / §17):

    ws_recv → vad → asr → extractor → merger → db_write

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


__all__ = [
    "OTEL_AVAILABLE",
    "init_otel",
    "new_trace_id",
    "streaming_span",
]
