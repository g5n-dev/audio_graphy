"""DSAR (Data Subject Access Request) schemas — PIPL §14.3.

Pydantic models for the three admin-only endpoints in ``api/dsar.py``:
    - POST /dsar/export/{recording_id}
    - POST /dsar/erase/{recording_id}
    - GET  /dsar/audit

See: docs/m6-architecture.md §3.5.2.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class DSARExportRequest(BaseModel):
    """Body for POST /dsar/export/{recording_id}."""

    reason: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="业务理由，会写入 audit_log",
    )


class DSARExportResponse(BaseModel):
    """Response metadata for export (returned alongside the ZIP stream).

    When the endpoint streams a ZIP directly, this object is also embedded
    as ``manifest.json`` inside the archive for offline verification.
    """

    recording_id: int
    reason: str
    requested_by: int
    requested_at: datetime


class DSAREraseResponse(BaseModel):
    """Response for POST /dsar/erase/{recording_id}."""

    recording_id: int
    deleted: bool
    audit_action: str = Field(default="dsar.erase")


class AuditLogOut(BaseModel):
    """One audit log row in GET /dsar/audit response."""

    id: int
    tenant_id: str
    user_id: int | None
    action: str
    target: str
    before_value: dict[str, Any] | None
    after_value: dict[str, Any] | None
    occurred_at: datetime


class AuditLogList(BaseModel):
    """Paginated audit log response."""

    items: list[AuditLogOut]
    total: int
    page: int
    page_size: int


__all__ = [
    "AuditLogList",
    "AuditLogOut",
    "DSAREraseResponse",
    "DSARExportRequest",
    "DSARExportResponse",
]
