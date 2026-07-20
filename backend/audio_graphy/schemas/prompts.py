"""Prompt schemas.

See: docs/m3-prd.md §4.7.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class PromptCreate(BaseModel):
    """POST /prompts request body."""

    name: str = Field(max_length=255)
    version: str = Field(max_length=64)
    content: str = Field(min_length=1, description="Prompt template content")
    changelog: str | None = None
    activate: bool = Field(default=False, description="Activate immediately after creation")


class PromptListItem(BaseModel):
    """Lightweight prompt for list view."""

    id: int
    name: str
    version: str
    active: bool
    changelog: str | None = None
    created_by: int | None = None
    created_at: datetime | None = None


class PromptResponse(BaseModel):
    """Full prompt response (with content)."""

    id: int
    name: str
    version: str
    content: str
    changelog: str | None = None
    active: bool
    created_by: int | None = None
    created_at: datetime | None = None


class PromptListResponse(BaseModel):
    """GET /prompts response."""

    items: list[PromptListItem]


class ActivateRequest(BaseModel):
    """POST /prompts/{id}/activate request body."""

    trigger_recompute: bool = Field(default=True)
    dry_run: bool = Field(default=False)


class ActivateResponse(BaseModel):
    """POST /prompts/{id}/activate response."""

    prompt_id: int
    name: str
    version: str
    active: bool
    previous_active_id: int | None = None
    recompute_task_id: str | None = None
    affected_count: int = 0
    message: str = "Prompt activated"
