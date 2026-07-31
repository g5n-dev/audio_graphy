"""Add the semantic-tag Harness evolution data foundation.

Revision ID: 0022_tag_harness_evolution
Revises: 0021_llm_cache
Create Date: 2026-07-25 18:00:00.000000
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "0022_tag_harness_evolution"
down_revision: str | None = "0021_llm_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _base_columns() -> list[sa.Column[Any]]:
    return [
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
        sa.PrimaryKeyConstraint("id"),
    ]


def _create(name: str, *items: Any) -> None:
    op.create_table(name, *_base_columns(), *items)
    op.create_index(f"ix_{name}_tenant_id", name, ["tenant_id"])


def _replace_check(
    table_name: str,
    constraint_name: str,
    condition: str,
) -> None:
    op.drop_constraint(constraint_name, table_name, type_="check")
    op.create_check_constraint(constraint_name, table_name, condition)


def _json_value(value: Any, *, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback
    return value


def _backfill_legacy_harness_specs() -> None:
    """Materialize the runtime-compatible V1 Harness for every legacy tagger."""

    taggers = sa.table(
        "tagger_versions",
        sa.column("id", sa.BigInteger()),
        sa.column("schema_version_id", sa.BigInteger()),
        sa.column("engine", sa.String()),
        sa.column("prompt_content", sa.Text()),
        sa.column("rule_bundle", sa.JSON()),
        sa.column("model_version", sa.String()),
        sa.column("thresholds", sa.JSON()),
        sa.column("harness_spec_version", sa.String()),
        sa.column("harness_spec", sa.JSON()),
        sa.column("parent_version_id", sa.BigInteger()),
        sa.column("origin", sa.String()),
        sa.column("optimization_run_id", sa.BigInteger()),
        sa.column("change_summary", sa.Text()),
    )
    connection = op.get_bind()
    rows = list(
        connection.execute(
            sa.select(
                taggers.c.id,
                taggers.c.schema_version_id,
                taggers.c.engine,
                taggers.c.prompt_content,
                taggers.c.rule_bundle,
                taggers.c.model_version,
                taggers.c.thresholds,
            ).where(taggers.c.harness_spec.is_(None))
        ).mappings()
    )
    route_by_engine = {
        "rule": "rule_only",
        "llm": "weak_llm",
        "hybrid": "rule_llm_fusion",
    }
    for row in rows:
        rule_bundle = _json_value(row["rule_bundle"], fallback={})
        thresholds = _json_value(row["thresholds"], fallback={})
        harness_spec = {
            "spec_version": "1.0",
            "context": {
                "neighbor_units": 0,
                "example_policy": "none",
                "example_top_k": 0,
            },
            "tools": {
                "registered_tools": ["rule_engine", "weak_llm", "strong_llm"],
                "primary_model": "weak",
                "critic_model": None,
            },
            "generation": {
                "temperature": 0,
                "max_tokens": 2048,
                "response_format": "strict_json",
                "prompt_template": str(row["prompt_content"] or ""),
            },
            "orchestration": {
                "route": route_by_engine.get(
                    str(row["engine"]),
                    "rule_llm_fusion",
                ),
                "fusion_policy": "rule_priority",
                "critic_enabled": False,
                "rule_bundle": rule_bundle,
            },
            "memory": {"policy": "none", "top_k": 0},
            "output": {
                "thresholds": thresholds,
                "fallback": "review",
                "schema_validation": True,
                "evidence_validation": True,
                "abstain_threshold": 0.0,
                "review_threshold": 0.7,
            },
        }
        connection.execute(
            taggers.update()
            .where(taggers.c.id == row["id"])
            .values(
                harness_spec_version="1.0",
                harness_spec=harness_spec,
                origin="migration",
            )
        )


def _add_compatibility_columns() -> None:
    op.add_column(
        "tagger_versions",
        sa.Column(
            "harness_spec_version",
            sa.String(length=16),
            server_default="1.0",
            nullable=False,
        ),
    )
    op.add_column(
        "tagger_versions",
        sa.Column("harness_spec", sa.JSON(), nullable=True),
    )
    op.add_column(
        "tagger_versions",
        sa.Column("parent_version_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "tagger_versions",
        sa.Column(
            "origin",
            sa.String(length=24),
            server_default="migration",
            nullable=False,
        ),
    )
    op.add_column(
        "tagger_versions",
        sa.Column("optimization_run_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "tagger_versions",
        sa.Column("change_summary", sa.Text(), nullable=True),
    )
    op.create_foreign_key(
        "fk_tagger_versions_parent_version_id",
        "tagger_versions",
        "tagger_versions",
        ["parent_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_tagger_versions_origin",
        "tagger_versions",
        "origin IN ('manual', 'optimizer', 'bootstrap', 'migration')",
    )
    op.create_index(
        "ix_tagger_versions_lineage",
        "tagger_versions",
        ["tenant_id", "parent_version_id", "optimization_run_id"],
    )
    op.create_index(
        "ux_tagger_versions_optimization_run",
        "tagger_versions",
        ["tenant_id", "optimization_run_id"],
        unique=True,
    )
    op.create_index(
        "ux_tag_deployments_tenant_evaluation",
        "tag_deployments",
        ["tenant_id", "evaluation_run_id"],
        unique=True,
    )
    op.create_index(
        "ix_tag_governance_audit_actor_action",
        "tag_governance_audit_events",
        ["tenant_id", "actor_user_id", "action", "resource_type", "resource_id"],
    )
    _replace_check(
        "tag_extraction_jobs",
        "ck_tag_extraction_jobs_type",
        "job_type IN ('extract', 'recompute', 'review_batch', 'evaluate', 'remediate', 'optimize')",
    )
    op.add_column(
        "tag_extraction_jobs",
        sa.Column(
            "origin",
            sa.String(length=16),
            server_default="system",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_tag_extraction_jobs_origin",
        "tag_extraction_jobs",
        "origin IN ('manual', 'serving', 'backfill', 'monitor', 'system')",
    )
    op.add_column(
        "tag_extraction_runs",
        sa.Column(
            "origin",
            sa.String(length=16),
            server_default="system",
            nullable=False,
        ),
    )
    for column in (
        sa.Column("deployment_stage", sa.String(length=24), nullable=True),
        sa.Column("deployment_revision", sa.Integer(), nullable=True),
        sa.Column(
            "served_current",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    ):
        op.add_column("tag_extraction_runs", column)
    op.create_check_constraint(
        "ck_tag_extraction_runs_origin",
        "tag_extraction_runs",
        "origin IN ('manual', 'serving', 'backfill', 'monitor', 'system')",
    )
    op.create_check_constraint(
        "ck_tag_extraction_runs_deployment_stage",
        "tag_extraction_runs",
        "deployment_stage IS NULL OR deployment_stage IN "
        "('shadow', 'canary_5', 'canary_25', 'awaiting_admin', "
        "'production', 'rolled_back', 'retired')",
    )
    op.create_check_constraint(
        "ck_tag_extraction_runs_deployment_revision",
        "tag_extraction_runs",
        "deployment_revision IS NULL OR deployment_revision > 0",
    )

    for column in (
        sa.Column("review_bundle_id", sa.String(length=64), nullable=True),
        sa.Column(
            "selection_policy",
            sa.String(length=64),
            server_default="legacy",
            nullable=False,
        ),
        sa.Column(
            "selection_policy_version",
            sa.String(length=32),
            server_default="1",
            nullable=False,
        ),
        sa.Column("sampling_probability", sa.Float(), nullable=True),
        sa.Column(
            "blind_mode",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("source_deployment_id", sa.BigInteger(), nullable=True),
        sa.Column("source_extraction_run_id", sa.BigInteger(), nullable=True),
        sa.Column("source_harness_execution_id", sa.BigInteger(), nullable=True),
        sa.Column("sampled_deployment_stage", sa.String(length=24), nullable=True),
        sa.Column("sampled_deployment_revision", sa.Integer(), nullable=True),
        sa.Column("sampling_manifest_checksum", sa.String(length=64), nullable=True),
    ):
        op.add_column("tag_review_tasks", column)
    _replace_check(
        "tag_review_tasks",
        "ck_tag_review_tasks_reason",
        "reason IN "
        "('conflict', 'missing', 'low_confidence', 'critical', 'random', "
        "'drift', 'audit', 'gold', 'adjudication', 'active_learning')",
    )
    op.create_check_constraint(
        "ck_tag_review_tasks_sampling_probability",
        "tag_review_tasks",
        "sampling_probability IS NULL OR (sampling_probability > 0 AND sampling_probability <= 1)",
    )
    op.create_check_constraint(
        "ck_tag_review_tasks_sampled_stage",
        "tag_review_tasks",
        "sampled_deployment_stage IS NULL OR sampled_deployment_stage IN "
        "('shadow', 'canary_5', 'canary_25', 'awaiting_admin', 'production')",
    )
    op.create_check_constraint(
        "ck_tag_review_tasks_sampled_revision",
        "tag_review_tasks",
        "sampled_deployment_revision IS NULL OR sampled_deployment_revision > 0",
    )
    op.create_index(
        "ix_tag_review_tasks_selection",
        "tag_review_tasks",
        ["tenant_id", "selection_policy", "review_bundle_id"],
    )

    for column in (
        sa.Column("truth_state", sa.String(length=24), nullable=True),
        sa.Column(
            "truth_tier",
            sa.String(length=8),
            server_default="t1",
            nullable=False,
        ),
        sa.Column(
            "annotator_round",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
        sa.Column("primary_failure_stage", sa.String(length=32), nullable=True),
        sa.Column("reason_codes", sa.JSON(), nullable=True),
        sa.Column("reviewer_confidence", sa.Float(), nullable=True),
        sa.Column("review_duration_ms", sa.Integer(), nullable=True),
    ):
        op.add_column("tag_review_decisions", column)
    _replace_check(
        "tag_review_decisions",
        "ck_tag_review_decisions_action",
        "action IN ('accept', 'correct', 'reject', 'uncertain', 'escalate')",
    )
    op.create_check_constraint(
        "ck_tag_review_decisions_truth_state",
        "tag_review_decisions",
        "truth_state IS NULL OR "
        "truth_state IN ('present', 'absent', 'not_applicable', 'uncertain')",
    )
    op.create_check_constraint(
        "ck_tag_review_decisions_truth_tier",
        "tag_review_decisions",
        "truth_tier IN ('t0', 't1', 't2', 't3')",
    )
    op.create_check_constraint(
        "ck_tag_review_decisions_failure_stage",
        "tag_review_decisions",
        "primary_failure_stage IS NULL OR primary_failure_stage IN "
        "('vad', 'asr', 'speaker', 'boundary', 'schema', 'tag_reasoning', "
        "'evidence', 'fusion', 'insufficient_audio')",
    )
    op.create_check_constraint(
        "ck_tag_review_decisions_reviewer_confidence",
        "tag_review_decisions",
        "reviewer_confidence IS NULL OR (reviewer_confidence >= 0 AND reviewer_confidence <= 1)",
    )
    op.create_check_constraint(
        "ck_tag_review_decisions_quality",
        "tag_review_decisions",
        "annotator_round > 0 AND (review_duration_ms IS NULL OR review_duration_ms >= 0)",
    )

    op.add_column(
        "tag_gold_set_versions",
        sa.Column("dataset_snapshot_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "tag_gold_set_versions",
        sa.Column("completeness_manifest", sa.JSON(), nullable=True),
    )
    for column in (
        sa.Column(
            "truth_state",
            sa.String(length=24),
            server_default="present",
            nullable=False,
        ),
        sa.Column(
            "truth_tier",
            sa.String(length=8),
            server_default="t1",
            nullable=False,
        ),
        sa.Column("input_hash", sa.String(length=64), nullable=True),
        sa.Column("input_snapshot", sa.JSON(), nullable=True),
        sa.Column("annotation_quality", sa.JSON(), nullable=True),
        sa.Column("cohort", sa.String(length=64), nullable=True),
        sa.Column("completeness_manifest", sa.JSON(), nullable=True),
    ):
        op.add_column("tag_gold_labels", column)
    _replace_check(
        "tag_gold_labels",
        "ck_tag_gold_labels_split",
        "split IN ('train', 'validation', 'challenge', 'holdout', 'audit')",
    )
    op.create_check_constraint(
        "ck_tag_gold_labels_truth_state",
        "tag_gold_labels",
        "truth_state IN ('present', 'absent', 'not_applicable', 'uncertain')",
    )
    op.create_check_constraint(
        "ck_tag_gold_labels_truth_tier",
        "tag_gold_labels",
        "truth_tier IN ('t0', 't1', 't2', 't3')",
    )
    op.create_index(
        "ix_tag_gold_labels_dataset_lane",
        "tag_gold_labels",
        ["tenant_id", "gold_set_version_id", "cohort", "split"],
    )

    op.add_column(
        "tag_evaluation_runs",
        sa.Column(
            "evaluator_version",
            sa.String(length=64),
            server_default="tag-evaluator-v2",
            nullable=False,
        ),
    )
    op.add_column(
        "tag_evaluation_runs",
        sa.Column(
            "dataset_snapshot_hash",
            sa.String(length=64),
            server_default="legacy-unfrozen",
            nullable=False,
        ),
    )

    for column in (
        sa.Column(
            "deployment_revision",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
        sa.Column(
            "source",
            sa.String(length=24),
            server_default="manual",
            nullable=False,
        ),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column(
            "is_trusted",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "served_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "paired_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "audited_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "adjudicated_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    ):
        op.add_column("tag_deployment_observations", column)
    op.create_check_constraint(
        "ck_tag_deployment_observations_source",
        "tag_deployment_observations",
        "source IN ('monitor', 'manual', 'imported')",
    )
    op.create_check_constraint(
        "ck_tag_deployment_observations_counts",
        "tag_deployment_observations",
        "sample_count >= 0 AND served_count >= 0 AND paired_count >= 0 "
        "AND audited_count >= 0 AND adjudicated_count >= 0",
    )
    op.create_check_constraint(
        "ck_tag_deployment_observations_revision",
        "tag_deployment_observations",
        "deployment_revision > 0",
    )
    _create(
        "tag_deployment_audit_subjects",
        sa.Column(
            "deployment_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_deployments.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "first_observation_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_deployment_observations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("stage", sa.String(length=24), nullable=False),
        sa.Column("deployment_revision", sa.Integer(), nullable=False),
        sa.Column(
            "count_kind",
            sa.String(length=16),
            nullable=False,
            server_default="audited",
        ),
        sa.Column("subject_type", sa.String(length=24), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "stage IN "
            "('shadow', 'canary_5', 'canary_25', 'awaiting_admin', "
            "'production', 'rolled_back', 'retired')",
            name="ck_tag_deployment_audit_subjects_stage",
        ),
        sa.CheckConstraint(
            "deployment_revision > 0",
            name="ck_tag_deployment_audit_subjects_revision",
        ),
        sa.CheckConstraint(
            "count_kind IN ('served', 'paired', 'audited')",
            name="ck_tag_deployment_audit_subjects_kind",
        ),
        sa.CheckConstraint(
            "subject_type IN ('dialogue_unit', 'reception') AND subject_id > 0",
            name="ck_tag_deployment_audit_subjects_subject",
        ),
    )
    op.create_index(
        "ux_tag_deployment_audit_subjects_stage_subject",
        "tag_deployment_audit_subjects",
        [
            "tenant_id",
            "deployment_id",
            "stage",
            "deployment_revision",
            "count_kind",
            "subject_type",
            "subject_id",
        ],
        unique=True,
    )
    op.create_index(
        "ix_tag_deployment_audit_subjects_observation",
        "tag_deployment_audit_subjects",
        ["tenant_id", "first_observation_id"],
    )


def _create_harness_tables() -> None:
    _create(
        "tag_harness_executions",
        sa.Column(
            "extraction_run_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_extraction_runs.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "tagger_version_id",
            sa.BigInteger(),
            sa.ForeignKey("tagger_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "deployment_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_deployments.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("subject_type", sa.String(length=24), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("scene_profile", sa.JSON(), nullable=False),
        sa.Column("resolved_harness_spec", sa.JSON(), nullable=False),
        sa.Column(
            "route",
            sa.String(length=64),
            server_default="unresolved",
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("output_snapshot", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("next_actions", sa.JSON(), nullable=False),
        sa.Column("artifacts", sa.JSON(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("token_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_units", sa.Float(), server_default="0", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "subject_type IN ('dialogue_unit', 'reception')",
            name="ck_tag_harness_executions_subject",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'abstained')",
            name="ck_tag_harness_executions_status",
        ),
        sa.CheckConstraint(
            "latency_ms >= 0 AND token_count >= 0 AND cost_units >= 0",
            name="ck_tag_harness_executions_usage",
        ),
    )
    op.create_index(
        "ix_tag_harness_executions_tagger",
        "tag_harness_executions",
        ["tenant_id", "tagger_version_id", "created_at"],
    )
    op.create_index(
        "ix_tag_harness_executions_subject",
        "tag_harness_executions",
        ["tenant_id", "subject_type", "subject_id", "created_at"],
    )
    op.create_index(
        "ix_tag_harness_executions_status",
        "tag_harness_executions",
        ["tenant_id", "status", "created_at"],
    )

    op.create_foreign_key(
        "fk_tag_review_tasks_source_deployment_id",
        "tag_review_tasks",
        "tag_deployments",
        ["source_deployment_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_tag_review_tasks_source_extraction_run_id",
        "tag_review_tasks",
        "tag_extraction_runs",
        ["source_extraction_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_tag_review_tasks_source_harness_execution_id",
        "tag_review_tasks",
        "tag_harness_executions",
        ["source_harness_execution_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    _create(
        "tag_harness_stage_traces",
        sa.Column(
            "harness_execution_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_harness_executions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(length=24), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=True),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("observation", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("next_actions", sa.JSON(), nullable=False),
        sa.Column("artifacts", sa.JSON(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("token_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_units", sa.Float(), server_default="0", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "stage IN ('context', 'tools', 'generation', 'orchestration', 'memory', 'output')",
            name="ck_tag_harness_stage_traces_stage",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'skipped')",
            name="ck_tag_harness_stage_traces_status",
        ),
        sa.CheckConstraint(
            "sequence_no > 0 AND latency_ms >= 0 AND token_count >= 0 AND cost_units >= 0",
            name="ck_tag_harness_stage_traces_usage",
        ),
        sa.UniqueConstraint(
            "harness_execution_id",
            "sequence_no",
            name="ux_tag_harness_stage_traces_execution_sequence",
        ),
    )
    op.create_index(
        "ix_tag_harness_stage_traces_status",
        "tag_harness_stage_traces",
        ["tenant_id", "status", "created_at"],
    )

    _create(
        "tag_feedback_events",
        sa.Column(
            "harness_execution_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_harness_executions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "review_decision_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_review_decisions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "deployment_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_deployments.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "truth_tier",
            sa.String(length=8),
            server_default="t0",
            nullable=False,
        ),
        sa.Column("subject_type", sa.String(length=24), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("tag_key", sa.String(length=128), nullable=False),
        sa.Column("truth_state", sa.String(length=24), nullable=True),
        sa.Column("error_stage", sa.String(length=32), nullable=True),
        sa.Column("correction", sa.JSON(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "training_eligible",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("selection_policy", sa.String(length=64), nullable=True),
        sa.Column("sampling_probability", sa.Float(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source IN ('human', 'system_constraint', 'business_outcome', 'model_disagreement')",
            name="ck_tag_feedback_events_source",
        ),
        sa.CheckConstraint(
            "truth_tier IN ('t0', 't1', 't2', 't3')",
            name="ck_tag_feedback_events_truth_tier",
        ),
        sa.CheckConstraint(
            "subject_type IN ('dialogue_unit', 'reception')",
            name="ck_tag_feedback_events_subject",
        ),
        sa.CheckConstraint(
            "truth_state IS NULL OR "
            "truth_state IN ('present', 'absent', 'not_applicable', 'uncertain')",
            name="ck_tag_feedback_events_truth_state",
        ),
        sa.CheckConstraint(
            "error_stage IS NULL OR error_stage IN "
            "('vad', 'asr', 'speaker', 'boundary', 'schema', 'tag_reasoning', "
            "'evidence', 'fusion', 'insufficient_audio')",
            name="ck_tag_feedback_events_error_stage",
        ),
        sa.CheckConstraint(
            "sampling_probability IS NULL OR "
            "(sampling_probability > 0 AND sampling_probability <= 1)",
            name="ck_tag_feedback_events_sampling_probability",
        ),
    )
    op.create_index(
        "ix_tag_feedback_events_training",
        "tag_feedback_events",
        ["tenant_id", "training_eligible", "truth_tier", "occurred_at"],
    )
    op.create_index(
        "ix_tag_feedback_events_subject",
        "tag_feedback_events",
        ["tenant_id", "subject_type", "subject_id", "tag_key"],
    )
    op.create_index(
        "ix_tag_feedback_events_execution",
        "tag_feedback_events",
        ["tenant_id", "harness_execution_id"],
    )

    _create(
        "tag_evaluation_items",
        sa.Column(
            "evaluation_run_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_evaluation_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "gold_label_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_gold_labels.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("subject_type", sa.String(length=24), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("tag_key", sa.String(length=128), nullable=False),
        sa.Column("truth_state", sa.String(length=24), nullable=False),
        sa.Column("candidate_prediction", sa.JSON(), nullable=True),
        sa.Column("baseline_prediction", sa.JSON(), nullable=True),
        sa.Column("candidate_score", sa.Float(), nullable=True),
        sa.Column("baseline_score", sa.Float(), nullable=True),
        sa.Column("candidate_evidence_refs", sa.JSON(), nullable=False),
        sa.Column("baseline_evidence_refs", sa.JSON(), nullable=False),
        sa.Column("error_taxonomy", sa.JSON(), nullable=False),
        sa.Column("slice_snapshot", sa.JSON(), nullable=False),
        sa.Column("paired_delta", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "subject_type IN ('dialogue_unit', 'reception')",
            name="ck_tag_evaluation_items_subject",
        ),
        sa.CheckConstraint(
            "truth_state IN ('present', 'absent', 'not_applicable', 'uncertain')",
            name="ck_tag_evaluation_items_truth_state",
        ),
        sa.CheckConstraint(
            "(candidate_score IS NULL OR "
            "(candidate_score >= 0 AND candidate_score <= 1)) AND "
            "(baseline_score IS NULL OR "
            "(baseline_score >= 0 AND baseline_score <= 1))",
            name="ck_tag_evaluation_items_scores",
        ),
        sa.UniqueConstraint(
            "evaluation_run_id",
            "gold_label_id",
            name="ux_tag_evaluation_items_run_gold",
        ),
    )
    op.create_index(
        "ix_tag_evaluation_items_slice",
        "tag_evaluation_items",
        ["tenant_id", "evaluation_run_id", "tag_key", "truth_state"],
    )

    _create(
        "tag_badcases",
        sa.Column(
            "source_evaluation_item_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_evaluation_items.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "source_feedback_event_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_feedback_events.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("subject_type", sa.String(length=24), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("tag_key", sa.String(length=128), nullable=False),
        sa.Column("failure_stage", sa.String(length=32), nullable=False),
        sa.Column("failure_mode", sa.String(length=96), nullable=False),
        sa.Column("signature_hash", sa.String(length=64), nullable=False),
        sa.Column("cluster_key", sa.String(length=128), nullable=True),
        sa.Column("root_cause", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default="open",
            nullable=False,
        ),
        sa.Column(
            "fix_candidate_tagger_version_id",
            sa.BigInteger(),
            sa.ForeignKey("tagger_versions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("regression_result", sa.JSON(), nullable=False),
        sa.Column(
            "occurrence_count",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "source_evaluation_item_id IS NOT NULL OR source_feedback_event_id IS NOT NULL",
            name="ck_tag_badcases_source",
        ),
        sa.CheckConstraint(
            "subject_type IN ('dialogue_unit', 'reception')",
            name="ck_tag_badcases_subject",
        ),
        sa.CheckConstraint(
            "failure_stage IN "
            "('vad', 'asr', 'speaker', 'boundary', 'schema', 'tag_reasoning', "
            "'evidence', 'fusion', 'insufficient_audio')",
            name="ck_tag_badcases_failure_stage",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'candidate_fix', 'verified', 'resolved', 'reopened', 'ignored')",
            name="ck_tag_badcases_status",
        ),
        sa.CheckConstraint(
            "occurrence_count > 0",
            name="ck_tag_badcases_occurrence_count",
        ),
    )
    op.create_index(
        "ix_tag_badcases_cluster",
        "tag_badcases",
        ["tenant_id", "status", "failure_stage", "cluster_key"],
    )
    op.create_index(
        "ix_tag_badcases_signature",
        "tag_badcases",
        ["tenant_id", "signature_hash"],
    )

    _create(
        "tag_experience_cases",
        sa.Column(
            "source_badcase_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_badcases.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "source_feedback_event_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_feedback_events.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("scene_signature", sa.JSON(), nullable=False),
        sa.Column("failure_signature", sa.JSON(), nullable=False),
        sa.Column("harness_spec", sa.JSON(), nullable=False),
        sa.Column("reward_vector", sa.JSON(), nullable=False),
        sa.Column("outcome", sa.String(length=24), nullable=False),
        sa.Column(
            "quality_tier",
            sa.String(length=8),
            server_default="t2",
            nullable=False,
        ),
        sa.Column(
            "eligible",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("materialized_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_badcase_id IS NOT NULL OR source_feedback_event_id IS NOT NULL",
            name="ck_tag_experience_cases_source",
        ),
        sa.CheckConstraint(
            "outcome IN ('successful', 'failed', 'regressed')",
            name="ck_tag_experience_cases_outcome",
        ),
        sa.CheckConstraint(
            "quality_tier IN ('t0', 't1', 't2', 't3')",
            name="ck_tag_experience_cases_quality_tier",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "checksum",
            name="ux_tag_experience_cases_checksum",
        ),
    )
    op.create_index(
        "ix_tag_experience_cases_retrieval",
        "tag_experience_cases",
        ["tenant_id", "eligible", "outcome", "materialized_at"],
    )

    _create(
        "tag_optimization_runs",
        sa.Column(
            "baseline_tagger_version_id",
            sa.BigInteger(),
            sa.ForeignKey("tagger_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "gold_set_version_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_gold_set_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_extraction_jobs.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("dataset_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "trigger",
            sa.String(length=32),
            quote=True,
            server_default="manual",
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default="queued",
            nullable=False,
        ),
        sa.Column(
            "phase",
            sa.String(length=24),
            server_default="prepare",
            nullable=False,
        ),
        sa.Column("cohort", sa.JSON(), nullable=False),
        sa.Column("objective", sa.JSON(), nullable=False),
        sa.Column("search_budget", sa.JSON(), nullable=False),
        sa.Column(
            "candidate_tagger_version_id",
            sa.BigInteger(),
            sa.ForeignKey("tagger_versions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "winner_tagger_version_id",
            sa.BigInteger(),
            sa.ForeignKey("tagger_versions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("next_actions", sa.JSON(), nullable=False),
        sa.Column("artifacts", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "`trigger` IN ('manual', 'scheduled', 'feedback_threshold', 'insight')",
            name="ck_tag_optimization_runs_trigger",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_tag_optimization_runs_status",
        ),
        sa.CheckConstraint(
            "phase IN ('prepare', 'search', 'validation', 'challenge', 'holdout', 'completed')",
            name="ck_tag_optimization_runs_phase",
        ),
    )
    op.create_index(
        "ix_tag_optimization_runs_status",
        "tag_optimization_runs",
        ["tenant_id", "status", "created_at"],
    )
    op.create_index(
        "ix_tag_optimization_runs_baseline",
        "tag_optimization_runs",
        ["tenant_id", "baseline_tagger_version_id", "created_at"],
    )
    op.create_index(
        "ux_tag_optimization_runs_job",
        "tag_optimization_runs",
        ["tenant_id", "job_id"],
        unique=True,
    )

    _create(
        "tag_optimization_trials",
        sa.Column(
            "optimization_run_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_optimization_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("parent_trial_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "candidate_tagger_version_id",
            sa.BigInteger(),
            sa.ForeignKey("tagger_versions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("mutation", sa.JSON(), nullable=False),
        sa.Column("harness_spec", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "phase",
            sa.String(length=24),
            server_default="train",
            nullable=False,
        ),
        sa.Column("reward_vector", sa.JSON(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("gate_results", sa.JSON(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("next_actions", sa.JSON(), nullable=False),
        sa.Column("artifacts", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "ordinal > 0",
            name="ck_tag_optimization_trials_ordinal",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'pruned', 'completed', 'failed', 'cancelled')",
            name="ck_tag_optimization_trials_status",
        ),
        sa.CheckConstraint(
            "phase IN ('train', 'validation', 'challenge', 'holdout')",
            name="ck_tag_optimization_trials_phase",
        ),
        sa.UniqueConstraint(
            "optimization_run_id",
            "ordinal",
            name="ux_tag_optimization_trials_run_ordinal",
        ),
    )
    op.create_foreign_key(
        "fk_tag_optimization_trials_parent_trial_id",
        "tag_optimization_trials",
        "tag_optimization_trials",
        ["parent_trial_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_tag_optimization_trials_status",
        "tag_optimization_trials",
        ["tenant_id", "optimization_run_id", "status"],
    )


def _create_immutability_triggers() -> None:
    if op.get_bind().dialect.name != "mysql":
        return
    for table_name in (
        "tag_harness_executions",
        "tag_harness_stage_traces",
        "tag_feedback_events",
        "tag_deployment_audit_subjects",
    ):
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"""
                CREATE TRIGGER trg_{table_name}_no_{operation.lower()}
                BEFORE {operation} ON {table_name} FOR EACH ROW
                SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = '{table_name} is append-only'
                """
            )
    op.execute(
        """
        CREATE TRIGGER trg_tag_extraction_jobs_origin_no_update
        BEFORE UPDATE ON tag_extraction_jobs FOR EACH ROW
        BEGIN
            IF NOT (NEW.origin <=> OLD.origin) THEN
                SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'tag job origin is immutable';
            END IF;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_tag_extraction_runs_routing_no_update
        BEFORE UPDATE ON tag_extraction_runs FOR EACH ROW
        BEGIN
            IF NOT (
                NEW.origin <=> OLD.origin
                AND NEW.deployment_stage <=> OLD.deployment_stage
                AND NEW.deployment_revision <=> OLD.deployment_revision
                AND NEW.served_current <=> OLD.served_current
            ) THEN
                SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'tag extraction routing snapshot is immutable';
            END IF;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_tag_review_tasks_sampling_no_update
        BEFORE UPDATE ON tag_review_tasks FOR EACH ROW
        BEGIN
            IF NOT (
                NEW.sampled_deployment_stage <=> OLD.sampled_deployment_stage
                AND NEW.sampled_deployment_revision <=> OLD.sampled_deployment_revision
                AND NEW.sampling_manifest_checksum <=> OLD.sampling_manifest_checksum
                AND NEW.selection_policy <=> OLD.selection_policy
                AND NEW.selection_policy_version <=> OLD.selection_policy_version
                AND NEW.sampling_probability <=> OLD.sampling_probability
                AND NEW.source_deployment_id <=> OLD.source_deployment_id
                AND NEW.source_extraction_run_id <=> OLD.source_extraction_run_id
                AND NEW.source_harness_execution_id <=> OLD.source_harness_execution_id
            ) THEN
                SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'review sampling lineage is immutable';
            END IF;
        END
        """
    )
    child_immutability = (
        (
            "tag_gold_labels",
            "gold_set_version_id",
            "tag_gold_set_versions",
            "status = 'frozen'",
            "frozen gold labels are immutable",
        ),
        (
            "tag_evaluation_metrics",
            "evaluation_run_id",
            "tag_evaluation_runs",
            "status = 'completed' AND passed = 1",
            "certified evaluation metrics are immutable",
        ),
        (
            "tag_gate_results",
            "evaluation_run_id",
            "tag_evaluation_runs",
            "status = 'completed' AND passed = 1",
            "certified evaluation gates are immutable",
        ),
        (
            "tag_evaluation_items",
            "evaluation_run_id",
            "tag_evaluation_runs",
            "status = 'completed' AND passed = 1",
            "certified evaluation items are immutable",
        ),
    )
    for table_name, parent_column, parent_table, condition, message in child_immutability:
        for operation in ("INSERT", "UPDATE", "DELETE"):
            if operation == "INSERT":
                parent_predicate = (
                    f"EXISTS (SELECT 1 FROM {parent_table} "  # noqa: S608
                    f"WHERE id = NEW.{parent_column} AND {condition})"
                )
            elif operation == "UPDATE":
                parent_predicate = (
                    f"EXISTS (SELECT 1 FROM {parent_table} "  # noqa: S608
                    f"WHERE id = OLD.{parent_column} AND {condition}) OR "
                    f"EXISTS (SELECT 1 FROM {parent_table} "
                    f"WHERE id = NEW.{parent_column} AND {condition})"
                )
            else:
                parent_predicate = (
                    f"EXISTS (SELECT 1 FROM {parent_table} "  # noqa: S608
                    f"WHERE id = OLD.{parent_column} AND {condition})"
                )
            op.execute(
                f"""
                CREATE TRIGGER trg_{table_name}_terminal_no_{operation.lower()}
                BEFORE {operation} ON {table_name} FOR EACH ROW
                BEGIN
                    IF {parent_predicate} THEN
                        SIGNAL SQLSTATE '45000'
                        SET MESSAGE_TEXT = '{message}';
                    END IF;
                END
                """
            )
    op.execute(
        """
        CREATE TRIGGER trg_tag_schema_versions_terminal_no_update
        BEFORE UPDATE ON tag_schema_versions FOR EACH ROW
        BEGIN
            IF OLD.status IN ('published', 'deprecated')
               AND NOT (
                   OLD.status = 'published'
                   AND NEW.status = 'deprecated'
                   AND NEW.id <=> OLD.id
                   AND NEW.tenant_id <=> OLD.tenant_id
                   AND NEW.created_at <=> OLD.created_at
                   AND NEW.schema_id <=> OLD.schema_id
                   AND NEW.version <=> OLD.version
                   AND NEW.definitions <=> OLD.definitions
                   AND NEW.checksum <=> OLD.checksum
                   AND NEW.created_by <=> OLD.created_by
                   AND NEW.published_by <=> OLD.published_by
                   AND NEW.published_at <=> OLD.published_at
               )
            THEN
                SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'published tag schema versions are immutable';
            END IF;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_tag_schema_versions_terminal_no_delete
        BEFORE DELETE ON tag_schema_versions FOR EACH ROW
        BEGIN
            IF OLD.status IN ('published', 'deprecated') THEN
                SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'published tag schema versions are immutable';
            END IF;
        END
        """
    )
    # A qualified Harness is immutable except for the single lifecycle transition
    # used by an automatic/manual rollback.  Rejected Harnesses are terminal too:
    # otherwise qualified -> rejected would unlock the row for a later mutation
    # or deletion.  Keeping the configuration and lineage columns byte-for-byte
    # stable prevents the rollback path from becoming an accidental escape hatch.
    op.execute(
        """
        CREATE TRIGGER trg_tagger_versions_terminal_no_update
        BEFORE UPDATE ON tagger_versions FOR EACH ROW
        BEGIN
            IF OLD.status IN ('qualified', 'rejected')
               AND NOT (
                   OLD.status = 'qualified'
                   AND NEW.status = 'rejected'
                   AND NEW.qualified_at IS NULL
                   AND NEW.id <=> OLD.id
                   AND NEW.tenant_id <=> OLD.tenant_id
                   AND NEW.created_at <=> OLD.created_at
                   AND NEW.schema_version_id <=> OLD.schema_version_id
                   AND NEW.version <=> OLD.version
                   AND NEW.engine <=> OLD.engine
                   AND NEW.prompt_content <=> OLD.prompt_content
                   AND NEW.rule_bundle <=> OLD.rule_bundle
                   AND NEW.model_version <=> OLD.model_version
                   AND NEW.thresholds <=> OLD.thresholds
                   AND NEW.harness_spec_version <=> OLD.harness_spec_version
                   AND NEW.harness_spec <=> OLD.harness_spec
                   AND NEW.parent_version_id <=> OLD.parent_version_id
                   AND NEW.origin <=> OLD.origin
                   AND NEW.optimization_run_id <=> OLD.optimization_run_id
                   AND NEW.change_summary <=> OLD.change_summary
                   AND NEW.config_checksum <=> OLD.config_checksum
                   AND NEW.created_by <=> OLD.created_by
               )
            THEN
                SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'terminal Harness versions are immutable';
            END IF;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_tagger_versions_terminal_no_delete
        BEFORE DELETE ON tagger_versions FOR EACH ROW
        BEGIN
            IF OLD.status IN ('qualified', 'rejected') THEN
                SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'terminal Harness versions are immutable';
            END IF;
        END
        """
    )
    terminal_tables = (
        (
            "tag_gold_set_versions",
            "terminal",
            "OLD.status = 'frozen'",
            "frozen gold set versions are immutable",
        ),
        (
            "tag_evaluation_runs",
            "certified",
            "OLD.status = 'completed' AND OLD.passed = 1",
            "certified tag evaluations are immutable",
        ),
    )
    for table_name, trigger_kind, condition, message in terminal_tables:
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"""
                CREATE TRIGGER trg_{table_name}_{trigger_kind}_no_{operation.lower()}
                BEFORE {operation} ON {table_name} FOR EACH ROW
                BEGIN
                    IF {condition} THEN
                        SIGNAL SQLSTATE '45000'
                        SET MESSAGE_TEXT = '{message}';
                    END IF;
                END
                """
            )


def upgrade() -> None:
    _add_compatibility_columns()
    _backfill_legacy_harness_specs()
    _create_harness_tables()
    _create_immutability_triggers()


def _drop_immutability_triggers() -> None:
    if op.get_bind().dialect.name != "mysql":
        return
    op.execute("DROP TRIGGER IF EXISTS trg_tag_review_tasks_sampling_no_update")
    op.execute("DROP TRIGGER IF EXISTS trg_tag_extraction_runs_routing_no_update")
    op.execute("DROP TRIGGER IF EXISTS trg_tag_extraction_jobs_origin_no_update")
    for table_name, trigger_kind in (
        ("tag_schema_versions", "terminal"),
        ("tagger_versions", "terminal"),
        ("tag_gold_set_versions", "terminal"),
        ("tag_evaluation_runs", "certified"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_{trigger_kind}_no_delete")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_{trigger_kind}_no_update")
    for table_name in (
        "tag_harness_executions",
        "tag_feedback_events",
        "tag_harness_stage_traces",
        "tag_deployment_audit_subjects",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_no_delete")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_no_update")
    for table_name in (
        "tag_gold_labels",
        "tag_evaluation_metrics",
        "tag_gate_results",
        "tag_evaluation_items",
    ):
        for operation in ("insert", "update", "delete"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_terminal_no_{operation}")


def _drop_compatibility_columns() -> None:
    op.drop_index(
        "ix_tag_governance_audit_actor_action",
        table_name="tag_governance_audit_events",
    )
    op.drop_index(
        "ux_tag_deployments_tenant_evaluation",
        table_name="tag_deployments",
    )
    _replace_check(
        "tag_extraction_jobs",
        "ck_tag_extraction_jobs_type",
        "job_type IN ('extract', 'recompute', 'review_batch', 'evaluate', 'remediate')",
    )
    op.drop_constraint(
        "ck_tag_extraction_runs_deployment_revision",
        "tag_extraction_runs",
        type_="check",
    )
    op.drop_constraint(
        "ck_tag_extraction_runs_deployment_stage",
        "tag_extraction_runs",
        type_="check",
    )
    for column_name in ("served_current", "deployment_revision", "deployment_stage"):
        op.drop_column("tag_extraction_runs", column_name)
    op.drop_constraint(
        "ck_tag_extraction_runs_origin",
        "tag_extraction_runs",
        type_="check",
    )
    op.drop_column("tag_extraction_runs", "origin")
    op.drop_constraint(
        "ck_tag_extraction_jobs_origin",
        "tag_extraction_jobs",
        type_="check",
    )
    op.drop_column("tag_extraction_jobs", "origin")
    op.drop_constraint(
        "fk_tag_review_tasks_source_harness_execution_id",
        "tag_review_tasks",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_tag_review_tasks_source_extraction_run_id",
        "tag_review_tasks",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_tag_review_tasks_source_deployment_id",
        "tag_review_tasks",
        type_="foreignkey",
    )

    op.drop_constraint(
        "ck_tag_deployment_observations_revision",
        "tag_deployment_observations",
        type_="check",
    )
    op.drop_constraint(
        "ck_tag_deployment_observations_counts",
        "tag_deployment_observations",
        type_="check",
    )
    op.drop_constraint(
        "ck_tag_deployment_observations_source",
        "tag_deployment_observations",
        type_="check",
    )
    for column_name in (
        "adjudicated_count",
        "audited_count",
        "paired_count",
        "served_count",
        "is_trusted",
        "provenance",
        "source",
        "deployment_revision",
    ):
        op.drop_column("tag_deployment_observations", column_name)

    op.drop_column("tag_evaluation_runs", "dataset_snapshot_hash")
    op.drop_column("tag_evaluation_runs", "evaluator_version")

    op.drop_index("ix_tag_gold_labels_dataset_lane", table_name="tag_gold_labels")
    op.drop_constraint(
        "ck_tag_gold_labels_truth_tier",
        "tag_gold_labels",
        type_="check",
    )
    op.drop_constraint(
        "ck_tag_gold_labels_truth_state",
        "tag_gold_labels",
        type_="check",
    )
    _replace_check(
        "tag_gold_labels",
        "ck_tag_gold_labels_split",
        "split IN ('train', 'validation', 'holdout')",
    )
    for column_name in (
        "completeness_manifest",
        "cohort",
        "annotation_quality",
        "input_snapshot",
        "input_hash",
        "truth_tier",
        "truth_state",
    ):
        op.drop_column("tag_gold_labels", column_name)
    op.drop_column("tag_gold_set_versions", "completeness_manifest")
    op.drop_column("tag_gold_set_versions", "dataset_snapshot_hash")

    for constraint_name in (
        "ck_tag_review_decisions_quality",
        "ck_tag_review_decisions_reviewer_confidence",
        "ck_tag_review_decisions_failure_stage",
        "ck_tag_review_decisions_truth_tier",
        "ck_tag_review_decisions_truth_state",
    ):
        op.drop_constraint(
            constraint_name,
            "tag_review_decisions",
            type_="check",
        )
    _replace_check(
        "tag_review_decisions",
        "ck_tag_review_decisions_action",
        "action IN ('accept', 'correct', 'reject')",
    )
    for column_name in (
        "review_duration_ms",
        "reviewer_confidence",
        "reason_codes",
        "primary_failure_stage",
        "annotator_round",
        "truth_tier",
        "truth_state",
    ):
        op.drop_column("tag_review_decisions", column_name)

    op.drop_index("ix_tag_review_tasks_selection", table_name="tag_review_tasks")
    op.drop_constraint(
        "ck_tag_review_tasks_sampled_revision",
        "tag_review_tasks",
        type_="check",
    )
    op.drop_constraint(
        "ck_tag_review_tasks_sampled_stage",
        "tag_review_tasks",
        type_="check",
    )
    op.drop_constraint(
        "ck_tag_review_tasks_sampling_probability",
        "tag_review_tasks",
        type_="check",
    )
    _replace_check(
        "tag_review_tasks",
        "ck_tag_review_tasks_reason",
        "reason IN ('conflict', 'missing', 'low_confidence', 'critical', 'random', 'drift')",
    )
    for column_name in (
        "sampling_manifest_checksum",
        "sampled_deployment_revision",
        "sampled_deployment_stage",
        "source_harness_execution_id",
        "source_extraction_run_id",
        "source_deployment_id",
        "blind_mode",
        "sampling_probability",
        "selection_policy_version",
        "selection_policy",
        "review_bundle_id",
    ):
        op.drop_column("tag_review_tasks", column_name)

    op.drop_index(
        "ux_tagger_versions_optimization_run",
        table_name="tagger_versions",
    )
    op.drop_index("ix_tagger_versions_lineage", table_name="tagger_versions")
    op.drop_constraint(
        "ck_tagger_versions_origin",
        "tagger_versions",
        type_="check",
    )
    op.drop_constraint(
        "fk_tagger_versions_parent_version_id",
        "tagger_versions",
        type_="foreignkey",
    )
    for column_name in (
        "change_summary",
        "optimization_run_id",
        "origin",
        "parent_version_id",
        "harness_spec",
        "harness_spec_version",
    ):
        op.drop_column("tagger_versions", column_name)


def _assert_downgrade_compatible() -> None:
    """Refuse a lossy partial downgrade when retained tables contain V2 enums."""

    connection = op.get_bind()
    checks = (
        (
            "tag_extraction_jobs.job_type",
            "tag_extraction_jobs",
            "job_type",
            ("optimize",),
        ),
        (
            "tag_review_tasks.reason",
            "tag_review_tasks",
            "reason",
            ("audit", "gold", "adjudication", "active_learning"),
        ),
        (
            "tag_review_decisions.action",
            "tag_review_decisions",
            "action",
            ("uncertain", "escalate"),
        ),
        (
            "tag_gold_labels.split",
            "tag_gold_labels",
            "split",
            ("challenge", "audit"),
        ),
    )
    incompatible: list[str] = []
    for label, table_name, column_name, values in checks:
        table = sa.table(table_name, sa.column(column_name, sa.String()))
        count = int(
            connection.execute(
                sa.select(sa.func.count())
                .select_from(table)
                .where(table.c[column_name].in_(values))
            ).scalar_one()
        )
        if count:
            incompatible.append(f"{label}={list(values)!r} ({count} row(s))")
    if incompatible:
        details = "; ".join(incompatible)
        raise RuntimeError(
            "incompatible 0022 data prevents a safe downgrade; "
            f"resolve or export these rows first: {details}"
        )


def downgrade() -> None:
    _assert_downgrade_compatible()
    _drop_immutability_triggers()
    op.drop_table("tag_deployment_audit_subjects")
    op.drop_table("tag_optimization_trials")
    op.drop_table("tag_optimization_runs")
    op.drop_table("tag_experience_cases")
    op.drop_table("tag_badcases")
    op.drop_table("tag_evaluation_items")
    op.drop_table("tag_feedback_events")
    op.drop_table("tag_harness_stage_traces")
    _drop_compatibility_columns()
    op.drop_table("tag_harness_executions")
