"""Recordings router — POST/GET/GET{id}/GET{status}/POST reindex.

See: docs/m3-prd.md §4.2.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from audio_graphy.api.deps import get_current_user, get_db, get_session_factory
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.roles import require_admin
from audio_graphy.auth.tenants import get_agent_filter, get_tenant_id
from audio_graphy.schemas.recordings import (
    RecordingCreate,
    RecordingListItem,
    RecordingListResponse,
    RecordingResponse,
    RecordingStatusResponse,
    ReindexRequest,
    ReindexResponse,
    TagSummary,
)
from audio_graphy.services.ingestion import IngestionService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recordings", tags=["recordings"])


async def _write_audit(
    request: Request,
    *,
    tenant_id: str,
    user_id: int,
    action: str,
    target: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    """Fire-and-forget audit record via AuditWriter (if configured)."""
    writer = getattr(request.app.state, "audit_writer", None)
    if writer is None:
        return
    try:
        await writer.record(
            tenant_id=tenant_id,
            user_id=user_id,
            action=action,
            target=target,
            before=before,
            after=after,
        )
    except Exception as exc:
        logger.warning("Audit write failed (action=%s): %s", action, exc)


@router.post(
    "", response_model=RecordingResponse, status_code=201, summary="Register a new recording"
)
async def create_recording(
    body: RecordingCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _user: AuthUser = Depends(require_admin()),
) -> RecordingResponse:
    """Register a new recording file for pipeline processing.

    Role: admin only.
    """
    tenant_id = get_tenant_id(request)
    factory = get_session_factory(request)
    svc = IngestionService(factory)
    recording = await svc.register_recording(tenant_id, body)

    return RecordingResponse(
        id=recording.id,
        tenant_id=str(recording.tenant_id),
        store_id=str(recording.store_id),
        agent_name=str(recording.agent_name),
        customer_hash=recording.customer_hash,
        path=str(recording.path),
        status=str(recording.status),
        pipeline_state=str(recording.pipeline_state),
        recorded_at=recording.recorded_at,
        prompt_version=recording.prompt_version,
        indexed_at=recording.indexed_at,
        created_at=recording.created_at,
        segments_count=0,
        chunks_count=0,
        current_tags=[],
    )


@router.get("", response_model=RecordingListResponse, summary="List recordings")
async def list_recordings(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    store_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    agent_name: str | None = Query(default=None),
    recorded_from: datetime | None = Query(default=None),
    recorded_to: datetime | None = Query(default=None),
    sort: str = Query(default="-recorded_at"),
    _user: AuthUser = Depends(get_current_user),
) -> RecordingListResponse:
    """List recordings with filters and pagination.

    Agent role: only sees own recordings (agent_name = self).
    """
    tenant_id = get_tenant_id(request)
    agent_filter = get_agent_filter(request)
    factory = get_session_factory(request)
    svc = IngestionService(factory)

    recordings, total = await svc.list_recordings(
        tenant_id,
        agent_filter=agent_filter,
        store_id=store_id,
        status=status,
        agent_name=agent_name,
        recorded_from=recorded_from,
        recorded_to=recorded_to,
        sort=sort,
        page=page,
        page_size=page_size,
    )

    items = [
        RecordingListItem(
            id=r.id,
            store_id=r.store_id,
            agent_name=r.agent_name,
            status=r.status,
            pipeline_state=r.pipeline_state,
            recorded_at=r.recorded_at,
            indexed_at=r.indexed_at,
            prompt_version=r.prompt_version,
        )
        for r in recordings
    ]

    return RecordingListResponse(items=items, total=total, page=page, page_size=page_size)


@router.get("/{recording_id}", response_model=RecordingResponse, summary="Get recording detail")
async def get_recording(
    recording_id: int,
    request: Request,
    _user: AuthUser = Depends(get_current_user),
) -> RecordingResponse:
    """Get recording detail with segments_count, chunks_count, and current tags.

    Cross-tenant access returns 404 (not 403).
    """
    tenant_id = get_tenant_id(request)
    agent_filter = get_agent_filter(request)
    factory = get_session_factory(request)
    svc = IngestionService(factory)

    detail = await svc.get_recording_detail(recording_id, tenant_id, agent_filter=agent_filter)
    recording = detail["recording"]

    current_tags = [
        TagSummary(
            tag_path=str(t.tag_path),
            tag_value=str(t.tag_value),
            version=t.version,
            prompt_version=t.prompt_version,
        )
        for t in detail["current_tags"]
    ]

    return RecordingResponse(
        id=recording.id,
        tenant_id=str(recording.tenant_id),
        store_id=str(recording.store_id),
        agent_name=str(recording.agent_name),
        customer_hash=recording.customer_hash,
        path=str(recording.path),
        status=str(recording.status),
        pipeline_state=str(recording.pipeline_state),
        recorded_at=recording.recorded_at,
        prompt_version=recording.prompt_version,
        indexed_at=recording.indexed_at,
        created_at=recording.created_at,
        segments_count=detail["segments_count"],
        chunks_count=detail["chunks_count"],
        current_tags=current_tags,
    )


@router.get(
    "/{recording_id}/status", response_model=RecordingStatusResponse, summary="Get recording status"
)
async def get_recording_status(
    recording_id: int,
    request: Request,
    _user: AuthUser = Depends(get_current_user),
) -> RecordingStatusResponse:
    """Get recording processing status (lightweight)."""
    tenant_id = get_tenant_id(request)
    agent_filter = get_agent_filter(request)
    factory = get_session_factory(request)
    svc = IngestionService(factory)

    recording = await svc.get_recording(recording_id, tenant_id, agent_filter=agent_filter)
    return RecordingStatusResponse(
        id=recording.id,
        status=recording.status,
        pipeline_state=recording.pipeline_state,
        indexed_at=recording.indexed_at,
    )


@router.post("/{recording_id}/reindex", response_model=ReindexResponse, summary="Trigger re-index",
    dependencies=[Depends(require_admin())],
)
async def reindex_recording(
    recording_id: int,
    request: Request,
    body: ReindexRequest | None = None,
    current_user: AuthUser = Depends(get_current_user),
) -> ReindexResponse:
    """Trigger re-indexing of a recording (reset to queued).

    Role: admin only. Writes audit_log(action="recording.reindex").
    """
    tenant_id = get_tenant_id(request)
    factory = get_session_factory(request)
    svc = IngestionService(factory)

    force = body.force if body is not None else False
    recording = await svc.trigger_reindex(recording_id, tenant_id, force=force)

    # ---- Audit log (fire-and-forget; Q2 quick win PIPL §14.3) ----
    await _write_audit(
        request,
        tenant_id=tenant_id,
        user_id=current_user.id,
        action="recording.reindex",
        target=f"recording:{recording.id}",
        after={"force": force, "status": recording.status},
    )

    return ReindexResponse(
        id=recording.id,
        status=recording.status,
        pipeline_state=recording.pipeline_state,
        message="Reindex triggered",
    )
