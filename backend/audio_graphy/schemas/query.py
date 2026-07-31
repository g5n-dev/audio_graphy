"""Query schemas: request and response for POST /query.

See: docs/m3-prd.md §4.4.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from audio_graphy.adapters.protocols import EdgeConfidence


class TimeRange(BaseModel):
    """Time range filter for queries."""

    start: datetime = Field(description="Time lower bound (ISO 8601)")
    end: datetime = Field(description="Time upper bound (ISO 8601)")

    @model_validator(mode="after")
    def _check_order(self) -> TimeRange:
        if self.end < self.start:
            raise ValueError("time_range.end must be >= time_range.start")
        return self


class QueryRequest(BaseModel):
    """POST /query request body."""

    query: str = Field(min_length=1, max_length=500, description="Natural language question")
    time_range: TimeRange | None = Field(default=None, description="Optional time window filter")
    top_k: int = Field(default=10, ge=1, le=50, description="Max candidates to retrieve")
    store_id: str | None = Field(default=None, max_length=64, description="Limit to specific store")


class Citation(BaseModel):
    """A citation in the final answer — 3-level provenance chain."""

    entity: str
    chunk_id: int
    segment_ids: list[int]
    recording_id: int
    recorded_at: datetime | None = None
    transcript_snippet: str
    confidence: EdgeConfidence = Field(
        description=(
            "Provenance strength of the edge this citation came from. "
            "DEPRECATED is reachable: graph compression downgrades AMBIGUOUS edges to it."
        )
    )


class RetrievalStats(BaseModel):
    """Retrieval statistics."""

    naive_hits: int = 0
    graph_hits: int = 0
    filtered_by_time: int = 0
    filtered_by_judge: int = 0


class QueryResponse(BaseModel):
    """POST /query response."""

    query: str
    answer: str
    citations: list[Citation]
    retrieval_stats: RetrievalStats
