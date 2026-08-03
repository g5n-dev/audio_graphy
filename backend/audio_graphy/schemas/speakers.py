"""Speaker schemas — M7 WS-3 T12.

Pydantic response models for the ``/speakers`` API. PIPL compliance:
never expose raw voiceprint vectors; only the ``voiceprint_id`` (which is
already a sha256 hash).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class SpeakerListItem(BaseModel):
    """One row in GET /speakers list response."""

    id: int = Field(..., description="Speaker node database ID")
    tenant_id: str = Field(..., description="Tenant scope")
    display_name: str = Field(..., description="Human-readable label")
    voiceprint_hash: str = Field(
        ...,
        description="First 8 chars of voiceprint sha256 (e.g. 'vp_a1b2c3d4')",
    )
    speaker_role: str = Field(..., description="agent / customer / unknown")
    recordings_count: int = Field(..., description="Number of recordings this speaker appears in")
    first_seen: datetime | None = Field(default=None, description="First recording timestamp")
    total_speech_sec: float = Field(..., description="Cumulative speech duration in seconds")
    merge_confidence: float = Field(..., description="Highest merge confidence observed")
    merge_strategy: str = Field(..., description="voiceprint / fuzzy / manual / single_recording")
    ambiguity_tag: str | None = Field(
        default=None,
        description="None / 'AMBIGUOUS' / 'PENDING_REVIEW'",
    )


class SpeakerListResponse(BaseModel):
    """GET /speakers response."""

    items: list[SpeakerListItem]
    total: int


class SpeakerRecordingRef(BaseModel):
    """One recording in a speaker's recording list."""

    recording_id: int
    voiceprint_id: str = Field(..., description="Voiceprint hash for this recording")
    duration_sec: float
    strategy: str = Field(..., description="How this recording was linked to the speaker")
    ambiguity_tag: str | None = None
    cosine_similarity: float | None = Field(
        default=None,
        description="Voiceprint cosine that produced this link. None for links "
        "made without a voiceprint comparison (new speaker, fuzzy match).",
    )
    merge_confidence: float | None = Field(
        default=None,
        description="Confidence recorded for this link, whatever produced it.",
    )


class SpeakerDetailResponse(SpeakerListItem):
    """GET /speakers/{id} response — full detail."""

    recordings_list: list[int] = Field(..., description="Recording IDs this speaker appears in")
    related_recordings: list[SpeakerRecordingRef] = Field(
        default_factory=list,
        description="Recording refs with linking metadata (joined via speaker_links)",
    )


class RecordingSpeakerRef(BaseModel):
    """One diarization label in a recording, resolved to its speaker.

    This is what a transcript or timeline view needs: segments carry only
    the per-file label, so without this mapping every line shows ``spk_0``
    with no identity and no quality signal attached.
    """

    source_speaker_label: str = Field(..., description="Diarization-local label, e.g. 'spk_0'")
    speaker_node_id: int = Field(..., description="Canonical speaker this resolves to")
    display_name: str
    speaker_role: str = Field(..., description="agent / customer / unknown")
    ambiguity_tag: str | None = Field(
        default=None, description="None / 'AMBIGUOUS' / 'PENDING_REVIEW'"
    )
    merge_confidence: float | None = Field(
        default=None, description="Confidence of the link that resolved this label"
    )
    cosine_similarity: float | None = Field(
        default=None, description="Voiceprint cosine, when one produced the link"
    )
    strategy: str = Field(..., description="How the label was linked")


class RecordingSpeakerListResponse(BaseModel):
    """GET /recordings/{id}/speakers response."""

    recording_id: int
    items: list[RecordingSpeakerRef]


class VoiceprintPolicyLayer1(BaseModel):
    """Layer-1 voiceprint cosine thresholds (SpeakerLinker, L9/Q2)."""

    cosine_threshold: float = Field(..., description="Minimum cosine for a cross-recording merge")
    ambiguous_threshold: float = Field(
        ...,
        description="Cosine ≥ this merges without ambiguity_tag; between the two "
        "thresholds the merge is tagged AMBIGUOUS",
    )


class VoiceprintPolicyLayer2(BaseModel):
    """Layer-2 fuzzy name-match thresholds (SpeakerFuzzyMatcher, L8)."""

    enabled: bool = Field(..., description="Whether Layer-2 fuzzy matching is active")
    fuzzy_inferred_threshold: float = Field(
        ..., description="Minimum fuzzy score to propose a match"
    )
    fuzzy_ambiguous_threshold: float = Field(
        ..., description="Fuzzy score ≥ this is a strong (non-ambiguous) proposal"
    )
    voiceprint_reconfirm_cosine: float = Field(
        ..., description="Cosine required to auto-resolve a pending fuzzy match"
    )


class VoiceprintPolicySampling(BaseModel):
    """Voiceprint sampling parameters and quality gates (ADR-0001)."""

    strategy: str = Field(
        ...,
        description="weighted_mean (per-segment embeddings averaged by duration) "
        "or longest_segment (single embedding from the longest segment)",
    )
    min_segment_sec: float = Field(
        ...,
        description="Segments shorter than this never contribute to a candidate",
    )
    min_total_sec: float = Field(
        ...,
        description="A speaker with less qualifying speech than this gets no "
        "cross-recording voiceprint",
    )
    max_segments_per_speaker: int = Field(
        ..., description="Extraction cap per speaker per recording"
    )
    diarization_min_segment_sec: float = Field(
        ..., description="Diarization segments shorter than this are dropped upstream"
    )
    max_speakers: int = Field(..., description="Diarization speaker-count cap per file")
    embedding_dim: int = Field(..., description="Voiceprint vector dimensionality")


class VoiceprintPolicyResponse(BaseModel):
    """GET /speakers/voiceprint-policy response — read-only runtime policy.

    Surfaces the speaker-linking thresholds and sampling parameters so the
    UI can explain *why* a speaker was merged / tagged AMBIGUOUS. Contains
    no biometric data.
    """

    enable_voiceprint: bool = Field(
        ..., description="Whether the voiceprint pipeline is enabled for this deployment"
    )
    adapter_voiceprint_mode: str = Field(..., description="mock / real adapter mode")
    layer1: VoiceprintPolicyLayer1
    layer2: VoiceprintPolicyLayer2
    sampling: VoiceprintPolicySampling
    retention_cascade: bool = Field(
        ...,
        description="Whether DSAR/retention erasure cascades to voiceprint rows",
    )


__all__ = [
    "RecordingSpeakerListResponse",
    "RecordingSpeakerRef",
    "SpeakerDetailResponse",
    "SpeakerListItem",
    "SpeakerListResponse",
    "SpeakerRecordingRef",
    "VoiceprintPolicyLayer1",
    "VoiceprintPolicyLayer2",
    "VoiceprintPolicyResponse",
    "VoiceprintPolicySampling",
]
