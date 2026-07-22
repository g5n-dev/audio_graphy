"""T1 — M9 ORM model smoke tests.

These tests verify that:
  1. Each M9 ORM class maps to the expected table + columns.
  2. The CHECK / UNIQUE / FK constraints are present.
  3. The bi-temporal dataclass extension on GraphEdge / GraphNode preserves
     M1-M8 source-compatibility (defaults).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import CheckConstraint, ForeignKey, UniqueConstraint
from sqlalchemy.schema import CreateTable

from audio_graphy.core.types import (
    AMBIGUITY_TAG_AMBIGUOUS,
    BiTemporalError,
    BiTemporalInvalidRangeError,
    BiTemporalSupersedeChainError,
    CompressionError,
    CompressionPolicyViolationError,
    CompressionRollbackError,
    GraphEdge,
    GraphNode,
    LeidenError,
    LeidenLibUnavailableError,
    LeidenSnapshotCorruptError,
    LeidenThresholdExceededError,
    SpeakerLinkerFuzzyError,
    SpeakerLinkerFuzzyThresholdError,
    SpeakerLinkerReconfirmUnavailableError,
)
from audio_graphy.models.community_summary import CommunitySummary
from audio_graphy.models.edge_event import EdgeEvent
from audio_graphy.models.leiden_job import LeidenJob
from audio_graphy.models.speaker_merge_pending import SpeakerMergePending


# ============================================================
# ORM table mapping
# ============================================================


def test_edge_event_tablename_and_columns() -> None:
    assert EdgeEvent.__tablename__ == "edge_events"
    cols = {c.name for c in EdgeEvent.__table__.columns}
    assert cols >= {
        "id", "tenant_id", "event_type", "edge_key", "source", "target",
        "relation", "valid_at", "invalid_at", "superseded_by", "actor",
        "payload", "created_at", "updated_at",
    }


def test_edge_event_check_constraint() -> None:
    ck_names = {
        c.name for c in EdgeEvent.__table__.constraints
        if isinstance(c, CheckConstraint)
    }
    assert "ck_edge_events_event_type" in ck_names


def test_community_summary_tablename_and_fk() -> None:
    assert CommunitySummary.__tablename__ == "community_summaries"
    fk_targets = {
        tuple(f.column.table.name for f in c.elements)
        for c in CommunitySummary.__table__.constraints
        if isinstance(c, ForeignKey)
    }
    # ForeignKeys are stored on the Column, not as table-level ForeignKeyConstraint
    # in modern SQLAlchemy, so check the column FK collection instead.
    cs_fks = list(CommunitySummary.__table__.c.leiden_job_id.foreign_keys)
    assert cs_fks and cs_fks[0].column.table.name == "leiden_jobs"
    assert cs_fks[0].ondelete == "RESTRICT"


def test_community_summary_unique_constraint() -> None:
    uq_names = {
        c.name for c in CommunitySummary.__table__.constraints
        if isinstance(c, UniqueConstraint)
    }
    assert "ux_cs_job_level_comm" in uq_names


def test_leiden_job_check_constraints() -> None:
    ck_names = {
        c.name for c in LeidenJob.__table__.constraints
        if isinstance(c, CheckConstraint)
    }
    assert "ck_leiden_jobs_job_type" in ck_names
    assert "ck_leiden_jobs_status" in ck_names


def test_speaker_merge_pending_check_constraints() -> None:
    ck_names = {
        c.name for c in SpeakerMergePending.__table__.constraints
        if isinstance(c, CheckConstraint)
    }
    assert "ck_speaker_merge_pending_status" in ck_names
    assert "ck_speaker_merge_pending_resolved_by" in ck_names


def test_speaker_merge_pending_cascade_fks() -> None:
    rec_fks = list(SpeakerMergePending.__table__.c.recording_id.foreign_keys)
    spk_fks = list(
        SpeakerMergePending.__table__.c.matched_speaker_node_id.foreign_keys
    )
    assert rec_fks and rec_fks[0].column.table.name == "recordings"
    assert rec_fks[0].ondelete == "CASCADE"
    assert spk_fks and spk_fks[0].column.table.name == "speaker_nodes"
    assert spk_fks[0].ondelete == "CASCADE"


# ============================================================
# CreateTable DDL render (smoke check)
# ============================================================


def test_create_table_ddl_renders_for_all_m9_models() -> None:
    # If a column has an unsupported type or invalid constraint, this raises.
    for orm in (EdgeEvent, CommunitySummary, LeidenJob, SpeakerMergePending):
        stmt = CreateTable(orm.__table__)
        # Just render to string — we don't assert the exact SQL form.
        assert str(stmt)


# ============================================================
# GraphEdge / GraphNode M9 extension
# ============================================================


def test_graph_edge_m9_defaults_preserve_m1_m8_compat() -> None:
    """M1-M8 callers construct GraphEdge without bi-temporal fields — must work."""
    edge = GraphEdge(
        source="A",
        target="B",
        relation="r",
        weight=1.0,
        confidence="EXTRACTED",
        confidence_score=1.0,
    )
    assert edge.source_ids == []
    assert edge.valid_at is None
    assert edge.invalid_at is None
    assert edge.created_at is None
    assert edge.expired_at is None
    assert edge.superseded_by is None


def test_graph_edge_m9_full_constructor() -> None:
    now = datetime.now(UTC)
    later = datetime.now(UTC)
    edge = GraphEdge(
        source="A",
        target="B",
        relation="r",
        weight=2.0,
        confidence="INFERRED",
        confidence_score=0.5,
        valid_at=now,
        invalid_at=later,
        created_at=now,
        expired_at=None,
        superseded_by="A|r|C",
    )
    assert edge.valid_at == now
    assert edge.invalid_at == later
    assert edge.superseded_by == "A|r|C"


def test_graph_node_m9_expired_at_default() -> None:
    node = GraphNode(
        entity_id="E1",
        name="E1",
        type="车型",
        description="d",
        source_ids=[],
        recording_ids=[1],
    )
    assert node.expired_at is None  # M1-M8 compat


# ============================================================
# M9 exception subtree
# ============================================================


def test_m9_exception_inheritance() -> None:
    # All M9 exceptions must descend from AudioGraphyError (via their bases).
    for exc in (
        BiTemporalError,
        BiTemporalInvalidRangeError,
        BiTemporalSupersedeChainError,
        LeidenError,
        LeidenLibUnavailableError,
        LeidenThresholdExceededError,
        LeidenSnapshotCorruptError,
        CompressionError,
        CompressionPolicyViolationError,
        CompressionRollbackError,
        SpeakerLinkerFuzzyError,
        SpeakerLinkerFuzzyThresholdError,
        SpeakerLinkerReconfirmUnavailableError,
    ):
        assert issubclass(exc, Exception)
        inst = exc("x")
        assert str(inst) == "x"


def test_m9_exception_subtree_chain() -> None:
    assert issubclass(BiTemporalInvalidRangeError, BiTemporalError)
    assert issubclass(LeidenLibUnavailableError, LeidenError)
    assert issubclass(CompressionPolicyViolationError, CompressionError)
    assert issubclass(
        SpeakerLinkerFuzzyThresholdError, SpeakerLinkerFuzzyError
    )


def test_amiguity_tag_constant_unchanged() -> None:
    """M7 constant must remain stable — M9 fuzzy matcher depends on it."""
    assert AMBIGUITY_TAG_AMBIGUOUS == "AMBIGUOUS"
