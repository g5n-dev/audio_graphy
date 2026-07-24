"""Recording schemas: create, response, list, status.

See: docs/m3-prd.md §4.2.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RecordingCreate(BaseModel):
    """POST /recordings request body."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    store_id: str = Field(max_length=64, description="Store ID")
    path: str = Field(max_length=512, description="Audio file path (must exist)")
    agent_name: str | None = Field(default=None, max_length=255, description="Agent name")
    customer_hash: str | None = Field(
        default=None, max_length=64, description="Customer hash (SHA-256)"
    )
    recorded_at: datetime | None = Field(default=None, description="Recording timestamp (ISO 8601)")
    prompt_version: str | None = Field(
        default=None, max_length=64, description="Tag prompt version"
    )


class TagSummary(BaseModel):
    """Current tag summary in recording detail."""

    tag_path: str
    tag_value: str
    version: int
    prompt_version: str


class RecordingResponse(BaseModel):
    """Full recording response."""

    id: int
    tenant_id: str
    store_id: str
    agent_name: str | None = None
    agent_user_id: int | None = None
    customer_hash: str | None = None
    status: str
    pipeline_state: str
    recorded_at: datetime | None = None
    prompt_version: str | None = None
    indexed_at: datetime | None = None
    created_at: datetime | None = None
    segments_count: int = 0
    chunks_count: int = 0
    current_tags: list[TagSummary] = Field(default_factory=list)


class RecordingListItem(BaseModel):
    """Lightweight recording item for list view."""

    id: int
    store_id: str
    agent_name: str | None = None
    agent_user_id: int | None = None
    status: str
    pipeline_state: str
    recorded_at: datetime | None = None
    indexed_at: datetime | None = None
    prompt_version: str | None = None


class RecordingListResponse(BaseModel):
    """GET /recordings paginated response."""

    items: list[RecordingListItem]
    total: int
    page: int
    page_size: int


class RecordingStatusResponse(BaseModel):
    """GET /recordings/{id}/status lightweight response."""

    id: int
    agent_user_id: int | None = None
    status: str
    pipeline_state: str
    indexed_at: datetime | None = None


class ReindexRequest(BaseModel):
    """POST /recordings/{id}/reindex request body."""

    force: bool = Field(default=False, description="Force re-index ignoring content_hash")


class ReindexResponse(BaseModel):
    """POST /recordings/{id}/reindex response."""

    id: int
    status: str
    pipeline_state: str
    message: str = "Reindex triggered"
