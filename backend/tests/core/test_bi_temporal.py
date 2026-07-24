"""T2 — BiTemporalEdgeService tests.

Covers the three mutation paths (insert / merge / supersede), the
time-travel read, and the retention cascade hook. Q1 dual-track ruling
is asserted explicitly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from audio_graphy.core.bi_temporal import (
    BiTemporalEdgeService,
    _edge_key,
    edge_fingerprint,
)
from audio_graphy.core.types import (
    BiTemporalInvalidRangeError,
    BiTemporalSupersedeChainError,
    GraphEdge,
)


@pytest.fixture()
def svc() -> BiTemporalEdgeService:
    return BiTemporalEdgeService(tenant_id="tenant_test")


def _make_edge(
    *,
    valid_at: datetime | None = None,
    invalid_at: datetime | None = None,
    superseded_by: str | None = None,
    expired_at: datetime | None = None,
    weight: float = 1.0,
) -> GraphEdge:
    return GraphEdge(
        source="A",
        target="B",
        relation="推荐",
        weight=weight,
        confidence="EXTRACTED",
        confidence_score=1.0,
        source_ids=["1_0"],
        valid_at=valid_at,
        invalid_at=invalid_at,
        created_at=valid_at,
        expired_at=expired_at,
        superseded_by=superseded_by,
    )


# ============================================================
# insert
# ============================================================


def test_insert_sets_valid_at_to_now_when_omitted(svc: BiTemporalEdgeService) -> None:
    before = datetime.now(UTC)
    edge, event = svc.insert_edge(
        source="A",
        target="B",
        relation="r",
        weight=1.0,
        confidence="EXTRACTED",
        confidence_score=1.0,
        source_ids=["1_0"],
    )
    after = datetime.now(UTC)
    assert edge.valid_at is not None
    assert before <= edge.valid_at <= after
    assert edge.invalid_at is None
    assert edge.expired_at is None
    assert edge.superseded_by is None
    assert event.event_type == "insert"
    assert event.tenant_id == "tenant_test"
    assert event.edge_key == "A|r|B"


def test_insert_honours_explicit_valid_at_in_past(
    svc: BiTemporalEdgeService,
) -> None:
    past = datetime.now(UTC) - timedelta(days=1)
    edge, _ = svc.insert_edge(
        source="A",
        target="B",
        relation="r",
        weight=1.0,
        confidence="EXTRACTED",
        confidence_score=1.0,
        source_ids=[],
        valid_at=past,
    )
    assert edge.valid_at == past


# ============================================================
# merge
# ============================================================


def test_merge_accumulates_weight(svc: BiTemporalEdgeService) -> None:
    existing = _make_edge(weight=2.0)
    new_edge, event = svc.merge_edge(
        existing=existing, weight_delta=1.5, merged_source_ids=["1_0", "2_0"]
    )
    assert new_edge.weight == pytest.approx(3.5)
    assert new_edge.source_ids == ["1_0", "2_0"]
    assert new_edge.valid_at == existing.valid_at
    assert new_edge.invalid_at == existing.invalid_at
    assert event.event_type == "merge"


def test_merge_does_not_supersede(svc: BiTemporalEdgeService) -> None:
    """Merge must NOT touch invalid_at or superseded_by (Q1 distinction)."""
    existing = _make_edge(weight=2.0)
    new_edge, _ = svc.merge_edge(existing=existing, weight_delta=1.0)
    assert new_edge.invalid_at is None
    assert new_edge.superseded_by is None


# ============================================================
# supersede — Q1 dual-track ruling
# ============================================================


def test_supersede_dual_track(svc: BiTemporalEdgeService) -> None:
    """Q1: BOTH auto-invalidate AND supersede pointer; new.valid_at=old.invalid_at."""
    past = datetime.now(UTC) - timedelta(hours=1)
    old = _make_edge(valid_at=past, weight=2.0)

    invalidated, replacement, old_event, new_event = svc.supersede_edge(
        old=old,
        new_relation="推荐",
        new_target="C",
        new_weight=3.0,
        new_confidence="INFERRED",
        new_confidence_score=0.6,
        new_source_ids=["2_0", "3_0"],
    )

    # Q1 dual-track assertions
    assert invalidated.invalid_at is not None
    assert invalidated.superseded_by is not None
    assert invalidated.superseded_by == "A|推荐|C"
    assert replacement.valid_at == invalidated.invalid_at  # Q1 strict

    # Replacement is fresh (open interval)
    assert replacement.invalid_at is None
    assert replacement.expired_at is None
    assert replacement.superseded_by is None
    assert replacement.target == "C"
    assert replacement.weight == pytest.approx(3.0)

    # Two events emitted (Q1 explicit)
    assert old_event.event_type == "supersede"
    assert new_event.event_type == "insert"
    assert old_event.edge_key == "A|推荐|B"
    assert new_event.edge_key == "A|推荐|C"


def test_supersede_refuses_when_old_valid_at_missing(
    svc: BiTemporalEdgeService,
) -> None:
    """Pre-M9 edge corruption: valid_at must be set before supersede."""
    old = _make_edge(valid_at=None)
    with pytest.raises(BiTemporalInvalidRangeError):
        svc.supersede_edge(old=old, new_relation="x")


def test_supersede_refuses_double_supersede(svc: BiTemporalEdgeService) -> None:
    """Q1: a superseded edge MUST NOT be superseded again (chain cap)."""
    past = datetime.now(UTC) - timedelta(hours=1)
    already_superseded = _make_edge(
        valid_at=past, invalid_at=datetime.now(UTC), superseded_by="A|推荐|C"
    )
    with pytest.raises(BiTemporalSupersedeChainError):
        svc.supersede_edge(old=already_superseded, new_relation="x")


def test_supersede_preserves_source_ids_when_omitted(
    svc: BiTemporalEdgeService,
) -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    old = _make_edge(valid_at=past)
    old = GraphEdge(
        source=old.source,
        target=old.target,
        relation=old.relation,
        weight=old.weight,
        confidence=old.confidence,
        confidence_score=old.confidence_score,
        source_ids=["seed_id"],
        valid_at=old.valid_at,
        invalid_at=old.invalid_at,
        created_at=old.created_at,
        expired_at=old.expired_at,
        superseded_by=None,
    )
    _, replacement, _, _ = svc.supersede_edge(old=old, new_relation="推荐", new_target="C")
    assert replacement.source_ids == ["seed_id"]


# ============================================================
# time_travel_query
# ============================================================


def test_time_travel_filters_by_valid_interval(svc: BiTemporalEdgeService) -> None:
    now = datetime.now(UTC)
    long_ago = now - timedelta(days=10)
    recent = now - timedelta(hours=1)

    open_edge = _make_edge(valid_at=long_ago)  # alive
    closed_edge = _make_edge(
        valid_at=long_ago,
        invalid_at=recent,  # closed 1h ago
    )
    future_edge = _make_edge(valid_at=now + timedelta(hours=1))  # not yet valid

    visible = svc.time_travel_query([open_edge, closed_edge, future_edge], as_of=now)
    assert visible == [open_edge]


def test_time_travel_includes_soft_deleted_when_flag_set(
    svc: BiTemporalEdgeService,
) -> None:
    now = datetime.now(UTC)
    long_ago = now - timedelta(days=10)
    soft_deleted = _make_edge(valid_at=long_ago, expired_at=now)
    open_edge = _make_edge(valid_at=long_ago)

    visible_default = svc.time_travel_query([soft_deleted, open_edge], as_of=now)
    assert visible_default == [open_edge]

    visible_admin = svc.time_travel_query(
        [soft_deleted, open_edge],
        as_of=now,
        include_soft_deleted=True,
    )
    assert visible_admin == [soft_deleted, open_edge]


def test_time_travel_pre_m9_edges_always_visible(
    svc: BiTemporalEdgeService,
) -> None:
    """M1-M8 edges constructed without valid_at must remain visible (compat)."""
    legacy = _make_edge(valid_at=None)
    out = svc.time_travel_query([legacy], as_of=datetime.now(UTC))
    assert out == [legacy]


# ============================================================
# retention_cascade
# ============================================================


def test_retention_cascade_soft_deletes_open_edges(
    svc: BiTemporalEdgeService,
) -> None:
    past = datetime.now(UTC) - timedelta(days=5)
    open_a = _make_edge(valid_at=past, weight=1.0)
    open_b = _make_edge(valid_at=past, weight=2.0)
    already_closed = _make_edge(valid_at=past, invalid_at=datetime.now(UTC), weight=3.0)
    out = svc.retention_cascade(edges_on_node=[open_a, open_b, already_closed])
    # Already-closed edge is skipped (idempotent).
    assert len(out) == 2
    for invalidated, event in out:
        assert event.event_type == "soft_delete"
        assert event.actor == "retention"
        assert invalidated.invalid_at is not None


def test_retention_cascade_does_not_set_expired_at(
    svc: BiTemporalEdgeService,
) -> None:
    """Q3: edges use invalid_at for soft-delete; expired_at is reserved for nodes."""
    past = datetime.now(UTC) - timedelta(days=5)
    open_edge = _make_edge(valid_at=past)
    [(invalidated, _)] = svc.retention_cascade(edges_on_node=[open_edge])
    assert invalidated.invalid_at is not None
    assert invalidated.expired_at is None


# ============================================================
# Helpers
# ============================================================


def test_edge_key_format() -> None:
    assert _edge_key("A", "推荐", "B") == "A|推荐|B"


def test_edge_fingerprint_stable_for_same_edge() -> None:
    e = _make_edge(valid_at=datetime(2026, 7, 22, tzinfo=UTC))
    assert edge_fingerprint(e) == edge_fingerprint(e)
    assert len(edge_fingerprint(e)) == 16
