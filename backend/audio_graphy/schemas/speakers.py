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


class SpeakerDetailResponse(SpeakerListItem):
    """GET /speakers/{id} response — full detail."""

    recordings_list: list[int] = Field(
        ..., description="Recording IDs this speaker appears in"
    )
    related_recordings: list[SpeakerRecordingRef] = Field(
        default_factory=list,
        description="Recording refs with linking metadata (joined via speaker_links)",
    )


__all__ = [
    "SpeakerDetailResponse",
    "SpeakerListItem",
    "SpeakerListResponse",
    "SpeakerRecordingRef",
]
