"""Add reception, dialogue unit, state, tag and provenance workspace tables.

Revision ID: 0014_reception_dialogue
Revises: 0013_vp_batch_lookup
Create Date: 2026-07-23 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014_reception_dialogue"
down_revision: str | None = "0013_vp_batch_lookup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _base_columns() -> tuple[sa.Column[object], ...]:
    return (
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
    )


def upgrade() -> None:
    """Create the reception-centric workspace schema."""

    op.create_table(
        "receptions",
        *_base_columns(),
        sa.Column("external_session_id", sa.String(length=128), nullable=True),
        sa.Column("scenario", sa.String(length=32), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
        sa.Column("agent_name", sa.String(length=255), nullable=True),
        sa.Column("customer_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default="proposed",
            nullable=False,
        ),
        sa.Column(
            "merge_mode",
            sa.String(length=16),
            server_default="logical",
            nullable=False,
        ),
        sa.Column("merge_confidence", sa.Float(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("merged_audio_path", sa.String(length=512), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_receptions")),
        sa.CheckConstraint(
            "scenario IN ('gold', 'automotive', 'custom')",
            name="ck_receptions_scenario",
        ),
        sa.CheckConstraint(
            "status IN "
            "('proposed', 'needs_review', 'confirmed', 'processing', "
            "'ready', 'split', 'archived')",
            name="ck_receptions_status",
        ),
        sa.CheckConstraint(
            "merge_mode IN ('logical', 'physical', 'both')",
            name="ck_receptions_merge_mode",
        ),
        sa.CheckConstraint(
            "ended_at >= started_at", name="ck_receptions_time_order"
        ),
        sa.CheckConstraint(
            "merge_confidence IS NULL OR "
            "(merge_confidence >= 0 AND merge_confidence <= 1)",
            name="ck_receptions_merge_confidence",
        ),
        sa.CheckConstraint("version > 0", name="ck_receptions_version"),
    )
    op.create_index(
        op.f("ix_receptions_tenant_id"),
        "receptions",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ux_receptions_tenant_external_session",
        "receptions",
        ["tenant_id", "external_session_id"],
        unique=True,
    )
    op.create_index(
        "ix_receptions_tenant_store_start",
        "receptions",
        ["tenant_id", "store_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_receptions_tenant_status",
        "receptions",
        ["tenant_id", "status"],
        unique=False,
    )

    op.create_table(
        "reception_recordings",
        *_base_columns(),
        sa.Column(
            "reception_id",
            sa.BigInteger(),
            sa.ForeignKey("receptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "recording_id",
            sa.BigInteger(),
            sa.ForeignKey("recordings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("timeline_start_sec", sa.Float(), nullable=False),
        sa.Column("timeline_end_sec", sa.Float(), nullable=False),
        sa.Column(
            "source_start_sec",
            sa.Float(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("source_end_sec", sa.Float(), nullable=True),
        sa.Column(
            "gap_before_sec", sa.Float(), server_default="0", nullable=False
        ),
        sa.Column("decision_source", sa.String(length=16), nullable=False),
        sa.Column("merge_confidence", sa.Float(), nullable=True),
        sa.Column("merge_reasons", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reception_recordings")),
        sa.CheckConstraint(
            "timeline_end_sec > timeline_start_sec",
            name="ck_reception_recordings_timeline",
        ),
        sa.CheckConstraint(
            "source_end_sec IS NULL OR source_end_sec > source_start_sec",
            name="ck_reception_recordings_source_time",
        ),
        sa.CheckConstraint(
            "sequence_no >= 0", name="ck_reception_recordings_sequence"
        ),
        sa.CheckConstraint(
            "decision_source IN ('explicit', 'auto', 'manual')",
            name="ck_reception_recordings_decision_source",
        ),
        sa.CheckConstraint(
            "merge_confidence IS NULL OR "
            "(merge_confidence >= 0 AND merge_confidence <= 1)",
            name="ck_reception_recordings_confidence",
        ),
    )
    op.create_index(
        op.f("ix_reception_recordings_tenant_id"),
        "reception_recordings",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ux_reception_recordings_recording",
        "reception_recordings",
        ["reception_id", "recording_id"],
        unique=True,
    )
    op.create_index(
        "ux_reception_recordings_sequence",
        "reception_recordings",
        ["reception_id", "sequence_no"],
        unique=True,
    )
    op.create_index(
        "ix_reception_recordings_tenant_recording",
        "reception_recordings",
        ["tenant_id", "recording_id"],
        unique=False,
    )

    op.create_table(
        "dialogue_units",
        *_base_columns(),
        sa.Column(
            "reception_id",
            sa.BigInteger(),
            sa.ForeignKey("receptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_recording_id",
            sa.BigInteger(),
            sa.ForeignKey("recordings.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("unit_index", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("start_sec", sa.Float(), nullable=False),
        sa.Column("end_sec", sa.Float(), nullable=False),
        sa.Column("topic", sa.String(length=255), nullable=True),
        sa.Column("business_stage", sa.String(length=64), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("boundary_confidence", sa.Float(), nullable=True),
        sa.Column("boundary_reasons", sa.JSON(), nullable=False),
        sa.Column("segment_refs", sa.JSON(), nullable=False),
        sa.Column("speaker_refs", sa.JSON(), nullable=False),
        sa.Column(
            "edit_status",
            sa.String(length=16),
            server_default="auto",
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dialogue_units")),
        sa.CheckConstraint(
            "end_sec > start_sec", name="ck_dialogue_units_time_order"
        ),
        sa.CheckConstraint("unit_index >= 0", name="ck_dialogue_units_index"),
        sa.CheckConstraint("version > 0", name="ck_dialogue_units_version"),
        sa.CheckConstraint(
            "boundary_confidence IS NULL OR "
            "(boundary_confidence >= 0 AND boundary_confidence <= 1)",
            name="ck_dialogue_units_boundary_confidence",
        ),
        sa.CheckConstraint(
            "edit_status IN ('auto', 'manual_edited', 'locked')",
            name="ck_dialogue_units_edit_status",
        ),
    )
    op.create_index(
        op.f("ix_dialogue_units_tenant_id"),
        "dialogue_units",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ux_dialogue_units_reception_index_version",
        "dialogue_units",
        ["reception_id", "unit_index", "version"],
        unique=True,
    )
    op.create_index(
        "ix_dialogue_units_reception_timeline",
        "dialogue_units",
        ["reception_id", "start_sec", "end_sec"],
        unique=False,
    )
    op.create_index(
        "ix_dialogue_units_tenant_stage",
        "dialogue_units",
        ["tenant_id", "business_stage"],
        unique=False,
    )

    op.create_table(
        "dialogue_state_transitions",
        *_base_columns(),
        sa.Column(
            "reception_id",
            sa.BigInteger(),
            sa.ForeignKey("receptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "dialogue_unit_id",
            sa.BigInteger(),
            sa.ForeignKey("dialogue_units.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("from_state", sa.String(length=64), nullable=False),
        sa.Column("to_state", sa.String(length=64), nullable=False),
        sa.Column("trigger", sa.String(length=128), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("algorithm_version", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_dialogue_state_transitions")
        ),
        sa.CheckConstraint(
            "sequence_no >= 0",
            name="ck_dialogue_state_transitions_sequence",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_dialogue_state_transitions_confidence",
        ),
    )
    op.create_index(
        op.f("ix_dialogue_state_transitions_tenant_id"),
        "dialogue_state_transitions",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ux_dialogue_state_transitions_sequence",
        "dialogue_state_transitions",
        ["reception_id", "sequence_no"],
        unique=True,
    )
    op.create_index(
        "ix_dialogue_state_transitions_tenant_state",
        "dialogue_state_transitions",
        ["tenant_id", "to_state"],
        unique=False,
    )

    op.create_table(
        "dialogue_tag_assignments",
        *_base_columns(),
        sa.Column(
            "reception_id",
            sa.BigInteger(),
            sa.ForeignKey("receptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "dialogue_unit_id",
            sa.BigInteger(),
            sa.ForeignKey("dialogue_units.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("group_key", sa.String(length=64), nullable=False),
        sa.Column("group_version", sa.String(length=64), nullable=False),
        sa.Column("label_key", sa.String(length=128), nullable=False),
        sa.Column("label_value", sa.String(length=255), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("priority", sa.Integer(), server_default="0", nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("model_run_id", sa.String(length=128), nullable=True),
        sa.Column(
            "is_current", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_dialogue_tag_assignments")
        ),
        sa.CheckConstraint(
            "source IN ('rule', 'llm', 'manual', 'imported')",
            name="ck_dialogue_tag_assignments_source",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_dialogue_tag_assignments_confidence",
        ),
    )
    op.create_index(
        op.f("ix_dialogue_tag_assignments_tenant_id"),
        "dialogue_tag_assignments",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ux_dialogue_tags_assignment_version",
        "dialogue_tag_assignments",
        ["dialogue_unit_id", "group_key", "group_version", "label_key"],
        unique=True,
    )
    op.create_index(
        "ix_dialogue_tags_matrix",
        "dialogue_tag_assignments",
        [
            "tenant_id",
            "reception_id",
            "group_key",
            "group_version",
            "is_current",
        ],
        unique=False,
    )
    op.create_index(
        "ix_dialogue_tags_label_value",
        "dialogue_tag_assignments",
        ["tenant_id", "label_key", "label_value"],
        unique=False,
    )

    op.create_table(
        "provenance_events",
        *_base_columns(),
        sa.Column(
            "reception_id",
            sa.BigInteger(),
            sa.ForeignKey("receptions.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("object_type", sa.String(length=64), nullable=False),
        sa.Column("object_ref", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column(
            "actor",
            sa.String(length=128),
            server_default="system",
            nullable=False,
        ),
        sa.Column("algorithm_version", sa.String(length=64), nullable=True),
        sa.Column("parent_refs", sa.JSON(), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provenance_events")),
        sa.CheckConstraint(
            "event_type IN "
            "('created', 'derived', 'merged', 'split', 'edited', "
            "'superseded', 'deleted', 'restored')",
            name="ck_provenance_events_type",
        ),
    )
    op.create_index(
        op.f("ix_provenance_events_tenant_id"),
        "provenance_events",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_provenance_events_reception",
        "provenance_events",
        ["tenant_id", "reception_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_provenance_events_object",
        "provenance_events",
        ["tenant_id", "object_type", "object_ref", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_provenance_events_tenant_time",
        "provenance_events",
        ["tenant_id", "occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop reception workspace tables in reverse FK dependency order."""

    op.drop_table("provenance_events")
    op.drop_table("dialogue_tag_assignments")
    op.drop_table("dialogue_state_transitions")
    op.drop_table("dialogue_units")
    op.drop_table("reception_recordings")
    op.drop_table("receptions")
