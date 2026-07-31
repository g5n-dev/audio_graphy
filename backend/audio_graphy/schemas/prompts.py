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
    candidate_tagger_version_id: int | None = Field(
        default=None,
        ge=1,
        description="Qualified production candidate returned by canonical dry-run/evaluation",
    )
    sample_limit: int = Field(default=100, ge=1, le=100)
    max_provider_tokens: int = Field(default=5_000_000, ge=0, le=10_000_000)
    max_provider_calls: int = Field(default=400, ge=0, le=1_000)


class ActivateResponse(BaseModel):
    """POST /prompts/{id}/activate response."""

    prompt_id: int
    name: str
    version: str
    active: bool
    previous_active_id: int | None = None
    recompute_task_id: str | None = None
    successor: str | None = None
    affected_count: int = 0
    sampled_count: int = 0
    estimated_tokens: int = 0
    estimated_provider_calls: int = 0
    provider_calls: int = 0
    provider_tokens: int = 0
    changed_count: int = 0
    candidate_tagger_version_id: int | None = None
    quality_gate_status: str | None = None
    message: str = "Prompt activated"
