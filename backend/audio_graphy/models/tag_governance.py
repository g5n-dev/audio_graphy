"""Persistence model for the versioned tag-governance closed loop.

The legacy recording-level tag tables remain untouched.  These tables are the
canonical reception/dialogue-unit domain and deliberately separate immutable
facts from the mutable current projection.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from audio_graphy.models.base import TenantScopedBase


class TagSchema(TenantScopedBase):
    """Stable identity for a tenant-owned tag taxonomy."""

    __tablename__ = "tag_schemas"

    key: Mapped[str] = mapped_column(String(96), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft")
    active_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "tag_schema_versions.id",
            name="fk_tag_schemas_active_version_id",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        nullable=True,
    )
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'published', 'deprecated')",
            name="ck_tag_schemas_status",
        ),
        Index("ux_tag_schemas_tenant_key", "tenant_id", "key", unique=True),
    )


class TagSchemaVersion(TenantScopedBase):
    """Immutable snapshot of definitions and validation policy."""

    __tablename__ = "tag_schema_versions"

    schema_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag_schemas.id", ondelete="RESTRICT"),
        nullable=False,
    )
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    definitions: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft")
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    published_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'validated', 'published', 'deprecated')",
            name="ck_tag_schema_versions_status",
        ),
        Index(
            "ux_tag_schema_versions_schema_version",
            "schema_id",
            "version",
            unique=True,
        ),
        Index(
            "ux_tag_schema_versions_schema_checksum",
            "schema_id",
            "checksum",
            unique=True,
        ),
        Index("ix_tag_schema_versions_tenant_status", "tenant_id", "status"),
    )


class TaggerVersion(TenantScopedBase):
    """Immutable executable tagging configuration."""

    __tablename__ = "tagger_versions"

    schema_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag_schema_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    engine: Mapped[str] = mapped_column(String(16), nullable=False, default="hybrid")
    prompt_content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rule_bundle: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    model_version: Mapped[str] = mapped_column(String(128), nullable=False)
    thresholds: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    harness_spec_version: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="1.0",
    )
    harness_spec: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    parent_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tagger_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    origin: Mapped[str] = mapped_column(String(24), nullable=False, default="manual")
    # Intentionally not a physical FK: optimization runs can create candidate
    # taggers, so enforcing both directions would make online migration brittle.
    optimization_run_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft")
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    qualified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "engine IN ('rule', 'llm', 'hybrid')",
            name="ck_tagger_versions_engine",
        ),
        CheckConstraint(
            "status IN ('draft', 'validating', 'evaluating', 'rejected', 'qualified')",
            name="ck_tagger_versions_status",
        ),
        CheckConstraint(
            "origin IN ('manual', 'optimizer', 'bootstrap', 'migration')",
            name="ck_tagger_versions_origin",
        ),
        Index("ux_tagger_versions_tenant_version", "tenant_id", "version", unique=True),
        Index(
            "ux_tagger_versions_tenant_checksum",
            "tenant_id",
            "config_checksum",
            unique=True,
        ),
        Index(
            "ix_tagger_versions_lineage",
            "tenant_id",
            "parent_version_id",
            "optimization_run_id",
        ),
        Index(
            "ux_tagger_versions_optimization_run",
            "tenant_id",
            "optimization_run_id",
            unique=True,
        ),
    )


class TagExtractionJob(TenantScopedBase):
    """Durable, leased, idempotent unit of asynchronous governance work."""

    __tablename__ = "tag_extraction_jobs"

    job_type: Mapped[str] = mapped_column(String(24), nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="system")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    tagger_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tagger_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_subset: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    budget_max_provider_tokens: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    budget_max_provider_calls: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    budget_max_cost_microunits: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    budget_max_wall_seconds: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    budget_reserved_provider_tokens: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )
    budget_reserved_provider_calls: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    budget_reserved_cost_microunits: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )
    budget_consumed_provider_tokens: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )
    budget_consumed_provider_calls: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    budget_consumed_cost_microunits: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )
    budget_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    budget_exhausted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    current_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    budget_source: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="alert_only",
    )
    budget_purpose: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="extract",
    )
    budget_baseline_sample_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    budget_accounted_items: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    budget_usage_complete: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "job_type IN "
            "('extract', 'recompute', 'review_batch', 'evaluate', 'remediate', 'optimize')",
            name="ck_tag_extraction_jobs_type",
        ),
        CheckConstraint(
            "origin IN ('manual', 'serving', 'backfill', 'monitor', 'system')",
            name="ck_tag_extraction_jobs_origin",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'retry_wait', 'completed', 'failed', 'cancelled')",
            name="ck_tag_extraction_jobs_status",
        ),
        CheckConstraint(
            "completed_items >= 0 AND failed_items >= 0 AND total_items >= 0",
            name="ck_tag_extraction_jobs_counts",
        ),
        CheckConstraint(
            "(budget_max_provider_tokens IS NULL OR budget_max_provider_tokens > 0) "
            "AND (budget_max_provider_calls IS NULL OR budget_max_provider_calls > 0) "
            "AND (budget_max_cost_microunits IS NULL OR budget_max_cost_microunits > 0) "
            "AND (budget_max_wall_seconds IS NULL OR budget_max_wall_seconds > 0)",
            name="ck_tag_extraction_jobs_budget_limits",
        ),
        CheckConstraint(
            "budget_reserved_provider_tokens >= 0 "
            "AND budget_reserved_provider_calls >= 0 "
            "AND budget_reserved_cost_microunits >= 0 "
            "AND budget_consumed_provider_tokens >= 0 "
            "AND budget_consumed_provider_calls >= 0 "
            "AND budget_consumed_cost_microunits >= 0",
            name="ck_tag_extraction_jobs_budget_usage",
        ),
        CheckConstraint(
            "budget_source IN ('alert_only', 'explicit', 'default_p99')",
            name="ck_tag_extraction_jobs_budget_source",
        ),
        CheckConstraint(
            "budget_baseline_sample_count >= 0 AND budget_accounted_items >= 0",
            name="ck_tag_extraction_jobs_budget_baseline",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0 AND revision > 0",
            name="ck_tag_extraction_jobs_revisions",
        ),
        Index(
            "ux_tag_extraction_jobs_tenant_idempotency",
            "tenant_id",
            "idempotency_key",
            unique=True,
        ),
        Index(
            "ix_tag_extraction_jobs_claim",
            "status",
            "next_attempt_at",
            "lease_expires_at",
            "created_at",
        ),
        Index("ix_tag_extraction_jobs_tenant_status", "tenant_id", "status"),
        Index(
            "ix_tag_jobs_budget_baseline",
            "tenant_id",
            "job_type",
            "budget_purpose",
            "budget_usage_complete",
            "finished_at",
        ),
        Index(
            "ix_tag_extraction_jobs_tenant_created",
            "tenant_id",
            "created_at",
            "id",
        ),
    )


class TagExtractionRun(TenantScopedBase):
    """Per-subject extraction execution with a reproducible input snapshot."""

    __tablename__ = "tag_extraction_runs"

    job_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag_extraction_jobs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="system")
    deployment_stage: Mapped[str | None] = mapped_column(String(24), nullable=True)
    deployment_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    served_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tagger_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tagger_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    deployment_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tag_deployments.id", ondelete="RESTRICT"),
        nullable=True,
    )
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    output_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "subject_type IN ('dialogue_unit', 'reception', 'recording')",
            name="ck_tag_extraction_runs_subject",
        ),
        CheckConstraint(
            "origin IN ('manual', 'serving', 'backfill', 'monitor', 'system')",
            name="ck_tag_extraction_runs_origin",
        ),
        CheckConstraint(
            "deployment_stage IS NULL OR deployment_stage IN "
            "('shadow', 'canary_5', 'canary_25', 'awaiting_admin', "
            "'production', 'rolled_back', 'retired')",
            name="ck_tag_extraction_runs_deployment_stage",
        ),
        CheckConstraint(
            "deployment_revision IS NULL OR deployment_revision > 0",
            name="ck_tag_extraction_runs_deployment_revision",
        ),
        CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'cached')",
            name="ck_tag_extraction_runs_status",
        ),
        Index(
            "ux_tag_extraction_runs_job_subject_hash",
            "job_id",
            "subject_type",
            "subject_id",
            "input_hash",
            unique=True,
        ),
        Index(
            "ix_tag_extraction_runs_deployment_terminal",
            "tenant_id",
            "deployment_id",
            "status",
            "finished_at",
        ),
        Index(
            "ix_tag_extraction_runs_tagger_terminal_subject",
            "tenant_id",
            "tagger_version_id",
            "status",
            "finished_at",
            "subject_type",
            "subject_id",
        ),
    )


class TagHarnessExecution(TenantScopedBase):
    """One reproducible execution of a resolved semantic-tag Harness."""

    __tablename__ = "tag_harness_executions"

    extraction_run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tag_extraction_runs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    tagger_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tagger_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    deployment_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tag_deployments.id", ondelete="RESTRICT"),
        nullable=True,
    )
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scene_profile: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    resolved_harness_spec: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    route: Mapped[str] = mapped_column(String(64), nullable=False, default="unresolved")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    output_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_actions: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    artifacts: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_units: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "subject_type IN ('dialogue_unit', 'reception')",
            name="ck_tag_harness_executions_subject",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'abstained')",
            name="ck_tag_harness_executions_status",
        ),
        CheckConstraint(
            "latency_ms >= 0 AND token_count >= 0 AND cost_units >= 0",
            name="ck_tag_harness_executions_usage",
        ),
        Index(
            "ix_tag_harness_executions_tagger",
            "tenant_id",
            "tagger_version_id",
            "created_at",
        ),
        Index(
            "ix_tag_harness_executions_subject",
            "tenant_id",
            "subject_type",
            "subject_id",
            "created_at",
        ),
        Index(
            "ix_tag_harness_executions_status",
            "tenant_id",
            "status",
            "created_at",
        ),
    )


class TagHarnessStageTrace(TenantScopedBase):
    """Ordered observation emitted by one Harness execution stage."""

    __tablename__ = "tag_harness_stage_traces"

    harness_execution_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag_harness_executions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(24), nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    observation: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_actions: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    artifacts: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_units: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "stage IN ('context', 'tools', 'generation', 'orchestration', 'memory', 'output')",
            name="ck_tag_harness_stage_traces_stage",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'skipped')",
            name="ck_tag_harness_stage_traces_status",
        ),
        CheckConstraint(
            "sequence_no > 0 AND latency_ms >= 0 AND token_count >= 0 AND cost_units >= 0",
            name="ck_tag_harness_stage_traces_usage",
        ),
        Index(
            "ux_tag_harness_stage_traces_execution_sequence",
            "harness_execution_id",
            "sequence_no",
            unique=True,
        ),
        Index(
            "ix_tag_harness_stage_traces_status",
            "tenant_id",
            "status",
            "created_at",
        ),
    )


class TagAssignmentFact(TenantScopedBase):
    """Append-only canonical label fact."""

    __tablename__ = "tag_assignment_facts"

    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reception_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dialogue_unit_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tag_key: Mapped[str] = mapped_column(String(128), nullable=False)
    tag_value: Mapped[Any] = mapped_column(JSON, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence_refs: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    schema_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tag_schema_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    tagger_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tagger_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    extraction_run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tag_extraction_runs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    deployment_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tag_deployments.id", ondelete="RESTRICT"),
        nullable=True,
    )
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    recipe_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    superseded_fact_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tag_assignment_facts.id", ondelete="RESTRICT"),
        nullable=True,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    tombstone: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    actor_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "subject_type IN ('dialogue_unit', 'reception')",
            name="ck_tag_assignment_facts_subject",
        ),
        CheckConstraint(
            "source IN ('rule', 'llm', 'manual', 'imported')",
            name="ck_tag_assignment_facts_source",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_tag_assignment_facts_confidence",
        ),
        CheckConstraint("revision > 0", name="ck_tag_assignment_facts_revision"),
        Index(
            "ux_tag_assignment_facts_revision",
            "tenant_id",
            "subject_type",
            "subject_id",
            "tag_key",
            "revision",
            unique=True,
        ),
        Index(
            "ux_tag_assignment_facts_recipe",
            "tenant_id",
            "recipe_hash",
            unique=True,
        ),
        Index(
            "ix_tag_assignment_facts_lineage",
            "tenant_id",
            "subject_type",
            "subject_id",
            "tag_key",
            "created_at",
        ),
        Index("ix_tag_assignment_facts_input_hash", "tenant_id", "input_hash"),
        Index(
            "ix_tag_assignment_facts_extraction_run",
            "tenant_id",
            "extraction_run_id",
        ),
        Index(
            "ix_tag_assignment_facts_deployment_window",
            "tenant_id",
            "deployment_id",
            "tagger_version_id",
            "tombstone",
            "assigned_at",
            "id",
        ),
    )


class TagAssignmentCurrent(TenantScopedBase):
    """Mutable projection pointing at exactly one immutable fact."""

    __tablename__ = "tag_assignment_current"

    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tag_key: Mapped[str] = mapped_column(String(128), nullable=False)
    fact_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag_assignment_facts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "subject_type IN ('dialogue_unit', 'reception')",
            name="ck_tag_assignment_current_subject",
        ),
        Index(
            "ux_tag_assignment_current_subject_key",
            "tenant_id",
            "subject_type",
            "subject_id",
            "tag_key",
            unique=True,
        ),
        Index("ux_tag_assignment_current_fact", "fact_id", unique=True),
    )


class TagReviewTask(TenantScopedBase):
    """Human verification work created by confidence and policy triggers."""

    __tablename__ = "tag_review_tasks"

    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reception_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tag_key: Mapped[str] = mapped_column(String(128), nullable=False)
    proposed_value: Mapped[Any] = mapped_column(JSON, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence_refs: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    proposed_fact_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tag_assignment_facts.id", ondelete="RESTRICT"),
        nullable=True,
    )
    schema_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tag_schema_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    tagger_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tagger_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    review_bundle_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    selection_policy: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="legacy",
    )
    selection_policy_version: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="1",
    )
    sampling_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    blind_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_deployment_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tag_deployments.id", ondelete="RESTRICT"),
        nullable=True,
    )
    source_extraction_run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tag_extraction_runs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    source_harness_execution_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tag_harness_executions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    sampled_deployment_stage: Mapped[str | None] = mapped_column(String(24), nullable=True)
    sampled_deployment_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sampling_manifest_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claimed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "reason IN "
            "('conflict', 'missing', 'low_confidence', 'critical', 'random', "
            "'drift', 'audit', 'gold', 'adjudication', 'active_learning')",
            name="ck_tag_review_tasks_reason",
        ),
        CheckConstraint(
            "status IN ('pending', 'claimed', 'resolved', 'skipped')",
            name="ck_tag_review_tasks_status",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_tag_review_tasks_confidence",
        ),
        CheckConstraint(
            "sampling_probability IS NULL OR "
            "(sampling_probability > 0 AND sampling_probability <= 1)",
            name="ck_tag_review_tasks_sampling_probability",
        ),
        CheckConstraint(
            "sampled_deployment_stage IS NULL OR sampled_deployment_stage IN "
            "('shadow', 'canary_5', 'canary_25', 'awaiting_admin', 'production')",
            name="ck_tag_review_tasks_sampled_stage",
        ),
        CheckConstraint(
            "sampled_deployment_revision IS NULL OR sampled_deployment_revision > 0",
            name="ck_tag_review_tasks_sampled_revision",
        ),
        Index("ix_tag_review_tasks_queue", "tenant_id", "status", "priority", "created_at"),
        Index("ix_tag_review_tasks_batch", "tenant_id", "batch_id"),
        Index(
            "ix_tag_review_tasks_selection",
            "tenant_id",
            "selection_policy",
            "review_bundle_id",
        ),
    )


class TagReviewDecision(TenantScopedBase):
    """Append-only human decision; corrections produce a new manual fact."""

    __tablename__ = "tag_review_decisions"

    task_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag_review_tasks.id", ondelete="RESTRICT"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    corrected_value: Mapped[Any] = mapped_column(JSON, nullable=True)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_refs: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    resulting_fact_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tag_assignment_facts.id", ondelete="RESTRICT"),
        nullable=True,
    )
    reviewer_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    adjudication: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    truth_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    truth_tier: Mapped[str] = mapped_column(String(8), nullable=False, default="t1")
    annotator_round: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    primary_failure_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason_codes: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True, default=list)
    reviewer_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    review_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "action IN ('accept', 'correct', 'reject', 'uncertain', 'escalate')",
            name="ck_tag_review_decisions_action",
        ),
        CheckConstraint(
            "truth_state IS NULL OR "
            "truth_state IN ('present', 'absent', 'not_applicable', 'uncertain')",
            name="ck_tag_review_decisions_truth_state",
        ),
        CheckConstraint(
            "truth_tier IN ('t0', 't1', 't2', 't3')",
            name="ck_tag_review_decisions_truth_tier",
        ),
        CheckConstraint(
            "primary_failure_stage IS NULL OR primary_failure_stage IN "
            "('vad', 'asr', 'speaker', 'boundary', 'schema', 'tag_reasoning', "
            "'evidence', 'fusion', 'insufficient_audio')",
            name="ck_tag_review_decisions_failure_stage",
        ),
        CheckConstraint(
            "reviewer_confidence IS NULL OR "
            "(reviewer_confidence >= 0 AND reviewer_confidence <= 1)",
            name="ck_tag_review_decisions_reviewer_confidence",
        ),
        CheckConstraint(
            "annotator_round > 0 AND (review_duration_ms IS NULL OR review_duration_ms >= 0)",
            name="ck_tag_review_decisions_quality",
        ),
        Index("ix_tag_review_decisions_task", "tenant_id", "task_id", "decided_at"),
        Index(
            "ix_tag_review_decisions_window",
            "tenant_id",
            "decided_at",
            "task_id",
            "id",
        ),
    )


class TagFeedbackEvent(TenantScopedBase):
    """Append-only normalized feedback with explicit truth and sampling lineage."""

    __tablename__ = "tag_feedback_events"

    harness_execution_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tag_harness_executions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    review_decision_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tag_review_decisions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    deployment_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tag_deployments.id", ondelete="RESTRICT"),
        nullable=True,
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    truth_tier: Mapped[str] = mapped_column(String(8), nullable=False, default="t0")
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tag_key: Mapped[str] = mapped_column(String(128), nullable=False)
    truth_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    error_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    correction: Mapped[Any] = mapped_column(JSON, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    training_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    selection_policy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sampling_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "source IN ('human', 'system_constraint', 'business_outcome', 'model_disagreement')",
            name="ck_tag_feedback_events_source",
        ),
        CheckConstraint(
            "truth_tier IN ('t0', 't1', 't2', 't3')",
            name="ck_tag_feedback_events_truth_tier",
        ),
        CheckConstraint(
            "subject_type IN ('dialogue_unit', 'reception')",
            name="ck_tag_feedback_events_subject",
        ),
        CheckConstraint(
            "truth_state IS NULL OR "
            "truth_state IN ('present', 'absent', 'not_applicable', 'uncertain')",
            name="ck_tag_feedback_events_truth_state",
        ),
        CheckConstraint(
            "error_stage IS NULL OR error_stage IN "
            "('vad', 'asr', 'speaker', 'boundary', 'schema', 'tag_reasoning', "
            "'evidence', 'fusion', 'insufficient_audio')",
            name="ck_tag_feedback_events_error_stage",
        ),
        CheckConstraint(
            "sampling_probability IS NULL OR "
            "(sampling_probability > 0 AND sampling_probability <= 1)",
            name="ck_tag_feedback_events_sampling_probability",
        ),
        Index(
            "ix_tag_feedback_events_training",
            "tenant_id",
            "training_eligible",
            "truth_tier",
            "occurred_at",
        ),
        Index(
            "ix_tag_feedback_events_subject",
            "tenant_id",
            "subject_type",
            "subject_id",
            "tag_key",
        ),
        Index(
            "ix_tag_feedback_events_execution",
            "tenant_id",
            "harness_execution_id",
        ),
    )


class TagFeedbackLaneAssignment(TenantScopedBase):
    """Immutable server assignment of certified feedback to one dataset lane."""

    __tablename__ = "tag_feedback_lane_assignments"

    feedback_event_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag_feedback_events.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_gold_label_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag_gold_labels.id", ondelete="RESTRICT"),
        nullable=False,
    )
    gold_set_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag_gold_set_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    split: Mapped[str] = mapped_column(String(16), nullable=False)
    assigned_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "split IN ('train', 'validation', 'challenge', 'holdout', 'audit')",
            name="ck_tag_feedback_lane_assignments_split",
        ),
        Index(
            "ux_tag_feedback_lane_assignments_event",
            "tenant_id",
            "feedback_event_id",
            unique=True,
        ),
        Index(
            "ix_tag_feedback_lane_assignments_split",
            "tenant_id",
            "split",
            "feedback_event_id",
        ),
    )


class TagGoldSet(TenantScopedBase):
    """Stable identity for a reviewed-label dataset."""

    __tablename__ = "tag_gold_sets"

    key: Mapped[str] = mapped_column(String(96), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    schema_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag_schema_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (Index("ux_tag_gold_sets_tenant_key", "tenant_id", "key", unique=True),)


class TagGoldSetVersion(TenantScopedBase):
    """Immutable frozen gold-set version."""

    __tablename__ = "tag_gold_set_versions"

    gold_set_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag_gold_sets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dataset_snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completeness_manifest: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        default=dict,
    )
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    frozen_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'frozen', 'retired')",
            name="ck_tag_gold_set_versions_status",
        ),
        Index(
            "ux_tag_gold_set_versions_set_version",
            "gold_set_id",
            "version",
            unique=True,
        ),
    )


class TagGoldLabel(TenantScopedBase):
    """One immutable reviewed label in a frozen dataset."""

    __tablename__ = "tag_gold_labels"

    gold_set_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag_gold_set_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    review_decision_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag_review_decisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reception_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tag_key: Mapped[str] = mapped_column(String(128), nullable=False)
    tag_value: Mapped[Any] = mapped_column(JSON, nullable=True)
    evidence_refs: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    truth_state: Mapped[str] = mapped_column(String(24), nullable=False, default="present")
    truth_tier: Mapped[str] = mapped_column(String(8), nullable=False, default="t1")
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        default=dict,
    )
    annotation_quality: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        default=dict,
    )
    cohort: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completeness_manifest: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        default=dict,
    )
    split: Mapped[str] = mapped_column(String(16), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "split IN ('train', 'validation', 'challenge', 'holdout', 'audit')",
            name="ck_tag_gold_labels_split",
        ),
        CheckConstraint(
            "truth_state IN ('present', 'absent', 'not_applicable', 'uncertain')",
            name="ck_tag_gold_labels_truth_state",
        ),
        CheckConstraint(
            "truth_tier IN ('t0', 't1', 't2', 't3')",
            name="ck_tag_gold_labels_truth_tier",
        ),
        Index(
            "ux_tag_gold_labels_version_decision",
            "gold_set_version_id",
            "review_decision_id",
            unique=True,
        ),
        Index(
            "ix_tag_gold_labels_version_split",
            "tenant_id",
            "gold_set_version_id",
            "split",
        ),
        Index(
            "ux_tag_gold_labels_version_subject_tag",
            "gold_set_version_id",
            "subject_type",
            "subject_id",
            "tag_key",
            unique=True,
        ),
        Index(
            "ix_tag_gold_labels_dataset_lane",
            "tenant_id",
            "gold_set_version_id",
            "cohort",
            "split",
        ),
    )


class TagEvaluationRun(TenantScopedBase):
    """Dedicated tag evaluation over an immutable gold set."""

    __tablename__ = "tag_evaluation_runs"

    tagger_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tagger_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    baseline_tagger_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tagger_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    gold_set_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag_gold_set_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    evaluator_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="tag-evaluator-v2",
    )
    dataset_snapshot_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="legacy-unfrozen",
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="completed")
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    baseline_metrics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')",
            name="ck_tag_evaluation_runs_status",
        ),
        Index("ix_tag_evaluation_runs_tenant_status", "tenant_id", "status"),
    )


class TagEvaluationMetric(TenantScopedBase):
    """Normalized metric row for charting and comparisons."""

    __tablename__ = "tag_evaluation_metrics"

    evaluation_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag_evaluation_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    metric_key: Mapped[str] = mapped_column(String(96), nullable=False)
    label_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    support: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        Index(
            "ux_tag_evaluation_metrics_identity",
            "evaluation_run_id",
            "metric_key",
            "label_key",
            unique=True,
        ),
    )


class TagEvaluationItem(TenantScopedBase):
    """Paired candidate/baseline result for one frozen gold label."""

    __tablename__ = "tag_evaluation_items"

    evaluation_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag_evaluation_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    gold_label_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag_gold_labels.id", ondelete="RESTRICT"),
        nullable=False,
    )
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tag_key: Mapped[str] = mapped_column(String(128), nullable=False)
    truth_state: Mapped[str] = mapped_column(String(24), nullable=False)
    candidate_prediction: Mapped[Any] = mapped_column(JSON, nullable=True)
    baseline_prediction: Mapped[Any] = mapped_column(JSON, nullable=True)
    candidate_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    candidate_evidence_refs: Mapped[list[Any]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    baseline_evidence_refs: Mapped[list[Any]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    error_taxonomy: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    slice_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    paired_delta: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (
        CheckConstraint(
            "subject_type IN ('dialogue_unit', 'reception')",
            name="ck_tag_evaluation_items_subject",
        ),
        CheckConstraint(
            "truth_state IN ('present', 'absent', 'not_applicable', 'uncertain')",
            name="ck_tag_evaluation_items_truth_state",
        ),
        CheckConstraint(
            "(candidate_score IS NULL OR "
            "(candidate_score >= 0 AND candidate_score <= 1)) AND "
            "(baseline_score IS NULL OR "
            "(baseline_score >= 0 AND baseline_score <= 1))",
            name="ck_tag_evaluation_items_scores",
        ),
        Index(
            "ux_tag_evaluation_items_run_gold",
            "evaluation_run_id",
            "gold_label_id",
            unique=True,
        ),
        Index(
            "ix_tag_evaluation_items_slice",
            "tenant_id",
            "evaluation_run_id",
            "tag_key",
            "truth_state",
        ),
    )


class TagBadcase(TenantScopedBase):
    """Clusterable failure with root-cause and regression lifecycle."""

    __tablename__ = "tag_badcases"

    source_evaluation_item_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tag_evaluation_items.id", ondelete="RESTRICT"),
        nullable=True,
    )
    source_feedback_event_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tag_feedback_events.id", ondelete="RESTRICT"),
        nullable=True,
    )
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tag_key: Mapped[str] = mapped_column(String(128), nullable=False)
    failure_stage: Mapped[str] = mapped_column(String(32), nullable=False)
    failure_mode: Mapped[str] = mapped_column(String(96), nullable=False)
    signature_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    cluster_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dataset_split: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="operational",
    )
    root_cause: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="open")
    fix_candidate_tagger_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tagger_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    regression_result: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "source_evaluation_item_id IS NOT NULL OR source_feedback_event_id IS NOT NULL",
            name="ck_tag_badcases_source",
        ),
        CheckConstraint(
            "subject_type IN ('dialogue_unit', 'reception')",
            name="ck_tag_badcases_subject",
        ),
        CheckConstraint(
            "failure_stage IN "
            "('vad', 'asr', 'speaker', 'boundary', 'schema', 'tag_reasoning', "
            "'evidence', 'fusion', 'insufficient_audio')",
            name="ck_tag_badcases_failure_stage",
        ),
        CheckConstraint(
            "status IN ('open', 'candidate_fix', 'verified', 'resolved', 'reopened', 'ignored')",
            name="ck_tag_badcases_status",
        ),
        CheckConstraint(
            "dataset_split IN "
            "('operational', 'pending', 'train', 'validation', 'challenge', 'holdout', 'audit')",
            name="ck_tag_badcases_dataset_split",
        ),
        CheckConstraint("occurrence_count > 0", name="ck_tag_badcases_occurrence_count"),
        Index(
            "ix_tag_badcases_cluster",
            "tenant_id",
            "status",
            "failure_stage",
            "cluster_key",
        ),
        Index(
            "ix_tag_badcases_signature",
            "tenant_id",
            "signature_hash",
        ),
        Index(
            "ix_tag_badcases_visibility",
            "tenant_id",
            "dataset_split",
            "status",
            "last_seen_at",
        ),
    )


class TagExperienceCase(TenantScopedBase):
    """Approved reusable strategy experience derived from strong feedback."""

    __tablename__ = "tag_experience_cases"

    source_badcase_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tag_badcases.id", ondelete="RESTRICT"),
        nullable=True,
    )
    source_feedback_event_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tag_feedback_events.id", ondelete="RESTRICT"),
        nullable=True,
    )
    scene_signature: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    failure_signature: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    harness_spec: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    reward_vector: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    quality_tier: Mapped[str] = mapped_column(String(8), nullable=False, default="t2")
    dataset_split: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="operational",
    )
    eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    materialized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "source_badcase_id IS NOT NULL OR source_feedback_event_id IS NOT NULL",
            name="ck_tag_experience_cases_source",
        ),
        CheckConstraint(
            "outcome IN ('successful', 'failed', 'regressed')",
            name="ck_tag_experience_cases_outcome",
        ),
        CheckConstraint(
            "quality_tier IN ('t0', 't1', 't2', 't3')",
            name="ck_tag_experience_cases_quality_tier",
        ),
        CheckConstraint(
            "dataset_split IN "
            "('operational', 'pending', 'train', 'validation', 'challenge', 'holdout', 'audit')",
            name="ck_tag_experience_cases_dataset_split",
        ),
        Index(
            "ux_tag_experience_cases_checksum",
            "tenant_id",
            "checksum",
            unique=True,
        ),
        Index(
            "ix_tag_experience_cases_retrieval",
            "tenant_id",
            "eligible",
            "outcome",
            "materialized_at",
        ),
        Index(
            "ix_tag_experience_cases_visibility",
            "tenant_id",
            "dataset_split",
            "eligible",
            "materialized_at",
        ),
    )


class TagGateResult(TenantScopedBase):
    """Immutable result of one release quality gate."""

    __tablename__ = "tag_gate_results"

    evaluation_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag_evaluation_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    actual: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index(
            "ux_tag_gate_results_run_code",
            "evaluation_run_id",
            "code",
            unique=True,
        ),
    )


class TagOptimizationRun(TenantScopedBase):
    """Bounded Harness search over a frozen, server-owned dataset snapshot."""

    __tablename__ = "tag_optimization_runs"

    baseline_tagger_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tagger_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    gold_set_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag_gold_set_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    job_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tag_extraction_jobs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    dataset_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sealed_release_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trigger: Mapped[str] = mapped_column(
        "trigger",
        String(32),
        quote=True,
        nullable=False,
        default="manual",
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    phase: Mapped[str] = mapped_column(String(24), nullable=False, default="prepare")
    cohort: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    objective: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    search_budget: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    candidate_tagger_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tagger_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    winner_tagger_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tagger_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    next_actions: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    artifacts: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "`trigger` IN ('manual', 'scheduled', 'feedback_threshold', 'insight')",
            name="ck_tag_optimization_runs_trigger",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_tag_optimization_runs_status",
        ),
        CheckConstraint(
            "phase IN ('prepare', 'search', 'validation', 'challenge', 'holdout', 'completed')",
            name="ck_tag_optimization_runs_phase",
        ),
        Index(
            "ix_tag_optimization_runs_status",
            "tenant_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_tag_optimization_runs_baseline",
            "tenant_id",
            "baseline_tagger_version_id",
            "created_at",
        ),
        Index(
            "ux_tag_optimization_runs_job",
            "tenant_id",
            "job_id",
            unique=True,
        ),
        Index(
            "ux_tag_optimization_runs_sealed_release",
            "tenant_id",
            "sealed_release_key",
            unique=True,
        ),
    )


class TagOptimizationTrial(TenantScopedBase):
    """One deterministic mutation evaluated within an optimization run."""

    __tablename__ = "tag_optimization_trials"

    optimization_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag_optimization_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    parent_trial_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tag_optimization_trials.id", ondelete="RESTRICT"),
        nullable=True,
    )
    candidate_tagger_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tagger_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    mutation: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    harness_spec: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    phase: Mapped[str] = mapped_column(String(24), nullable=False, default="train")
    reward_vector: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    gate_results: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    next_actions: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    artifacts: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("ordinal > 0", name="ck_tag_optimization_trials_ordinal"),
        CheckConstraint(
            "status IN ('pending', 'running', 'pruned', 'completed', 'failed', 'cancelled')",
            name="ck_tag_optimization_trials_status",
        ),
        CheckConstraint(
            "phase IN ('train', 'validation', 'challenge', 'holdout')",
            name="ck_tag_optimization_trials_phase",
        ),
        Index(
            "ux_tag_optimization_trials_run_ordinal",
            "optimization_run_id",
            "ordinal",
            unique=True,
        ),
        Index(
            "ix_tag_optimization_trials_status",
            "tenant_id",
            "optimization_run_id",
            "status",
        ),
    )


class TagDeployment(TenantScopedBase):
    """Version route through shadow/canary/admin-approved production."""

    __tablename__ = "tag_deployments"

    tagger_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tagger_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    evaluation_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag_evaluation_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    baseline_tagger_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tagger_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="shadow")
    traffic_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    promotion_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pause_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    sampling_complete_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    approved_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rolled_back_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rollback_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN "
            "('shadow', 'canary_5', 'canary_25', 'awaiting_admin', "
            "'production', 'rolled_back', 'retired')",
            name="ck_tag_deployments_status",
        ),
        CheckConstraint(
            "traffic_percent >= 0 AND traffic_percent <= 100 AND revision > 0",
            name="ck_tag_deployments_traffic_revision",
        ),
        Index("ix_tag_deployments_tenant_status", "tenant_id", "status"),
        Index(
            "ux_tag_deployments_tenant_evaluation",
            "tenant_id",
            "evaluation_run_id",
            unique=True,
        ),
        Index("ix_tag_deployments_monitor", "status", "tenant_id", "id"),
        Index(
            "ix_tag_deployments_route",
            "tenant_id",
            "status",
            "approved_at",
            "id",
        ),
        Index(
            "ix_tag_deployments_baseline_active",
            "tenant_id",
            "baseline_tagger_version_id",
            "status",
            "created_at",
            "id",
        ),
    )


class TagDeploymentObservation(TenantScopedBase):
    """Time-series release health observation."""

    __tablename__ = "tag_deployment_observations"

    deployment_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag_deployments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    deployment_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    stage: Mapped[str] = mapped_column(String(24), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="manual")
    provenance: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        default=dict,
    )
    is_trusted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    served_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    paired_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    audited_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    adjudicated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    breach_codes: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    action: Mapped[str] = mapped_column(String(24), nullable=False, default="observe")

    __table_args__ = (
        CheckConstraint(
            "action IN ('observe', 'pause', 'rollback')",
            name="ck_tag_deployment_observations_action",
        ),
        CheckConstraint(
            "source IN ('monitor', 'manual', 'imported')",
            name="ck_tag_deployment_observations_source",
        ),
        CheckConstraint(
            "sample_count >= 0 AND served_count >= 0 AND paired_count >= 0 "
            "AND audited_count >= 0 AND adjudicated_count >= 0",
            name="ck_tag_deployment_observations_counts",
        ),
        CheckConstraint(
            "deployment_revision > 0",
            name="ck_tag_deployment_observations_revision",
        ),
        CheckConstraint(
            "stage IN "
            "('shadow', 'canary_5', 'canary_25', 'awaiting_admin', "
            "'production', 'rolled_back', 'retired')",
            name="ck_tag_deployment_observations_stage",
        ),
        Index(
            "ix_tag_deployment_observations_time",
            "tenant_id",
            "deployment_id",
            "window_end",
            "id",
        ),
        Index(
            "ux_tag_deployment_observations_window",
            "tenant_id",
            "deployment_id",
            "window_start",
            "window_end",
            unique=True,
        ),
    )


class TagDeploymentObservationSample(TenantScopedBase):
    """One reception counted once toward one deployment-stage gate."""

    __tablename__ = "tag_deployment_observation_samples"

    deployment_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag_deployments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    observation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag_deployment_observations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    stage: Mapped[str] = mapped_column(String(24), nullable=False)
    reception_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("receptions.id", ondelete="RESTRICT"),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "stage IN "
            "('shadow', 'canary_5', 'canary_25', 'awaiting_admin', "
            "'production', 'rolled_back', 'retired')",
            name="ck_tag_deployment_observation_samples_stage",
        ),
        Index(
            "ux_tag_deployment_observation_samples_stage_reception",
            "tenant_id",
            "deployment_id",
            "stage",
            "reception_id",
            unique=True,
        ),
        Index(
            "ix_tag_deployment_observation_samples_observation",
            "tenant_id",
            "observation_id",
        ),
    )


class TagDeploymentAuditSubject(TenantScopedBase):
    """A served/paired/audited subject counted once per stage revision."""

    __tablename__ = "tag_deployment_audit_subjects"

    deployment_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag_deployments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    first_observation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag_deployment_observations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    stage: Mapped[str] = mapped_column(String(24), nullable=False)
    deployment_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    count_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="audited")
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "stage IN "
            "('shadow', 'canary_5', 'canary_25', 'awaiting_admin', "
            "'production', 'rolled_back', 'retired')",
            name="ck_tag_deployment_audit_subjects_stage",
        ),
        CheckConstraint(
            "deployment_revision > 0",
            name="ck_tag_deployment_audit_subjects_revision",
        ),
        CheckConstraint(
            "count_kind IN ('served', 'paired', 'audited')",
            name="ck_tag_deployment_audit_subjects_kind",
        ),
        CheckConstraint(
            "subject_type IN ('dialogue_unit', 'reception') AND subject_id > 0",
            name="ck_tag_deployment_audit_subjects_subject",
        ),
        Index(
            "ux_tag_deployment_audit_subjects_stage_subject",
            "tenant_id",
            "deployment_id",
            "stage",
            "deployment_revision",
            "count_kind",
            "subject_type",
            "subject_id",
            unique=True,
        ),
        Index(
            "ix_tag_deployment_audit_subjects_observation",
            "tenant_id",
            "first_observation_id",
        ),
    )


class LegacyTagMapping(TenantScopedBase):
    """Explicit deterministic compatibility bridge for recording-level tags."""

    __tablename__ = "legacy_tag_mappings"

    legacy_tag_path: Mapped[str] = mapped_column(String(255), nullable=False)
    schema_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tag_schema_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    tag_key: Mapped[str] = mapped_column(String(128), nullable=False)
    mapping: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    deterministic: Mapped[bool] = mapped_column(
        "is_deterministic",
        Boolean,
        nullable=False,
        default=True,
    )

    __table_args__ = (
        Index(
            "ux_legacy_tag_mappings_path_schema",
            "tenant_id",
            "legacy_tag_path",
            "schema_version_id",
            unique=True,
        ),
    )


class TagGovernanceAuditEvent(TenantScopedBase):
    """Append-only domain audit stream for governance mutations."""

    __tablename__ = "tag_governance_audit_events"

    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index(
            "ix_tag_governance_audit_resource",
            "tenant_id",
            "resource_type",
            "resource_id",
            "occurred_at",
        ),
        Index(
            "ix_tag_governance_audit_timeline",
            "tenant_id",
            "occurred_at",
            "id",
        ),
        Index(
            "ix_tag_governance_audit_actor_action",
            "tenant_id",
            "actor_user_id",
            "action",
            "resource_type",
            "resource_id",
        ),
    )


@event.listens_for(TagAssignmentFact, "before_update")
def _reject_fact_update(_mapper: Any, _connection: Any, _target: TagAssignmentFact) -> None:
    raise RuntimeError("tag_assignment_facts is append-only")


@event.listens_for(TagAssignmentFact, "before_insert")
def _populate_fact_recipe_hash(
    _mapper: Any,
    _connection: Any,
    target: TagAssignmentFact,
) -> None:
    if target.recipe_hash:
        return
    payload = json.dumps(
        {
            "tenant_id": target.tenant_id,
            "subject_type": target.subject_type,
            "subject_id": target.subject_id,
            "tag_key": target.tag_key,
            "tagger_version_id": target.tagger_version_id or 0,
            "deployment_id": target.deployment_id or 0,
            "input_hash": target.input_hash,
            "source": target.source,
            "tombstone": target.tombstone,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    target.recipe_hash = hashlib.sha256(payload.encode()).hexdigest()


@event.listens_for(TagAssignmentFact, "before_delete")
def _reject_fact_delete(_mapper: Any, _connection: Any, _target: TagAssignmentFact) -> None:
    raise RuntimeError("tag_assignment_facts is append-only")


@event.listens_for(TagReviewDecision, "before_update")
def _reject_decision_update(
    _mapper: Any,
    _connection: Any,
    _target: TagReviewDecision,
) -> None:
    raise RuntimeError("tag_review_decisions is append-only")


@event.listens_for(TagReviewDecision, "before_delete")
def _reject_decision_delete(
    _mapper: Any,
    _connection: Any,
    _target: TagReviewDecision,
) -> None:
    raise RuntimeError("tag_review_decisions is append-only")


@event.listens_for(TagFeedbackLaneAssignment, "before_update")
def _reject_feedback_lane_update(
    _mapper: Any,
    _connection: Any,
    _target: TagFeedbackLaneAssignment,
) -> None:
    raise RuntimeError("tag_feedback_lane_assignments is append-only")


@event.listens_for(TagFeedbackLaneAssignment, "before_delete")
def _reject_feedback_lane_delete(
    _mapper: Any,
    _connection: Any,
    _target: TagFeedbackLaneAssignment,
) -> None:
    raise RuntimeError("tag_feedback_lane_assignments is append-only")


__all__ = [
    "LegacyTagMapping",
    "TagAssignmentCurrent",
    "TagAssignmentFact",
    "TagBadcase",
    "TagDeployment",
    "TagDeploymentAuditSubject",
    "TagDeploymentObservation",
    "TagDeploymentObservationSample",
    "TagEvaluationItem",
    "TagEvaluationMetric",
    "TagEvaluationRun",
    "TagExperienceCase",
    "TagExtractionJob",
    "TagExtractionRun",
    "TagFeedbackEvent",
    "TagFeedbackLaneAssignment",
    "TagGateResult",
    "TagGoldLabel",
    "TagGoldSet",
    "TagGoldSetVersion",
    "TagGovernanceAuditEvent",
    "TagHarnessExecution",
    "TagHarnessStageTrace",
    "TagOptimizationRun",
    "TagOptimizationTrial",
    "TagReviewDecision",
    "TagReviewTask",
    "TagSchema",
    "TagSchemaVersion",
    "TaggerVersion",
]
