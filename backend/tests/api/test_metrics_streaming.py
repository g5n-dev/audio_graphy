"""M8 Phase 4 — streaming Prometheus metrics tests (T11).

Covers the six new streaming metrics exposed by ``api/metrics.py``:
    - ``streaming_sessions_active`` Gauge
    - ``streaming_sessions_total`` Counter (tenant_id label)
    - ``streaming_segments_total`` Counter (mode label)
    - ``streaming_vad_resets_total`` Counter (reason label)
    - ``streaming_asr_latency_seconds`` Histogram
    - ``streaming_tag_recomputes_total`` Counter (status label)

Plus the /metrics text exposition and the OTel helper module.
"""

from __future__ import annotations

import pytest
from prometheus_client import generate_latest

from audio_graphy.api import metrics as m
from audio_graphy.core import otel


def _sample(name: str, labels: dict[str, str] | None = None) -> float | None:
    """Read the current value of one metric sample from the registry."""
    for metric in m.REGISTRY.collect():
        for sample in metric.samples:
            if sample.name == name and (
                labels is None or all(sample.labels.get(k) == v for k, v in labels.items())
            ):
                return float(sample.value)
    return None


# ============================================================
# Gauge
# ============================================================


class TestSessionsActiveGauge:
    def test_inc_dec(self) -> None:
        before = _sample("audiography_streaming_sessions_active") or 0.0
        m.STREAMING_SESSIONS_ACTIVE.inc()
        assert _sample("audiography_streaming_sessions_active") == before + 1
        m.STREAMING_SESSIONS_ACTIVE.dec()
        assert _sample("audiography_streaming_sessions_active") == before

    def test_gauge_can_go_to_zero(self) -> None:
        m.STREAMING_SESSIONS_ACTIVE.inc()
        m.STREAMING_SESSIONS_ACTIVE.dec()
        assert _sample("audiography_streaming_sessions_active") == 0.0


# ============================================================
# Counters
# ============================================================


class TestSessionsTotalCounter:
    def test_tenant_label(self) -> None:
        before = _sample("audiography_streaming_sessions_total", {"tenant_id": "metrics-t1"}) or 0.0
        m.STREAMING_SESSIONS_TOTAL.labels(tenant_id="metrics-t1").inc()
        assert (
            _sample("audiography_streaming_sessions_total", {"tenant_id": "metrics-t1"})
            == before + 1
        )

    def test_tenants_counted_independently(self) -> None:
        m.STREAMING_SESSIONS_TOTAL.labels(tenant_id="m-iso-a").inc()
        m.STREAMING_SESSIONS_TOTAL.labels(tenant_id="m-iso-a").inc()
        m.STREAMING_SESSIONS_TOTAL.labels(tenant_id="m-iso-b").inc()
        assert _sample("audiography_streaming_sessions_total", {"tenant_id": "m-iso-a"}) == 2.0
        assert _sample("audiography_streaming_sessions_total", {"tenant_id": "m-iso-b"}) == 1.0


class TestSegmentsTotalCounter:
    def test_confirmed_and_realtime_labels(self) -> None:
        before_c = _sample("audiography_streaming_segments_total", {"mode": "confirmed"}) or 0.0
        before_r = _sample("audiography_streaming_segments_total", {"mode": "realtime"}) or 0.0
        m.STREAMING_SEGMENTS_TOTAL.labels(mode="confirmed").inc(3)
        m.STREAMING_SEGMENTS_TOTAL.labels(mode="realtime").inc(2)
        assert (
            _sample("audiography_streaming_segments_total", {"mode": "confirmed"}) == before_c + 3
        )
        assert _sample("audiography_streaming_segments_total", {"mode": "realtime"}) == before_r + 2


class TestVadResetsCounter:
    def test_reason_labels(self) -> None:
        before_gap = _sample("audiography_streaming_vad_resets_total", {"reason": "seq_gap"}) or 0.0
        before_client = (
            _sample("audiography_streaming_vad_resets_total", {"reason": "client_request"}) or 0.0
        )
        m.STREAMING_VAD_RESETS_TOTAL.labels(reason="seq_gap").inc()
        m.STREAMING_VAD_RESETS_TOTAL.labels(reason="client_request").inc()
        assert (
            _sample("audiography_streaming_vad_resets_total", {"reason": "seq_gap"})
            == before_gap + 1
        )
        assert (
            _sample("audiography_streaming_vad_resets_total", {"reason": "client_request"})
            == before_client + 1
        )


class TestTagRecomputesCounter:
    def test_status_labels(self) -> None:
        before_ok = _sample("audiography_streaming_tag_recomputes_total", {"status": "ok"}) or 0.0
        before_err = (
            _sample("audiography_streaming_tag_recomputes_total", {"status": "error"}) or 0.0
        )
        m.STREAMING_TAG_RECOMPUTES_TOTAL.labels(status="ok").inc()
        m.STREAMING_TAG_RECOMPUTES_TOTAL.labels(status="error").inc()
        assert (
            _sample("audiography_streaming_tag_recomputes_total", {"status": "ok"}) == before_ok + 1
        )
        assert (
            _sample("audiography_streaming_tag_recomputes_total", {"status": "error"})
            == before_err + 1
        )


# ============================================================
# Histogram
# ============================================================


class TestAsrLatencyHistogram:
    def test_observe_increments_count_and_sum(self) -> None:
        before_count = _sample("audiography_streaming_asr_latency_seconds_count") or 0.0
        before_sum = _sample("audiography_streaming_asr_latency_seconds_sum") or 0.0
        m.STREAMING_ASR_LATENCY.observe(0.25)
        assert _sample("audiography_streaming_asr_latency_seconds_count") == before_count + 1
        assert _sample("audiography_streaming_asr_latency_seconds_sum") == pytest.approx(
            before_sum + 0.25
        )

    def test_bucket_samples_exist(self) -> None:
        m.STREAMING_ASR_LATENCY.observe(0.05)
        # At least one +Inf bucket sample must exist.
        value = _sample("audiography_streaming_asr_latency_seconds_bucket", {"le": "+Inf"})
        assert value is not None and value >= 1.0


# ============================================================
# /metrics exposition
# ============================================================


class TestMetricsEndpoint:
    def test_streaming_metrics_in_exposition(self) -> None:
        m.STREAMING_SESSIONS_ACTIVE.inc()
        m.STREAMING_SESSIONS_TOTAL.labels(tenant_id="expo-t").inc()
        m.STREAMING_SEGMENTS_TOTAL.labels(mode="confirmed").inc()
        m.STREAMING_VAD_RESETS_TOTAL.labels(reason="seq_gap").inc()
        m.STREAMING_ASR_LATENCY.observe(0.1)
        m.STREAMING_TAG_RECOMPUTES_TOTAL.labels(status="ok").inc()

        text = generate_latest(m.REGISTRY).decode("utf-8")
        for name in (
            "audiography_streaming_sessions_active",
            "audiography_streaming_sessions_total",
            "audiography_streaming_segments_total",
            "audiography_streaming_vad_resets_total",
            "audiography_streaming_asr_latency_seconds",
            "audiography_streaming_tag_recomputes_total",
        ):
            assert name in text


# ============================================================
# OTel helper module
# ============================================================


class TestOtelHelpers:
    def test_new_trace_id_is_32_hex(self) -> None:
        tid = otel.new_trace_id()
        assert len(tid) == 32
        int(tid, 16)  # must parse as hex

    def test_new_trace_id_unique(self) -> None:
        assert otel.new_trace_id() != otel.new_trace_id()

    def test_streaming_span_noop_without_sdk(self) -> None:
        """streaming_span never raises, regardless of SDK availability."""
        with otel.streaming_span("vad", session_id="s1", tenant_id="t1", trace_id="abc", seq=3):
            pass

    def test_init_otel_returns_bool(self) -> None:
        result = otel.init_otel(console_export=False)
        assert isinstance(result, bool)
        assert result == otel.OTEL_AVAILABLE
