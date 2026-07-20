"""Tags schemas: tag views, auto/manual tag requests, recompute.

See: docs/m3-prd.md §4.6.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class TagCurrentView(BaseModel):
    """A current tag entry (view=current)."""

    tag_path: str
    tag_value: str
    version: int
    prompt_version: str


class TagHistoryItem(BaseModel):
    """A history tag entry (view=history)."""

    tag_path: str
    tag_value: str
    version: int
    prompt_version: str
    source: str = "llm"
    confidence: float | None = None
    computed_at: datetime | None = None
    computed_by: int | None = None


class TagsListResponse(BaseModel):
    """GET /recordings/{id}/tags response."""

    recording_id: int
    view: str = "current"
    tags: list[dict[str, Any]] = Field(default_factory=list)


class TagAutoRequest(BaseModel):
    """POST /recordings/{id}/tags mode=auto request."""

    mode: Literal["auto"] = "auto"
    tag_paths: list[str] | None = Field(
        default=None, description="Tag paths to compute (empty=all)"
    )
    prompt_version: str | None = Field(default=None, description="Prompt version (empty=active)")


class TagManualRequest(BaseModel):
    """POST /recordings/{id}/tags mode=manual request."""

    mode: Literal["manual"] = "manual"
    tag_path: str = Field(max_length=255)
    tag_value: str = Field(max_length=255)
    reason: str | None = Field(default=None, description="Manual correction reason (audit)")


class TagResultItem(BaseModel):
    """A single auto-tag result."""

    tag_path: str
    tag_value: str
    version: int
    confidence: float | None = None
    cached: bool = False


class TagAutoResponse(BaseModel):
    """POST /recordings/{id}/tags mode=auto response."""

    recording_id: int
    tagged: int
    cached_hits: int = 0
    llm_calls: int = 0
    results: list[TagResultItem]


class TagManualResponse(BaseModel):
    """POST /recordings/{id}/tags mode=manual response."""

    recording_id: int
    tag_path: str
    tag_value: str
    version: int
    source: str = "manual"
    computed_by: int | None = None


class RecomputeRequest(BaseModel):
    """POST /tags/recompute request."""

    prompt_version: str = Field(max_length=64, description="Target prompt version")
    tag_paths: list[str] | None = Field(default=None)
    dry_run: bool = Field(default=False, description="Only diff, don't write")
    recording_ids: list[int] | None = Field(default=None, description="Limit scope")


class TagDeltaPreview(BaseModel):
    """A single tag delta preview (dry_run)."""

    recording_id: int
    tag_path: str
    old_value: str | None = None
    new_value: str | None = None


class RecomputeDryRunResponse(BaseModel):
    """POST /tags/recompute dry_run=true response."""

    dry_run: bool = True
    affected_count: int
    changed_count: int
    unchanged_count: int
    changes_preview: list[TagDeltaPreview]


class RecomputeCreateResponse(BaseModel):
    """POST /tags/recompute dry_run=false response."""

    dry_run: bool = False
    task_id: str
    status: str = "pending"
    affected_count: int = 0
    message: str = "Recompute task created. Poll /tags/recompute/{task_id} for status."


class RecomputeTaskResponse(BaseModel):
    """GET /tags/recompute/{task_id} response."""

    task_id: str
    status: str
    prompt_version: str
    total: int = 0
    processed: int = 0
    changed: int = 0
    cached_hits: int = 0
    llm_calls: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None
