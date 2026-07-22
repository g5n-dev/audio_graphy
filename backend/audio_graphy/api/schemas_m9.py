"""Shared Pydantic schemas for all M9 R2 APIs (architecture §12.2).

These request/response models are deliberately decoupled from the ORM
layer (architecture §7.7) so the API surface is stable across storage
driver swaps. Each schema maps 1:1 to a JSON body in the OpenAPI docs.

Conventions:
    - All response models use ``from_attributes=True`` so they can be
      built directly from ORM rows when needed.
    - All datetimes are timezone-aware (UTC); FastAPI serialises to ISO 8601.
    - ``tenant_id`` is never part of the request body — it always comes
      from the JWT via ``get_tenant_id(request)`` (L10 RBAC enforcement).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ============================================================
# T4 — Bi-temporal edge API schemas
# ============================================================


class EdgeOut(BaseModel):
    """One bi-temporal edge in a time-travel / history response."""

    model_config = ConfigDict(from_attributes=True)

    source: str
    target: str
    relation: str
    weight: float
    confidence: str
    confidence_score: float | None = None
    source_ids: list[str] = Field(default_factory=list)
    valid_at: datetime | None = None
    invalid_at: datetime | None = None
    created_at: datetime | None = None
    expired_at: datetime | None = None
    superseded_by: str | None = None


class EdgeEventOut(BaseModel):
    """One row from the append-only ``edge_events`` audit log."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    event_type: str
    edge_key: str
    source: str
    target: str
    relation: str
    valid_at: datetime
    invalid_at: datetime | None = None
    superseded_by: str | None = None
    actor: str
    occurred_at: datetime | None = None


class TimeTravelResponse(BaseModel):
    """Response for GET /recordings/{id}/edges?at=ISO."""

    recording_id: int
    as_of: datetime
    edges: list[EdgeOut]
    total: int


class EdgeHistoryResponse(BaseModel):
    """Response for GET /recordings/{id}/edges/{edge_id}/history."""

    recording_id: int
    edge_key: str
    events: list[EdgeEventOut]
    total: int


class EdgeRangeQueryResponse(BaseModel):
    """Response for GET /recordings/{id}/edges/range?from=T1&to=T2."""

    recording_id: int
    from_time: datetime
    to_time: datetime
    edges: list[EdgeOut]
    total: int


# ============================================================
# T6 — Leiden admin API schemas
# ============================================================


class LeidenRecomputeRequest(BaseModel):
    """Body for POST /admin/leiden/recompute."""

    force_full: bool = Field(
        default=False,
        description="If True, bypass the incremental threshold check.",
    )
    triggered_by: str = Field(
        default="manual",
        description="Trigger source: manual / scheduled / threshold / backfill.",
    )


class LeidenJobOut(BaseModel):
    """One LeidenJob row in responses."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: str
    job_type: str
    status: str
    triggered_by: str
    node_count_snapshot: int
    edge_count_snapshot: int
    diff_percent: float | None = None
    modularity: float | None = None
    levels: int
    snapshot_path: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None


class LeidenJobListResponse(BaseModel):
    """Paginated response for GET /admin/leiden/jobs."""

    items: list[LeidenJobOut]
    total: int
    page: int
    page_size: int


class LeidenStatusResponse(BaseModel):
    """Response for GET /admin/leiden/status."""

    tenant_id: str
    last_job: LeidenJobOut | None = None
    snapshot_exists: bool
    snapshot_path: str | None = None
    enabled: bool


# ============================================================
# T8 — GlobalSearcher / search API schemas
# ============================================================


class GlobalSearchRequest(BaseModel):
    """Body for POST /search/global.

    Map-reduce over community summaries (L4 ruling: top-k=5, concurrency ≤5).
    """

    query: str = Field(..., min_length=1, max_length=2048)
    top_k: int = Field(default=5, ge=1, le=50)
    level: int = Field(default=0, ge=0, le=2)
    community_ids: list[int] | None = Field(
        default=None,
        description="Optional allow-list of community ids at ``level``.",
    )


class CommunityHit(BaseModel):
    """One community match inside a global-search result."""

    community_id: int
    level: int
    title: str
    summary: str
    score: float
    member_count: int


class GlobalSearchResponse(BaseModel):
    """Response for POST /search/global."""

    query: str
    level: int
    hits: list[CommunityHit]
    total: int
    took_ms: float


class LocalSearchRequest(BaseModel):
    """Body for POST /search/local.

    Local search walks edges of an entity-seed set (architecture §10.3).
    """

    query: str = Field(..., min_length=1, max_length=2048)
    seed_entity_ids: list[str] = Field(
        ..., min_length=1, max_length=64
    )
    depth: int = Field(default=1, ge=0, le=3)
    top_k: int = Field(default=5, ge=1, le=50)


class LocalSearchHit(BaseModel):
    """One entity match in local-search results."""

    entity_id: str
    name: str
    type: str
    description: str
    score: float


class LocalSearchResponse(BaseModel):
    """Response for POST /search/local."""

    query: str
    seed_entity_ids: list[str]
    depth: int
    hits: list[LocalSearchHit]
    total: int
    took_ms: float


class DrillDownRequest(BaseModel):
    """Body for POST /search/communities/{community_id}/drill-down."""

    level: int = Field(default=0, ge=0, le=2)


class DrillDownResponse(BaseModel):
    """Response for POST /search/communities/{community_id}/drill-down."""

    community_id: int
    parent_level: int
    child_level: int
    children: list[CommunityHit]
    total: int


# ============================================================
# T10 — Compression admin API schemas
# ============================================================


class CompressionDryRunRequest(BaseModel):
    """Body for POST /admin/compression/dry-run."""

    max_candidates: int = Field(default=50, ge=1, le=500)
    god_node_degree_threshold: int | None = Field(default=None, ge=1)
    stale_days: int | None = Field(default=None, ge=1)


class CompressionCandidateOut(BaseModel):
    """One candidate emitted by phase-1 selection."""

    entity_id: str
    score: float
    reason: str


class CompressionDryRunResponse(BaseModel):
    """Response for POST /admin/compression/dry-run."""

    tenant_id: str
    candidates: list[CompressionCandidateOut]
    total: int


class CompressionRunRequest(BaseModel):
    """Body for POST /admin/compression/run."""

    max_candidates: int = Field(default=50, ge=1, le=500)
    policy_check: bool = True


class CompressionRunResponse(BaseModel):
    """Response for POST /admin/compression/run."""

    tenant_id: str
    candidates: list[CompressionCandidateOut]
    soft_deleted_nodes: list[str]
    soft_deleted_edges: list[str]
    rolled_back: bool
    error: str | None = None


class CompressionHistoryItem(BaseModel):
    """One historical run summary (audit log projection)."""

    action: str
    occurred_at: datetime
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    user_id: int | None = None


class CompressionHistoryResponse(BaseModel):
    """Response for GET /admin/compression/history."""

    items: list[CompressionHistoryItem]
    total: int
    page: int
    page_size: int


# ============================================================
# T13 — Speaker merge-pending API schemas
# ============================================================


class SpeakerMergePendingListItem(BaseModel):
    """One row from the L8 fuzzy reconfirm work-queue."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    recording_id: int
    candidate_name: str
    matched_speaker_node_id: int
    fuzzy_score: float
    status: str
    voiceprint_score: float | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    notes: str | None = None
    created_at: datetime | None = None


class SpeakerMergePendingListResponse(BaseModel):
    """Paginated response for GET /speakers/merge-pending."""

    items: list[SpeakerMergePendingListItem]
    total: int
    page: int
    page_size: int


class SpeakerConfirmMergeRequest(BaseModel):
    """Body for POST /speakers/{speaker_id}/merge/{target_id}."""

    voiceprint_score: float | None = Field(
        default=None, ge=-1.0, le=1.0
    )
    notes: str | None = None


class SpeakerConfirmMergeResponse(BaseModel):
    """Response for confirm/reject merge endpoints."""

    pending_id: int
    status: str
    resolved_by: str
    voiceprint_score: float | None = None


class SpeakerRejectMergeRequest(BaseModel):
    """Body for POST /speakers/{speaker_id}/reject-merge."""

    notes: str | None = None


__all__ = [
    "CommunityHit",
    "CompressionCandidateOut",
    "CompressionDryRunRequest",
    "CompressionDryRunResponse",
    "CompressionHistoryItem",
    "CompressionHistoryResponse",
    "CompressionRunRequest",
    "CompressionRunResponse",
    "DrillDownRequest",
    "DrillDownResponse",
    "EdgeEventOut",
    "EdgeHistoryResponse",
    "EdgeOut",
    "EdgeRangeQueryResponse",
    "GlobalSearchRequest",
    "GlobalSearchResponse",
    "LeidenJobListResponse",
    "LeidenJobOut",
    "LeidenRecomputeRequest",
    "LeidenStatusResponse",
    "LocalSearchHit",
    "LocalSearchRequest",
    "LocalSearchResponse",
    "SpeakerConfirmMergeRequest",
    "SpeakerConfirmMergeResponse",
    "SpeakerMergePendingListItem",
    "SpeakerMergePendingListResponse",
    "SpeakerRejectMergeRequest",
    "TimeTravelResponse",
]
