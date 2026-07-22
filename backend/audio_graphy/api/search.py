"""T8 — Global / local / drill-down search API (M9 architecture §25.8).

Endpoints (tenant-scoped; inspector+ read):
    POST /search/global              — map-reduce over community summaries
    POST /search/local               — entity-seed edge-walk
    POST /search/communities/{id}/drill-down
        — return children of one community at the next hierarchy level

L4 defaults: top_k=5, max_concurrency=5, level=0..2 (Q2).
L9: router is not registered when ``enable_advanced_graph=False``.
L10: every query is scoped by ``tenant_id`` from the JWT.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from audio_graphy.api.deps import get_db
from audio_graphy.api.schemas_m9 import (
    CommunityHit,
    DrillDownRequest,
    DrillDownResponse,
    GlobalSearchRequest,
    GlobalSearchResponse,
    LocalSearchHit,
    LocalSearchRequest,
    LocalSearchResponse,
)
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.roles import require_inspector_or_above
from audio_graphy.auth.tenants import get_tenant_id
from audio_graphy.core.community_summary import CommunitySummaryRecord
from audio_graphy.core.global_search import GlobalSearcher
from audio_graphy.errors import EntityNotFoundError
from audio_graphy.models.community_summary import CommunitySummary

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["M9 — Search"])


# ============================================================
# Helpers
# ============================================================


def _row_to_record(row: CommunitySummary) -> CommunitySummaryRecord:
    """ORM row → in-memory record expected by the search core."""
    import json

    try:
        members = json.loads(row.member_node_ids or "[]")
    except (TypeError, ValueError):
        members = []
    return CommunitySummaryRecord(
        leiden_job_id=int(row.leiden_job_id),
        level=int(row.level),
        community_id=int(row.community_id),
        title=str(row.title),
        summary=str(row.summary),
        member_count=int(row.member_count),
        member_node_ids=list(members),
        generated_at=row.generated_at,
        strategy=str(row.strategy),
    )


def _hit_from_record(
    rec: CommunitySummaryRecord, score: float
) -> CommunityHit:
    return CommunityHit(
        community_id=rec.community_id,
        level=rec.level,
        title=rec.title,
        summary=rec.summary,
        score=score,
        member_count=rec.member_count,
    )


async def _preload_summaries(
    db: AsyncSession,
    *,
    tenant_id: str,
    level: int,
) -> list[CommunitySummaryRecord]:
    """Load all community summaries for (tenant, level) from the DB."""
    stmt = select(CommunitySummary).where(
        CommunitySummary.tenant_id == tenant_id,
        CommunitySummary.level == level,
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return [_row_to_record(r) for r in rows]


# ============================================================
# POST /search/global
# ============================================================


@router.post(
    "/global",
    response_model=GlobalSearchResponse,
    summary="Global map-reduce search over community summaries (inspector+)",
)
async def global_search(
    body: GlobalSearchRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _user: AuthUser = Depends(require_inspector_or_above()),
) -> GlobalSearchResponse:
    """L4 map-reduce search.

    Loads all community summaries at ``body.level`` for the tenant, then
    scores each one against ``body.query`` (default: keyword overlap;
    production deploys swap in an LLM scorer). Top ``body.top_k`` results
    are returned sorted by score descending.
    """
    tenant_id = get_tenant_id(request)
    summaries = await _preload_summaries(
        db, tenant_id=tenant_id, level=body.level
    )
    if body.community_ids is not None:
        allow = set(body.community_ids)
        summaries = [s for s in summaries if s.community_id in allow]

    def _provider(*, tenant_id: str, level: int) -> list[CommunitySummaryRecord]:
        return summaries

    searcher = GlobalSearcher(
        provider=_provider,
        top_k=body.top_k,
        max_concurrency=5,  # L4 cap
    )
    result = await searcher.search(
        query=body.query,
        tenant_id=tenant_id,
        level=body.level,
    )
    return GlobalSearchResponse(
        query=body.query,
        level=body.level,
        hits=[
            CommunityHit(
                community_id=h.community_id,
                level=h.level,
                title=h.title,
                summary=h.summary,
                score=h.score,
                member_count=h.member_count,
            )
            for h in result.hits
        ],
        total=result.total,
        took_ms=result.took_ms,
    )


# ============================================================
# POST /search/local
# ============================================================


@router.post(
    "/local",
    response_model=LocalSearchResponse,
    summary="Local search — entity-seed edge-walk (inspector+)",
)
async def local_search(
    body: LocalSearchRequest,
    request: Request,
    _user: AuthUser = Depends(require_inspector_or_above()),
) -> LocalSearchResponse:
    """Walk edges from ``seed_entity_ids`` up to ``depth`` hops.

    Local search does NOT use community summaries; it iteratively
    expands the seed set by following edges in the tenant's NetworkX
    graph. Each candidate is scored by graph distance from the seed
    (closer = higher score) plus a keyword match against ``query``.
    """
    import time

    from audio_graphy.api.bi_temporal import _tenant_graph_or_404
    from audio_graphy.core.global_search import _tokenize

    tenant_id = get_tenant_id(request)
    started = time.perf_counter()

    graph = _tenant_graph_or_404(request, tenant_id)
    seeds = set(body.seed_entity_ids)
    if not all(graph.has_node(n) for n in seeds):
        missing = [n for n in seeds if not graph.has_node(n)]
        raise EntityNotFoundError(
            message="One or more seed entities not found in tenant graph",
            detail={"missing": missing},
        )

    # BFS up to ``depth`` hops.
    visited: dict[str, int] = dict.fromkeys(seeds, 0)
    frontier = list(seeds)
    for hop in range(body.depth):
        next_frontier: list[str] = []
        for node in frontier:
            for nbr in graph.successors(node):
                if nbr not in visited:
                    visited[nbr] = hop + 1
                    next_frontier.append(nbr)
            for nbr in graph.predecessors(node):
                if nbr not in visited:
                    visited[nbr] = hop + 1
                    next_frontier.append(nbr)
        frontier = next_frontier

    # Score by inverse distance + keyword overlap.
    q_tokens = _tokenize(body.query)
    hits: list[LocalSearchHit] = []
    for entity_id, dist in visited.items():
        attrs = graph.nodes[entity_id]
        name = str(attrs.get("name", entity_id))
        type_ = str(attrs.get("type", ""))
        desc = str(attrs.get("description", ""))
        text_tokens = _tokenize(name + " " + type_ + " " + desc)
        overlap = (
            len(q_tokens & text_tokens) / max(1, len(q_tokens))
            if q_tokens
            else 0.0
        )
        distance_score = 1.0 / (1.0 + dist)
        score = 0.5 * distance_score + 0.5 * overlap
        hits.append(
            LocalSearchHit(
                entity_id=entity_id,
                name=name,
                type=type_,
                description=desc,
                score=score,
            )
        )
    hits.sort(key=lambda h: h.score, reverse=True)
    top = hits[: body.top_k]
    return LocalSearchResponse(
        query=body.query,
        seed_entity_ids=list(body.seed_entity_ids),
        depth=body.depth,
        hits=top,
        total=len(top),
        took_ms=(time.perf_counter() - started) * 1000.0,
    )


# ============================================================
# POST /search/communities/{community_id}/drill-down
# ============================================================


@router.post(
    "/communities/{community_id}/drill-down",
    response_model=DrillDownResponse,
    summary="Drill into a community's children at the next level (inspector+)",
)
async def drill_down(
    community_id: int,
    body: DrillDownRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _user: AuthUser = Depends(require_inspector_or_above()),
) -> DrillDownResponse:
    """Return the child-level summaries whose members descend from ``community_id``.

    The current implementation treats ``level`` as a flat coordinate:
    we look up the parent summary at ``body.level``, then return every
    summary at ``body.level + 1`` whose ``member_node_ids`` intersect
    the parent's. (Architecture §10.4 — drill-down semantics.)
    """
    if body.level >= 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "Cannot drill below level 2 (Q2 cap)."},
        )
    tenant_id = get_tenant_id(request)

    parent_stmt = select(CommunitySummary).where(
        CommunitySummary.tenant_id == tenant_id,
        CommunitySummary.level == body.level,
        CommunitySummary.community_id == community_id,
    )
    parent = (await db.execute(parent_stmt)).scalar_one_or_none()
    if parent is None:
        raise EntityNotFoundError(
            message="Parent community not found",
            detail={"community_id": community_id, "level": body.level},
        )

    parent_members = set(_row_to_record(parent).member_node_ids)
    child_level = body.level + 1
    child_rows = list(
        (
            await db.execute(
                select(CommunitySummary).where(
                    CommunitySummary.tenant_id == tenant_id,
                    CommunitySummary.level == child_level,
                )
            )
        ).scalars().all()
    )
    children: list[CommunityHit] = []
    for r in child_rows:
        rec = _row_to_record(r)
        if set(rec.member_node_ids) & parent_members:
            children.append(
                CommunityHit(
                    community_id=rec.community_id,
                    level=rec.level,
                    title=rec.title,
                    summary=rec.summary,
                    score=1.0,
                    member_count=rec.member_count,
                )
            )
    return DrillDownResponse(
        community_id=community_id,
        parent_level=body.level,
        child_level=child_level,
        children=children,
        total=len(children),
    )


__all__ = ["router"]
