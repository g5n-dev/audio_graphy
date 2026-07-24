"""Contracts for persisted reception dialogue tags and database insights."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from audio_graphy.schemas.receptions import DialogueTagAssignmentResponse
from audio_graphy.schemas.tag_insights import (
    AnalyzeTagInsightsResponse,
    MergeStrategy,
    TrendGranularity,
)

MAX_EVIDENCE_SUMMARY_ITEMS = 256
MAX_RECEPTION_OUTPUT_EVIDENCE_REFS = 1_024

DialogueTargetLabel = Literal[
    "stage",
    "intent",
    "objection",
    "next_step",
    "compliance_risk",
]

ALL_DIALOGUE_TARGET_LABELS: tuple[DialogueTargetLabel, ...] = (
    "stage",
    "intent",
    "objection",
    "next_step",
    "compliance_risk",
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DeriveDialogueTagsRequest(_StrictModel):
    """Select an immutable rule version and the label dimensions to derive."""

    group_key: str = Field(
        default="reception-rules",
        min_length=1,
        max_length=64,
        pattern=r"^[\w.-]+$",
    )
    group_version: str = Field(
        default="rules-v1",
        min_length=1,
        max_length=64,
        pattern=r"^[\w.-]+$",
    )
    target_labels: list[DialogueTargetLabel] = Field(
        default_factory=lambda: list(ALL_DIALOGUE_TARGET_LABELS),
        min_length=1,
        max_length=len(ALL_DIALOGUE_TARGET_LABELS),
    )
    priority: int = Field(default=0, ge=-1_000, le=1_000)
    model_run_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_unique_targets(self) -> Self:
        if len(self.target_labels) != len(set(self.target_labels)):
            raise ValueError("target_labels must be unique")
        return self


class MissingDialogueTag(_StrictModel):
    """A requested unit/label cell that has no defensible assignment."""

    dialogue_unit_id: int
    unit_index: int = Field(ge=0)
    label_key: DialogueTargetLabel
    reason: Literal[
        "no_verified_segment_evidence",
        "missing_stage",
        "no_rule_match",
    ]


class DeriveDialogueTagsResponse(_StrictModel):
    reception_id: int
    group_key: str
    group_version: str
    requested_labels: list[DialogueTargetLabel]
    assignment_count: int = Field(ge=0)
    superseded_count: int = Field(ge=0)
    no_op: bool
    assignments: list[DialogueTagAssignmentResponse]
    missing: list[MissingDialogueTag]


class ReceptionTagEvidenceSummary(_StrictModel):
    """Bounded, path-free evidence rows for drill-down visualizations."""

    reception_id: int
    dialogue_unit_id: int
    group_id: str
    label_key: str
    label_value: str
    confidence: float | None
    evidence_count: int = Field(ge=0)
    evidence_refs: list[dict[str, Any]]


class ReceptionTagInsightsResponse(_StrictModel):
    """Database-backed, pagination-aware wrapper around the insight engine.

    Reception pagination is applied before the assignment limit. ``current``
    mode reads only ``is_current=true`` rows; ``exact_versions`` mode is the
    explicit opt-in that may read historical rows.
    """

    tenant_id: str
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total_receptions: int = Field(
        ge=0,
        description="Reception count before reception-page slicing.",
    )
    returned_reception_ids: list[int] = Field(
        description="Reception IDs on the requested page; assignments are scoped to these IDs.",
    )
    total_assignments: int = Field(
        ge=0,
        description="Matching assignment count within returned_reception_ids before limit.",
    )
    assignment_count: int = Field(
        ge=0,
        description="Assignments loaded for analysis after assignment_limit.",
    )
    assignment_limit: int = Field(ge=1, le=5_000)
    truncated: bool
    assignment_truncated: bool
    group_truncated: bool
    difference_truncated: bool
    evidence_truncated: bool
    evidence_ref_limit: int = Field(ge=1)
    evidence_ref_count: int = Field(ge=0)
    evidence_summary_total: int = Field(ge=0)
    evidence_summary_count: int = Field(ge=0)
    evidence_summary_limit: int = Field(
        ge=1,
        le=MAX_EVIDENCE_SUMMARY_ITEMS,
    )
    evidence_summary_truncated: bool
    selection_mode: Literal["current", "exact_versions"] = Field(
        description="Whether historical assignments were eligible.",
    )
    selected_group_ids: list[str] = Field(
        max_length=8,
        description="Unambiguous key@version columns selected for this page.",
    )
    merge_strategy: MergeStrategy
    trend_granularity: TrendGranularity
    insights: AnalyzeTagInsightsResponse | None
    evidence_summary: list[ReceptionTagEvidenceSummary]
    generated_at: datetime


__all__ = [
    "ALL_DIALOGUE_TARGET_LABELS",
    "MAX_EVIDENCE_SUMMARY_ITEMS",
    "MAX_RECEPTION_OUTPUT_EVIDENCE_REFS",
    "DeriveDialogueTagsRequest",
    "DeriveDialogueTagsResponse",
    "DialogueTargetLabel",
    "MissingDialogueTag",
    "ReceptionTagEvidenceSummary",
    "ReceptionTagInsightsResponse",
]
