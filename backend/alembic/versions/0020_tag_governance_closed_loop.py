"""Add the tenant-scoped tag-governance closed loop.

Revision ID: 0020_tag_governance
Revises: 0019_recording_agent_backfill
Create Date: 2026-07-25 10:00:00.000000
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "0020_tag_governance"
down_revision: str | None = "0019_recording_agent_backfill"
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


def _seed_baseline() -> None:
    """Bootstrap every existing tenant with a usable, published five-dimension baseline."""

    definitions = [
        {
            "key": "stage",
            "name": "接待阶段",
            "category": "dialogue",
            "value_type": "enum",
            "allowed_values": [
                "greeting",
                "requirement",
                "presentation",
                "experience",
                "objection",
                "closing",
            ],
            "subject_types": ["dialogue_unit"],
            "scenarios": ["gold", "automotive", "custom"],
            "evidence_required": True,
            "critical": False,
            "required": True,
            "threshold": 0.7,
        },
        {
            "key": "intent",
            "name": "客户意向",
            "category": "customer",
            "value_type": "enum",
            "allowed_values": ["browse", "compare", "try", "purchase", "follow_up"],
            "subject_types": ["dialogue_unit", "reception"],
            "scenarios": ["gold", "automotive", "custom"],
            "evidence_required": True,
            "critical": False,
            "required": False,
            "threshold": 0.7,
        },
        {
            "key": "objection",
            "name": "客户异议",
            "category": "customer",
            "value_type": "enum",
            "allowed_values": ["price", "product", "service", "timing", "none"],
            "subject_types": ["dialogue_unit", "reception"],
            "scenarios": ["gold", "automotive", "custom"],
            "evidence_required": True,
            "critical": False,
            "required": False,
            "threshold": 0.7,
        },
        {
            "key": "next_step",
            "name": "下一步",
            "category": "conversion",
            "value_type": "enum",
            "allowed_values": ["follow_up", "quote", "trial", "order", "none"],
            "subject_types": ["dialogue_unit", "reception"],
            "scenarios": ["gold", "automotive", "custom"],
            "evidence_required": True,
            "critical": False,
            "required": False,
            "threshold": 0.7,
        },
        {
            "key": "compliance_risk",
            "name": "合规风险",
            "category": "risk",
            "value_type": "enum",
            "allowed_values": [
                "none",
                "overpromise",
                "privacy",
                "personal_transfer",
                "misleading",
            ],
            "subject_types": ["dialogue_unit", "reception"],
            "scenarios": ["gold", "automotive", "custom"],
            "evidence_required": True,
            "critical": True,
            "required": False,
            "threshold": 0.8,
        },
    ]
    rules = {
        "dsl_version": "1",
        "rules": [
            {
                "tag_key": "stage",
                "value": "greeting",
                "contains_any": ["您好", "欢迎光临"],
                "confidence": 0.9,
            },
            {
                "tag_key": "intent",
                "value": "try",
                "contains_any": ["试戴", "试驾", "体验"],
                "confidence": 0.88,
            },
            {
                "tag_key": "intent",
                "value": "purchase",
                "contains_any": ["下单", "购买", "订车", "成交"],
                "confidence": 0.9,
            },
            {
                "tag_key": "objection",
                "value": "price",
                "contains_any": ["太贵", "预算", "优惠", "价格"],
                "confidence": 0.86,
            },
            {
                "tag_key": "next_step",
                "value": "follow_up",
                "contains_any": ["再联系", "回访", "加微信"],
                "confidence": 0.82,
            },
            {
                "tag_key": "compliance_risk",
                "value": "personal_transfer",
                "contains_any": ["转我个人", "私人账户", "个人收款"],
                "confidence": 0.98,
            },
        ],
    }
    definition_json = json.dumps(definitions, ensure_ascii=False, separators=(",", ":"))
    rule_json = json.dumps(rules, ensure_ascii=False, separators=(",", ":"))
    thresholds_json = json.dumps(
        {
            "default": 0.7,
            "stage": 0.7,
            "intent": 0.7,
            "objection": 0.7,
            "next_step": 0.7,
            "compliance_risk": 0.8,
        },
        separators=(",", ":"),
    )
    schema_checksum = hashlib.sha256(
        json.dumps(
            definitions,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    tagger_checksum = hashlib.sha256(
        json.dumps(
            {
                "definitions": definitions,
                "rules": rules,
                "thresholds": json.loads(thresholds_json),
                "model": "rules-bootstrap-v1",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            INSERT INTO tag_schemas
                (tenant_id, created_at, updated_at, `key`, name, description,
                 status, active_version_id, created_by)
            SELECT code, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                   'reception-canonical', '接待标签体系',
                   '由旧五维标签迁移的首个已发布 Schema',
                   'published', NULL, 0
            FROM tenants
            """
        )
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO tag_schema_versions
                (tenant_id, created_at, updated_at, schema_id, version,
                 definitions, checksum, status, created_by, published_by,
                 published_at)
            SELECT tenant_id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, id, '1.0.0',
                   :definitions, :checksum, 'published', 0, 0, CURRENT_TIMESTAMP
            FROM tag_schemas
            WHERE `key` = 'reception-canonical'
            """
        ),
        {"definitions": definition_json, "checksum": schema_checksum},
    )
    bind.execute(
        sa.text(
            """
            UPDATE tag_schemas
            SET active_version_id = (
                SELECT tag_schema_versions.id
                FROM tag_schema_versions
                WHERE tag_schema_versions.schema_id = tag_schemas.id
                  AND tag_schema_versions.version = '1.0.0'
            )
            WHERE `key` = 'reception-canonical'
            """
        )
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO tagger_versions
                (tenant_id, created_at, updated_at, schema_version_id, version,
                 engine, prompt_content, rule_bundle, model_version, thresholds,
                 config_checksum, status, created_by, qualified_at)
            SELECT tenant_id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, id,
                   'bootstrap-rules-v1', 'rule', '', :rules,
                   'rules-bootstrap-v1', :thresholds, :checksum,
                   'qualified', 0, CURRENT_TIMESTAMP
            FROM tag_schema_versions
            WHERE version = '1.0.0' AND status = 'published'
            """
        ),
        {
            "rules": rule_json,
            "thresholds": thresholds_json,
            "checksum": tagger_checksum,
        },
    )
    for tag_key in (
        "stage",
        "intent",
        "objection",
        "next_step",
        "compliance_risk",
    ):
        bind.execute(
            sa.text(
                """
                INSERT INTO legacy_tag_mappings
                    (tenant_id, created_at, updated_at, legacy_tag_path,
                     schema_version_id, tag_key, mapping, is_deterministic)
                SELECT tenant_id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                       :legacy_path, id, :tag_key, :mapping, 1
                FROM tag_schema_versions
                WHERE version = '1.0.0' AND status = 'published'
                """
            ),
            {
                "legacy_path": f"dialogue_tag_assignments.{tag_key}",
                "tag_key": tag_key,
                "mapping": json.dumps(
                    {
                        "mode": "identity",
                        "source_subject": "dialogue_unit",
                        "target_subject": "dialogue_unit",
                    },
                    separators=(",", ":"),
                ),
            },
        )


def upgrade() -> None:
    _create(
        "tag_schemas",
        sa.Column("key", sa.String(96), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("status", sa.String(24), server_default="draft", nullable=False),
        sa.Column("active_version_id", sa.BigInteger()),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft','published','deprecated')",
            name="ck_tag_schemas_status",
        ),
        sa.UniqueConstraint("tenant_id", "key", name="ux_tag_schemas_tenant_key"),
    )
    _create(
        "tag_schema_versions",
        sa.Column(
            "schema_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_schemas.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("definitions", sa.JSON(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), server_default="draft", nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("published_by", sa.BigInteger()),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('draft','validated','published','deprecated')",
            name="ck_tag_schema_versions_status",
        ),
        sa.UniqueConstraint("schema_id", "version", name="ux_tag_schema_versions_schema_version"),
        sa.UniqueConstraint("schema_id", "checksum", name="ux_tag_schema_versions_schema_checksum"),
    )
    op.create_index(
        "ix_tag_schema_versions_tenant_status",
        "tag_schema_versions",
        ["tenant_id", "status"],
    )
    _create(
        "tagger_versions",
        sa.Column(
            "schema_version_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_schema_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("engine", sa.String(16), server_default="hybrid", nullable=False),
        sa.Column("prompt_content", sa.Text(), nullable=False),
        sa.Column("rule_bundle", sa.JSON(), nullable=False),
        sa.Column("model_version", sa.String(128), nullable=False),
        sa.Column("thresholds", sa.JSON(), nullable=False),
        sa.Column("config_checksum", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), server_default="draft", nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("qualified_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "engine IN ('rule','llm','hybrid')",
            name="ck_tagger_versions_engine",
        ),
        sa.CheckConstraint(
            "status IN ('draft','validating','evaluating','rejected','qualified')",
            name="ck_tagger_versions_status",
        ),
        sa.UniqueConstraint("tenant_id", "version", name="ux_tagger_versions_tenant_version"),
        sa.UniqueConstraint(
            "tenant_id",
            "config_checksum",
            name="ux_tagger_versions_tenant_checksum",
        ),
    )
    _create(
        "tag_extraction_jobs",
        sa.Column("job_type", sa.String(24), nullable=False),
        sa.Column("status", sa.String(24), server_default="queued", nullable=False),
        sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column("tagger_version_id", sa.BigInteger()),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("total_items", sa.Integer(), server_default="0", nullable=False),
        sa.Column("completed_items", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_items", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_subset", sa.JSON(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("lease_owner", sa.String(128)),
        sa.Column("lease_token", sa.String(64)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("last_error_message", sa.Text()),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "job_type IN ('extract','recompute','review_batch','evaluate','remediate')",
            name="ck_tag_extraction_jobs_type",
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','retry_wait','completed','failed','cancelled')",
            name="ck_tag_extraction_jobs_status",
        ),
        sa.CheckConstraint(
            "completed_items >= 0 AND failed_items >= 0 AND total_items >= 0",
            name="ck_tag_extraction_jobs_counts",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0 AND revision > 0",
            name="ck_tag_extraction_jobs_revisions",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="ux_tag_extraction_jobs_tenant_idempotency",
        ),
    )
    op.create_index(
        "ix_tag_extraction_jobs_claim",
        "tag_extraction_jobs",
        ["status", "next_attempt_at", "lease_expires_at", "created_at"],
    )
    op.create_index(
        "ix_tag_extraction_jobs_tenant_status",
        "tag_extraction_jobs",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_tag_extraction_jobs_tenant_created",
        "tag_extraction_jobs",
        ["tenant_id", "created_at", "id"],
    )
    _create(
        "tag_extraction_runs",
        sa.Column(
            "job_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_extraction_jobs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("subject_type", sa.String(24), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("tagger_version_id", sa.BigInteger()),
        sa.Column("deployment_id", sa.BigInteger()),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("input_snapshot", sa.JSON(), nullable=False),
        sa.Column("output_snapshot", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), server_default="running", nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "subject_type IN ('dialogue_unit','reception','recording')",
            name="ck_tag_extraction_runs_subject",
        ),
        sa.CheckConstraint(
            "status IN ('running','completed','failed','cached')",
            name="ck_tag_extraction_runs_status",
        ),
        sa.UniqueConstraint(
            "job_id",
            "subject_type",
            "subject_id",
            "input_hash",
            name="ux_tag_extraction_runs_job_subject_hash",
        ),
    )
    op.create_index(
        "ix_tag_extraction_runs_deployment_terminal",
        "tag_extraction_runs",
        ["tenant_id", "deployment_id", "status", "finished_at"],
    )
    op.create_index(
        "ix_tag_extraction_runs_tagger_terminal_subject",
        "tag_extraction_runs",
        [
            "tenant_id",
            "tagger_version_id",
            "status",
            "finished_at",
            "subject_type",
            "subject_id",
        ],
    )
    _create(
        "tag_assignment_facts",
        sa.Column("subject_type", sa.String(24), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("reception_id", sa.BigInteger()),
        sa.Column("dialogue_unit_id", sa.BigInteger()),
        sa.Column("tag_key", sa.String(128), nullable=False),
        sa.Column("tag_value", sa.JSON()),
        sa.Column("confidence", sa.Float()),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("schema_version_id", sa.BigInteger()),
        sa.Column("tagger_version_id", sa.BigInteger()),
        sa.Column("extraction_run_id", sa.BigInteger()),
        sa.Column("deployment_id", sa.BigInteger()),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("recipe_hash", sa.String(64), nullable=False),
        sa.Column("superseded_fact_id", sa.BigInteger()),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("tombstone", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger()),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "subject_type IN ('dialogue_unit','reception')",
            name="ck_tag_assignment_facts_subject",
        ),
        sa.CheckConstraint(
            "source IN ('rule','llm','manual','imported')",
            name="ck_tag_assignment_facts_source",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_tag_assignment_facts_confidence",
        ),
        sa.CheckConstraint("revision > 0", name="ck_tag_assignment_facts_revision"),
        sa.UniqueConstraint(
            "tenant_id",
            "subject_type",
            "subject_id",
            "tag_key",
            "revision",
            name="ux_tag_assignment_facts_revision",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "recipe_hash",
            name="ux_tag_assignment_facts_recipe",
        ),
    )
    op.create_index(
        "ix_tag_assignment_facts_lineage",
        "tag_assignment_facts",
        ["tenant_id", "subject_type", "subject_id", "tag_key", "created_at"],
    )
    op.create_index(
        "ix_tag_assignment_facts_input_hash",
        "tag_assignment_facts",
        ["tenant_id", "input_hash"],
    )
    op.create_index(
        "ix_tag_assignment_facts_extraction_run",
        "tag_assignment_facts",
        ["tenant_id", "extraction_run_id"],
    )
    op.create_index(
        "ix_tag_assignment_facts_deployment_window",
        "tag_assignment_facts",
        [
            "tenant_id",
            "deployment_id",
            "tagger_version_id",
            "tombstone",
            "assigned_at",
            "id",
        ],
    )
    _create(
        "tag_assignment_current",
        sa.Column("subject_type", sa.String(24), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("tag_key", sa.String(128), nullable=False),
        sa.Column(
            "fact_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_assignment_facts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "subject_type IN ('dialogue_unit','reception')",
            name="ck_tag_assignment_current_subject",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "subject_type",
            "subject_id",
            "tag_key",
            name="ux_tag_assignment_current_subject_key",
        ),
        sa.UniqueConstraint("fact_id", name="ux_tag_assignment_current_fact"),
    )
    _create(
        "tag_review_tasks",
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("subject_type", sa.String(24), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("reception_id", sa.BigInteger()),
        sa.Column("tag_key", sa.String(128), nullable=False),
        sa.Column("proposed_value", sa.JSON()),
        sa.Column("confidence", sa.Float()),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("proposed_fact_id", sa.BigInteger()),
        sa.Column("schema_version_id", sa.BigInteger()),
        sa.Column("tagger_version_id", sa.BigInteger()),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("priority", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claimed_by", sa.BigInteger()),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "reason IN ('conflict','missing','low_confidence','critical','random','drift')",
            name="ck_tag_review_tasks_reason",
        ),
        sa.CheckConstraint(
            "status IN ('pending','claimed','resolved','skipped')",
            name="ck_tag_review_tasks_status",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_tag_review_tasks_confidence",
        ),
    )
    op.create_index(
        "ix_tag_review_tasks_queue",
        "tag_review_tasks",
        ["tenant_id", "status", "priority", "created_at"],
    )
    op.create_index(
        "ix_tag_review_tasks_batch",
        "tag_review_tasks",
        ["tenant_id", "batch_id"],
    )
    _create(
        "tag_review_decisions",
        sa.Column(
            "task_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_review_tasks.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("corrected_value", sa.JSON()),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("resulting_fact_id", sa.BigInteger()),
        sa.Column("reviewer_user_id", sa.BigInteger(), nullable=False),
        sa.Column("adjudication", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('accept','correct','reject')",
            name="ck_tag_review_decisions_action",
        ),
    )
    op.create_index(
        "ix_tag_review_decisions_task",
        "tag_review_decisions",
        ["tenant_id", "task_id", "decided_at"],
    )
    op.create_index(
        "ix_tag_review_decisions_window",
        "tag_review_decisions",
        ["tenant_id", "decided_at", "task_id", "id"],
    )
    _create(
        "tag_gold_sets",
        sa.Column("key", sa.String(96), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("schema_version_id", sa.BigInteger(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("tenant_id", "key", name="ux_tag_gold_sets_tenant_key"),
    )
    _create(
        "tag_gold_set_versions",
        sa.Column(
            "gold_set_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_gold_sets.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), server_default="draft", nullable=False),
        sa.Column("checksum", sa.String(64)),
        sa.Column("item_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("frozen_by", sa.BigInteger()),
        sa.Column("frozen_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('draft','frozen','retired')",
            name="ck_tag_gold_set_versions_status",
        ),
        sa.UniqueConstraint(
            "gold_set_id",
            "version",
            name="ux_tag_gold_set_versions_set_version",
        ),
    )
    _create(
        "tag_gold_labels",
        sa.Column(
            "gold_set_version_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_gold_set_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("review_decision_id", sa.BigInteger(), nullable=False),
        sa.Column("reception_id", sa.BigInteger()),
        sa.Column("subject_type", sa.String(24), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("tag_key", sa.String(128), nullable=False),
        sa.Column("tag_value", sa.JSON()),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("split", sa.String(16), nullable=False),
        sa.CheckConstraint(
            "split IN ('train','validation','holdout')",
            name="ck_tag_gold_labels_split",
        ),
        sa.UniqueConstraint(
            "gold_set_version_id",
            "review_decision_id",
            name="ux_tag_gold_labels_version_decision",
        ),
    )
    op.create_index(
        "ix_tag_gold_labels_version_split",
        "tag_gold_labels",
        ["tenant_id", "gold_set_version_id", "split"],
    )
    op.create_index(
        "ux_tag_gold_labels_version_subject_tag",
        "tag_gold_labels",
        ["gold_set_version_id", "subject_type", "subject_id", "tag_key"],
        unique=True,
    )
    _create(
        "tag_evaluation_runs",
        sa.Column("tagger_version_id", sa.BigInteger(), nullable=False),
        sa.Column("baseline_tagger_version_id", sa.BigInteger(), nullable=False),
        sa.Column("gold_set_version_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("baseline_metrics", sa.JSON(), nullable=False),
        sa.Column("passed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued','running','completed','failed')",
            name="ck_tag_evaluation_runs_status",
        ),
    )
    op.create_index(
        "ix_tag_evaluation_runs_tenant_status",
        "tag_evaluation_runs",
        ["tenant_id", "status"],
    )
    _create(
        "tag_evaluation_metrics",
        sa.Column(
            "evaluation_run_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_evaluation_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("metric_key", sa.String(96), nullable=False),
        sa.Column("label_key", sa.String(128)),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("support", sa.Integer()),
        sa.UniqueConstraint(
            "evaluation_run_id",
            "metric_key",
            "label_key",
            name="ux_tag_evaluation_metrics_identity",
        ),
    )
    _create(
        "tag_gate_results",
        sa.Column(
            "evaluation_run_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_evaluation_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("actual", sa.Float()),
        sa.Column("threshold", sa.Float()),
        sa.Column("message", sa.Text(), nullable=False),
        sa.UniqueConstraint("evaluation_run_id", "code", name="ux_tag_gate_results_run_code"),
    )
    _create(
        "tag_deployments",
        sa.Column("tagger_version_id", sa.BigInteger(), nullable=False),
        sa.Column("evaluation_run_id", sa.BigInteger(), nullable=False),
        sa.Column("baseline_tagger_version_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(24), server_default="shadow", nullable=False),
        sa.Column("traffic_percent", sa.Integer(), server_default="0", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "promotion_paused",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("pause_reason", sa.Text()),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("approved_by", sa.BigInteger()),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("rolled_back_by", sa.BigInteger()),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True)),
        sa.Column("rollback_reason", sa.Text()),
        sa.CheckConstraint(
            "status IN ('shadow','canary_5','canary_25','awaiting_admin','production','rolled_back','retired')",
            name="ck_tag_deployments_status",
        ),
        sa.CheckConstraint(
            "traffic_percent >= 0 AND traffic_percent <= 100 AND revision > 0",
            name="ck_tag_deployments_traffic_revision",
        ),
    )
    op.create_index(
        "ix_tag_deployments_tenant_status",
        "tag_deployments",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_tag_deployments_monitor",
        "tag_deployments",
        ["status", "tenant_id", "id"],
    )
    op.create_index(
        "ix_tag_deployments_route",
        "tag_deployments",
        ["tenant_id", "status", "approved_at", "id"],
    )
    op.create_index(
        "ix_tag_deployments_baseline_active",
        "tag_deployments",
        ["tenant_id", "baseline_tagger_version_id", "status", "created_at", "id"],
    )
    _create(
        "tag_deployment_observations",
        sa.Column(
            "deployment_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_deployments.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("stage", sa.String(24), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sample_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("breach_codes", sa.JSON(), nullable=False),
        sa.Column("action", sa.String(24), server_default="observe", nullable=False),
        sa.CheckConstraint(
            "action IN ('observe','pause','rollback')",
            name="ck_tag_deployment_observations_action",
        ),
        sa.CheckConstraint(
            "stage IN ('shadow','canary_5','canary_25','awaiting_admin','production','rolled_back','retired')",
            name="ck_tag_deployment_observations_stage",
        ),
    )
    op.create_index(
        "ix_tag_deployment_observations_time",
        "tag_deployment_observations",
        ["tenant_id", "deployment_id", "window_end", "id"],
    )
    op.create_index(
        "ux_tag_deployment_observations_window",
        "tag_deployment_observations",
        ["tenant_id", "deployment_id", "window_start", "window_end"],
        unique=True,
    )
    _create(
        "tag_deployment_observation_samples",
        sa.Column(
            "deployment_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_deployments.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "observation_id",
            sa.BigInteger(),
            sa.ForeignKey("tag_deployment_observations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("stage", sa.String(24), nullable=False),
        sa.Column(
            "reception_id",
            sa.BigInteger(),
            sa.ForeignKey("receptions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "stage IN ('shadow','canary_5','canary_25','awaiting_admin','production','rolled_back','retired')",
            name="ck_tag_deployment_observation_samples_stage",
        ),
    )
    op.create_index(
        "ux_tag_deployment_observation_samples_stage_reception",
        "tag_deployment_observation_samples",
        ["tenant_id", "deployment_id", "stage", "reception_id"],
        unique=True,
    )
    op.create_index(
        "ix_tag_deployment_observation_samples_observation",
        "tag_deployment_observation_samples",
        ["tenant_id", "observation_id"],
    )
    _create(
        "legacy_tag_mappings",
        sa.Column("legacy_tag_path", sa.String(255), nullable=False),
        sa.Column("schema_version_id", sa.BigInteger(), nullable=False),
        sa.Column("tag_key", sa.String(128), nullable=False),
        sa.Column("mapping", sa.JSON(), nullable=False),
        sa.Column(
            "is_deterministic",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "legacy_tag_path",
            "schema_version_id",
            name="ux_legacy_tag_mappings_path_schema",
        ),
    )
    _create(
        "tag_governance_audit_events",
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_tag_governance_audit_resource",
        "tag_governance_audit_events",
        ["tenant_id", "resource_type", "resource_id", "occurred_at"],
    )
    op.create_index(
        "ix_tag_governance_audit_timeline",
        "tag_governance_audit_events",
        ["tenant_id", "occurred_at", "id"],
    )
    lineage_fks = (
        (
            "fk_tag_schemas_active_version_id",
            "tag_schemas",
            "tag_schema_versions",
            ["active_version_id"],
        ),
        (
            "fk_tag_extraction_jobs_tagger_version_id",
            "tag_extraction_jobs",
            "tagger_versions",
            ["tagger_version_id"],
        ),
        (
            "fk_tag_extraction_runs_tagger_version_id",
            "tag_extraction_runs",
            "tagger_versions",
            ["tagger_version_id"],
        ),
        (
            "fk_tag_extraction_runs_deployment_id",
            "tag_extraction_runs",
            "tag_deployments",
            ["deployment_id"],
        ),
        (
            "fk_tag_assignment_facts_schema_version_id",
            "tag_assignment_facts",
            "tag_schema_versions",
            ["schema_version_id"],
        ),
        (
            "fk_tag_assignment_facts_tagger_version_id",
            "tag_assignment_facts",
            "tagger_versions",
            ["tagger_version_id"],
        ),
        (
            "fk_tag_assignment_facts_extraction_run_id",
            "tag_assignment_facts",
            "tag_extraction_runs",
            ["extraction_run_id"],
        ),
        (
            "fk_tag_assignment_facts_deployment_id",
            "tag_assignment_facts",
            "tag_deployments",
            ["deployment_id"],
        ),
        (
            "fk_tag_assignment_facts_superseded_fact_id",
            "tag_assignment_facts",
            "tag_assignment_facts",
            ["superseded_fact_id"],
        ),
        (
            "fk_tag_review_tasks_proposed_fact_id",
            "tag_review_tasks",
            "tag_assignment_facts",
            ["proposed_fact_id"],
        ),
        (
            "fk_tag_review_tasks_schema_version_id",
            "tag_review_tasks",
            "tag_schema_versions",
            ["schema_version_id"],
        ),
        (
            "fk_tag_review_tasks_tagger_version_id",
            "tag_review_tasks",
            "tagger_versions",
            ["tagger_version_id"],
        ),
        (
            "fk_tag_review_decisions_resulting_fact_id",
            "tag_review_decisions",
            "tag_assignment_facts",
            ["resulting_fact_id"],
        ),
        (
            "fk_tag_gold_sets_schema_version_id",
            "tag_gold_sets",
            "tag_schema_versions",
            ["schema_version_id"],
        ),
        (
            "fk_tag_gold_labels_review_decision_id",
            "tag_gold_labels",
            "tag_review_decisions",
            ["review_decision_id"],
        ),
        (
            "fk_tag_evaluation_runs_tagger_version_id",
            "tag_evaluation_runs",
            "tagger_versions",
            ["tagger_version_id"],
        ),
        (
            "fk_tag_evaluation_runs_baseline_tagger_version_id",
            "tag_evaluation_runs",
            "tagger_versions",
            ["baseline_tagger_version_id"],
        ),
        (
            "fk_tag_evaluation_runs_gold_set_version_id",
            "tag_evaluation_runs",
            "tag_gold_set_versions",
            ["gold_set_version_id"],
        ),
        (
            "fk_tag_deployments_tagger_version_id",
            "tag_deployments",
            "tagger_versions",
            ["tagger_version_id"],
        ),
        (
            "fk_tag_deployments_evaluation_run_id",
            "tag_deployments",
            "tag_evaluation_runs",
            ["evaluation_run_id"],
        ),
        (
            "fk_tag_deployments_baseline_tagger_version_id",
            "tag_deployments",
            "tagger_versions",
            ["baseline_tagger_version_id"],
        ),
        (
            "fk_legacy_tag_mappings_schema_version_id",
            "legacy_tag_mappings",
            "tag_schema_versions",
            ["schema_version_id"],
        ),
    )
    for name, source, target, columns in lineage_fks:
        op.create_foreign_key(
            name,
            source,
            target,
            columns,
            ["id"],
            ondelete="RESTRICT",
        )
    if op.get_bind().dialect.name == "mysql":
        op.execute(
            """
            CREATE TRIGGER trg_tag_assignment_facts_no_update
            BEFORE UPDATE ON tag_assignment_facts FOR EACH ROW
            SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'tag_assignment_facts is append-only'
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_tag_assignment_facts_no_delete
            BEFORE DELETE ON tag_assignment_facts FOR EACH ROW
            SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'tag_assignment_facts is append-only'
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_tag_review_decisions_no_update
            BEFORE UPDATE ON tag_review_decisions FOR EACH ROW
            SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'tag_review_decisions is append-only'
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_tag_review_decisions_no_delete
            BEFORE DELETE ON tag_review_decisions FOR EACH ROW
            SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'tag_review_decisions is append-only'
            """
        )
    _seed_baseline()


def downgrade() -> None:
    if op.get_bind().dialect.name == "mysql":
        for trigger in (
            "trg_tag_review_decisions_no_delete",
            "trg_tag_review_decisions_no_update",
            "trg_tag_assignment_facts_no_delete",
            "trg_tag_assignment_facts_no_update",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.drop_constraint(
        "fk_tag_schemas_active_version_id",
        "tag_schemas",
        type_="foreignkey",
    )
    for table in (
        "tag_governance_audit_events",
        "legacy_tag_mappings",
        "tag_deployment_observation_samples",
        "tag_deployment_observations",
        "tag_assignment_current",
        "tag_gold_labels",
        "tag_review_decisions",
        "tag_review_tasks",
        "tag_assignment_facts",
        "tag_extraction_runs",
        "tag_extraction_jobs",
        "tag_deployments",
        "tag_gate_results",
        "tag_evaluation_metrics",
        "tag_evaluation_runs",
        "tag_gold_set_versions",
        "tag_gold_sets",
        "tagger_versions",
        "tag_schema_versions",
        "tag_schemas",
    ):
        op.drop_table(table)
