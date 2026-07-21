"""Eval REST API — async eval run lifecycle (M6 WS-2).

Four endpoints (Inspector+ role):

    POST /api/v1/eval/runs
        Body: ``EvalRunCreate``. Creates an ``EvalRunORM`` row with
        ``status='pending'`` and submits an APScheduler job that runs
        ``EvalRunner`` in the background. Returns 202 + run_id.

    GET /api/v1/eval/runs/{run_id}
        Returns the current ``EvalRunOut`` (status + aggregate_metrics).

    GET /api/v1/eval/runs/{run_id}/report?format=markdown|json
        Streams the generated Markdown or JSON report file.
        Returns 404 if the run is not yet completed or the file is missing.

    GET /api/v1/eval/runs?status=&limit=20&offset=0
        Paginated list filtered by status (tenant-scoped).

All reads/writes are tenant-scoped. Audit records are written for
``eval.run.created``, ``eval.run.completed``, ``eval.run.failed``.

See: docs/m6-architecture.md §4.3, docs/m6-prd.md §5.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse

from audio_graphy.api.deps import get_current_user, get_session_factory
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.errors import ForbiddenError, NotFoundError
from audio_graphy.eval.state import EvalRunState
from audio_graphy.models.eval_run import EvalRunORM
from audio_graphy.schemas.eval import (
    EvalRunCreate,
    EvalRunCreateResponse,
    EvalRunListResponse,
    EvalRunOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/eval", tags=["Eval (M6)"])

_ALLOWED_ROLES = frozenset({"admin", "inspector"})
_REPORT_FORMATS = frozenset({"markdown", "json"})


def _require_inspector(user: AuthUser) -> None:
    """Reject any role below inspector (403). Admin passes implicitly."""
    if user.role not in _ALLOWED_ROLES:
        raise ForbiddenError(
            message="Eval endpoints require inspector role or higher",
            detail={"required_roles": sorted(_ALLOWED_ROLES), "actual_role": user.role},
        )


def _state(request: Request) -> EvalRunState:
    """Build an EvalRunState bound to the request's session factory."""
    factory = get_session_factory(request)
    return EvalRunState(factory)


def _serialize_run(run: EvalRunORM) -> EvalRunOut:
    """Convert ORM → pydantic response."""
    return EvalRunOut(
        id=str(run.id),
        tenant_id=str(run.tenant_id),
        gold_set_path=str(run.gold_set_path),
        pipeline=str(run.pipeline),
        judge_enabled=bool(run.judge_enabled),
        k_value=int(run.k_value),
        status=str(run.status),
        config=dict(run.config or {}),
        aggregate_metrics=dict(run.aggregate_metrics) if run.aggregate_metrics else None,
        report_markdown_path=run.report_markdown_path,
        report_json_path=run.report_json_path,
        error_message=run.error_message,
        started_at=run.started_at,
        finished_at=run.finished_at,
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


# ============================================================
# POST /eval/runs
# ============================================================


@router.post(
    "/runs",
    response_model=EvalRunCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Create + schedule a new async evaluation run (inspector+)",
)
async def create_run(
    body: EvalRunCreate,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> EvalRunCreateResponse:
    """Create a new EvalRun and schedule the background worker.

    Returns 202 with ``run_id`` and ``poll_interval_seconds=5``. The
    caller polls ``GET /eval/runs/{run_id}`` until ``status`` reaches
    ``completed`` or ``failed``.
    """
    _require_inspector(user)
    state = _state(request)

    config_snapshot: dict[str, Any] = {
        "position_debias": bool(body.position_debias),
        "metadata": dict(body.metadata),
    }
    run_id = await state.create(
        gold_set_path=body.gold_set_path,
        pipeline=body.pipeline,
        judge_enabled=body.judge_enabled,
        k=body.k,
        tenant_id=user.tenant_id,
        user_id=user.id,
        config=config_snapshot,
    )

    # Schedule the background job (best-effort; the polling worker will
    # also pick up any pending runs missed here).
    _schedule_eval_job(request, run_id=run_id, tenant_id=user.tenant_id)

    await _write_audit(
        request,
        tenant_id=user.tenant_id,
        user_id=user.id,
        action="eval.run.created",
        target=f"eval_run:{run_id}",
        after={
            "pipeline": body.pipeline,
            "gold_set_path": body.gold_set_path,
            "judge_enabled": body.judge_enabled,
            "k": body.k,
        },
    )

    return EvalRunCreateResponse(run_id=run_id, status="pending")


# ============================================================
# GET /eval/runs/{run_id}
# ============================================================


@router.get(
    "/runs/{run_id}",
    response_model=EvalRunOut,
    summary="Get one eval run's status + aggregate metrics (inspector+)",
)
async def get_run(
    run_id: str,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> EvalRunOut:
    """Return the current state of one eval run.

    Returns 404 if the run does not exist or belongs to another tenant.
    """
    _require_inspector(user)
    state = _state(request)
    run = await state.get(run_id, user.tenant_id)
    if run is None:
        raise NotFoundError(
            message=f"Eval run not found: {run_id}",
            detail={"run_id": run_id},
        )
    return _serialize_run(run)


# ============================================================
# GET /eval/runs/{run_id}/report
# ============================================================


@router.get(
    "/runs/{run_id}/report",
    summary="Download a generated report (inspector+)",
    response_class=FileResponse,
)
async def get_run_report(
    run_id: str,
    request: Request,
    format: str = Query("markdown", pattern="^(markdown|json)$"),
    user: AuthUser = Depends(get_current_user),
) -> FileResponse:
    """Stream the Markdown or JSON report file produced by the run.

    Returns 404 if the run is not completed, the report file path is
    missing, or the file does not exist on disk.
    """
    _require_inspector(user)
    if format not in _REPORT_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"format must be one of {sorted(_REPORT_FORMATS)}, got {format!r}",
        )
    state = _state(request)
    run = await state.get(run_id, user.tenant_id)
    if run is None:
        raise NotFoundError(
            message=f"Eval run not found: {run_id}",
            detail={"run_id": run_id},
        )
    if run.status != "completed":
        raise NotFoundError(
            message=f"Report not ready (run status={run.status})",
            detail={"run_id": run_id, "status": run.status},
        )
    path_str = run.report_markdown_path if format == "markdown" else run.report_json_path
    if not path_str:
        raise NotFoundError(
            message=f"Report path missing for format={format}",
            detail={"run_id": run_id, "format": format},
        )
    path = Path(path_str)
    if not await asyncio.to_thread(path.is_file):
        raise NotFoundError(
            message=f"Report file not on disk: {path}",
            detail={"run_id": run_id, "format": format, "path": str(path)},
        )

    media_type = "text/markdown" if format == "markdown" else "application/json"
    download_name = f"eval_run_{run_id}.{ 'md' if format == 'markdown' else 'json'}"
    return FileResponse(
        path=str(path),
        media_type=media_type,
        filename=download_name,
    )


# ============================================================
# GET /eval/runs
# ============================================================


@router.get(
    "/runs",
    response_model=EvalRunListResponse,
    summary="List eval runs (inspector+, tenant-scoped, paginated)",
)
async def list_runs(
    request: Request,
    run_status: str | None = Query(None, alias="status"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: AuthUser = Depends(get_current_user),
) -> EvalRunListResponse:
    """Paginated list of eval runs filtered by optional status."""
    _require_inspector(user)
    state = _state(request)
    rows, total = await state.list(
        tenant_id=user.tenant_id,
        status=run_status,
        limit=limit,
        offset=offset,
    )
    return EvalRunListResponse(
        items=[_serialize_run(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


# ============================================================
# Internal: APScheduler integration
# ============================================================


def _schedule_eval_job(request: Request, *, run_id: str, tenant_id: str) -> None:
    """Submit ``run_eval_job`` to the in-process scheduler if configured.

    The scheduler is optional in tests; if it is missing we still return
    202 — the caller can poll, and a separate worker can be started to
    drain the pending queue.
    """
    scheduler = getattr(request.app.state, "eval_scheduler", None)
    if scheduler is None:
        logger.info(
            "No eval_scheduler configured — run %s will be picked up by polling worker",
            run_id,
        )
        return
    try:
        scheduler.add_job(
            "audio_graphy.scheduler:run_eval_job",
            kwargs={"run_id": run_id, "tenant_id": tenant_id},
            id=f"eval_run_{run_id}",
            replace_existing=True,
        )
    except Exception as exc:
        logger.warning("Failed to schedule eval job for run %s: %s", run_id, exc)


__all__ = ["router"]
