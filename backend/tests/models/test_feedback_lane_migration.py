"""Regression tests for the sealed feedback-lane migration backfill."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection


def _load_migration() -> ModuleType:
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "0023_sealed_feedback_lane_isolation.py"
    )
    spec = importlib.util.spec_from_file_location(
        "migration_0023_sealed_feedback_lane_isolation",
        migration_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    local_alembic = ModuleType("alembic")
    local_alembic.op = SimpleNamespace(get_bind=lambda: None)  # type: ignore[attr-defined]
    previous_alembic = sys.modules.get("alembic")
    sys.modules["alembic"] = local_alembic
    try:
        spec.loader.exec_module(module)
    finally:
        if previous_alembic is None:
            sys.modules.pop("alembic", None)
        else:
            sys.modules["alembic"] = previous_alembic
    return module


def _create_backfill_fixture(connection: Connection) -> None:
    metadata = sa.MetaData()
    feedback = sa.Table(
        "tag_feedback_events",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("review_decision_id", sa.Integer(), nullable=False),
        sa.Column("truth_tier", sa.String(), nullable=False),
    )
    labels = sa.Table(
        "tag_gold_labels",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("gold_set_version_id", sa.Integer(), nullable=False),
        sa.Column("review_decision_id", sa.Integer(), nullable=False),
        sa.Column("split", sa.String(), nullable=False),
    )
    sa.Table(
        "tag_feedback_lane_assignments",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("feedback_event_id", sa.Integer(), nullable=False),
        sa.Column("source_gold_label_id", sa.Integer(), nullable=False),
        sa.Column("gold_set_version_id", sa.Integer(), nullable=False),
        sa.Column("split", sa.String(), nullable=False),
        sa.Column("assigned_by", sa.Integer(), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
    )
    badcases = sa.Table(
        "tag_badcases",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_feedback_event_id", sa.Integer()),
        sa.Column("root_cause", sa.JSON(), nullable=False),
        sa.Column("dataset_split", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("fix_candidate_tagger_version_id", sa.Integer()),
    )
    experiences = sa.Table(
        "tag_experience_cases",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_badcase_id", sa.Integer()),
        sa.Column("source_feedback_event_id", sa.Integer()),
        sa.Column("dataset_split", sa.String(), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False),
    )
    metadata.create_all(connection)
    connection.execute(
        feedback.insert(),
        [
            {
                "id": event_id,
                "tenant_id": "chang_an",
                "review_decision_id": decision_id,
                "truth_tier": "t3",
            }
            for event_id, decision_id in ((1, 101), (2, 102), (3, 103))
        ],
    )
    connection.execute(
        labels.insert(),
        [
            # A holdout/challenge conflict must retain the exact holdout row.
            {
                "id": 10,
                "tenant_id": "chang_an",
                "gold_set_version_id": 900,
                "review_decision_id": 101,
                "split": "holdout",
            },
            {
                "id": 11,
                "tenant_id": "chang_an",
                "gold_set_version_id": 100,
                "review_decision_id": 101,
                "split": "challenge",
            },
            # An ambiguity containing only learning lanes must be quarantined.
            {
                "id": 20,
                "tenant_id": "chang_an",
                "gold_set_version_id": 201,
                "review_decision_id": 102,
                "split": "train",
            },
            {
                "id": 21,
                "tenant_id": "chang_an",
                "gold_set_version_id": 202,
                "review_decision_id": 102,
                "split": "validation",
            },
            # Even an unambiguous split must source all lineage from one row.
            {
                "id": 30,
                "tenant_id": "chang_an",
                "gold_set_version_id": 330,
                "review_decision_id": 103,
                "split": "train",
            },
            {
                "id": 31,
                "tenant_id": "chang_an",
                "gold_set_version_id": 300,
                "review_decision_id": 103,
                "split": "train",
            },
        ],
    )
    connection.execute(
        badcases.insert(),
        [
            {
                "id": 100 + event_id,
                "source_feedback_event_id": event_id,
                "root_cause": {},
                "dataset_split": "operational",
                "status": "open",
                "fix_candidate_tagger_version_id": 700 + event_id,
            }
            for event_id in (1, 2, 3)
        ],
    )
    connection.execute(
        experiences.insert(),
        [
            {
                "id": 200 + event_id,
                "source_badcase_id": 100 + event_id,
                "source_feedback_event_id": event_id,
                "dataset_split": "operational",
                "eligible": True,
            }
            for event_id in (1, 2, 3)
        ],
    )


@pytest.mark.integration
def test_0023_backfill_uses_one_label_row_and_quarantines_split_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            _create_backfill_fixture(connection)
            monkeypatch.setattr(migration.op, "get_bind", lambda: connection)

            migration._backfill_feedback_lanes()

            lanes = list(
                connection.execute(
                    sa.text(
                        """
                        SELECT feedback_event_id, source_gold_label_id,
                               gold_set_version_id, split
                        FROM tag_feedback_lane_assignments
                        ORDER BY feedback_event_id
                        """
                    )
                ).mappings()
            )
            badcases = list(
                connection.execute(
                    sa.text(
                        """
                        SELECT source_feedback_event_id, dataset_split, status,
                               fix_candidate_tagger_version_id
                        FROM tag_badcases
                        ORDER BY source_feedback_event_id
                        """
                    )
                ).mappings()
            )
            experiences = list(
                connection.execute(
                    sa.text(
                        """
                        SELECT source_feedback_event_id, dataset_split, eligible
                        FROM tag_experience_cases
                        ORDER BY source_feedback_event_id
                        """
                    )
                ).mappings()
            )
    finally:
        engine.dispose()

    assert [dict(row) for row in lanes] == [
        {
            "feedback_event_id": 1,
            "source_gold_label_id": 10,
            "gold_set_version_id": 900,
            "split": "holdout",
        },
        {
            "feedback_event_id": 3,
            "source_gold_label_id": 30,
            "gold_set_version_id": 330,
            "split": "train",
        },
    ]
    assert [dict(row) for row in badcases] == [
        {
            "source_feedback_event_id": 1,
            "dataset_split": "holdout",
            "status": "ignored",
            "fix_candidate_tagger_version_id": None,
        },
        {
            "source_feedback_event_id": 2,
            "dataset_split": "pending",
            "status": "ignored",
            "fix_candidate_tagger_version_id": None,
        },
        {
            "source_feedback_event_id": 3,
            "dataset_split": "train",
            "status": "open",
            "fix_candidate_tagger_version_id": 703,
        },
    ]
    assert [dict(row) for row in experiences] == [
        {
            "source_feedback_event_id": 1,
            "dataset_split": "holdout",
            "eligible": 0,
        },
        {
            "source_feedback_event_id": 2,
            "dataset_split": "pending",
            "eligible": 0,
        },
        {
            "source_feedback_event_id": 3,
            "dataset_split": "train",
            "eligible": 1,
        },
    ]
