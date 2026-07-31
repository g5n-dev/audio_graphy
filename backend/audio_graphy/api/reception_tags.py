"""Reception dialogue-tag derivation and persisted insight endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import Field, StringConstraints

from audio_graphy.api.deps import get_current_user, get_session_factory
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.roles import require_write_access
from audio_graphy.auth.tenants import get_tenant_id
from audio_graphy.errors import ForbiddenError
from audio_graphy.models.reception import DialogueTagAssignment
from audio_graphy.schemas.reception_tags import (
    MAX_EVIDENCE_SUMMARY_ITEMS,
    MAX_RECEPTION_OUTPUT_EVIDENCE_REFS,
    CorrectDialogueTagRequest,
    CorrectDialogueTagResponse,
    DeriveDialogueTagsRequest,
    ReceptionTagEvidenceSummary,
    ReceptionTagInsightsResponse,
)
from audio_graphy.schemas.receptions import DialogueTagAssignmentResponse
from audio_graphy.schemas.tag_insights import (
    MAX_DIFFERENCE_ITEMS,
    MAX_MATRIX_ROWS,
    MergeStrategy,
    TrendGranularity,
)
from audio_graphy.services.legacy_tag_compatibility import (
    LegacyTagCompatibilityService,
)
from audio_graphy.services.reception_tagging import ReceptionTaggingService
from audio_graphy.services.tag_governance import (
    GovernanceConflictError,
    GovernanceNotFoundError,
    TagGovernanceService,
)

router = APIRouter(tags=["reception tags"])

StoreFilter = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]
AgentFilter = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
]
GroupFilter = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[\w.-]+$",
    ),
]
GroupVersionFilter = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=3,
        max_length=129,
        pattern=r"^[\w.-]+@[\w.-]+$",
    ),
]
ScenarioFilter = Literal["gold", "automotive", "custom"]
PositiveReceptionId = Annotated[int, Field(gt=0)]


def _service(request: Request) -> ReceptionTaggingService:
    return ReceptionTaggingService(get_session_factory(request))


async def _enforce_blind_review_isolation(request: Request, user: AuthUser) -> None:
    if await TagGovernanceService(get_session_factory(request)).has_active_blind_review(
        tenant_id=get_tenant_id(request),
        reviewer_user_id=user.id,
    ):
        raise ForbiddenError("Blind review isolation forbids tag output access before submission")


async def _reserve_semantic_access(
    request: Request,
    user: AuthUser,
    *,
    access_kind: str,
    reception_id: int | None = None,
) -> None:
    if not await TagGovernanceService(get_session_factory(request)).record_blind_sensitive_access(
        tenant_id=get_tenant_id(request),
        actor_user_id=user.id,
        access_kind=access_kind,
        reception_id=reception_id,
    ):
        raise ForbiddenError("Blind review isolation forbids tag output access before submission")


def _assignment_response(
    assignment: DialogueTagAssignment,
) -> DialogueTagAssignmentResponse:
    return DialogueTagAssignmentResponse(
        id=assignment.id,
        reception_id=assignment.reception_id,
        dialogue_unit_id=assignment.dialogue_unit_id,
        group_key=assignment.group_key,
        group_version=assignment.group_version,
        label_key=assignment.label_key,
        label_value=assignment.label_value,
        confidence=assignment.confidence,
        source=assignment.source,
        priority=assignment.priority,
        evidence_refs=assignment.evidence_refs,
        model_run_id=assignment.model_run_id,
        is_current=assignment.is_current,
        assigned_at=assignment.assigned_at,
    )


@router.post(
    "/receptions/{reception_id}/dialogue-tags/derive",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Derive versioned, evidence-bound dialogue tags",
    dependencies=[Depends(require_write_access())],
)
async def derive_dialogue_tags(
    reception_id: int,
    body: DeriveDialogueTagsRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=1, max_length=128),
    ] = None,
) -> dict[str, object]:
    """Compatibility adapter: create one canonical async job, never legacy rows."""

    await _enforce_blind_review_isolation(request, user)
    tenant_id = get_tenant_id(request)
    adapter = LegacyTagCompatibilityService(get_session_factory(request))
    try:
        job = await adapter.enqueue_reception(
            tenant_id=tenant_id,
            reception_id=reception_id,
            legacy_paths=list(body.target_labels),
            actor_user_id=user.id,
            idempotency_key=idempotency_key,
        )
    except GovernanceNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except GovernanceConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {
        "job_id": job.id,
        "status": job.status,
        "reception_id": reception_id,
        "requested_labels": list(body.target_labels),
        "successor": f"/api/v1/tag-jobs/{job.id}",
    }


@router.patch(
    "/receptions/{reception_id}/dialogue-tags/{assignment_id}",
    response_model=CorrectDialogueTagResponse,
    summary="Append an evidence-bound manual correction for one current dialogue tag",
    dependencies=[Depends(require_write_access())],
)
async def correct_dialogue_tag(
    reception_id: int,
    assignment_id: int,
    body: CorrectDialogueTagRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> CorrectDialogueTagResponse:
    await _reserve_semantic_access(
        request,
        user,
        access_kind="tag_correction",
        reception_id=reception_id,
    )
    tenant_id = get_tenant_id(request)
    result = await _service(request).correct_assignment(
        reception_id=reception_id,
        assignment_id=assignment_id,
        tenant_id=tenant_id,
        request=body,
        actor=f"user:{user.id}",
        actor_user_id=user.id,
    )
    return CorrectDialogueTagResponse(
        reception_id=result.reception_id,
        reception_version=result.reception_version,
        superseded_assignment_id=result.superseded_assignment_id,
        assignment=_assignment_response(result.assignment),
    )


@router.get(
    "/reception-tag-insights",
    response_model=ReceptionTagInsightsResponse,
    summary="Analyze current or explicitly selected historical reception tags",
)
async def get_reception_tag_insights(
    request: Request,
    user: AuthUser = Depends(get_current_user),
    store_id: Annotated[list[StoreFilter] | None, Query()] = None,
    agent_name: Annotated[list[AgentFilter] | None, Query()] = None,
    scenario: Annotated[list[ScenarioFilter] | None, Query()] = None,
    started_from: datetime | None = None,
    started_to: datetime | None = None,
    reception_id: Annotated[
        list[PositiveReceptionId] | None,
        Query(),
    ] = None,
    group_key: Annotated[list[GroupFilter] | None, Query()] = None,
    group_id: Annotated[
        list[GroupVersionFilter] | None,
        Query(
            description=(
                "Repeat exact key@version identities (max 8) to compare historical "
                "versions. Cannot be combined with group_key."
            )
        ),
    ] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    assignment_limit: Annotated[int, Query(ge=1, le=5_000)] = 1_000,
    matrix_limit: Annotated[int, Query(ge=1, le=MAX_MATRIX_ROWS)] = MAX_MATRIX_ROWS,
    difference_limit: Annotated[
        int,
        Query(ge=0, le=MAX_DIFFERENCE_ITEMS),
    ] = MAX_DIFFERENCE_ITEMS,
    evidence_summary_limit: Annotated[
        int,
        Query(ge=1, le=MAX_EVIDENCE_SUMMARY_ITEMS),
    ] = MAX_EVIDENCE_SUMMARY_ITEMS,
    merge_strategy: MergeStrategy = "manual_wins",
    trend_granularity: TrendGranularity = "day",
    top_n_co_occurrences: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ReceptionTagInsightsResponse:
    await _reserve_semantic_access(
        request,
        user,
        access_kind="reception_tag_insights",
    )
    tenant_id = get_tenant_id(request)
    result = await _service(request).insights(
        tenant_id=tenant_id,
        store_ids=store_id or [],
        agent_names=agent_name or [],
        scenarios=scenario or [],
        started_from=started_from,
        started_to=started_to,
        reception_ids=reception_id or [],
        group_keys=group_key or [],
        group_ids=group_id or [],
        forced_agent_user_id=user.id if user.role == "agent" else None,
        page=page,
        page_size=page_size,
        assignment_limit=assignment_limit,
        matrix_limit=matrix_limit,
        difference_limit=difference_limit,
        evidence_summary_limit=evidence_summary_limit,
        merge_strategy=merge_strategy,
        trend_granularity=trend_granularity,
        top_n_co_occurrences=top_n_co_occurrences,
    )
    return ReceptionTagInsightsResponse(
        tenant_id=tenant_id,
        page=result.page,
        page_size=result.page_size,
        total_receptions=result.total_receptions,
        returned_reception_ids=result.returned_reception_ids,
        total_assignments=result.total_assignments,
        assignment_count=result.loaded_assignment_count,
        assignment_limit=result.assignment_limit,
        truncated=result.truncated,
        assignment_truncated=result.assignment_truncated,
        group_truncated=result.group_truncated,
        difference_truncated=result.difference_truncated,
        evidence_truncated=result.evidence_truncated,
        evidence_ref_limit=MAX_RECEPTION_OUTPUT_EVIDENCE_REFS,
        evidence_ref_count=result.evidence_ref_count,
        evidence_summary_total=result.evidence_summary_total,
        evidence_summary_count=len(result.evidence_summary),
        evidence_summary_limit=result.evidence_summary_limit,
        evidence_summary_truncated=result.evidence_summary_truncated,
        selection_mode=result.selection_mode,
        selected_group_ids=result.selected_group_ids,
        merge_strategy=merge_strategy,
        trend_granularity=trend_granularity,
        insights=result.insights,
        evidence_summary=[
            ReceptionTagEvidenceSummary(
                reception_id=item.reception_id,
                dialogue_unit_id=item.dialogue_unit_id,
                group_id=item.group_id,
                label_key=item.label_key,
                label_value=item.label_value,
                confidence=item.confidence,
                evidence_count=item.evidence_count,
                evidence_refs=item.evidence_refs,
            )
            for item in result.evidence_summary
        ],
        generated_at=datetime.now(UTC),
    )


__all__ = ["router"]
