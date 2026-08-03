"""Stats router — GET /tags/stats.

See: docs/m3-prd.md §4.8.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request

from audio_graphy.api.deps import get_current_user, get_session_factory
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.tenants import get_tenant_id
from audio_graphy.errors import ForbiddenError
from audio_graphy.schemas.stats import StatsItem, StatsResponse
from audio_graphy.services.tag_governance import TagGovernanceService
from audio_graphy.tags.stats import TagStatsService

router = APIRouter(tags=["stats"])


@router.get("/tags/stats", response_model=StatsResponse, summary="Tag statistics aggregation")
async def get_tag_stats(
    request: Request,
    store_id: str | None = Query(default=None),
    agent_name: str | None = Query(default=None),
    tag_path: str | None = Query(default=None),
    tag_value: str | None = Query(default=None),
    group_by: Literal["store_id", "agent_name", "tag_path", "tag_value"] = Query(
        default="tag_path"
    ),
    user: AuthUser = Depends(get_current_user),
) -> StatsResponse:
    """Get multi-dimensional tag statistics (dashboard).

    Agent role: only sees own data.
    """
    tenant_id = get_tenant_id(request)
    agent_user_id = user.id if user.role == "agent" else None
    effective_agent = None if agent_user_id is not None else agent_name

    factory = get_session_factory(request)
    if not await TagGovernanceService(factory).record_blind_sensitive_access(
        tenant_id=tenant_id,
        actor_user_id=user.id,
        access_kind="legacy_tag_stats",
    ):
        raise ForbiddenError("Blind review isolation forbids tag statistics before submission")
    svc = TagStatsService(factory)

    stats = await svc.get_stats(
        tenant_id=tenant_id,
        store_id=store_id,
        agent_name=effective_agent,
        tag_path_prefix=tag_path,
        tag_value=tag_value,
        group_by=group_by,
        agent_user_id=agent_user_id,
    )

    items = [StatsItem(**s) for s in stats]
    return StatsResponse(
        dimensions=[group_by],
        items=items,
        total_records=sum(i.tag_count for i in items),
    )
