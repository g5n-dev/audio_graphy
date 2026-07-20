"""Stats schemas.

See: docs/m3-prd.md §4.8.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class StatsQueryParams(BaseModel):
    """GET /tags/stats query parameters."""

    store_id: str | None = None
    agent_name: str | None = None
    tag_path: str | None = Field(default=None, description="Tag path or prefix (e.g. quality.*)")
    tag_value: str | None = None
    group_by: Literal["store_id", "agent_name", "tag_path", "tag_value"] = Field(default="tag_path")


class StatsItem(BaseModel):
    """A single stats aggregation row."""

    store_id: str | None = None
    agent_name: str | None = None
    tag_path: str | None = None
    tag_value: str | None = None
    tag_count: int


class StatsResponse(BaseModel):
    """GET /tags/stats response."""

    dimensions: list[str]
    items: list[StatsItem]
    total_records: int
