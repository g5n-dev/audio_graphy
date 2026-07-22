"""T6 — Leiden admin API (M9 architecture §25.6).

Endpoints (admin-only; inspector/viewer → 403; 404 when flag=False):
    POST /admin/leiden/recompute
        Schedule a fresh Leiden pass on the current graph snapshot.

    GET  /admin/leiden/jobs/{job_id}
        Fetch one LeidenJob row by id.

    GET  /admin/leiden/jobs
        Paginated list of recent LeidenJob rows.

    GET  /admin/leiden/status
        Latest job + snapshot existence flag.

L9 + L10 enforced (architecture §25.6).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from audio_graphy.api.deps import get_current_user, get_db
from audio_graphy.api.schemas_m9 import (
    LeidenJobListResponse,
    LeidenJobOut,
    LeidenRecomputeRequest,
    LeidenStatusResponse,
)
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.tenants import get_tenant_id
from audio_graphy.core.leiden import IncrementalLeidenService
from audio_graphy.core.types import (
    GraphEdge,
    GraphNode,
    LeidenThresholdExceededError,
)
from audio_graphy.errors import ForbiddenError, TaskNotFoundError
from audio_graphy.models.leiden_job import LeidenJob

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/leiden", tags=["M9 — Leiden admin"])


# ============================================================
# Helpers
# ============================================================


def _require_admin_role(user: AuthUser) -> None:
    """Defensive check — the route also has a dependency-based guard."""
    if user.role != "admin":
        raise ForbiddenError(
            message="Leiden admin endpoints require admin role",
            detail={"required_role": "admin", "actual_role": user.role},
        )


def _job_to_out(job: LeidenJob) -> LeidenJobOut:
    """Convert ORM row to response model."""
    return LeidenJobOut(
        id=int(job.id),
        tenant_id=str(job.tenant_id),
        job_type=str(job.job_type),
        status=str(job.status),
        triggered_by=str(job.triggered_by),
        node_count_snapshot=int(job.node_count_snapshot),
        edge_count_snapshot=int(job.edge_count_snapshot),
        diff_percent=(
            float(job.diff_percent) if job.diff_percent is not None else None
        ),
        modularity=(
            float(job.modularity) if job.modularity is not None else None
        ),
        levels=int(job.levels),
        snapshot_path=job.snapshot_path,
        error_message=job.error_message,
        started_at=job.started_at,
        finished_at=job.finished_at,
        created_at=None,
    )


def _tenant_graph(request: Request, tenant_id: str) -> Any:
    """Return the per-tenant NetworkX graph (or raise 404)."""
    from audio_graphy.api.bi_temporal import _tenant_graph_or_404

    return _tenant_graph_or_404(request, tenant_id)


def _graph_to_leiden_inputs(graph: Any) -> tuple[list[GraphNode], list[GraphEdge]]:
    """Materialise (nodes, edges) from the NetworkX graph in Leiden format."""
    from audio_graphy.api.bi_temporal import _edge_from_graph_attrs
    from audio_graphy.core.types import GraphNode, _str_to_list

    nodes: list[GraphNode] = []
    for nid, attrs in graph.nodes(data=True):
        nodes.append(
            GraphNode(
                entity_id=str(nid),
                name=str(attrs.get("name", nid)),
                type=str(attrs.get("type", "")),
                description=str(attrs.get("description", "")),
                source_ids=_str_to_list(attrs.get("source_ids", "[]")),
                recording_ids=[
                    int(x) for x in _str_to_list(attrs.get("recording_ids", "[]"))
                ],
                degree=int(attrs.get("degree", 0)),
                expired_at=None,
            )
        )
    edges: list[GraphEdge] = []
    for source, target, attrs in graph.edges(data=True):
        rel = attrs.get("relation") or attrs.get("key") or ""
        edges.append(_edge_from_graph_attrs(source, target, rel, attrs))
    return nodes, edges


def _settings(request: Request) -> Any:
    return request.app.state.settings


def _snapshot_dir(settings: Any, tenant_id: str) -> Path:
    """Per-tenant snapshot directory under working_dir."""
    base = Path(str(settings.working_dir)) / tenant_id / "leiden"
    base.mkdir(parents=True, exist_ok=True)
    return base


# ============================================================
# POST /admin/leiden/recompute
# ============================================================


@router.post(
    "/recompute",
    response_model=LeidenJobOut,
    status_code=status.HTTP_201_CREATED,
    summary="Trigger a Leiden recompute (admin only)",
)
async def recompute_leiden(
    body: LeidenRecomputeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
) -> LeidenJobOut:
    """Synchronously run one Leiden pass and persist a LeidenJob row.

    Returns the resulting job row. If the incremental diff exceeds the
    threshold, a full recompute is performed and ``status=succeeded``
    with ``job_type=full``.
    """
    _require_admin_role(user)
    tenant_id = get_tenant_id(request)
    settings = _settings(request)

    graph = _tenant_graph(request, tenant_id)
    nodes, edges = _graph_to_leiden_inputs(graph)

    job = LeidenJob(
        tenant_id=tenant_id,
        job_type="full",  # tentative; updated after the run
        status="running",
        triggered_by=body.triggered_by,
        node_count_snapshot=len(nodes),
        edge_count_snapshot=len(edges),
        levels=int(getattr(settings, "leiden_max_levels", 2)),
    )
    db.add(job)
    await db.flush()

    started = datetime.now(UTC)
    job.started_at = started
    service = IncrementalLeidenService(
        snapshot_dir=_snapshot_dir(settings, tenant_id),
        threshold_percent=float(
            getattr(settings, "leiden_threshold_percent", 30.0)
        ),
        preferred_lib=str(getattr(settings, "leiden_lib", "networkx")),
        tenant_id=tenant_id,
    )

    try:
        if body.force_full:
            # Force a fresh pass by removing the cached snapshot.
            import contextlib

            with contextlib.suppress(Exception):
                service.clear_snapshot()

        try:
            result = service.run(current_nodes=nodes, current_edges=edges)
        except LeidenThresholdExceededError:
            # Service has already expanded to full recompute; re-run cleanly.
            result = service.run(current_nodes=nodes, current_edges=edges)

        job.job_type = result.job_type
        job.status = "succeeded"
        job.diff_percent = (
            float(result.diff_percent)
            if body.triggered_by == "incremental"
            else None
        )
        job.modularity = (
            float(result.modularity)
            if result.modularity == result.modularity  # NaN guard
            else None
        )
        job.levels = int(result.levels)
        job.snapshot_path = str(result.snapshot_path)
        job.finished_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(job)
        return _job_to_out(job)
    except Exception as exc:
        job.status = "failed"
        job.error_message = str(exc)[:500]
        job.finished_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(job)
        logger.warning("Leiden recompute failed for tenant %s: %s", tenant_id, exc)
        return _job_to_out(job)


# ============================================================
# GET /admin/leiden/jobs/{job_id}
# ============================================================


@router.get(
    "/jobs/{job_id}",
    response_model=LeidenJobOut,
    summary="Get one Leiden job by id (admin only)",
)
async def get_leiden_job(
    job_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
) -> LeidenJobOut:
    _require_admin_role(user)
    tenant_id = get_tenant_id(request)
    job = await db.get(LeidenJob, job_id)
    if job is None or str(job.tenant_id) != tenant_id:
        raise TaskNotFoundError(detail={"job_id": job_id, "tenant_id": tenant_id})
    return _job_to_out(job)


# ============================================================
# GET /admin/leiden/jobs
# ============================================================


@router.get(
    "/jobs",
    response_model=LeidenJobListResponse,
    summary="List recent Leiden jobs (admin only)",
)
async def list_leiden_jobs(
    request: Request,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
) -> LeidenJobListResponse:
    _require_admin_role(user)
    tenant_id = get_tenant_id(request)

    stmt = select(LeidenJob).where(LeidenJob.tenant_id == tenant_id)
    count_stmt = (
        select(func.count())
        .select_from(LeidenJob)
        .where(LeidenJob.tenant_id == tenant_id)
    )
    if status_filter is not None:
        stmt = stmt.where(LeidenJob.status == status_filter)
        count_stmt = count_stmt.where(LeidenJob.status == status_filter)

    total = (await db.execute(count_stmt)).scalar_one()
    stmt = stmt.order_by(LeidenJob.id.desc()).limit(limit).offset(offset)
    rows = list((await db.execute(stmt)).scalars().all())

    return LeidenJobListResponse(
        items=[_job_to_out(r) for r in rows],
        total=int(total),
        page=offset // limit + 1 if limit else 1,
        page_size=limit,
    )


# ============================================================
# GET /admin/leiden/status
# ============================================================


@router.get(
    "/status",
    response_model=LeidenStatusResponse,
    summary="Tenant Leiden status snapshot (admin only)",
)
async def leiden_status(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
) -> LeidenStatusResponse:
    _require_admin_role(user)
    tenant_id = get_tenant_id(request)
    settings = _settings(request)

    stmt = (
        select(LeidenJob)
        .where(LeidenJob.tenant_id == tenant_id)
        .order_by(LeidenJob.id.desc())
        .limit(1)
    )
    latest = (await db.execute(stmt)).scalar_one_or_none()

    snap_path = _snapshot_dir(settings, tenant_id) / f"leiden_{tenant_id}.pkl"
    return LeidenStatusResponse(
        tenant_id=tenant_id,
        last_job=_job_to_out(latest) if latest is not None else None,
        snapshot_exists=bool(snap_path.exists()),
        snapshot_path=str(snap_path) if snap_path.exists() else None,
        enabled=bool(getattr(settings, "enable_advanced_graph", False)),
    )


__all__ = ["router"]
