"""Cross-reception dialogue-state graph insight endpoint."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field, StringConstraints

from audio_graphy.api.deps import get_current_user, get_session_factory
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.tenants import get_tenant_id
from audio_graphy.errors import ForbiddenError
from audio_graphy.schemas.reception_state_insights import (
    DEFAULT_STATE_TRANSITION_LIMIT,
    MAX_STATE_TRANSITION_LIMIT,
    ReceptionStateInsightsResponse,
)
from audio_graphy.services.reception_state_insights import (
    ReceptionStateInsightService,
)
from audio_graphy.services.tag_governance import TagGovernanceService

router = APIRouter(tags=["reception state insights"])

StoreFilter = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]
AgentFilter = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
]
ScenarioFilter = Literal["gold", "automotive", "custom"]
PositiveReceptionId = Annotated[int, Field(gt=0)]


@router.get(
    "/reception-state-insights",
    response_model=ReceptionStateInsightsResponse,
    summary="Aggregate dialogue-state flows across authorized receptions",
)
async def get_reception_state_insights(
    request: Request,
    user: AuthUser = Depends(get_current_user),
    store_id: Annotated[list[StoreFilter] | None, Query()] = None,
    agent_name: Annotated[list[AgentFilter] | None, Query()] = None,
    scenario: Annotated[list[ScenarioFilter] | None, Query()] = None,
    started_from: datetime | None = None,
    started_to: datetime | None = None,
    reception_id: Annotated[list[PositiveReceptionId] | None, Query()] = None,
    transition_limit: Annotated[
        int,
        Query(ge=1, le=MAX_STATE_TRANSITION_LIMIT),
    ] = DEFAULT_STATE_TRANSITION_LIMIT,
) -> ReceptionStateInsightsResponse:
    tenant_id = get_tenant_id(request)
    if not await TagGovernanceService(get_session_factory(request)).record_blind_sensitive_access(
        tenant_id=tenant_id,
        actor_user_id=user.id,
        access_kind="reception_state_insights",
    ):
        raise ForbiddenError(
            "Blind review isolation forbids state insight access before submission"
        )
    service = ReceptionStateInsightService(get_session_factory(request))
    return await service.analyze(
        tenant_id=tenant_id,
        forced_agent_user_id=user.id if user.role == "agent" else None,
        store_ids=store_id or [],
        agent_names=agent_name or [],
        scenarios=scenario or [],
        started_from=started_from,
        started_to=started_to,
        reception_ids=reception_id or [],
        transition_limit=transition_limit,
    )


__all__ = ["router"]
