"""Isolate sealed-holdout feedback from Harness learning surfaces.

Revision ID: 0023_feedback_lane_isolation
Revises: 0022_tag_harness_evolution
Create Date: 2026-07-25 22:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "0023_feedback_lane_isolation"
down_revision: str | None = "0022_tag_harness_evolution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEARNING_SPLITS = {"train", "validation", "challenge"}


def _backfill_feedback_lanes() -> None:
    connection = op.get_bind()
    feedback = sa.table(
        "tag_feedback_events",
        sa.column("id", sa.BigInteger()),
        sa.column("tenant_id", sa.String()),
        sa.column("review_decision_id", sa.BigInteger()),
        sa.column("truth_tier", sa.String()),
    )
    labels = sa.table(
        "tag_gold_labels",
        sa.column("id", sa.BigInteger()),
        sa.column("tenant_id", sa.String()),
        sa.column("gold_set_version_id", sa.BigInteger()),
        sa.column("review_decision_id", sa.BigInteger()),
        sa.column("split", sa.String()),
    )
    lanes = sa.table(
        "tag_feedback_lane_assignments",
        sa.column("tenant_id", sa.String()),
        sa.column("feedback_event_id", sa.BigInteger()),
        sa.column("source_gold_label_id", sa.BigInteger()),
        sa.column("gold_set_version_id", sa.BigInteger()),
        sa.column("split", sa.String()),
        sa.column("assigned_by", sa.BigInteger()),
        sa.column("assigned_at", sa.DateTime(timezone=True)),
    )
    labels_by_event: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}
    for row in connection.execute(
        sa.select(
            feedback.c.tenant_id,
            feedback.c.id.label("feedback_event_id"),
            labels.c.id.label("source_gold_label_id"),
            labels.c.gold_set_version_id,
            labels.c.split,
        )
        .join(
            labels,
            sa.and_(
                labels.c.tenant_id == feedback.c.tenant_id,
                labels.c.review_decision_id == feedback.c.review_decision_id,
            ),
        )
        .where(feedback.c.truth_tier == "t3")
        .order_by(
            feedback.c.tenant_id,
            feedback.c.id,
            labels.c.id,
        )
    ).mappings():
        event_key = (str(row["tenant_id"]), int(row["feedback_event_id"]))
        split = str(row["split"])
        by_split = labels_by_event.setdefault(event_key, {})
        # Preserve one physical gold-label row as the complete lineage tuple;
        # independent MIN aggregates can combine IDs, versions, and lanes from
        # different rows.
        by_split.setdefault(split, dict(row))

    lane_rows: list[dict[str, Any]] = []
    for by_split in labels_by_event.values():
        if len(by_split) == 1:
            selected = next(iter(by_split.values()))
        elif "holdout" in by_split:
            # Any ambiguity touching sealed holdout must remain sealed.
            selected = by_split["holdout"]
        elif "audit" in by_split:
            # Audit is the strictest available non-learning lane.
            selected = by_split["audit"]
        else:
            # Conflicting learning lanes have no trustworthy assignment.
            # Leaving them unassigned makes dependent artifacts `pending`.
            continue
        lane_rows.append(selected)
    now = datetime.now(UTC)
    if lane_rows:
        connection.execute(
            lanes.insert(),
            [
                {
                    **dict(row),
                    "assigned_by": 0,
                    "assigned_at": now,
                }
                for row in lane_rows
            ],
        )
    split_by_event = {int(row["feedback_event_id"]): str(row["split"]) for row in lane_rows}
    t3_event_ids = set(
        connection.execute(sa.select(feedback.c.id).where(feedback.c.truth_tier == "t3")).scalars()
    )

    badcases = sa.table(
        "tag_badcases",
        sa.column("id", sa.BigInteger()),
        sa.column("source_feedback_event_id", sa.BigInteger()),
        sa.column("root_cause", sa.JSON()),
        sa.column("dataset_split", sa.String()),
        sa.column("status", sa.String()),
        sa.column("fix_candidate_tagger_version_id", sa.BigInteger()),
    )
    badcase_split_by_id: dict[int, str] = {}
    for row in connection.execute(
        sa.select(
            badcases.c.id,
            badcases.c.source_feedback_event_id,
            badcases.c.root_cause,
        )
    ).mappings():
        source_event_id = row["source_feedback_event_id"]
        root_cause = row["root_cause"] if isinstance(row["root_cause"], dict) else {}
        latest_event_id = root_cause.get("latest_feedback_event_id")
        source_event_int = (
            int(source_event_id)
            if isinstance(source_event_id, int) and not isinstance(source_event_id, bool)
            else None
        )
        latest_event_int = (
            int(latest_event_id)
            if isinstance(latest_event_id, int) and not isinstance(latest_event_id, bool)
            else None
        )
        linked_event_id = (
            source_event_int
            if source_event_int in t3_event_ids
            else (latest_event_int if latest_event_int in t3_event_ids else None)
        )
        if linked_event_id is None:
            continue
        dataset_split = split_by_event.get(linked_event_id, "pending")
        values: dict[str, Any] = {"dataset_split": dataset_split}
        if dataset_split not in _LEARNING_SPLITS:
            values.update(
                {
                    "status": "ignored",
                    "fix_candidate_tagger_version_id": None,
                }
            )
        connection.execute(badcases.update().where(badcases.c.id == row["id"]).values(**values))
        badcase_split_by_id[int(row["id"])] = dataset_split

    experiences = sa.table(
        "tag_experience_cases",
        sa.column("id", sa.BigInteger()),
        sa.column("source_badcase_id", sa.BigInteger()),
        sa.column("source_feedback_event_id", sa.BigInteger()),
        sa.column("dataset_split", sa.String()),
        sa.column("eligible", sa.Boolean()),
    )
    for row in connection.execute(
        sa.select(
            experiences.c.id,
            experiences.c.source_badcase_id,
            experiences.c.source_feedback_event_id,
        )
    ).mappings():
        source_event_id = row["source_feedback_event_id"]
        experience_split = (
            split_by_event.get(int(source_event_id), "pending")
            if source_event_id in t3_event_ids
            else badcase_split_by_id.get(int(row["source_badcase_id"] or 0))
        )
        if experience_split is None:
            continue
        values = {"dataset_split": experience_split}
        if experience_split not in _LEARNING_SPLITS:
            values["eligible"] = False
        connection.execute(
            experiences.update().where(experiences.c.id == row["id"]).values(**values)
        )


def _create_lane_immutability_triggers() -> None:
    if op.get_bind().dialect.name != "mysql":
        return
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            f"""
            CREATE TRIGGER trg_tag_feedback_lane_assignments_no_{operation.lower()}
            BEFORE {operation} ON tag_feedback_lane_assignments FOR EACH ROW
            SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'tag_feedback_lane_assignments is append-only'
            """
        )


def upgrade() -> None:
    op.create_table(
        "tag_feedback_lane_assignments",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "feedback_event_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_feedback_events.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_gold_label_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_gold_labels.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "gold_set_version_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_gold_set_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("split", sa.String(length=16), nullable=False),
        sa.Column("assigned_by", sa.BigInteger(), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "split IN ('train', 'validation', 'challenge', 'holdout', 'audit')",
            name="ck_tag_feedback_lane_assignments_split",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_tag_feedback_lane_assignments_tenant_id",
        "tag_feedback_lane_assignments",
        ["tenant_id"],
    )
    op.create_index(
        "ux_tag_feedback_lane_assignments_event",
        "tag_feedback_lane_assignments",
        ["tenant_id", "feedback_event_id"],
        unique=True,
    )
    op.create_index(
        "ix_tag_feedback_lane_assignments_split",
        "tag_feedback_lane_assignments",
        ["tenant_id", "split", "feedback_event_id"],
    )
    op.add_column(
        "tag_badcases",
        sa.Column(
            "dataset_split",
            sa.String(length=16),
            server_default="operational",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_tag_badcases_dataset_split",
        "tag_badcases",
        "dataset_split IN "
        "('operational', 'pending', 'train', 'validation', 'challenge', 'holdout', 'audit')",
    )
    op.create_index(
        "ix_tag_badcases_visibility",
        "tag_badcases",
        ["tenant_id", "dataset_split", "status", "last_seen_at"],
    )
    op.add_column(
        "tag_experience_cases",
        sa.Column(
            "dataset_split",
            sa.String(length=16),
            server_default="operational",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_tag_experience_cases_dataset_split",
        "tag_experience_cases",
        "dataset_split IN "
        "('operational', 'pending', 'train', 'validation', 'challenge', 'holdout', 'audit')",
    )
    op.create_index(
        "ix_tag_experience_cases_visibility",
        "tag_experience_cases",
        ["tenant_id", "dataset_split", "eligible", "materialized_at"],
    )
    _backfill_feedback_lanes()
    _create_lane_immutability_triggers()


def downgrade() -> None:
    if op.get_bind().dialect.name == "mysql":
        op.execute("DROP TRIGGER IF EXISTS trg_tag_feedback_lane_assignments_no_update")
        op.execute("DROP TRIGGER IF EXISTS trg_tag_feedback_lane_assignments_no_delete")
    op.drop_index(
        "ix_tag_experience_cases_visibility",
        table_name="tag_experience_cases",
    )
    op.drop_constraint(
        "ck_tag_experience_cases_dataset_split",
        "tag_experience_cases",
        type_="check",
    )
    op.drop_column("tag_experience_cases", "dataset_split")
    op.drop_index("ix_tag_badcases_visibility", table_name="tag_badcases")
    op.drop_constraint(
        "ck_tag_badcases_dataset_split",
        "tag_badcases",
        type_="check",
    )
    op.drop_column("tag_badcases", "dataset_split")
    op.drop_table("tag_feedback_lane_assignments")
