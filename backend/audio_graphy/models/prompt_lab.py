"""Persistence for offline prompt compilation and its human review trail.

Four tables, each answering a question the closed loop already asks of every other
artefact it produces:

* ``tag_prompt_artifacts`` -- what was compiled, from which baseline, and which parts
  a reviewer accepted.
* ``tag_prompt_gradients`` -- why each part was proposed, and what a human decided.
* ``tag_prompt_demo_sources`` -- which conversation every inlined example came from,
  so an erasure request can find it again.
* ``tag_silver_labels`` -- machine-proposed labels, fenced off from evaluation by
  database constraints rather than by convention.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from audio_graphy.models.base import TenantScopedBase

# Keep in step with audio_graphy.optimizers.artifacts.PatchOrigin.
_COMPILERS = (
    "'builtin', 'builtin_grounded', 'dspy_mipro', 'dspy_bootstrap', "
    "'dspy_gepa', 'textgrad_tgd', 'manual'"
)
_ARTIFACT_STATUSES = "'draft', 'review', 'accepted', 'rejected', 'superseded'"
_REDACTION_MODES = "'verbatim', 'masked', 'synthetic'"
_TRUTH_STATES = "'present', 'absent', 'not_applicable', 'uncertain'"
_FAILURE_STAGES = (
    "'vad', 'asr', 'speaker', 'boundary', 'schema', "
    "'tag_reasoning', 'evidence', 'fusion', 'insufficient_audio'"
)


class TagPromptArtifact(TenantScopedBase):
    """One compilation of a prompt candidate, immutable once accepted.

    ``artifact_checksum`` is content-addressed over the rendered prompt and every
    part that produced it, so submitting the same review decisions twice resolves to
    the same row instead of minting a second candidate.
    """

    __tablename__ = "tag_prompt_artifacts"

    compilation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    optimization_run_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    baseline_tagger_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "tagger_versions.id",
            name="fk_tag_prompt_artifacts_baseline",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    gold_set_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "tag_gold_set_versions.id",
            name="fk_tag_prompt_artifacts_gold_set_version",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    parent_artifact_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "tag_prompt_artifacts.id",
            name="fk_tag_prompt_artifacts_parent",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    candidate_tagger_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "tagger_versions.id",
            name="fk_tag_prompt_artifacts_candidate",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    compiler: Mapped[str] = mapped_column(String(32), nullable=False)
    compiler_version: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_version: Mapped[str] = mapped_column(String(32), nullable=False)
    gradient_prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft")
    baseline_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    header: Mapped[str] = mapped_column(Text, nullable=False)
    rendered_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    patches: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    demos: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    accepted_patch_ids: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    prompt_token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_budget_report: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    redaction_report: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    artifact_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        CheckConstraint(f"compiler IN ({_COMPILERS})", name="ck_tag_prompt_artifacts_compiler"),
        CheckConstraint(
            f"status IN ({_ARTIFACT_STATUSES})",
            name="ck_tag_prompt_artifacts_status",
        ),
        CheckConstraint(
            "prompt_token_estimate >= 0",
            name="ck_tag_prompt_artifacts_token_estimate",
        ),
        Index(
            "ux_tag_prompt_artifacts_checksum",
            "tenant_id",
            "artifact_checksum",
            unique=True,
        ),
        Index("ix_tag_prompt_artifacts_compilation", "tenant_id", "compilation_id", "created_at"),
        Index("ix_tag_prompt_artifacts_status", "tenant_id", "status", "created_at"),
        Index("ix_tag_prompt_artifacts_run", "tenant_id", "optimization_run_id"),
    )


class TagPromptGradient(TenantScopedBase):
    """The reasoning behind one patch, and the human verdict on it.

    ``evaluation`` records what the patch did when applied -- including the per-tag
    deltas, because a patch that repairs its own cluster while degrading a different
    tag is the characteristic failure of prompt optimisation.
    """

    __tablename__ = "tag_prompt_gradients"

    artifact_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "tag_prompt_artifacts.id",
            name="fk_tag_prompt_gradients_artifact",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    patch_id: Mapped[str] = mapped_column(String(32), nullable=False)
    iteration: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_badcase_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "tag_badcases.id",
            name="fk_tag_prompt_gradients_badcase",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    tag_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    failure_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    failure_mode: Mapped[str | None] = mapped_column(String(96), nullable=True)
    gradient_text: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_edit: Mapped[str] = mapped_column(Text, nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    decided_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    evaluation: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    llm_logical_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        CheckConstraint("iteration > 0", name="ck_tag_prompt_gradients_iteration"),
        CheckConstraint(
            "decision IN ('pending', 'accepted', 'rejected')",
            name="ck_tag_prompt_gradients_decision",
        ),
        CheckConstraint(
            f"failure_stage IS NULL OR failure_stage IN ({_FAILURE_STAGES})",
            name="ck_tag_prompt_gradients_failure_stage",
        ),
        Index(
            "ux_tag_prompt_gradients_patch",
            "artifact_id",
            "patch_id",
            "iteration",
            unique=True,
        ),
        Index("ix_tag_prompt_gradients_decision", "tenant_id", "artifact_id", "decision"),
        Index("ix_tag_prompt_gradients_badcase", "tenant_id", "source_badcase_id"),
    )


class TagPromptDemoSource(TenantScopedBase):
    """Where an inlined example came from, so erasure can reach it.

    A demonstration baked into a prompt outlives the conversation it was drawn from:
    the prompt is copied into an immutable TaggerVersion and sent to the provider on
    every request. This table is the only way back from a served prompt to the
    reception that produced it.
    """

    __tablename__ = "tag_prompt_demo_sources"

    artifact_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "tag_prompt_artifacts.id",
            name="fk_tag_prompt_demo_sources_artifact",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    demo_id: Mapped[str] = mapped_column(String(32), nullable=False)
    gold_label_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "tag_gold_labels.id",
            name="fk_tag_prompt_demo_sources_gold_label",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    reception_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    segment_ids: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    recording_ids: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    redaction_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    source_checksum: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "subject_type IN ('dialogue_unit', 'reception')",
            name="ck_tag_prompt_demo_sources_subject_type",
        ),
        CheckConstraint(
            f"redaction_mode IN ({_REDACTION_MODES})",
            name="ck_tag_prompt_demo_sources_redaction",
        ),
        Index("ux_tag_prompt_demo_sources_demo", "artifact_id", "demo_id", unique=True),
        # The erasure lookup: given a reception, which artefacts embedded it?
        Index("ix_tag_prompt_demo_sources_reception", "tenant_id", "reception_id"),
        Index(
            "ix_tag_prompt_demo_sources_subject",
            "tenant_id",
            "subject_type",
            "subject_id",
        ),
    )


class TagSilverLabel(TenantScopedBase):
    """A machine-proposed label, usable for bootstrapping and nothing else.

    Two constraints do the fencing that a naming convention could not. ``split`` is
    pinned to ``train`` so a silver label cannot reach a validation or holdout lane,
    and ``truth_tier`` is capped below ``t2`` so it can never satisfy the tier
    requirement for freezing a gold set. Evaluation reads ``tag_gold_labels``; these
    rows are a different table entirely, and gold labels require a human review
    decision that no machine can supply.
    """

    __tablename__ = "tag_silver_labels"

    subject_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reception_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tag_key: Mapped[str] = mapped_column(String(128), nullable=False)
    tag_value: Mapped[Any] = mapped_column(JSON, nullable=True)
    evidence_refs: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    truth_state: Mapped[str] = mapped_column(String(24), nullable=False, default="present")
    truth_tier: Mapped[str] = mapped_column(String(8), nullable=False, default="t1")
    split: Mapped[str] = mapped_column(String(16), nullable=False, default="train")
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    teacher_tagger_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "tagger_versions.id",
            name="fk_tag_silver_labels_teacher",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    teacher_model_tier: Mapped[str] = mapped_column(String(16), nullable=False, default="strong")
    teacher_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    agreement_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    promoted_review_task_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "tag_review_tasks.id",
            name="fk_tag_silver_labels_review_task",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        # The two fences. Neither is enforceable in application code alone.
        CheckConstraint("split = 'train'", name="ck_tag_silver_labels_split"),
        CheckConstraint(
            "truth_tier IN ('t0', 't1')",
            name="ck_tag_silver_labels_truth_tier",
        ),
        CheckConstraint(
            f"truth_state IN ({_TRUTH_STATES})",
            name="ck_tag_silver_labels_truth_state",
        ),
        CheckConstraint(
            "teacher_model_tier IN ('strong', 'weak')",
            name="ck_tag_silver_labels_model_tier",
        ),
        CheckConstraint("agreement_count > 0", name="ck_tag_silver_labels_agreement"),
        CheckConstraint(
            "teacher_confidence IS NULL OR (teacher_confidence >= 0 AND teacher_confidence <= 1)",
            name="ck_tag_silver_labels_confidence",
        ),
        CheckConstraint(
            "source IN ('strong_critic', 'seed_import', 'self_consistency')",
            name="ck_tag_silver_labels_source",
        ),
        CheckConstraint(
            "subject_type IN ('dialogue_unit', 'reception')",
            name="ck_tag_silver_labels_subject_type",
        ),
        Index(
            "ux_tag_silver_labels_subject_tag",
            "tenant_id",
            "subject_type",
            "subject_id",
            "tag_key",
            unique=True,
        ),
        # Active-learning ordering: least confident first, within a tag domain.
        Index(
            "ix_tag_silver_labels_uncertainty",
            "tenant_id",
            "tag_key",
            "teacher_confidence",
        ),
        Index("ix_tag_silver_labels_source", "tenant_id", "source", "created_at"),
    )
