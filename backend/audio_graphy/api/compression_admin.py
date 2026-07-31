"""T10 — Compression admin API (M9 architecture §25.10).

Endpoints (admin-only; 404 when flag=False):
    POST /admin/compression/dry-run   — phase-1 candidate selection only.
    POST /admin/compression/run       — 3-phase pipeline (Q3 SOFT-only).
    GET  /admin/compression/history   — recent ``compression.*`` audit rows.

The weekly Sunday 03:00 cron wiring is added to ``retention.py`` and
registered from ``main.py`` (this router is the manual + audit surface).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from audio_graphy.api.deps import get_current_user, get_db, get_session_factory
from audio_graphy.api.schemas_m9 import (
    CompressionCandidateOut,
    CompressionDryRunRequest,
    CompressionDryRunResponse,
    CompressionHistoryItem,
    CompressionHistoryResponse,
    CompressionRunRequest,
    CompressionRunResponse,
)
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.tenants import get_tenant_id
from audio_graphy.core.bi_temporal import BiTemporalEdgeService
from audio_graphy.core.compression import CompressionService
from audio_graphy.errors import ForbiddenError
from audio_graphy.models.audit_log import AuditLog
from audio_graphy.storage.graph_bitemporal import GraphCompressionSink, all_graph_nodes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/compression", tags=["M9 — Compression admin"])

# Kept for importers that still reference the pre-move private names.
_GraphCompressionSink = GraphCompressionSink
_all_graph_nodes = all_graph_nodes


# ============================================================
# Helpers
# ============================================================


def _require_admin_role(user: AuthUser) -> None:
    if user.role != "admin":
        raise ForbiddenError(
            message="Compression endpoints require admin role",
            detail={"required_role": "admin", "actual_role": user.role},
        )


def _settings(request: Request) -> Any:
    return request.app.state.settings


def _get_graph_store(request: Request, tenant_id: str) -> Any:
    """Fetch or lazily create the per-tenant graph store."""

    # _tenant_graph_or_404 returns the .graph attribute; we need the store
    # object itself for the sink wrapper, so we replicate the lookup.
    graph_stores: dict[str, Any] = request.app.state.graph_stores
    store = graph_stores.get(tenant_id)
    if store is None:
        from audio_graphy.storage.graph_networkx import NetworkXGraphStore

        settings = request.app.state.settings
        store = NetworkXGraphStore(settings.working_dir, tenant_id=tenant_id)
        graph_stores[tenant_id] = store
    return store


async def _write_audit_safe(
    request: Request,
    *,
    tenant_id: str,
    user_id: int,
    action: str,
    target: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    """Best-effort audit row insert (swallows failures)."""
    writer = getattr(request.app.state, "audit_writer", None)
    if writer is not None:
        try:
            await writer.record(
                tenant_id=tenant_id,
                user_id=user_id,
                action=action,
                target=target,
                before=before,
                after=after,
            )
            await writer.flush()
        except Exception as exc:  # pragma: no cover — best-effort
            logger.warning("AuditWriter.record failed: %s", exc)
        return

    # Direct insert fallback (test environments without AuditWriter).
    factory = get_session_factory(request)
    from datetime import UTC, datetime

    from audio_graphy.models.audit_log import AuditLog as _AuditLog

    try:
        async with factory() as session:
            session.add(
                _AuditLog(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    action=action,
                    target=target,
                    before_value=before,
                    after_value=after,
                    occurred_at=datetime.now(UTC),
                )
            )
            await session.commit()
    except Exception as exc:  # pragma: no cover — best-effort
        logger.warning("audit direct insert failed: %s", exc)


# ============================================================
# POST /admin/compression/dry-run
# ============================================================


@router.post(
    "/dry-run",
    response_model=CompressionDryRunResponse,
    summary="Phase-1 candidate selection only (admin)",
)
async def compression_dry_run(
    body: CompressionDryRunRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> CompressionDryRunResponse:
    """Return the candidate list that ``run`` would act on. No mutations."""
    _require_admin_role(user)
    tenant_id = get_tenant_id(request)
    settings = _settings(request)

    store = _get_graph_store(request, tenant_id)
    nodes = all_graph_nodes(store)
    bt = BiTemporalEdgeService(tenant_id=tenant_id)
    service = CompressionService(
        sink=GraphCompressionSink(store, tenant_id),
        bt_service=bt,
        god_node_degree_threshold=(
            body.god_node_degree_threshold
            or int(getattr(settings, "compression_god_node_degree", 50))
        ),
        stale_days=body.stale_days or int(getattr(settings, "compression_stale_days", 180)),
        tenant_id=tenant_id,
    )
    candidates = service.select_candidates(nodes)[: body.max_candidates]
    return CompressionDryRunResponse(
        tenant_id=tenant_id,
        candidates=[
            CompressionCandidateOut(entity_id=c.entity_id, score=c.score, reason=c.reason)
            for c in candidates
        ],
        total=len(candidates),
    )


# ============================================================
# POST /admin/compression/run
# ============================================================


@router.post(
    "/run",
    response_model=CompressionRunResponse,
    status_code=status.HTTP_200_OK,
    summary="Run the 3-phase compression pipeline (admin)",
)
async def compression_run(
    body: CompressionRunRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> CompressionRunResponse:
    """Run the 3-phase pipeline (architecture §9, Q3 SOFT-only)."""
    _require_admin_role(user)
    tenant_id = get_tenant_id(request)
    settings = _settings(request)

    store = _get_graph_store(request, tenant_id)
    nodes = all_graph_nodes(store)
    sink = GraphCompressionSink(store, tenant_id)
    bt = BiTemporalEdgeService(tenant_id=tenant_id)
    service = CompressionService(
        sink=sink,
        bt_service=bt,
        god_node_degree_threshold=int(getattr(settings, "compression_god_node_degree", 50)),
        stale_days=int(getattr(settings, "compression_stale_days", 180)),
        tenant_id=tenant_id,
    )
    candidates = service.select_candidates(nodes)[: body.max_candidates]
    report = service.apply(candidates, policy_check=body.policy_check)

    # Audit
    await _write_audit_safe(
        request,
        tenant_id=tenant_id,
        user_id=user.id,
        action="compression.run",
        target=f"tenant:{tenant_id}",
        before={"candidate_count": len(candidates)},
        after={
            "soft_deleted_nodes": report.soft_deleted_nodes,
            "soft_deleted_edges": report.soft_deleted_edges,
            "rolled_back": report.rolled_back,
            "error": str(report.error) if report.error else None,
        },
    )

    return CompressionRunResponse(
        tenant_id=tenant_id,
        candidates=[
            CompressionCandidateOut(entity_id=c.entity_id, score=c.score, reason=c.reason)
            for c in report.candidates
        ],
        soft_deleted_nodes=report.soft_deleted_nodes,
        soft_deleted_edges=report.soft_deleted_edges,
        rolled_back=report.rolled_back,
        error=str(report.error) if report.error else None,
    )


# ============================================================
# GET /admin/compression/history
# ============================================================


@router.get(
    "/history",
    response_model=CompressionHistoryResponse,
    summary="Recent compression.* audit log entries (admin)",
)
async def compression_history(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
) -> CompressionHistoryResponse:
    _require_admin_role(user)
    tenant_id = get_tenant_id(request)

    stmt = (
        select(AuditLog)
        .where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.action.like("compression%"),
        )
        .order_by(AuditLog.occurred_at.desc())
        .limit(limit)
        .offset(offset)
    )
    count_stmt = (
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.action.like("compression%"),
        )
    )
    total = (await db.execute(count_stmt)).scalar_one()
    rows = list((await db.execute(stmt)).scalars().all())
    items = [
        CompressionHistoryItem(
            action=str(r.action),
            occurred_at=r.occurred_at
            or __import__("datetime").datetime.now(__import__("datetime").UTC),
            before=r.before_value,
            after=r.after_value,
            user_id=r.user_id,
        )
        for r in rows
    ]
    return CompressionHistoryResponse(
        items=items,
        total=int(total),
        page=offset // limit + 1 if limit else 1,
        page_size=limit,
    )


__all__ = ["router"]
