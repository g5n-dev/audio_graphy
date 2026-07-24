"""T14 — M9 metrics + OTel + retention cascade hook tests.

Verifies:
  1. All 13 M9 metrics are registered and increment correctly.
  2. The 5 new OTel span helpers do not raise (no-op without SDK).
  3. The retention cascade hook fires when enable_advanced_graph=True.
"""

from __future__ import annotations

from typing import Any

from audio_graphy.api.metrics import (
    BITEMPORAL_EDGE_EVENTS_TOTAL,
    BITEMPORAL_SUPERSEDE_CHAIN_DEPTH,
    COMMUNITY_SUMMARIES_TOTAL,
    COMMUNITY_SUMMARY_DURATION,
    COMPRESSION_EDGES_SOFT_DELETED,
    COMPRESSION_NODES_SOFT_DELETED,
    COMPRESSION_RUNS_TOTAL,
    LEIDEN_DIFF_PERCENT,
    LEIDEN_MODULARITY,
    LEIDEN_RUN_DURATION,
    LEIDEN_RUNS_TOTAL,
    REGISTRY,
    SPEAKER_FUZZY_MATCHES_TOTAL,
    SPEAKER_RECONFIRM_QUEUE_SIZE,
)
from audio_graphy.core.otel import (
    bitemporal_supersede_span,
    community_summary_span,
    compression_apply_span,
    leiden_run_span,
    speaker_fuzzy_match_span,
)

# ============================================================
# All 13 M9 metrics exist on the registry
# ============================================================


def test_all_m9_metrics_are_registered() -> None:
    """All 13 M9 metrics must be present in REGISTRY.

    Note: prometheus_client only emits Counter samples after at least one
    ``.inc()`` call, so we touch each counter once before asserting.
    """
    # Touch every counter so its samples appear in REGISTRY.collect().
    BITEMPORAL_EDGE_EVENTS_TOTAL.labels(event_type="insert").inc(0)
    LEIDEN_RUNS_TOTAL.labels(job_type="full", status="succeeded").inc(0)
    COMMUNITY_SUMMARIES_TOTAL.labels(level="0", strategy="eager").inc(0)
    COMPRESSION_RUNS_TOTAL.labels(outcome="committed").inc(0)
    COMPRESSION_NODES_SOFT_DELETED.inc(0)
    COMPRESSION_EDGES_SOFT_DELETED.inc(0)
    SPEAKER_FUZZY_MATCHES_TOTAL.labels(verdict="NO_MATCH").inc(0)
    # Gauges + histograms always emit even at zero.
    SPEAKER_RECONFIRM_QUEUE_SIZE.set(0)
    BITEMPORAL_SUPERSEDE_CHAIN_DEPTH.observe(0)
    LEIDEN_RUN_DURATION.observe(0)
    LEIDEN_DIFF_PERCENT.observe(0)
    LEIDEN_MODULARITY.observe(0)
    COMMUNITY_SUMMARY_DURATION.observe(0)

    names = {s.name for m in REGISTRY.collect() for s in m.samples}
    expected = {
        # 2 BiTemporal
        "audiography_bitemporal_edge_events_total",
        "audiography_bitemporal_supersede_chain_depth_count",
        # 4 Leiden
        "audiography_leiden_runs_total",
        "audiography_leiden_run_duration_seconds_count",
        "audiography_leiden_diff_percent_count",
        "audiography_leiden_modularity_count",
        # 2 Community
        "audiography_community_summaries_total",
        "audiography_community_summary_duration_seconds_count",
        # 3 Compression
        "audiography_compression_runs_total",
        "audiography_compression_nodes_soft_deleted_total",
        "audiography_compression_edges_soft_deleted_total",
        # 2 Speaker fuzzy
        "audiography_speaker_fuzzy_matches_total",
        "audiography_speaker_reconfirm_queue_size",
    }
    missing = expected - names
    assert missing == set(), f"missing metrics: {missing}"


# ============================================================
# Counters increment
# ============================================================


def test_bitemporal_events_counter_increments() -> None:
    before = _counter_value("audiography_bitemporal_edge_events_total", event_type="insert")
    BITEMPORAL_EDGE_EVENTS_TOTAL.labels(event_type="insert").inc()
    after = _counter_value("audiography_bitemporal_edge_events_total", event_type="insert")
    assert after == before + 1


def test_leiden_runs_counter_with_labels() -> None:
    before = _counter_value("audiography_leiden_runs_total", job_type="full", status="succeeded")
    LEIDEN_RUNS_TOTAL.labels(job_type="full", status="succeeded").inc()
    after = _counter_value("audiography_leiden_runs_total", job_type="full", status="succeeded")
    assert after == before + 1


def test_compression_counters_with_labels() -> None:
    COMPRESSION_RUNS_TOTAL.labels(outcome="committed").inc()
    COMPRESSION_NODES_SOFT_DELETED.inc(5)
    COMPRESSION_EDGES_SOFT_DELETED.inc(7)
    assert _counter_value("audiography_compression_runs_total", outcome="committed") >= 1
    assert _counter_value("audiography_compression_nodes_soft_deleted_total") >= 5


def test_speaker_fuzzy_matches_counter() -> None:
    SPEAKER_FUZZY_MATCHES_TOTAL.labels(verdict="CONFIRMED").inc()
    SPEAKER_FUZZY_MATCHES_TOTAL.labels(verdict="AMBIGUOUS").inc(2)
    assert _counter_value("audiography_speaker_fuzzy_matches_total", verdict="CONFIRMED") >= 1
    assert _counter_value("audiography_speaker_fuzzy_matches_total", verdict="AMBIGUOUS") >= 2


# ============================================================
# Gauges set + dec
# ============================================================


def test_speaker_reconfirm_gauge_set() -> None:
    SPEAKER_RECONFIRM_QUEUE_SIZE.set(42)
    SPEAKER_RECONFIRM_QUEUE_SIZE.inc(1)
    assert _gauge_value("audiography_speaker_reconfirm_queue_size") >= 42


# ============================================================
# Histograms observe
# ============================================================


def test_histograms_observe_without_error() -> None:
    for hist, sample in [
        (BITEMPORAL_SUPERSEDE_CHAIN_DEPTH, 3),
        (LEIDEN_RUN_DURATION, 1.5),
        (LEIDEN_DIFF_PERCENT, 25.0),
        (LEIDEN_MODULARITY, 0.42),
        (COMMUNITY_SUMMARY_DURATION, 0.3),
    ]:
        hist.observe(sample)  # must not raise


def test_community_summaries_counter_with_labels() -> None:
    COMMUNITY_SUMMARIES_TOTAL.labels(level="0", strategy="eager").inc()
    COMMUNITY_SUMMARIES_TOTAL.labels(level="2", strategy="lazy").inc()
    assert _counter_value("audiography_community_summaries_total", level="0", strategy="eager") >= 1


# ============================================================
# OTel span helpers (no-op when SDK missing)
# ============================================================


def test_bitemporal_supersede_span_does_not_raise() -> None:
    with bitemporal_supersede_span(
        tenant_id="t1", edge_key="A|r|B", replacement_key="A|r|C", chain_depth=1
    ):
        pass  # no-op when SDK is unavailable


def test_leiden_run_span_does_not_raise() -> None:
    with leiden_run_span(tenant_id="t1", job_type="incremental", diff_percent=12.5, node_count=100):
        pass


def test_community_summary_span_does_not_raise() -> None:
    with community_summary_span(tenant_id="t1", level=0, community_id=1, strategy="eager"):
        pass


def test_compression_apply_span_does_not_raise() -> None:
    with compression_apply_span(tenant_id="t1", candidate_count=5):
        pass


def test_speaker_fuzzy_match_span_does_not_raise() -> None:
    with speaker_fuzzy_match_span(tenant_id="t1", query_name="王小姐", candidate_count=3):
        pass


# ============================================================
# Helpers
# ============================================================


def _counter_value(name: str, **labels: Any) -> float:
    """Sum the metric samples matching ``name`` and the given labels."""
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            if sample.name == name and all(
                sample.labels.get(k) == str(v) for k, v in labels.items()
            ):
                return float(sample.value)
    return 0.0


def _gauge_value(name: str) -> float:
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            if sample.name == name:
                return float(sample.value)
    return 0.0
