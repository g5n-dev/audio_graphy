"""Tags router — GET/POST tags + recompute.

See: docs/m3-prd.md §4.6.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from audio_graphy.api.deps import (
    get_adapters,
    get_current_user,
    get_db,
    get_file_index,
    get_session_factory,
)
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.roles import require_admin, require_write_access
from audio_graphy.auth.tenants import get_tenant_id
from audio_graphy.errors import ForbiddenError, RecordingNotFoundError, RecordingNotIndexedError
from audio_graphy.models.enums import RecordingStatus
from audio_graphy.models.recording import Recording
from audio_graphy.schemas.tags import (
    RecomputeRequest,
    TagsListResponse,
)
from audio_graphy.services.legacy_tag_compatibility import (
    LEGACY_RECORDING_DEFAULT_TAG_PATHS,
    LegacyTagCompatibilityService,
)
from audio_graphy.services.tag_governance import (
    GovernanceConflictError,
    GovernanceNotFoundError,
    TagGovernanceService,
)
from audio_graphy.tags.current_view import TagCurrentService
from audio_graphy.tags.facts import TagFactsService
from audio_graphy.tags.recompute import RecomputeService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tags"])


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


@router.get(
    "/recordings/{recording_id}/tags",
    response_model=TagsListResponse,
    summary="Get recording tags",
)
async def get_tags(
    recording_id: int,
    request: Request,
    view: str = "current",
    tag_path: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
) -> TagsListResponse:
    """Get tags for a recording (current / history / facts view).

    Cross-tenant returns 404.
    """
    tenant_id = get_tenant_id(request)

    # Verify recording exists
    rec_stmt = select(Recording).where(
        Recording.id == recording_id,
        Recording.tenant_id == tenant_id,
    )
    if user.role == "agent":
        rec_stmt = rec_stmt.where(Recording.agent_user_id == user.id)
    rec_result = await db.execute(rec_stmt)
    if rec_result.scalar_one_or_none() is None:
        raise RecordingNotFoundError(detail={"recording_id": recording_id})

    factory = get_session_factory(request)
    if not await TagGovernanceService(factory).record_blind_sensitive_access(
        tenant_id=tenant_id,
        actor_user_id=user.id,
        access_kind="legacy_recording_tags",
    ):
        raise ForbiddenError("Blind review isolation forbids tag output access before submission")

    if view == "current":
        svc = TagCurrentService(factory)
        tags = await svc.get_current_tags(recording_id, tenant_id)
        tag_data = [
            {
                "tag_path": t.tag_path,
                "tag_value": t.tag_value,
                "version": t.version,
                "prompt_version": t.prompt_version,
            }
            for t in tags
        ]
    elif view == "history":
        facts_svc = TagFactsService(factory)
        facts = await facts_svc.get_history(recording_id, tenant_id, tag_path_prefix=tag_path)
        tag_data = [
            {
                "tag_path": f.tag_path,
                "tag_value": f.tag_value,
                "version": f.version,
                "prompt_version": f.prompt_version,
                "source": f.source,
                "confidence": f.confidence,
                "computed_at": f.computed_at.isoformat() if f.computed_at else None,
                "computed_by": f.computed_by,
            }
            for f in facts
        ]
    else:  # facts
        facts_svc = TagFactsService(factory)
        facts = await facts_svc.get_facts(recording_id, tag_path, tenant_id)
        tag_data = [
            {
                "tag_path": f.tag_path,
                "tag_value": f.tag_value,
                "version": f.version,
                "prompt_version": f.prompt_version,
                "model_version": f.model_version,
                "source": f.source,
                "input_hash": f.input_hash,
                "confidence": f.confidence,
                "computed_at": f.computed_at.isoformat() if f.computed_at else None,
                "computed_by": f.computed_by,
            }
            for f in facts
        ]

    return TagsListResponse(recording_id=recording_id, view=view, tags=tag_data)


@router.post(
    "/recordings/{recording_id}/tags",
    response_model=Any,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Tag a recording (auto or manual)",
    dependencies=[Depends(require_write_access())],
)
async def post_tags(
    recording_id: int,
    request: Request,
    body: dict[str, Any],
    db: AsyncSession = Depends(get_db),
    current_user: AuthUser = Depends(get_current_user),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
    ),
) -> Any:
    """Map an unambiguous legacy auto request to one canonical tag job."""
    tenant_id = get_tenant_id(request)
    factory = get_session_factory(request)

    # Verify recording exists and is indexed
    rec_stmt = select(Recording).where(
        Recording.id == recording_id,
        Recording.tenant_id == tenant_id,
    )
    if current_user.role == "agent":
        rec_stmt = rec_stmt.where(Recording.agent_user_id == current_user.id)
    rec_result = await db.execute(rec_stmt)
    recording = rec_result.scalar_one_or_none()
    if recording is None:
        raise RecordingNotFoundError(detail={"recording_id": recording_id})
    if recording.status != RecordingStatus.INDEXED.value:
        raise RecordingNotIndexedError(
            detail={"recording_id": recording_id, "status": recording.status}
        )

    if body.get("mode", "auto") == "manual":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "legacy recording-level manual tags have no evidence-bound dialogue "
                "subject; use the reception review workbench"
            ),
        )
    raw_paths = body.get("tag_paths") or list(LEGACY_RECORDING_DEFAULT_TAG_PATHS)
    if not isinstance(raw_paths, list) or not all(
        isinstance(path, str) and path.strip() for path in raw_paths
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "legacy tag paths are not deterministically mapped; use the reception workbench"
            ),
        )
    try:
        job = await LegacyTagCompatibilityService(factory).enqueue_recordings(
            tenant_id=tenant_id,
            recording_ids=[recording.id],
            legacy_paths=raw_paths,
            actor_user_id=current_user.id,
            operation="legacy_recording_auto",
            idempotency_key=idempotency_key,
        )
    except GovernanceConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {
        "job_id": job.id,
        "status": job.status,
        "recording_id": recording.id,
        "successor": f"/api/v1/tag-jobs/{job.id}",
    }


@router.post(
    "/tags/recompute",
    response_model=Any,
    summary="Trigger batch recompute",
    dependencies=[Depends(require_admin())],
)
async def recompute(
    request: Request,
    body: RecomputeRequest,
    response: Response,
    current_user: AuthUser = Depends(get_current_user),
) -> Any:
    """Trigger batch tag recompute (prompt version switch).

    Role: admin only. Writes audit_log(action="tags.recompute").
    """
    tenant_id = get_tenant_id(request)
    factory = get_session_factory(request)

    if body.dry_run:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "legacy recording-level dry-run is read-only but not comparable to "
                "DialogueUnit extraction; use /tag-evaluations for a versioned preview"
            ),
        )

    recording_ids = body.recording_ids
    if recording_ids is None:
        async with factory() as session:
            recording_ids = list(
                (
                    await session.execute(
                        select(Recording.id).where(Recording.tenant_id == tenant_id)
                    )
                )
                .scalars()
                .all()
            )
    try:
        job = await LegacyTagCompatibilityService(factory).enqueue_recordings(
            tenant_id=tenant_id,
            recording_ids=recording_ids,
            legacy_paths=body.tag_paths or list(LEGACY_RECORDING_DEFAULT_TAG_PATHS),
            actor_user_id=current_user.id,
            operation="legacy_recompute",
        )
    except GovernanceConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    # ---- Audit log (fire-and-forget; Q2 quick win PIPL §14.3) ----
    await _write_audit(
        request,
        tenant_id=tenant_id,
        user_id=current_user.id,
        action="tags.recompute",
        target=f"tag_job:{job.id}",
        after={
            "prompt_version": body.prompt_version,
            "recording_ids": recording_ids,
            "tag_paths": body.tag_paths or [],
        },
    )
    response.status_code = status.HTTP_202_ACCEPTED
    return {
        "dry_run": False,
        "job_id": job.id,
        "status": job.status,
        "affected_count": job.total_items,
        "successor": f"/api/v1/tag-jobs/{job.id}",
    }


@router.get(
    "/tags/recompute/{task_id}",
    response_model=Any,
    summary="Get recompute task status",
)
async def get_recompute_task(
    task_id: str,
    request: Request,
    _user: AuthUser = Depends(require_admin()),
) -> Any:
    """Get recompute task status by ID.

    Role: admin only.
    """
    tenant_id = get_tenant_id(request)
    factory = get_session_factory(request)
    bundle = get_adapters(request)
    file_index = get_file_index(request)

    if task_id.isdigit():
        try:
            job = await TagGovernanceService(factory).get_job(
                tenant_id=tenant_id,
                job_id=int(task_id),
            )
        except GovernanceNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        return {
            "task_id": str(job.id),
            "job_id": job.id,
            "status": job.status,
            "prompt_version": None,
            "total": job.total_items,
            "processed": job.completed_items + job.failed_items,
            "changed": job.completed_items,
            "cached_hits": 0,
            "llm_calls": 0,
            "started_at": job.created_at,
            "finished_at": job.finished_at,
            "error_message": job.last_error_message,
            "successor": f"/api/v1/tag-jobs/{job.id}",
        }
    task = await RecomputeService(
        factory,
        bundle,
        file_index,
        enable_hybrid_rule_short_circuit=bool(
            getattr(request.app.state.settings, "enable_hybrid_rule_short_circuit", True)
        ),
    ).get_task_status(
        task_id,
        tenant_id,
    )
    return {
        "task_id": task.task_id,
        "status": task.status,
        "prompt_version": task.prompt_version,
        "total": task.total,
        "processed": task.processed,
        "changed": task.changed,
        "cached_hits": task.cached_hits,
        "llm_calls": task.llm_calls,
        "started_at": task.started_at,
        "finished_at": task.finished_at,
        "error_message": task.error_message,
        "successor": "/api/v1/tag-jobs",
    }
