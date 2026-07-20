"""Common Pydantic schemas: pagination, error envelope, shared types.

See: docs/m3-architecture.md §7.7, docs/m3-prd.md §4.
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PaginationParams(BaseModel):
    """Standard pagination query parameters."""

    page: int = Field(default=1, ge=1, description="Page number (1-based)")
    page_size: int = Field(default=20, ge=1, le=100, description="Items per page (max 100)")


class PaginatedResponse(BaseModel, Generic[T]):
    """Generic paginated response wrapper."""

    items: list[T]
    total: int = Field(description="Total matching records")
    page: int = Field(description="Current page number")
    page_size: int = Field(description="Items per page")


class ErrorDetail(BaseModel):
    """Error detail envelope."""

    code: str = Field(description="Machine-readable error code")
    message: str = Field(description="Human-readable error message")
    detail: dict[str, object] = Field(default_factory=dict, description="Structured detail")


class ErrorResponse(BaseModel):
    """Standard error response wrapper."""

    error: ErrorDetail
