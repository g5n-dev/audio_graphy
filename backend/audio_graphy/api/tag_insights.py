"""Visualization-ready multi-group dialogue-tag analysis API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from audio_graphy.analytics.tag_insights import analyze_tag_insights
from audio_graphy.api.deps import get_current_user, get_session_factory
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.roles import require_inspector_or_above
from audio_graphy.auth.tenants import get_tenant_id
from audio_graphy.errors import ForbiddenError
from audio_graphy.schemas.tag_insights import (
    AnalyzeTagInsightsRequest,
    AnalyzeTagInsightsResponse,
)
from audio_graphy.services.tag_governance import TagGovernanceService

router = APIRouter(prefix="/tag-insights", tags=["tag insights"])


@router.post(
    "/analyze",
    response_model=AnalyzeTagInsightsResponse,
    summary="Merge and compare multiple dialogue-tag groups",
    dependencies=[Depends(require_inspector_or_above())],
)
async def analyze_dialogue_tags(
    request: Request,
    body: AnalyzeTagInsightsRequest,
    user: AuthUser = Depends(get_current_user),
) -> AnalyzeTagInsightsResponse:
    """Return a bounded, tenant-protected, storage-free insight snapshot."""
    tenant_id = get_tenant_id(request)
    if body.tenant_id != tenant_id:
        raise ForbiddenError(
            "Cross-tenant tag insight analysis is forbidden",
            detail={
                "authenticated_tenant_id": tenant_id,
                "payload_tenant_id": body.tenant_id,
            },
        )
    if not await TagGovernanceService(get_session_factory(request)).record_blind_sensitive_access(
        tenant_id=tenant_id,
        actor_user_id=user.id,
        access_kind="tag_insights",
    ):
        raise ForbiddenError("Blind review isolation forbids tag insight access before submission")

    return await run_in_threadpool(
        analyze_tag_insights,
        body,
        tenant_id=tenant_id,
    )
