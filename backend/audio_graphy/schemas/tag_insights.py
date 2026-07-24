"""Schemas for multi-group dialogue-tag merge, comparison, and insights.

The API is intentionally storage-agnostic: callers submit bounded tag snapshots
and receive a deterministic analysis result.  ``tenant_id`` is still explicit
so the route can reject accidental cross-tenant analysis before any data is
processed.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_GROUPS = 8
MAX_ASSIGNMENTS = 5_000
MAX_EVIDENCE_PER_ASSIGNMENT = 16
MAX_LABELS_PER_GROUP_WINDOW = 128
MAX_ASSIGNMENTS_PER_WINDOW = 512
MAX_MATRIX_ROWS = 96
MAX_DIFFERENCE_ITEMS = 128
MAX_DISTRIBUTION_ITEMS = 512
MAX_TREND_ITEMS = 512
MAX_DIMENSION_ITEMS = 256
MAX_OUTPUT_EVIDENCE_REFS = 512
MAX_OUTPUT_EVIDENCE_TEXT_BYTES = 32 * 1024

MergeStrategy = Literal["union", "intersection", "priority", "manual_wins"]
EvidenceKind = Literal["audio", "text"]
TrendGranularity = Literal["day", "week", "month"]
DimensionName = Literal["store", "agent"]


class _StrictModel(BaseModel):
    """Base model that rejects unknown fields and strips text boundaries."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TimeWindow(_StrictModel):
    """Dialogue-relative time window in milliseconds."""

    start_ms: int = Field(ge=0, le=86_400_000)
    end_ms: int = Field(gt=0, le=86_400_000)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.end_ms <= self.start_ms:
            raise ValueError("window end_ms must be greater than start_ms")
        return self


class EvidenceRef(_StrictModel):
    """Traceable source reference for an audio span or transcript excerpt."""

    ref_id: str = Field(min_length=1, max_length=128)
    kind: EvidenceKind
    recording_id: str = Field(min_length=1, max_length=128)
    start_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    end_ms: int | None = Field(default=None, gt=0, le=86_400_000)
    text_excerpt: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def validate_span(self) -> Self:
        if (self.start_ms is None) != (self.end_ms is None):
            raise ValueError("evidence start_ms and end_ms must be provided together")
        if self.start_ms is not None and self.end_ms is not None and self.end_ms <= self.start_ms:
            raise ValueError("evidence end_ms must be greater than start_ms")
        if self.kind == "text" and not self.text_excerpt:
            raise ValueError("text evidence requires text_excerpt")
        return self


class EvidenceRefSummary(_StrictModel):
    """Small evidence pointer used in repeated comparison structures."""

    ref_id: str = Field(min_length=1, max_length=128)
    kind: EvidenceKind
    recording_id: str = Field(min_length=1, max_length=128)
    start_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    end_ms: int | None = Field(default=None, gt=0, le=86_400_000)

    @model_validator(mode="after")
    def validate_span(self) -> Self:
        if (self.start_ms is None) != (self.end_ms is None):
            raise ValueError("evidence start_ms and end_ms must be provided together")
        if self.start_ms is not None and self.end_ms is not None and self.end_ms <= self.start_ms:
            raise ValueError("evidence end_ms must be greater than start_ms")
        return self


class TagGroup(_StrictModel):
    """A versioned source column in the comparison matrix."""

    group_key: str = Field(min_length=1, max_length=64, pattern=r"^[\w.-]+$")
    version: str = Field(min_length=1, max_length=64)
    group_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
        pattern=r"^[\w.@:-]+$",
    )
    source: str = Field(min_length=1, max_length=32)
    priority: int = Field(default=0, ge=-1_000, le=1_000)


class TagAssignment(_StrictModel):
    """One tag value produced by one group for one aligned dialogue cell."""

    group_key: str = Field(min_length=1, max_length=64, pattern=r"^[\w.-]+$")
    group_version: str | None = Field(default=None, min_length=1, max_length=64)
    group_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
        pattern=r"^[\w.@:-]+$",
    )
    target_id: str = Field(min_length=1, max_length=128)
    window: TimeWindow
    label_key: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=512)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence_refs: list[EvidenceRef] = Field(
        default_factory=list,
        max_length=MAX_EVIDENCE_PER_ASSIGNMENT,
    )
    is_manual: bool = False
    occurred_at: datetime | None = None
    store_id: str | None = Field(default=None, min_length=1, max_length=128)
    agent_id: str | None = Field(default=None, min_length=1, max_length=128)


class AnalyzeTagInsightsRequest(_StrictModel):
    """Bounded request for deterministic in-memory tag analysis."""

    tenant_id: str = Field(min_length=1, max_length=64)
    merge_strategy: MergeStrategy = "manual_wins"
    groups: list[TagGroup] = Field(min_length=1, max_length=MAX_GROUPS)
    assignments: list[TagAssignment] = Field(min_length=1, max_length=MAX_ASSIGNMENTS)
    trend_granularity: TrendGranularity = "day"
    top_n_co_occurrences: int = Field(default=50, ge=1, le=200)
    matrix_limit: int = Field(default=MAX_MATRIX_ROWS, ge=1, le=MAX_MATRIX_ROWS)
    difference_limit: int = Field(
        default=MAX_DIFFERENCE_ITEMS,
        ge=0,
        le=MAX_DIFFERENCE_ITEMS,
    )

    @model_validator(mode="after")
    def validate_relations(self) -> Self:
        groups_by_key: dict[str, list[TagGroup]] = defaultdict(list)
        for group in self.groups:
            groups_by_key[group.group_key].append(group)

        declared: dict[str, TagGroup] = {}
        for group in self.groups:
            if group.group_id is None:
                group.group_id = (
                    group.group_key
                    if len(groups_by_key[group.group_key]) == 1
                    else f"{group.group_key}@{group.version}"
                )
            if group.group_id in declared:
                raise ValueError(f"duplicate group_id declaration: {group.group_id}")
            declared[group.group_id] = group

        undeclared: set[str] = set()
        for assignment in self.assignments:
            candidates = groups_by_key.get(assignment.group_key, [])
            selected: TagGroup | None = None
            if assignment.group_id is not None:
                selected = declared.get(assignment.group_id)
                if selected is None or selected.group_key != assignment.group_key:
                    undeclared.add(assignment.group_id)
                    continue
                if (
                    assignment.group_version is not None
                    and assignment.group_version != selected.version
                ):
                    raise ValueError(
                        "assignment group_version does not match its declared group_id"
                    )
            elif assignment.group_version is not None:
                matching = [
                    group for group in candidates if group.version == assignment.group_version
                ]
                if len(matching) != 1:
                    raise ValueError(
                        "assignment group_key/group_version is undeclared or ambiguous"
                    )
                selected = matching[0]
            elif len(candidates) == 1:
                selected = candidates[0]
            elif len(candidates) > 1:
                raise ValueError(
                    "assignment group_key is ambiguous; provide group_version or group_id"
                )
            else:
                undeclared.add(assignment.group_key)
                continue

            assignment.group_id = selected.group_id
            assignment.group_version = selected.version

        if undeclared:
            raise ValueError(f"assignments reference undeclared groups: {sorted(undeclared)}")

        seen: set[tuple[str, str, int, int, str]] = set()
        per_group_window: Counter[tuple[str, str, int, int]] = Counter()
        per_window: Counter[tuple[str, int, int]] = Counter()
        for assignment in self.assignments:
            assert assignment.group_id is not None
            cell = (
                assignment.group_id,
                assignment.target_id,
                assignment.window.start_ms,
                assignment.window.end_ms,
                assignment.label_key,
            )
            if cell in seen:
                raise ValueError("duplicate assignment for group/target/window/label cell")
            seen.add(cell)
            group_window = (
                assignment.group_id,
                assignment.target_id,
                assignment.window.start_ms,
                assignment.window.end_ms,
            )
            window = (
                assignment.target_id,
                assignment.window.start_ms,
                assignment.window.end_ms,
            )
            per_group_window[group_window] += 1
            per_window[window] += 1
            if per_group_window[group_window] > MAX_LABELS_PER_GROUP_WINDOW:
                raise ValueError("too many labels for one group/target/window combination")
            if per_window[window] > MAX_ASSIGNMENTS_PER_WINDOW:
                raise ValueError("too many assignments for one target/window")
        return self


class MatrixCell(_StrictModel):
    """One source-group column inside an aligned matrix row."""

    group: TagGroup
    assignments: list[TagAssignment] = Field(default_factory=list)
    missing: bool


class MergedTagResult(_StrictModel):
    """Merged value(s) plus their complete provenance."""

    strategy: MergeStrategy
    values: list[str] = Field(default_factory=list)
    selected_group_keys: list[str] = Field(default_factory=list)
    confidence: float | None = None
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)


class MatrixRow(_StrictModel):
    """A target/window/label row aligned across every source group."""

    target_id: str
    window: TimeWindow
    label_key: str
    store_ids: list[str] = Field(default_factory=list)
    agent_ids: list[str] = Field(default_factory=list)
    cells: list[MatrixCell]
    merged: MergedTagResult
    conflict: bool
    missing_group_keys: list[str] = Field(default_factory=list)


class InsightOverview(_StrictModel):
    """Top-level analysis cardinalities."""

    group_count: int
    assignment_count: int
    total_cells: int
    complete_cells: int
    incomplete_cells: int
    conflict_cells: int
    conflict_rate: float


class CoverageInsight(_StrictModel):
    """Coverage and missingness for one source group."""

    group_key: str
    assigned_cells: int
    missing_cells: int
    coverage_rate: float


class PairwiseDifference(_StrictModel):
    """An evidence-backed difference between two groups."""

    target_id: str
    window: TimeWindow
    label_key: str
    left_value: str
    right_value: str
    left_evidence_count: int = Field(ge=0)
    right_evidence_count: int = Field(ge=0)
    left_evidence_refs: list[EvidenceRefSummary] = Field(
        default_factory=list,
        max_length=1,
    )
    right_evidence_refs: list[EvidenceRefSummary] = Field(
        default_factory=list,
        max_length=1,
    )


class PairwiseComparison(_StrictModel):
    """Agreement, difference, and asymmetric coverage for a group pair."""

    left_group_key: str
    right_group_key: str
    comparable_cells: int
    agreements: int
    differences: int
    agreement_rate: float | None
    left_only_cells: int
    right_only_cells: int
    overlap_rate: float
    difference_items: list[PairwiseDifference] = Field(default_factory=list)
    difference_items_truncated: bool = False


class DistributionInsight(_StrictModel):
    """Value distribution for one label and source group."""

    group_key: str
    label_key: str
    value: str
    count: int
    proportion: float


class TrendInsight(_StrictModel):
    """Time-bucketed label value count."""

    bucket_key: str
    group_key: str
    label_key: str
    value: str
    count: int


class CoOccurrenceInsight(_StrictModel):
    """Co-occurring label-value pair within the same dialogue window."""

    group_key: str
    left_label: str
    right_label: str
    count: int


class ConfidenceInsight(_StrictModel):
    """Confidence calibration bucket for one source group."""

    group_key: str
    bucket: str
    count: int
    average_confidence: float | None


class DimensionComparison(_StrictModel):
    """Store or agent slice for side-by-side source comparison."""

    dimension: DimensionName
    dimension_value: str
    group_key: str
    total_cells: int
    assignment_count: int
    missing_cells: int
    coverage_rate: float
    unique_targets: int
    average_confidence: float | None
    conflict_assignments: int
    conflict_rate: float


class InsightOutputBudget(_StrictModel):
    """Observable hard limits applied after computing complete aggregates."""

    matrix_limit: int
    matrix_total_rows: int
    matrix_returned_rows: int
    difference_limit: int
    difference_total_items: int
    difference_returned_items: int
    distribution_limit: int
    distribution_total_items: int
    distribution_returned_items: int
    trend_limit: int
    trend_total_items: int
    trend_returned_items: int
    dimension_limit: int
    dimension_total_items: int
    dimension_returned_items: int
    evidence_ref_limit: int
    evidence_ref_count: int
    evidence_text_byte_limit: int
    evidence_text_bytes: int


class AnalyzeTagInsightsResponse(_StrictModel):
    """Complete visualization-ready tag insight payload."""

    tenant_id: str
    merge_strategy: MergeStrategy
    groups: list[TagGroup]
    truncated: bool
    matrix_truncated: bool
    difference_truncated: bool
    evidence_truncated: bool
    output_budget: InsightOutputBudget
    overview: InsightOverview
    matrix: list[MatrixRow]
    coverage: list[CoverageInsight]
    pairwise: list[PairwiseComparison]
    distributions: list[DistributionInsight]
    trends: list[TrendInsight]
    co_occurrences: list[CoOccurrenceInsight]
    confidence: list[ConfidenceInsight]
    dimension_comparisons: list[DimensionComparison]
