"""Reception-centric dialogue, state, tag and provenance persistence models."""

from __future__ import annotations

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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from audio_graphy.models.base import TenantScopedBase


class Reception(TenantScopedBase):
    """One business reception, potentially assembled from many recordings."""

    __tablename__ = "receptions"

    external_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scenario: Mapped[str] = mapped_column(String(32), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Stable authorization identity. ``agent_name`` remains a display snapshot
    # and must never be used as an ownership key because names are mutable and
    # are not unique within a tenant.
    agent_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    customer_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="proposed")
    merge_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="logical")
    merge_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    merged_audio_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    active_timeline_revision_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "reception_timeline_revisions.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_receptions_active_timeline_revision_id",
        ),
        nullable=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    recordings: Mapped[list[ReceptionRecording]] = relationship(
        back_populates="reception",
        cascade="all, delete-orphan",
        order_by="ReceptionRecording.sequence_no",
    )
    dialogue_units: Mapped[list[DialogueUnit]] = relationship(
        back_populates="reception",
        cascade="all, delete-orphan",
        order_by="DialogueUnit.unit_index",
    )
    state_transitions: Mapped[list[DialogueStateTransition]] = relationship(
        back_populates="reception",
        cascade="all, delete-orphan",
        order_by="DialogueStateTransition.sequence_no",
    )

    __table_args__ = (
        CheckConstraint(
            "scenario IN ('gold', 'automotive', 'custom')",
            name="ck_receptions_scenario",
        ),
        CheckConstraint(
            "status IN "
            "('proposed', 'needs_review', 'confirmed', 'processing', "
            "'ready', 'split', 'archived')",
            name="ck_receptions_status",
        ),
        CheckConstraint(
            "merge_mode IN ('logical', 'physical', 'both')",
            name="ck_receptions_merge_mode",
        ),
        CheckConstraint(
            "ended_at >= started_at",
            name="ck_receptions_time_order",
        ),
        CheckConstraint(
            "merge_confidence IS NULL OR (merge_confidence >= 0 AND merge_confidence <= 1)",
            name="ck_receptions_merge_confidence",
        ),
        CheckConstraint("version > 0", name="ck_receptions_version"),
        Index(
            "ux_receptions_tenant_external_session",
            "tenant_id",
            "external_session_id",
            unique=True,
        ),
        Index(
            "ix_receptions_tenant_store_start",
            "tenant_id",
            "store_id",
            "started_at",
        ),
        Index("ix_receptions_tenant_status", "tenant_id", "status"),
        Index(
            "ix_receptions_tenant_started_id",
            "tenant_id",
            "started_at",
            "id",
        ),
        Index(
            "ix_receptions_tenant_agent_started_id",
            "tenant_id",
            "agent_user_id",
            "started_at",
            "id",
        ),
    )


class ReceptionAutomationRun(TenantScopedBase):
    """Durable checkpoint and lease for one reception automation workflow."""

    __tablename__ = "reception_automation_runs"

    reception_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("receptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    stage: Mapped[str] = mapped_column(String(24), nullable=False, default="merge")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checkpoints: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    segmentation_algorithm: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="dialogue-hybrid-v1",
    )
    tag_group_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="reception-rules",
    )
    tag_group_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="rules-v1",
    )
    target_labels: Mapped[list[Any]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    tag_priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'failed', 'ready')",
            name="ck_reception_automation_runs_status",
        ),
        CheckConstraint(
            "stage IN ('merge', 'segmentation', 'tagging', 'ready')",
            name="ck_reception_automation_runs_stage",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_reception_automation_runs_attempt_count",
        ),
        CheckConstraint(
            "tag_priority >= -1000 AND tag_priority <= 1000",
            name="ck_reception_automation_runs_tag_priority",
        ),
        Index(
            "ux_reception_automation_runs_reception",
            "reception_id",
            unique=True,
        ),
        Index(
            "ix_reception_automation_runs_tenant_status",
            "tenant_id",
            "status",
            "stage",
        ),
    )


class ReceptionRecording(TenantScopedBase):
    """Timeline mapping from an immutable source recording into a reception."""

    __tablename__ = "reception_recordings"

    reception_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("receptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    recording_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("recordings.id", ondelete="CASCADE"),
        nullable=False,
    )
    timeline_revision_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("reception_timeline_revisions.id", ondelete="SET NULL"),
        nullable=True,
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    timeline_start_sec: Mapped[float] = mapped_column(Float, nullable=False)
    timeline_end_sec: Mapped[float] = mapped_column(Float, nullable=False)
    source_start_sec: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    source_end_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_start_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    source_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    timeline_start_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    timeline_end_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    gap_before_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    gap_before_sec: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    decision_source: Mapped[str] = mapped_column(String(16), nullable=False)
    merge_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    merge_reasons: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    reception: Mapped[Reception] = relationship(back_populates="recordings")

    __table_args__ = (
        CheckConstraint(
            "timeline_end_sec > timeline_start_sec",
            name="ck_reception_recordings_timeline",
        ),
        CheckConstraint(
            "source_end_sec IS NULL OR source_end_sec > source_start_sec",
            name="ck_reception_recordings_source_time",
        ),
        CheckConstraint("sequence_no >= 0", name="ck_reception_recordings_sequence"),
        CheckConstraint(
            "source_start_ms >= 0 AND timeline_start_ms >= 0 AND "
            "timeline_end_ms >= timeline_start_ms AND gap_before_ms >= 0 AND "
            "(source_end_ms IS NULL OR source_end_ms > source_start_ms)",
            name="ck_reception_recordings_integer_timeline",
        ),
        CheckConstraint(
            "decision_source IN ('explicit', 'auto', 'manual')",
            name="ck_reception_recordings_decision_source",
        ),
        CheckConstraint(
            "merge_confidence IS NULL OR (merge_confidence >= 0 AND merge_confidence <= 1)",
            name="ck_reception_recordings_confidence",
        ),
        Index(
            "ux_reception_recordings_recording",
            "reception_id",
            "recording_id",
            unique=True,
        ),
        Index(
            "ux_reception_recordings_sequence",
            "reception_id",
            "sequence_no",
            unique=True,
        ),
        Index(
            "ix_reception_recordings_tenant_recording",
            "tenant_id",
            "recording_id",
        ),
    )


class DialogueUnit(TenantScopedBase):
    """Semantic/business dialogue unit over the merged reception timeline."""

    __tablename__ = "dialogue_units"

    reception_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("receptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_recording_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("recordings.id", ondelete="SET NULL"),
        nullable=True,
    )
    unit_index: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    start_sec: Mapped[float] = mapped_column(Float, nullable=False)
    end_sec: Mapped[float] = mapped_column(Float, nullable=False)
    topic: Mapped[str | None] = mapped_column(String(255), nullable=True)
    business_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    boundary_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    stage_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    timeline_revision_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("reception_timeline_revisions.id", ondelete="SET NULL"),
        nullable=True,
    )
    boundary_reasons: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    segment_refs: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    speaker_refs: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    edit_status: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")

    reception: Mapped[Reception] = relationship(back_populates="dialogue_units")
    tag_assignments: Mapped[list[DialogueTagAssignment]] = relationship(
        back_populates="dialogue_unit",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint("end_sec > start_sec", name="ck_dialogue_units_time_order"),
        CheckConstraint("unit_index >= 0", name="ck_dialogue_units_index"),
        CheckConstraint("version > 0", name="ck_dialogue_units_version"),
        CheckConstraint(
            "boundary_confidence IS NULL OR "
            "(boundary_confidence >= 0 AND boundary_confidence <= 1)",
            name="ck_dialogue_units_boundary_confidence",
        ),
        CheckConstraint(
            "stage_confidence IS NULL OR (stage_confidence >= 0 AND stage_confidence <= 1)",
            name="ck_dialogue_units_stage_confidence",
        ),
        CheckConstraint(
            "edit_status IN ('auto', 'manual_edited', 'locked')",
            name="ck_dialogue_units_edit_status",
        ),
        Index(
            "ux_dialogue_units_reception_index_version",
            "reception_id",
            "unit_index",
            "version",
            unique=True,
        ),
        Index(
            "ix_dialogue_units_reception_timeline",
            "reception_id",
            "start_sec",
            "end_sec",
        ),
        Index(
            "ix_dialogue_units_tenant_stage",
            "tenant_id",
            "business_stage",
        ),
    )


class DialogueStateTransition(TenantScopedBase):
    """Auditable state transition between business stages."""

    __tablename__ = "dialogue_state_transitions"

    reception_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("receptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    dialogue_unit_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("dialogue_units.id", ondelete="SET NULL"),
        nullable=True,
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    from_state: Mapped[str] = mapped_column(String(64), nullable=False)
    to_state: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger: Mapped[str] = mapped_column(String(128), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence_refs: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False)
    timeline_revision_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("reception_timeline_revisions.id", ondelete="SET NULL"),
        nullable=True,
    )

    reception: Mapped[Reception] = relationship(back_populates="state_transitions")

    __table_args__ = (
        CheckConstraint("sequence_no >= 0", name="ck_dialogue_state_transitions_sequence"),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_dialogue_state_transitions_confidence",
        ),
        Index(
            "ux_dialogue_state_transitions_sequence",
            "reception_id",
            "sequence_no",
            unique=True,
        ),
        Index(
            "ix_dialogue_state_transitions_tenant_state",
            "tenant_id",
            "to_state",
        ),
    )


class DialogueTagAssignment(TenantScopedBase):
    """Versioned label assignment with evidence for one dialogue unit."""

    __tablename__ = "dialogue_tag_assignments"

    reception_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("receptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    dialogue_unit_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("dialogue_units.id", ondelete="CASCADE"),
        nullable=False,
    )
    group_key: Mapped[str] = mapped_column(String(64), nullable=False)
    group_version: Mapped[str] = mapped_column(String(64), nullable=False)
    label_key: Mapped[str] = mapped_column(String(128), nullable=False)
    label_value: Mapped[str] = mapped_column(String(255), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evidence_refs: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    model_run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timeline_revision_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("reception_timeline_revisions.id", ondelete="SET NULL"),
        nullable=True,
    )

    dialogue_unit: Mapped[DialogueUnit] = relationship(back_populates="tag_assignments")

    __table_args__ = (
        CheckConstraint(
            "source IN ('rule', 'llm', 'manual', 'imported')",
            name="ck_dialogue_tag_assignments_source",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_dialogue_tag_assignments_confidence",
        ),
        Index(
            "ux_dialogue_tags_assignment_version",
            "dialogue_unit_id",
            "group_key",
            "group_version",
            "label_key",
            unique=True,
        ),
        Index(
            "ix_dialogue_tags_matrix",
            "tenant_id",
            "reception_id",
            "group_key",
            "group_version",
            "is_current",
        ),
        Index(
            "ix_dialogue_tags_label_value",
            "tenant_id",
            "label_key",
            "label_value",
        ),
    )


class ProvenanceEvent(TenantScopedBase):
    """Append-only derivation/edit/merge event for lineage and replay."""

    __tablename__ = "provenance_events"

    reception_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("receptions.id", ondelete="CASCADE"),
        nullable=True,
    )
    object_type: Mapped[str] = mapped_column(String(64), nullable=False)
    object_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False, default="system")
    algorithm_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parent_refs: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    evidence_refs: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "event_type IN "
            "('created', 'derived', 'merged', 'split', 'edited', "
            "'superseded', 'deleted', 'restored')",
            name="ck_provenance_events_type",
        ),
        Index(
            "ix_provenance_events_reception",
            "tenant_id",
            "reception_id",
            "occurred_at",
        ),
        Index(
            "ix_provenance_events_object",
            "tenant_id",
            "object_type",
            "object_ref",
            "occurred_at",
        ),
        Index(
            "ix_provenance_events_tenant_time",
            "tenant_id",
            "occurred_at",
        ),
    )


__all__ = [
    "DialogueStateTransition",
    "DialogueTagAssignment",
    "DialogueUnit",
    "ProvenanceEvent",
    "Reception",
    "ReceptionAutomationRun",
    "ReceptionRecording",
]
