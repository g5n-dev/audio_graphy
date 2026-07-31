"""Recordings router — POST/GET/GET{id}/GET{status}/POST reindex.

See: docs/m3-prd.md §4.2.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, cast

from fastapi import APIRouter, Depends, Header, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from audio_graphy.api.deps import get_current_user, get_db, get_session_factory
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.roles import require_admin
from audio_graphy.auth.tenants import get_tenant_id
from audio_graphy.schemas.recordings import (
    PipelineRunResponse,
    RecordingCreate,
    RecordingListItem,
    RecordingListResponse,
    RecordingResponse,
    RecordingStatusResponse,
    RecordingStatusValue,
    ReindexRequest,
    ReindexResponse,
    TagSummary,
)
from audio_graphy.services.ingestion import IngestionService
from audio_graphy.services.tag_governance import TagGovernanceService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recordings", tags=["recordings"])


def _service(request: Request) -> IngestionService:
    """Build the ingestion service with lifespan-managed privacy controls."""
    return IngestionService(
        get_session_factory(request),
        crypto=getattr(request.app.state, "audio_crypto", None),
        pii_scrubber=getattr(request.app.state, "pii_scrubber", None),
        audit=getattr(request.app.state, "audit_writer", None),
        allowed_root=request.app.state.settings.working_dir,
        max_audio_bytes=request.app.state.settings.max_recording_audio_bytes,
    )


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
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        max_length=256,
    ),
) -> RecordingResponse:
    """Register a new recording file for pipeline processing.

    Role: admin only.
    """
    tenant_id = get_tenant_id(request)
    svc = _service(request)
    recording = await svc.register_recording(
        tenant_id,
        body,
        idempotency_key=idempotency_key,
    )

    return RecordingResponse(
        id=recording.id,
        tenant_id=str(recording.tenant_id),
        store_id=str(recording.store_id),
        agent_name=recording.agent_name,
        agent_user_id=recording.agent_user_id,
        customer_hash=recording.customer_hash,
        status=cast(RecordingStatusValue, recording.status),
        pipeline_state=str(recording.pipeline_state),
        recorded_at=recording.recorded_at,
        prompt_version=recording.prompt_version,
        indexed_at=recording.indexed_at,
        audio_duration_ms=recording.audio_duration_ms,
        audio_sha256=recording.audio_sha256,
        audio_size_bytes=recording.audio_size_bytes,
        audio_sample_rate=recording.audio_sample_rate,
        audio_channels=recording.audio_channels,
        source_revision=recording.source_revision,
        active_pipeline_run_id=recording.active_pipeline_run_id,
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
    status: RecordingStatusValue | None = Query(default=None),
    agent_name: str | None = Query(default=None),
    recorded_from: datetime | None = Query(default=None),
    recorded_to: datetime | None = Query(default=None),
    sort: str = Query(default="-recorded_at"),
    user: AuthUser = Depends(get_current_user),
) -> RecordingListResponse:
    """List recordings with filters and pagination.

    Agent role: only sees recordings owned by the authenticated user ID.
    """
    tenant_id = get_tenant_id(request)
    agent_user_id = user.id if user.role == "agent" else None
    svc = _service(request)

    recordings, total = await svc.list_recordings(
        tenant_id,
        agent_user_id=agent_user_id,
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
            agent_user_id=r.agent_user_id,
            status=cast(RecordingStatusValue, r.status),
            pipeline_state=r.pipeline_state,
            recorded_at=r.recorded_at,
            indexed_at=r.indexed_at,
            prompt_version=r.prompt_version,
            active_pipeline_run_id=r.active_pipeline_run_id,
        )
        for r in recordings
    ]

    return RecordingListResponse(items=items, total=total, page=page, page_size=page_size)


@router.get("/{recording_id}", response_model=RecordingResponse, summary="Get recording detail")
async def get_recording(
    recording_id: int,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> RecordingResponse:
    """Get recording detail with segments_count, chunks_count, and current tags.

    Cross-tenant access returns 404 (not 403).
    """
    tenant_id = get_tenant_id(request)
    agent_user_id = user.id if user.role == "agent" else None
    svc = _service(request)
    semantic_access_allowed = await TagGovernanceService(
        get_session_factory(request)
    ).record_blind_sensitive_access(
        tenant_id=tenant_id,
        actor_user_id=user.id,
        access_kind="recording_detail_current_tags",
    )

    detail = await svc.get_recording_detail(
        recording_id,
        tenant_id,
        agent_user_id=agent_user_id,
    )
    recording = detail["recording"]

    current_tags = (
        [
            TagSummary(
                tag_path=str(t.tag_path),
                tag_value=str(t.tag_value),
                version=t.version,
                prompt_version=t.prompt_version,
            )
            for t in detail["current_tags"]
        ]
        if semantic_access_allowed
        else []
    )

    return RecordingResponse(
        id=recording.id,
        tenant_id=str(recording.tenant_id),
        store_id=str(recording.store_id),
        agent_name=recording.agent_name,
        agent_user_id=recording.agent_user_id,
        customer_hash=recording.customer_hash,
        status=cast(RecordingStatusValue, recording.status),
        pipeline_state=str(recording.pipeline_state),
        recorded_at=recording.recorded_at,
        prompt_version=recording.prompt_version,
        indexed_at=recording.indexed_at,
        audio_duration_ms=recording.audio_duration_ms,
        audio_sha256=recording.audio_sha256,
        audio_size_bytes=recording.audio_size_bytes,
        audio_sample_rate=recording.audio_sample_rate,
        audio_channels=recording.audio_channels,
        source_revision=recording.source_revision,
        active_pipeline_run_id=recording.active_pipeline_run_id,
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
    user: AuthUser = Depends(get_current_user),
) -> RecordingStatusResponse:
    """Get recording processing status (lightweight)."""
    tenant_id = get_tenant_id(request)
    agent_user_id = user.id if user.role == "agent" else None
    svc = _service(request)

    recording = await svc.get_recording(
        recording_id,
        tenant_id,
        agent_user_id=agent_user_id,
    )
    return RecordingStatusResponse(
        id=recording.id,
        agent_user_id=recording.agent_user_id,
        status=cast(RecordingStatusValue, recording.status),
        pipeline_state=recording.pipeline_state,
        indexed_at=recording.indexed_at,
        active_pipeline_run_id=recording.active_pipeline_run_id,
    )


@router.get(
    "/{recording_id}/processing-runs/{run_id}",
    response_model=PipelineRunResponse,
    summary="Get recording processing operation",
)
async def get_processing_run(
    recording_id: int,
    run_id: int,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> PipelineRunResponse:
    tenant_id = get_tenant_id(request)
    service = _service(request)
    await service.get_recording(
        recording_id,
        tenant_id,
        agent_user_id=user.id if user.role == "agent" else None,
    )
    run = await service.get_pipeline_run(recording_id, run_id, tenant_id)
    return PipelineRunResponse(
        id=run.id,
        recording_id=run.recording_id,
        generation=run.generation,
        state=run.state,
        attempt_count=run.attempt_count,
        required_projections=list(run.required_projections),
        completed_projections=list(run.completed_projections),
        error_code=run.error_code,
        error_message=run.error_message,
        lease_expires_at=run.lease_expires_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        activated_at=run.activated_at,
    )


@router.post(
    "/{recording_id}/reindex",
    response_model=ReindexResponse,
    status_code=202,
    summary="Trigger re-index",
    dependencies=[Depends(require_admin())],
)
async def reindex_recording(
    recording_id: int,
    request: Request,
    body: ReindexRequest | None = None,
    current_user: AuthUser = Depends(get_current_user),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        max_length=256,
    ),
) -> ReindexResponse:
    """Trigger re-indexing of a recording (reset to queued).

    Role: admin only. Writes audit_log(action="recording.reindex").
    """
    tenant_id = get_tenant_id(request)
    svc = _service(request)

    force = body.force if body is not None else False
    queued = await svc.queue_reindex(
        recording_id,
        tenant_id,
        force=force,
        idempotency_key=idempotency_key,
    )
    recording = queued.recording
    run = queued.run

    # ---- Audit log (fire-and-forget; Q2 quick win PIPL §14.3) ----
    await _write_audit(
        request,
        tenant_id=tenant_id,
        user_id=current_user.id,
        action="recording.reindex",
        target=f"recording:{recording.id}",
        after={
            "force": force,
            "status": recording.status,
            "pipeline_run_id": run.id,
            "generation": run.generation,
        },
    )

    return ReindexResponse(
        id=recording.id,
        status=cast(RecordingStatusValue, recording.status),
        pipeline_state=recording.pipeline_state,
        operation_id=run.id,
        generation=run.generation,
        operation_state=run.state,
        message="Reindex triggered",
    )
