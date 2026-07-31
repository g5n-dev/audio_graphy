"""Pydantic contracts for the reception listening workspace."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from audio_graphy.core.audio_timeline import seconds_to_milliseconds

ReceptionScenario = Literal["gold", "automotive", "custom"]
ReceptionStatus = Literal[
    "proposed",
    "needs_review",
    "confirmed",
    "processing",
    "ready",
    "split",
    "archived",
]
MergeMode = Literal["logical", "physical", "both"]
DecisionSource = Literal["explicit", "auto", "manual"]
ReceptionProposalDecision = Literal["merge", "reject", "needs_review"]
DEFAULT_WORKSPACE_WINDOW_SIZE_SEC = 600.0
MAX_WORKSPACE_WINDOW_SIZE_SEC = 3_600.0
MAX_WORKSPACE_DIALOGUE_UNITS = 100
MAX_WORKSPACE_TAG_ASSIGNMENTS = 200
MAX_WORKSPACE_STATE_TRANSITIONS = 100
MAX_WORKSPACE_TRANSCRIPT_ITEMS = 300
MAX_WORKSPACE_PROVENANCE_EVENTS = 100
DEFAULT_RECEPTION_TRACE_PAGE_SIZE = 100
MAX_RECEPTION_TRACE_PAGE_SIZE = 200


class _StrictModel(BaseModel):
    """Reject unknown fields so editing clients cannot silently lose intent."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReceptionRecordingCreate(_StrictModel):
    """One immutable source span mapped onto the reception timeline."""

    recording_id: int = Field(gt=0)
    sequence_no: int = Field(ge=0)
    timeline_start_sec: float = Field(ge=0)
    timeline_end_sec: float = Field(gt=0)
    source_start_sec: float = Field(default=0.0, ge=0)
    source_end_sec: float | None = Field(default=None, gt=0)
    gap_before_sec: float = Field(default=0.0, ge=0)
    decision_source: DecisionSource = "explicit"
    merge_confidence: float | None = Field(default=None, ge=0, le=1)
    merge_reasons: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_span(self) -> Self:
        if self.timeline_end_sec <= self.timeline_start_sec:
            raise PydanticCustomError(
                "reception_timeline_order",
                "timeline_end_sec must be greater than timeline_start_sec",
            )
        if self.source_end_sec is not None:
            if self.source_end_sec <= self.source_start_sec:
                raise PydanticCustomError(
                    "reception_source_order",
                    "source_end_sec must be greater than source_start_sec",
                )
            timeline_duration_ms = seconds_to_milliseconds(
                self.timeline_end_sec
            ) - seconds_to_milliseconds(self.timeline_start_sec)
            source_duration_ms = seconds_to_milliseconds(
                self.source_end_sec
            ) - seconds_to_milliseconds(self.source_start_sec)
            if timeline_duration_ms != source_duration_ms:
                raise PydanticCustomError(
                    "reception_duration_mismatch",
                    "timeline and source span durations must match",
                )
        return self


class ReceptionCreate(_StrictModel):
    """Create a reception and its complete logical source mapping."""

    external_session_id: str | None = Field(default=None, min_length=1, max_length=128)
    scenario: ReceptionScenario
    store_id: str = Field(min_length=1, max_length=64)
    agent_name: str | None = Field(default=None, min_length=1, max_length=255)
    customer_hash: str | None = Field(default=None, min_length=1, max_length=64)
    status: ReceptionStatus = "proposed"
    merge_mode: MergeMode = "logical"
    merge_confidence: float | None = Field(default=None, ge=0, le=1)
    started_at: datetime
    ended_at: datetime
    recordings: list[ReceptionRecordingCreate] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_reception_timeline(self) -> Self:
        if self.ended_at < self.started_at:
            raise PydanticCustomError(
                "reception_time_order",
                "ended_at must be greater than or equal to started_at",
            )

        ids = [mapping.recording_id for mapping in self.recordings]
        if len(ids) != len(set(ids)):
            raise PydanticCustomError(
                "duplicate_reception_recording",
                "recording_id values must be unique",
            )

        expected_sequence = list(range(len(self.recordings)))
        if [mapping.sequence_no for mapping in self.recordings] != expected_sequence:
            raise PydanticCustomError(
                "reception_sequence_order",
                "sequence_no must be consecutive and match list order",
            )

        previous_end = 0.0
        for sequence_no, mapping in enumerate(self.recordings):
            if sequence_no == 0 and seconds_to_milliseconds(mapping.gap_before_sec) != 0:
                raise PydanticCustomError(
                    "reception_first_gap",
                    "the first source cannot have gap_before_sec",
                )
            expected_start_ms = seconds_to_milliseconds(
                previous_end
            ) + seconds_to_milliseconds(mapping.gap_before_sec)
            if seconds_to_milliseconds(mapping.timeline_start_sec) != expected_start_ms:
                raise PydanticCustomError(
                    "reception_timeline_gap",
                    "timeline_start_sec must equal previous end plus gap_before_sec",
                )
            previous_end = mapping.timeline_end_sec
        return self


class ReceptionMergeRequest(_StrictModel):
    """Append or reorder source recordings for an existing reception."""

    recording_ids: list[int] = Field(min_length=1, max_length=100)
    mode: MergeMode = "logical"
    expected_version: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_recording_ids(self) -> Self:
        if len(self.recording_ids) != len(set(self.recording_ids)):
            raise PydanticCustomError(
                "duplicate_reception_recording",
                "recording_ids must be unique",
            )
        return self


class ReceptionAudioPlanSourceRequest(_StrictModel):
    mapping_id: int = Field(gt=0)
    gap_before_ms: int = Field(ge=0, le=86_400_000)


class ReceptionAudioPlanRequest(_StrictModel):
    sources: list[ReceptionAudioPlanSourceRequest] = Field(min_length=1, max_length=100)
    expected_version: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_mapping_ids(self) -> Self:
        mapping_ids = [source.mapping_id for source in self.sources]
        if len(mapping_ids) != len(set(mapping_ids)):
            raise PydanticCustomError(
                "duplicate_reception_mapping",
                "mapping_id values must be unique",
            )
        if self.sources[0].gap_before_ms != 0:
            raise PydanticCustomError(
                "reception_first_gap",
                "the first source cannot have gap_before_ms",
            )
        return self


class ReceptionAudioPlanSourceResponse(_StrictModel):
    mapping_id: int
    recording_id: int
    sequence_no: int
    source_start_ms: int
    source_end_ms: int
    gap_before_ms: int
    timeline_start_ms: int
    timeline_end_ms: int


class ReceptionAudioPlanResponse(_StrictModel):
    plan_token: str
    timeline_revision: int
    total_duration_ms: int
    physical_eligible: bool
    warnings: list[str]
    sources: list[ReceptionAudioPlanSourceResponse]


class ReceptionAudioOperationCreateRequest(_StrictModel):
    plan_token: str = Field(min_length=32, max_length=2048)
    mode: MergeMode
    expected_version: int = Field(gt=0)


ReceptionAudioOperationStatus = Literal[
    "queued",
    "claimed",
    "probing",
    "slicing",
    "assembling",
    "encrypting",
    "verifying",
    "committing",
    "succeeded",
    "failed",
    "cancelled",
]


class ReceptionAudioOperationResponse(_StrictModel):
    id: int
    reception_id: int
    status: ReceptionAudioOperationStatus
    mode: MergeMode
    progress: float = Field(ge=0, le=1)
    error: str | None
    created_at: datetime
    updated_at: datetime


class ReceptionMergeProposalRequest(_StrictModel):
    """Evaluate short recordings without creating or mutating a reception."""

    recording_ids: list[int] = Field(min_length=2, max_length=100)
    force_merge: list[tuple[int, int]] = Field(default_factory=list, max_length=100)
    force_split: list[tuple[int, int]] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_constraints(self) -> Self:
        recording_ids = set(self.recording_ids)
        if len(recording_ids) != len(self.recording_ids):
            raise PydanticCustomError(
                "duplicate_reception_recording",
                "recording_ids must be unique",
            )

        def normalize(
            pairs: list[tuple[int, int]],
            *,
            field_name: str,
        ) -> set[frozenset[int]]:
            normalized: set[frozenset[int]] = set()
            for left, right in pairs:
                if left <= 0 or right <= 0 or left == right:
                    raise PydanticCustomError(
                        "invalid_reception_constraint",
                        f"{field_name} pairs require two distinct positive IDs",
                    )
                if left not in recording_ids or right not in recording_ids:
                    raise PydanticCustomError(
                        "unknown_reception_constraint_recording",
                        f"{field_name} pairs must reference recording_ids",
                    )
                normalized.add(frozenset((left, right)))
            return normalized

        merge_pairs = normalize(self.force_merge, field_name="force_merge")
        split_pairs = normalize(self.force_split, field_name="force_split")
        if merge_pairs & split_pairs:
            raise PydanticCustomError(
                "conflicting_reception_constraint",
                "the same pair cannot be force-merged and force-split",
            )
        return self


class ReceptionSegmentRequest(_StrictModel):
    """Generate dialogue units from persisted ASR segments."""

    expected_version: int = Field(gt=0)
    replace_auto: bool = False
    algorithm_version: str = Field(
        default="dialogue-hybrid-v2",
        min_length=1,
        max_length=64,
    )


class DialogueUnitSplitRequest(_StrictModel):
    """Optimistic-locking request for a manual dialogue boundary."""

    split_at_sec: float = Field(gt=0)
    expected_reception_version: int = Field(gt=0)
    expected_unit_version: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=500)


class DialogueUnitMergeRequest(_StrictModel):
    """Optimistic-locking request for two adjacent dialogue units."""

    other_unit_id: int = Field(gt=0)
    expected_reception_version: int = Field(gt=0)
    expected_unit_version: int = Field(gt=0)
    expected_other_unit_version: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=500)


class ReceptionRecordingResponse(_StrictModel):
    """Source recording plus its reception-time alignment."""

    id: int
    mapping_id: int
    recording_id: int
    sequence_no: int
    timeline_start_sec: float
    timeline_end_sec: float
    source_start_sec: float
    source_end_sec: float | None
    source_start_ms: int
    source_end_ms: int | None
    timeline_start_ms: int
    timeline_end_ms: int
    gap_before_ms: int
    time_origin_ms: int
    legal_source_start_ms: int
    legal_source_end_ms: int | None
    gap_before_sec: float
    decision_source: DecisionSource
    merge_confidence: float | None
    merge_reasons: dict[str, Any]
    source_recorded_at: datetime | None
    audio_url: str
    playback_expires_at: datetime


class ReceptionMetadataResponse(_StrictModel):
    """Reception metadata without nested workspace collections."""

    id: int
    tenant_id: str
    external_session_id: str | None
    scenario: ReceptionScenario
    store_id: str
    agent_name: str | None
    agent_user_id: int | None = None
    customer_hash: str | None
    status: ReceptionStatus
    merge_mode: MergeMode
    merge_confidence: float | None
    started_at: datetime
    ended_at: datetime
    audio_url: str | None
    playback_expires_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


class ReceptionResponse(ReceptionMetadataResponse):
    """Reception mutation response with its ordered source mapping."""

    recordings: list[ReceptionRecordingResponse] = Field(default_factory=list)


class ReceptionMergeReasonResponse(_StrictModel):
    """One auditable feature contribution to a merge proposal."""

    code: str
    contribution: float
    detail: str
    hard_constraint: bool


class ReceptionMergeProposalItemResponse(_StrictModel):
    """Pair or grouped reception decision with explainable evidence."""

    recording_ids: list[int]
    decision: ReceptionProposalDecision
    confidence: float = Field(ge=0, le=1)
    reasons: list[ReceptionMergeReasonResponse]
    manual_override: bool


class ReceptionMergeProposalResponse(_StrictModel):
    """Pure proposal result; this contract never implies a database write."""

    recording_ids: list[int]
    proposals: list[ReceptionMergeProposalItemResponse]
    groups: list[ReceptionMergeProposalItemResponse]


class DialogueTagAssignmentResponse(_StrictModel):
    """Versioned label with evidence that can jump back to source audio."""

    id: int
    reception_id: int
    dialogue_unit_id: int
    group_key: str
    group_version: str
    label_key: str
    label_value: str
    confidence: float | None
    source: str
    priority: int
    evidence_refs: list[Any]
    model_run_id: str | None
    is_current: bool
    assigned_at: datetime


class DialogueUnitResponse(_StrictModel):
    """Dialogue unit shown on the listening timeline."""

    id: int
    source_recording_id: int | None
    unit_index: int
    version: int
    start_sec: float
    end_sec: float
    topic: str | None
    business_stage: str | None
    summary: str | None
    boundary_confidence: float | None
    stage_confidence: float | None = None
    boundary_reasons: list[Any]
    segment_refs: list[Any]
    speaker_refs: list[Any]
    edit_status: str
    tag_assignments: list[DialogueTagAssignmentResponse] = Field(default_factory=list)


class DialogueStateTransitionResponse(_StrictModel):
    """Auditable business-stage transition."""

    id: int
    dialogue_unit_id: int | None
    sequence_no: int
    from_state: str
    to_state: str
    trigger: str
    confidence: float
    evidence_refs: list[Any]
    algorithm_version: str
    created_at: datetime


class ProvenanceEventResponse(_StrictModel):
    """Append-only derivation or manual-edit event."""

    id: int
    reception_id: int | None
    object_type: str
    object_ref: str
    event_type: str
    actor: str
    algorithm_version: str | None
    parent_refs: list[Any]
    evidence_refs: list[Any]
    payload: dict[str, Any]
    occurred_at: datetime


class TranscriptItemResponse(_StrictModel):
    """Real persisted transcript segment aligned to the reception timeline."""

    segment_id: int
    recording_id: int
    segment_index: int
    source_start_sec: float
    source_end_sec: float
    timeline_start_sec: float
    timeline_end_sec: float
    speaker: str | None
    text: str
    vad_confidence: float | None


class WorkspaceCollectionWindow(_StrictModel):
    """Cardinality budget for one collection inside a timeline window."""

    total: int = Field(ge=0)
    returned: int = Field(ge=0)
    limit: int = Field(ge=1)
    truncated: bool


class ReceptionWorkspaceWindow(_StrictModel):
    """Observable time window and response budget for a workspace slice."""

    start_sec: float = Field(ge=0)
    end_sec: float = Field(ge=0)
    size_sec: float = Field(gt=0, le=MAX_WORKSPACE_WINDOW_SIZE_SEC)
    reception_duration_sec: float = Field(ge=0)
    truncated: bool
    has_previous: bool
    has_next: bool
    previous_start_sec: float | None = Field(default=None, ge=0)
    next_start_sec: float | None = Field(default=None, ge=0)
    total_dialogue_units: int = Field(ge=0)
    protected_dialogue_units: int = Field(ge=0)
    dialogue_units: WorkspaceCollectionWindow
    tag_assignments: WorkspaceCollectionWindow
    state_transitions: WorkspaceCollectionWindow
    transcript_items: WorkspaceCollectionWindow
    provenance_events: WorkspaceCollectionWindow


class ReceptionWorkspaceCapabilities(_StrictModel):
    can_manage_audio: bool
    can_run_segmentation: bool
    can_edit_dialogue: bool
    can_edit_tags: bool
    supports_audio_plans: bool
    supports_audio_operations: bool
    can_cancel_audio_operation: bool
    can_stream_audio: bool


class ReceptionWorkspaceNeighbors(_StrictModel):
    previous_dialogue_unit: DialogueUnitResponse | None = None
    next_dialogue_unit: DialogueUnitResponse | None = None


class ReceptionWorkspaceResponse(_StrictModel):
    """One bounded page of a listening-workbench snapshot."""

    reception: ReceptionMetadataResponse
    recordings: list[ReceptionRecordingResponse]
    dialogue_units: list[DialogueUnitResponse]
    state_transitions: list[DialogueStateTransitionResponse]
    tag_assignments: list[DialogueTagAssignmentResponse]
    transcript_items: list[TranscriptItemResponse]
    provenance_events: list[ProvenanceEventResponse]
    window: ReceptionWorkspaceWindow
    capabilities: ReceptionWorkspaceCapabilities
    neighbors: ReceptionWorkspaceNeighbors | None = None
    active_audio_operation: ReceptionAudioOperationResponse | None = None


class DialogueEditResponse(_StrictModel):
    """Result of a split/merge, including the complete active unit sequence."""

    reception_id: int
    reception_version: int
    dialogue_units: list[DialogueUnitResponse]


class StateTransitionListResponse(_StrictModel):
    """Ordered state transition trace."""

    reception_id: int
    items: list[DialogueStateTransitionResponse]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=MAX_RECEPTION_TRACE_PAGE_SIZE)
    truncated: bool


class ProvenanceListResponse(_StrictModel):
    """Chronological provenance chain for one object reference."""

    object_type: str
    object_ref: str
    items: list[ProvenanceEventResponse]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=MAX_RECEPTION_TRACE_PAGE_SIZE)
    truncated: bool


ReceptionCandidateType = Literal[
    "merge_group",
    "recording_split",
    "duration_review",
]
ReceptionAcceptCandidateType = Literal["merge_group", "recording_split"]
ReceptionDurationStatus = Literal["available", "unavailable"]


class ReceptionListResponse(_StrictModel):
    """Tenant-scoped paginated reception work queue."""

    items: list[ReceptionMetadataResponse]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)


class ReceptionDiscoveryRequest(_StrictModel):
    """Bounded store/time scan used to find reception candidates."""

    scenario: ReceptionScenario
    store_id: str = Field(min_length=1, max_length=64)
    recorded_from: datetime
    recorded_to: datetime
    short_recording_max_sec: float = Field(default=300.0, gt=0, le=14_400)
    limit: int = Field(default=200, ge=1, le=500)

    @model_validator(mode="after")
    def validate_discovery_window(self) -> Self:
        if (self.recorded_from.utcoffset() is None) != (self.recorded_to.utcoffset() is None):
            raise PydanticCustomError(
                "reception_discovery_timezone_mismatch",
                "recorded_from and recorded_to must use the same timezone form",
            )
        if self.recorded_to <= self.recorded_from:
            raise PydanticCustomError(
                "reception_discovery_time_order",
                "recorded_to must be greater than recorded_from",
            )
        if self.recorded_to - self.recorded_from > timedelta(days=31):
            raise PydanticCustomError(
                "reception_discovery_window_too_large",
                "discovery window cannot exceed 31 days",
            )
        return self


class ReceptionAutomaticProposalResponse(_StrictModel):
    """One explainable merge, split, or missing-duration review candidate."""

    candidate_type: ReceptionCandidateType
    recording_ids: list[int] = Field(min_length=1)
    decision: ReceptionProposalDecision
    confidence: float = Field(ge=0, le=1)
    reasons: list[ReceptionMergeReasonResponse]
    store_id: str
    started_at: datetime
    ended_at: datetime | None
    duration_status: ReceptionDurationStatus
    split_at_sec: float | None = Field(default=None, ge=0)
    at_segment_id: int | None = Field(default=None, gt=0)
    proposal_token: str | None = Field(default=None, min_length=32, max_length=2048)
    proposal_expires_at: datetime | None = None


class ReceptionDiscoveryResponse(_StrictModel):
    """Read-only automatic discovery result."""

    items: list[ReceptionAutomaticProposalResponse]
    total: int = Field(ge=0)
    scanned_recordings: int = Field(ge=0)
    truncated: bool


class ReceptionProposalAcceptRequest(_StrictModel):
    """Accept a discovered group while leaving all timing geometry server-owned."""

    scenario: ReceptionScenario
    recording_ids: list[int] = Field(min_length=1, max_length=100)
    external_session_id: str | None = Field(default=None, min_length=1, max_length=128)
    merge_mode: MergeMode = "logical"
    candidate_type: ReceptionAcceptCandidateType = "merge_group"
    split_at_sec: float | None = Field(default=None, gt=0)
    at_segment_id: int | None = Field(default=None, gt=0)
    proposal_token: str | None = Field(default=None, min_length=32, max_length=2048)

    @model_validator(mode="after")
    def validate_unique_recording_ids(self) -> Self:
        if len(self.recording_ids) != len(set(self.recording_ids)):
            raise PydanticCustomError(
                "duplicate_reception_recording",
                "recording_ids must be unique",
            )
        split_fields = (
            self.split_at_sec,
            self.at_segment_id,
            self.proposal_token,
        )
        if self.candidate_type == "recording_split":
            if len(self.recording_ids) != 1:
                raise PydanticCustomError(
                    "recording_split_cardinality",
                    "recording_split requires exactly one recording_id",
                )
            if any(value is None for value in split_fields):
                raise PydanticCustomError(
                    "recording_split_snapshot_required",
                    "recording_split requires split_at_sec, at_segment_id and proposal_token",
                )
            if self.merge_mode != "logical":
                raise PydanticCustomError(
                    "recording_split_logical_only",
                    "recording_split accepts only logical source mappings",
                )
        elif any(value is not None for value in split_fields):
            raise PydanticCustomError(
                "merge_group_split_fields",
                "split fields are valid only for recording_split",
            )
        return self


class ReceptionSplitAcceptanceResponse(_StrictModel):
    """Explicit result for an atomic split of one immutable source recording."""

    candidate_type: Literal["recording_split"]
    recording_id: int
    split_at_sec: float
    at_segment_id: int
    source_duration_sec: float
    receptions: list[ReceptionResponse] = Field(min_length=2, max_length=2)
    provenance_event_ids: list[int] = Field(min_length=3)


__all__ = [
    "DEFAULT_RECEPTION_TRACE_PAGE_SIZE",
    "DEFAULT_WORKSPACE_WINDOW_SIZE_SEC",
    "MAX_RECEPTION_TRACE_PAGE_SIZE",
    "MAX_WORKSPACE_DIALOGUE_UNITS",
    "MAX_WORKSPACE_PROVENANCE_EVENTS",
    "MAX_WORKSPACE_STATE_TRANSITIONS",
    "MAX_WORKSPACE_TAG_ASSIGNMENTS",
    "MAX_WORKSPACE_TRANSCRIPT_ITEMS",
    "MAX_WORKSPACE_WINDOW_SIZE_SEC",
    "DialogueEditResponse",
    "DialogueStateTransitionResponse",
    "DialogueTagAssignmentResponse",
    "DialogueUnitMergeRequest",
    "DialogueUnitResponse",
    "DialogueUnitSplitRequest",
    "MergeMode",
    "ProvenanceEventResponse",
    "ProvenanceListResponse",
    "ReceptionAudioOperationCreateRequest",
    "ReceptionAudioOperationResponse",
    "ReceptionAudioPlanRequest",
    "ReceptionAudioPlanResponse",
    "ReceptionAudioPlanSourceRequest",
    "ReceptionAudioPlanSourceResponse",
    "ReceptionAutomaticProposalResponse",
    "ReceptionCreate",
    "ReceptionDiscoveryRequest",
    "ReceptionDiscoveryResponse",
    "ReceptionListResponse",
    "ReceptionMergeProposalItemResponse",
    "ReceptionMergeProposalRequest",
    "ReceptionMergeProposalResponse",
    "ReceptionMergeReasonResponse",
    "ReceptionMergeRequest",
    "ReceptionMetadataResponse",
    "ReceptionProposalAcceptRequest",
    "ReceptionRecordingResponse",
    "ReceptionResponse",
    "ReceptionSegmentRequest",
    "ReceptionSplitAcceptanceResponse",
    "ReceptionWorkspaceCapabilities",
    "ReceptionWorkspaceNeighbors",
    "ReceptionWorkspaceResponse",
    "ReceptionWorkspaceWindow",
    "StateTransitionListResponse",
    "TranscriptItemResponse",
    "WorkspaceCollectionWindow",
]
