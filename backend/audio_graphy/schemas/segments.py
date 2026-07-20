"""Segment schemas.

See: docs/m3-prd.md §4.3.
"""

from __future__ import annotations

from pydantic import BaseModel


class SegmentResponse(BaseModel):
    """A single VAD segment."""

    id: int
    idx: int
    start_sec: float
    end_sec: float
    transcript: str | None = None
    speaker: str | None = None
    vad_conf: float | None = None


class SegmentListResponse(BaseModel):
    """GET /recordings/{id}/segments paginated response."""

    recording_id: int
    items: list[SegmentResponse]
    total: int
    page: int
    page_size: int
