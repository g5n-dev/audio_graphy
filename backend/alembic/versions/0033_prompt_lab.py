"""Offline prompt compilation: artifacts, gradients, demo provenance, silver labels.

Three things here are load-bearing beyond the new tables.

``tagger_versions`` gains ``prompt_artifact_id``, which means the terminal-immutability
trigger must be rebuilt: it compares columns one by one, so a column it does not
mention is a column a qualified version can be silently rewritten through.

``tag_silver_labels`` carries two CHECK constraints that are the whole point of the
table -- machine-proposed labels pinned to the train split and below tier t2, so they
cannot reach an evaluation lane or satisfy a gold-set freeze no matter what the
application layer does.

``tag_extraction_jobs`` learns the ``prompt_compile`` job type. The existing worker
must be given an explicit job-type filter in the same release, or it will claim these
jobs and fail them.

Revision ID: 0033_prompt_lab
Revises: 0032_voiceprint_duration_idx
Create Date: 2026-07-31 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0033_prompt_lab"
down_revision: str | None = "0032_voiceprint_duration_idx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COMPILERS = "'builtin', 'dspy_mipro', 'dspy_bootstrap', 'dspy_gepa', 'textgrad_tgd', 'manual'"
_ARTIFACT_STATUSES = "'draft', 'review', 'accepted', 'rejected', 'superseded'"
_REDACTION_MODES = "'verbatim', 'masked', 'synthetic'"
_TRUTH_STATES = "'present', 'absent', 'not_applicable', 'uncertain'"
_FAILURE_STAGES = (
    "'vad', 'asr', 'speaker', 'boundary', 'schema', "
    "'tag_reasoning', 'evidence', 'fusion', 'insufficient_audio'"
)
_OLD_ORIGINS = "'manual', 'optimizer', 'bootstrap', 'migration'"
_NEW_ORIGINS = f"{_OLD_ORIGINS}, 'prompt_lab'"
_OLD_JOB_TYPES = "'extract', 'recompute', 'review_batch', 'evaluate', 'remediate', 'optimize'"
_NEW_JOB_TYPES = f"{_OLD_JOB_TYPES}, 'prompt_compile'"

# Every column a qualified->rejected transition must leave untouched. Adding a column
# to tagger_versions without adding it here reopens the row for silent mutation.
_IMMUTABLE_COLUMNS = (
    "id",
    "tenant_id",
    "created_at",
    "schema_version_id",
    "version",
    "engine",
    "prompt_content",
    "rule_bundle",
    "model_version",
    "thresholds",
    "harness_spec_version",
    "harness_spec",
    "parent_version_id",
    "origin",
    "optimization_run_id",
    "prompt_artifact_id",
    "change_summary",
    "config_checksum",
    "created_by",
)


_TRIGGER_COMPARISONS_MARKER = "__COLUMN_COMPARISONS__"

_TERMINAL_UPDATE_TRIGGER = """
        CREATE TRIGGER trg_tagger_versions_terminal_no_update
        BEFORE UPDATE ON tagger_versions FOR EACH ROW
        BEGIN
            IF OLD.status IN ('qualified', 'rejected')
               AND NOT (
                   OLD.status = 'qualified'
                   AND NEW.status = 'rejected'
                   AND NEW.qualified_at IS NULL
__COLUMN_COMPARISONS__
               )
            THEN
                SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'terminal Harness versions are immutable';
            END IF;
        END
    """


def _terminal_update_trigger(columns: Sequence[str]) -> str:
    """Render the trigger for a given column list.

    Generated from :data:`_IMMUTABLE_COLUMNS` rather than written out by hand, so the
    trigger body cannot drift from the list of columns it is meant to freeze.
    """

    comparisons = "\n".join(f"                   AND NEW.{name} <=> OLD.{name}" for name in columns)
    return _TERMINAL_UPDATE_TRIGGER.replace(_TRIGGER_COMPARISONS_MARKER, comparisons)


def _rebuild_terminal_trigger(columns: Sequence[str]) -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_tagger_versions_terminal_no_update")
    op.execute(_terminal_update_trigger(columns))


def _replace_check(table: str, name: str, condition: str) -> None:
    op.execute(f"ALTER TABLE {table} DROP CHECK {name}")
    op.create_check_constraint(name, table, condition)


def _create_prompt_artifacts() -> None:
    op.create_table(
        "tag_prompt_artifacts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("compilation_id", sa.BigInteger(), nullable=False),
        sa.Column("optimization_run_id", sa.BigInteger(), nullable=True),
        sa.Column("baseline_tagger_version_id", sa.BigInteger(), nullable=False),
        sa.Column("gold_set_version_id", sa.BigInteger(), nullable=True),
        sa.Column("parent_artifact_id", sa.BigInteger(), nullable=True),
        sa.Column("candidate_tagger_version_id", sa.BigInteger(), nullable=True),
        sa.Column("compiler", sa.String(length=32), nullable=False),
        sa.Column("compiler_version", sa.String(length=64), nullable=False),
        sa.Column("metric_version", sa.String(length=32), nullable=False),
        sa.Column("gradient_prompt_version", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("baseline_prompt", sa.Text(), nullable=False),
        sa.Column("header", sa.Text(), nullable=False),
        sa.Column("rendered_prompt", sa.Text(), nullable=False),
        sa.Column("patches", sa.JSON(), nullable=False),
        sa.Column("demos", sa.JSON(), nullable=False),
        sa.Column("accepted_patch_ids", sa.JSON(), nullable=False),
        sa.Column("prompt_token_estimate", sa.Integer(), nullable=False),
        sa.Column("input_budget_report", sa.JSON(), nullable=False),
        sa.Column("redaction_report", sa.JSON(), nullable=False),
        sa.Column("artifact_checksum", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["baseline_tagger_version_id"],
            ["tagger_versions.id"],
            name="fk_tag_prompt_artifacts_baseline",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["gold_set_version_id"],
            ["tag_gold_set_versions.id"],
            name="fk_tag_prompt_artifacts_gold_set_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_artifact_id"],
            ["tag_prompt_artifacts.id"],
            name="fk_tag_prompt_artifacts_parent",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_tagger_version_id"],
            ["tagger_versions.id"],
            name="fk_tag_prompt_artifacts_candidate",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"compiler IN ({_COMPILERS})",
            name="ck_tag_prompt_artifacts_compiler",
        ),
        sa.CheckConstraint(
            f"status IN ({_ARTIFACT_STATUSES})",
            name="ck_tag_prompt_artifacts_status",
        ),
        sa.CheckConstraint(
            "prompt_token_estimate >= 0",
            name="ck_tag_prompt_artifacts_token_estimate",
        ),
    )
    op.create_index("ix_tag_prompt_artifacts_tenant_id", "tag_prompt_artifacts", ["tenant_id"])
    op.create_index(
        "ux_tag_prompt_artifacts_checksum",
        "tag_prompt_artifacts",
        ["tenant_id", "artifact_checksum"],
        unique=True,
    )
    op.create_index(
        "ix_tag_prompt_artifacts_compilation",
        "tag_prompt_artifacts",
        ["tenant_id", "compilation_id", "created_at"],
    )
    op.create_index(
        "ix_tag_prompt_artifacts_status",
        "tag_prompt_artifacts",
        ["tenant_id", "status", "created_at"],
    )
    op.create_index(
        "ix_tag_prompt_artifacts_run",
        "tag_prompt_artifacts",
        ["tenant_id", "optimization_run_id"],
    )


def _create_prompt_gradients() -> None:
    op.create_table(
        "tag_prompt_gradients",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("artifact_id", sa.BigInteger(), nullable=False),
        sa.Column("patch_id", sa.String(length=32), nullable=False),
        sa.Column("iteration", sa.Integer(), nullable=False),
        sa.Column("source_badcase_id", sa.BigInteger(), nullable=True),
        sa.Column("tag_key", sa.String(length=128), nullable=True),
        sa.Column("failure_stage", sa.String(length=32), nullable=True),
        sa.Column("failure_mode", sa.String(length=96), nullable=True),
        sa.Column("gradient_text", sa.Text(), nullable=False),
        sa.Column("proposed_edit", sa.Text(), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("decided_by", sa.BigInteger(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("evaluation", sa.JSON(), nullable=False),
        sa.Column("llm_logical_request_id", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["tag_prompt_artifacts.id"],
            name="fk_tag_prompt_gradients_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_badcase_id"],
            ["tag_badcases.id"],
            name="fk_tag_prompt_gradients_badcase",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("iteration > 0", name="ck_tag_prompt_gradients_iteration"),
        sa.CheckConstraint(
            "decision IN ('pending', 'accepted', 'rejected')",
            name="ck_tag_prompt_gradients_decision",
        ),
        sa.CheckConstraint(
            f"failure_stage IS NULL OR failure_stage IN ({_FAILURE_STAGES})",
            name="ck_tag_prompt_gradients_failure_stage",
        ),
    )
    op.create_index("ix_tag_prompt_gradients_tenant_id", "tag_prompt_gradients", ["tenant_id"])
    op.create_index(
        "ux_tag_prompt_gradients_patch",
        "tag_prompt_gradients",
        ["artifact_id", "patch_id", "iteration"],
        unique=True,
    )
    op.create_index(
        "ix_tag_prompt_gradients_decision",
        "tag_prompt_gradients",
        ["tenant_id", "artifact_id", "decision"],
    )
    op.create_index(
        "ix_tag_prompt_gradients_badcase",
        "tag_prompt_gradients",
        ["tenant_id", "source_badcase_id"],
    )


def _create_demo_sources() -> None:
    op.create_table(
        "tag_prompt_demo_sources",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("artifact_id", sa.BigInteger(), nullable=False),
        sa.Column("demo_id", sa.String(length=32), nullable=False),
        sa.Column("gold_label_id", sa.BigInteger(), nullable=False),
        sa.Column("reception_id", sa.BigInteger(), nullable=True),
        sa.Column("subject_type", sa.String(length=24), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("segment_ids", sa.JSON(), nullable=False),
        sa.Column("recording_ids", sa.JSON(), nullable=False),
        sa.Column("redaction_mode", sa.String(length=16), nullable=False),
        sa.Column("source_checksum", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["tag_prompt_artifacts.id"],
            name="fk_tag_prompt_demo_sources_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["gold_label_id"],
            ["tag_gold_labels.id"],
            name="fk_tag_prompt_demo_sources_gold_label",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "subject_type IN ('dialogue_unit', 'reception')",
            name="ck_tag_prompt_demo_sources_subject_type",
        ),
        sa.CheckConstraint(
            f"redaction_mode IN ({_REDACTION_MODES})",
            name="ck_tag_prompt_demo_sources_redaction",
        ),
    )
    op.create_index(
        "ix_tag_prompt_demo_sources_tenant_id",
        "tag_prompt_demo_sources",
        ["tenant_id"],
    )
    op.create_index(
        "ux_tag_prompt_demo_sources_demo",
        "tag_prompt_demo_sources",
        ["artifact_id", "demo_id"],
        unique=True,
    )
    op.create_index(
        "ix_tag_prompt_demo_sources_reception",
        "tag_prompt_demo_sources",
        ["tenant_id", "reception_id"],
    )
    op.create_index(
        "ix_tag_prompt_demo_sources_subject",
        "tag_prompt_demo_sources",
        ["tenant_id", "subject_type", "subject_id"],
    )


def _create_silver_labels() -> None:
    op.create_table(
        "tag_silver_labels",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("subject_type", sa.String(length=24), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("reception_id", sa.BigInteger(), nullable=True),
        sa.Column("tag_key", sa.String(length=128), nullable=False),
        sa.Column("tag_value", sa.JSON(), nullable=True),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("truth_state", sa.String(length=24), nullable=False),
        sa.Column("truth_tier", sa.String(length=8), nullable=False),
        sa.Column("split", sa.String(length=16), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=True),
        sa.Column("input_snapshot", sa.JSON(), nullable=True),
        sa.Column("teacher_tagger_version_id", sa.BigInteger(), nullable=True),
        sa.Column("teacher_model_tier", sa.String(length=16), nullable=False),
        sa.Column("teacher_confidence", sa.Float(), nullable=True),
        sa.Column("agreement_count", sa.Integer(), nullable=False),
        sa.Column("promoted_review_task_id", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["teacher_tagger_version_id"],
            ["tagger_versions.id"],
            name="fk_tag_silver_labels_teacher",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["promoted_review_task_id"],
            ["tag_review_tasks.id"],
            name="fk_tag_silver_labels_review_task",
            ondelete="RESTRICT",
        ),
        # The two fences that keep machine labels out of evaluation.
        sa.CheckConstraint("split = 'train'", name="ck_tag_silver_labels_split"),
        sa.CheckConstraint(
            "truth_tier IN ('t0', 't1')",
            name="ck_tag_silver_labels_truth_tier",
        ),
        sa.CheckConstraint(
            f"truth_state IN ({_TRUTH_STATES})",
            name="ck_tag_silver_labels_truth_state",
        ),
        sa.CheckConstraint(
            "teacher_model_tier IN ('strong', 'weak')",
            name="ck_tag_silver_labels_model_tier",
        ),
        sa.CheckConstraint("agreement_count > 0", name="ck_tag_silver_labels_agreement"),
        sa.CheckConstraint(
            "teacher_confidence IS NULL OR (teacher_confidence >= 0 AND teacher_confidence <= 1)",
            name="ck_tag_silver_labels_confidence",
        ),
        sa.CheckConstraint(
            "source IN ('strong_critic', 'seed_import', 'self_consistency')",
            name="ck_tag_silver_labels_source",
        ),
        sa.CheckConstraint(
            "subject_type IN ('dialogue_unit', 'reception')",
            name="ck_tag_silver_labels_subject_type",
        ),
    )
    op.create_index("ix_tag_silver_labels_tenant_id", "tag_silver_labels", ["tenant_id"])
    op.create_index(
        "ux_tag_silver_labels_subject_tag",
        "tag_silver_labels",
        ["tenant_id", "subject_type", "subject_id", "tag_key"],
        unique=True,
    )
    op.create_index(
        "ix_tag_silver_labels_uncertainty",
        "tag_silver_labels",
        ["tenant_id", "tag_key", "teacher_confidence"],
    )
    op.create_index(
        "ix_tag_silver_labels_source",
        "tag_silver_labels",
        ["tenant_id", "source", "created_at"],
    )


def upgrade() -> None:
    _create_prompt_artifacts()
    _create_prompt_gradients()
    _create_demo_sources()
    _create_silver_labels()

    op.add_column(
        "tagger_versions",
        sa.Column("prompt_artifact_id", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_tagger_versions_prompt_artifact",
        "tagger_versions",
        ["tenant_id", "prompt_artifact_id"],
    )
    _replace_check(
        "tagger_versions",
        "ck_tagger_versions_origin",
        f"origin IN ({_NEW_ORIGINS})",
    )
    # Must follow add_column: the trigger body references the new column.
    _rebuild_terminal_trigger(_IMMUTABLE_COLUMNS)

    _replace_check(
        "tag_extraction_jobs",
        "ck_tag_extraction_jobs_type",
        f"job_type IN ({_NEW_JOB_TYPES})",
    )


def downgrade() -> None:
    _replace_check(
        "tag_extraction_jobs",
        "ck_tag_extraction_jobs_type",
        f"job_type IN ({_OLD_JOB_TYPES})",
    )
    # Restore the trigger without the new column before dropping it.
    _rebuild_terminal_trigger([c for c in _IMMUTABLE_COLUMNS if c != "prompt_artifact_id"])
    _replace_check(
        "tagger_versions",
        "ck_tagger_versions_origin",
        f"origin IN ({_OLD_ORIGINS})",
    )
    op.drop_index("ix_tagger_versions_prompt_artifact", table_name="tagger_versions")
    op.drop_column("tagger_versions", "prompt_artifact_id")

    op.drop_table("tag_silver_labels")
    op.drop_table("tag_prompt_demo_sources")
    op.drop_table("tag_prompt_gradients")
    op.drop_table("tag_prompt_artifacts")
