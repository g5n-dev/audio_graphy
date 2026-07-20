"""Unit tests for common schemas: pagination, error envelope.

These are simple Pydantic model tests — no DB needed.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from audio_graphy.schemas.common import (
    ErrorDetail,
    ErrorResponse,
    PaginatedResponse,
    PaginationParams,
)


class TestPaginationParams:
    """Tests for PaginationParams schema."""

    def test_defaults(self) -> None:
        """Default pagination is page=1, page_size=20."""
        params = PaginationParams()
        assert params.page == 1
        assert params.page_size == 20

    def test_custom_values(self) -> None:
        """Custom pagination values are accepted."""
        params = PaginationParams(page=3, page_size=50)
        assert params.page == 3
        assert params.page_size == 50

    def test_page_minimum(self) -> None:
        """page must be >= 1."""
        with pytest.raises(ValidationError):
            PaginationParams(page=0)

    def test_page_size_minimum(self) -> None:
        """page_size must be >= 1."""
        with pytest.raises(ValidationError):
            PaginationParams(page_size=0)

    def test_page_size_maximum(self) -> None:
        """page_size must be <= 100."""
        with pytest.raises(ValidationError):
            PaginationParams(page_size=101)


class TestPaginatedResponse:
    """Tests for PaginatedResponse schema."""

    def test_with_items(self) -> None:
        """PaginatedResponse holds items + metadata."""
        resp = PaginatedResponse[int](
            items=[1, 2, 3],
            total=100,
            page=1,
            page_size=3,
        )
        assert resp.items == [1, 2, 3]
        assert resp.total == 100
        assert resp.page == 1
        assert resp.page_size == 3

    def test_empty_items(self) -> None:
        """PaginatedResponse with empty list."""
        resp = PaginatedResponse[str](
            items=[],
            total=0,
            page=1,
            page_size=20,
        )
        assert resp.items == []
        assert resp.total == 0


class TestErrorDetail:
    """Tests for ErrorDetail schema."""

    def test_with_detail(self) -> None:
        """ErrorDetail with code + message + detail."""
        detail = ErrorDetail(
            code="NOT_FOUND",
            message="Resource not found",
            detail={"id": 42},
        )
        assert detail.code == "NOT_FOUND"
        assert detail.message == "Resource not found"
        assert detail.detail == {"id": 42}

    def test_default_detail(self) -> None:
        """ErrorDetail defaults to empty detail dict."""
        detail = ErrorDetail(code="ERROR", message="Something went wrong")
        assert detail.detail == {}


class TestErrorResponse:
    """Tests for ErrorResponse schema."""

    def test_error_response(self) -> None:
        """ErrorResponse wraps an ErrorDetail."""
        resp = ErrorResponse(
            error=ErrorDetail(code="FORBIDDEN", message="Access denied"),
        )
        assert resp.error.code == "FORBIDDEN"
        assert resp.error.message == "Access denied"
