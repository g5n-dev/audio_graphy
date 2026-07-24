"""T4 — Bi-temporal time-travel + edge-history API (M9 architecture §25.4).

Endpoints (all tenant-scoped, inspector+ read access):
    GET /recordings/{recording_id}/edges
        Time-travel query: return all live edges as-of ``?at=ISO``.

    GET /recordings/{recording_id}/edges/range
        Range query: return edges whose ``[valid_at, invalid_at)`` overlaps
        ``[?from=T1, ?to=T2)``.

    GET /recordings/{recording_id}/edges/{edge_id}/history
        Return the append-only ``edge_events`` history for one edge.

L9: when ``settings.enable_advanced_graph`` is False the router is NOT
registered (see main.py); requests to these paths return 404.

L10: every query is scoped by ``tenant_id`` from the JWT.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from audio_graphy.api.deps import get_db
from audio_graphy.api.schemas_m9 import (
    EdgeEventOut,
    EdgeHistoryResponse,
    EdgeOut,
    EdgeRangeQueryResponse,
    TimeTravelResponse,
)
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.roles import require_inspector_or_above
from audio_graphy.auth.tenants import get_tenant_id
from audio_graphy.core.bi_temporal import BiTemporalEdgeService
from audio_graphy.core.types import GraphEdge
from audio_graphy.errors import EntityNotFoundError, RecordingNotFoundError
from audio_graphy.models.edge_event import EdgeEvent
from audio_graphy.models.recording import Recording

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recordings", tags=["M9 — Bi-temporal edges"])


# ============================================================
# Helpers
# ============================================================


def _parse_iso(dt_str: str | None, *, param_name: str) -> datetime | None:
    """Parse an ISO 8601 datetime string from a query param.

    Raises HTTPException(400) on malformed input so callers don't trip
    FastAPI's default 422 (we want the message to be actionable).
    """
    if dt_str is None:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "VALIDATION_ERROR",
                "message": f"Invalid datetime for ?{param_name}=",
                "raw": dt_str,
                "error": str(exc),
            },
        ) from exc


async def _fetch_recording_or_404(
    session: AsyncSession,
    *,
    recording_id: int,
    tenant_id: str,
) -> Recording:
    """Fetch one recording row, 404 if missing or cross-tenant."""
    result = await session.execute(
        select(Recording).where(
            Recording.id == recording_id,
            Recording.tenant_id == tenant_id,
        )
    )
    rec = result.scalar_one_or_none()
    if rec is None:
        raise RecordingNotFoundError(detail={"recording_id": recording_id})
    return rec


def _edge_from_graph_attrs(
    source: str,
    target: str,
    relation: str,
    attrs: dict[str, Any],
) -> GraphEdge:
    """Build a GraphEdge from a NetworkX edge attribute dict.

    The graph store persists bi-temporal timestamps as ISO strings inside
    the attribute dict (per architecture §6.6); we deserialise them here.
    """
    from audio_graphy.core.types import _str_to_list

    def _dt(key: str) -> datetime | None:
        v = attrs.get(key)
        if not v or not isinstance(v, str):
            return None
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None

    confidence = attrs.get("confidence", "AMBIGUOUS")
    if confidence not in ("EXTRACTED", "INFERRED", "AMBIGUOUS"):
        confidence = "AMBIGUOUS"

    return GraphEdge(
        source=source,
        target=target,
        relation=relation,
        weight=float(attrs.get("weight", 1.0)),
        confidence=confidence,
        confidence_score=(
            float(attrs["confidence_score"]) if attrs.get("confidence_score") is not None else None
        ),
        source_ids=_str_to_list(attrs.get("source_ids", "[]")),
        valid_at=_dt("valid_at"),
        invalid_at=_dt("invalid_at"),
        created_at=_dt("created_at"),
        expired_at=_dt("expired_at"),
        superseded_by=attrs.get("superseded_by"),
    )


def _edges_for_recording(graph: Any, recording_id: int) -> list[GraphEdge]:
    """Extract edges that mention ``recording_id`` in source_ids.

    Each edge attribute dict has ``source_ids`` as a JSON list of
    ``"{recording_id}_{chunk_id}"`` strings. We filter on the
    ``recording_id`` prefix so the response only shows edges that
    belong to this recording.
    """
    from audio_graphy.core.types import _str_to_list

    out: list[GraphEdge] = []
    prefix = f"{recording_id}_"
    for source, target, attrs in graph.edges(data=True):
        rel = attrs.get("relation") or attrs.get("key") or ""
        src_ids = _str_to_list(attrs.get("source_ids", "[]"))
        if not any(isinstance(s, str) and s.startswith(prefix) for s in src_ids):
            continue
        out.append(_edge_from_graph_attrs(source, target, rel, attrs))
    return out


# ============================================================
# Endpoints
# ============================================================


@router.get(
    "/{recording_id}/edges",
    response_model=TimeTravelResponse,
    summary="Time-travel query: live edges as-of ?at=ISO (inspector+)",
)
async def time_travel_edges(
    recording_id: int,
    request: Request,
    at: str | None = Query(
        default=None,
        description=(
            "ISO 8601 datetime. Default = now(). Returns edges whose "
            "bi-temporal interval contains this timestamp."
        ),
    ),
    include_soft_deleted: bool = Query(
        default=False,
        description="If True, edges with expired_at set are NOT filtered.",
    ),
    db: AsyncSession = Depends(get_db),
    _user: AuthUser = Depends(require_inspector_or_above()),
) -> TimeTravelResponse:
    """Return all live edges for the recording as-of ``at``."""
    tenant_id = get_tenant_id(request)
    rec = await _fetch_recording_or_404(db, recording_id=recording_id, tenant_id=tenant_id)
    del rec  # 404-or-pass

    as_of = _parse_iso(at, param_name="at") or datetime.now(_utc())

    graph = _tenant_graph_or_404(request, tenant_id)
    bt = BiTemporalEdgeService(tenant_id=tenant_id)
    all_edges = _edges_for_recording(graph, recording_id)
    live = bt.time_travel_query(all_edges, as_of=as_of, include_soft_deleted=include_soft_deleted)
    payload = [EdgeOut(**_edge_to_dict(e)) for e in live]
    return TimeTravelResponse(
        recording_id=recording_id, as_of=as_of, edges=payload, total=len(payload)
    )


@router.get(
    "/{recording_id}/edges/range",
    response_model=EdgeRangeQueryResponse,
    summary="Range query: edges alive during [from, to) (inspector+)",
)
async def edges_in_range(
    recording_id: int,
    request: Request,
    from_time: str = Query(..., alias="from", description="ISO 8601 start (inclusive)."),
    to_time: str = Query(..., alias="to", description="ISO 8601 end (exclusive)."),
    db: AsyncSession = Depends(get_db),
    _user: AuthUser = Depends(require_inspector_or_above()),
) -> EdgeRangeQueryResponse:
    """Return every edge whose bi-temporal interval intersects [from, to)."""
    tenant_id = get_tenant_id(request)
    await _fetch_recording_or_404(db, recording_id=recording_id, tenant_id=tenant_id)

    t_from = _parse_iso(from_time, param_name="from")
    t_to = _parse_iso(to_time, param_name="to")
    if t_from is None or t_to is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "Both ?from= and ?to= are required in ISO format."},
        )
    if t_from >= t_to:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "?from= must be earlier than ?to=."},
        )

    graph = _tenant_graph_or_404(request, tenant_id)
    all_edges = _edges_for_recording(graph, recording_id)
    # Intersect test: edge interval [valid_at, invalid_at) overlaps
    # [t_from, t_to).  Edges with no valid_at default to "always alive".
    kept: list[EdgeOut] = []
    for e in all_edges:
        v = e.valid_at
        iv = e.invalid_at
        if v is None:
            # Pre-M9 edge — always included (compat).
            kept.append(EdgeOut(**_edge_to_dict(e)))
            continue
        if v >= t_to:
            continue
        if iv is not None and iv <= t_from:
            continue
        kept.append(EdgeOut(**_edge_to_dict(e)))
    return EdgeRangeQueryResponse(
        recording_id=recording_id,
        from_time=t_from,
        to_time=t_to,
        edges=kept,
        total=len(kept),
    )


@router.get(
    "/{recording_id}/edges/{edge_id}/history",
    response_model=EdgeHistoryResponse,
    summary="Edge event-history (append-only audit log, inspector+)",
)
async def edge_history(
    recording_id: int,
    edge_id: str,
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    _user: AuthUser = Depends(require_inspector_or_above()),
) -> EdgeHistoryResponse:
    """Return the append-only ``edge_events`` rows for one edge.

    ``edge_id`` is the ``edge_key`` value (``"{source}|{relation}|{target}"``).
    We return the most recent events first.
    """
    tenant_id = get_tenant_id(request)
    await _fetch_recording_or_404(db, recording_id=recording_id, tenant_id=tenant_id)

    stmt = (
        select(EdgeEvent)
        .where(
            EdgeEvent.tenant_id == tenant_id,
            EdgeEvent.edge_key == edge_id,
        )
        .order_by(EdgeEvent.id.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    items = [
        EdgeEventOut(
            id=int(r.id),
            event_type=str(r.event_type),
            edge_key=str(r.edge_key),
            source=str(r.source),
            target=str(r.target),
            relation=str(r.relation),
            valid_at=r.valid_at,
            invalid_at=r.invalid_at,
            superseded_by=r.superseded_by,
            actor=str(r.actor),
            occurred_at=None,  # table has no occurred_at column; id is monotonic
        )
        for r in rows
    ]
    return EdgeHistoryResponse(
        recording_id=recording_id,
        edge_key=edge_id,
        events=items,
        total=len(items),
    )


# ============================================================
# Internal helpers (kept tiny + explicit)
# ============================================================


def _utc() -> Any:
    """Return ``datetime.timezone.utc`` (helper avoids shadowing imports)."""
    from datetime import UTC

    return UTC


def _edge_to_dict(edge: GraphEdge) -> dict[str, Any]:
    """Convert a GraphEdge dataclass to a dict suitable for ``EdgeOut``."""
    return {
        "source": edge.source,
        "target": edge.target,
        "relation": edge.relation,
        "weight": edge.weight,
        "confidence": str(edge.confidence),
        "confidence_score": edge.confidence_score,
        "source_ids": list(edge.source_ids),
        "valid_at": edge.valid_at,
        "invalid_at": edge.invalid_at,
        "created_at": edge.created_at,
        "expired_at": edge.expired_at,
        "superseded_by": edge.superseded_by,
    }


def _tenant_graph_or_404(request: Request, tenant_id: str) -> Any:
    """Return the per-tenant NetworkX graph; 404 if no store is configured."""
    graph_stores: dict[str, Any] | None = getattr(request.app.state, "graph_stores", None)
    if graph_stores is None:
        raise EntityNotFoundError(
            message="Graph store not initialised for this deployment",
            detail={"tenant_id": tenant_id},
        )
    store = graph_stores.get(tenant_id)
    if store is None:
        from audio_graphy.storage.graph_networkx import NetworkXGraphStore

        settings = request.app.state.settings
        store = NetworkXGraphStore(settings.working_dir, tenant_id=tenant_id)
        graph_stores[tenant_id] = store
    return store.graph


__all__ = ["router"]
