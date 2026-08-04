"""REST API for offline prompt compilation and its human review loop."""

from __future__ import annotations

from collections.abc import Awaitable, Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status

from audio_graphy.api.deps import get_current_user, get_session_factory
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.roles import require_admin, require_inspector_or_above
from audio_graphy.auth.tenants import get_tenant_id
from audio_graphy.errors import APIError
from audio_graphy.schemas.prompt_lab import (
    PatchDecisionBatch,
    PromptCompilationCreate,
    artifact_resource,
    gradient_resource,
)
from audio_graphy.services.prompt_lab import (
    PatchDecision,
    PromptLabNotFoundError,
    PromptLabPrivacyError,
    PromptLabService,
)
from audio_graphy.services.tag_governance import (
    GovernanceConflictError,
    GovernanceError,
    GovernanceNotFoundError,
    TagGovernanceService,
)

router = APIRouter(prefix="/prompt-lab", tags=["prompt lab"])


def _service(request: Request) -> PromptLabService:
    return PromptLabService(get_session_factory(request))


async def _domain[T](awaitable: Awaitable[T]) -> T:
    try:
        return await awaitable
    except PromptLabPrivacyError as exc:
        raise APIError(str(exc), code="PROMPT_LAB_PRIVACY", status_code=422) from exc
    except (PromptLabNotFoundError, GovernanceNotFoundError) as exc:
        raise APIError(str(exc), code="PROMPT_LAB_NOT_FOUND", status_code=404) from exc
    except GovernanceConflictError as exc:
        raise APIError(str(exc), code="PROMPT_LAB_CONFLICT", status_code=409) from exc
    except GovernanceError as exc:
        raise APIError(str(exc), code="PROMPT_LAB_INVALID", status_code=400) from exc


async def _deny_blind_review_side_channel(request: Request, *, user: AuthUser) -> None:
    """Reuse the governance isolation rule: a blind reviewer sees no model output."""

    governance = TagGovernanceService(get_session_factory(request))
    if not await governance.record_blind_sensitive_access(
        tenant_id=get_tenant_id(request),
        actor_user_id=user.id,
        access_kind="prompt_lab",
    ):
        raise APIError(
            "该盲审任务提交前不可访问模型输出、历史结论或执行谱系",
            code="BLIND_REVIEW_ISOLATION",
            status_code=403,
        )


@router.get("/readiness", dependencies=[Depends(require_inspector_or_above())])
async def get_prompt_lab_readiness(
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    await _deny_blind_review_side_channel(request, user=user)
    readiness = await _domain(_service(request).readiness(tenant_id=get_tenant_id(request)))
    return readiness.as_payload()


@router.get("/artifacts", dependencies=[Depends(require_inspector_or_above())])
async def list_prompt_artifacts(
    request: Request,
    user: AuthUser = Depends(get_current_user),
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    await _deny_blind_review_side_channel(request, user=user)
    rows = await _domain(
        _service(request).list_artifacts(
            tenant_id=get_tenant_id(request),
            status=status_filter,
            limit=limit,
        )
    )
    # Prompt bodies are omitted from the list: they are large, and the list view has
    # no use for them.
    return {
        "items": [artifact_resource(row, include_prompt=False) for row in rows],
        "total": len(rows),
    }


@router.get("/artifacts/{artifact_id}", dependencies=[Depends(require_inspector_or_above())])
async def get_prompt_artifact(
    artifact_id: int,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    await _deny_blind_review_side_channel(request, user=user)
    row = await _domain(
        _service(request).get_artifact(
            tenant_id=get_tenant_id(request),
            artifact_id=artifact_id,
        )
    )
    return artifact_resource(row)


def _measured_tokens(budget: Mapping[str, Any], key: str) -> int | None:
    """Read one measured token count from the preflight report, or None.

    An absent count must not fall back to zero: subtracting a real token count from a
    number nobody measured produces a difference that looks like a saving.
    """

    value = budget.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


@router.get("/artifacts/{artifact_id}/diff", dependencies=[Depends(require_inspector_or_above())])
async def get_prompt_artifact_diff(
    artifact_id: int,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Everything the review view needs to price a candidate before accepting it.

    Two different token quantities live here and they are not interchangeable:
    ``prompt_token_estimate`` is the bare rendered policy text, while the budget
    report's ``fixed_tokens``/``baseline_fixed_tokens`` are whole per-call transport
    costs (system wrapper + tag schema + response schema + framing). Only the latter
    pair may be subtracted from one another, which is why the exposed difference names
    the quantity it measures instead of being a unit-less ``token_delta``.
    """

    await _deny_blind_review_side_channel(request, user=user)
    service = _service(request)
    tenant_id = get_tenant_id(request)
    row = await _domain(service.get_artifact(tenant_id=tenant_id, artifact_id=artifact_id))
    budget = dict(row.input_budget_report or {})
    candidate_fixed = _measured_tokens(budget, "fixed_tokens")
    baseline_fixed = _measured_tokens(budget, "baseline_fixed_tokens")
    return {
        "artifact_id": int(row.id),
        "status": str(row.status),
        "baseline_prompt": str(row.baseline_prompt),
        "candidate_prompt": str(row.rendered_prompt),
        "patches": list(row.patches or []),
        "demos": list(row.demos or []),
        "accepted_patch_ids": list(row.accepted_patch_ids or []),
        "prompt_token_estimate": int(row.prompt_token_estimate),
        "fixed_token_delta": (
            None
            if candidate_fixed is None or baseline_fixed is None
            else candidate_fixed - baseline_fixed
        ),
        "input_budget_report": budget,
        "redaction_report": dict(row.redaction_report or {}),
    }


@router.get("/gradients", dependencies=[Depends(require_inspector_or_above())])
async def list_prompt_gradients(
    request: Request,
    artifact_id: Annotated[int, Query(gt=0)],
    user: AuthUser = Depends(get_current_user),
    decision: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    await _deny_blind_review_side_channel(request, user=user)
    rows = await _domain(
        _service(request).list_gradients(
            tenant_id=get_tenant_id(request),
            artifact_id=artifact_id,
            decision=decision,
        )
    )
    return {"items": [gradient_resource(row) for row in rows], "total": len(rows)}


@router.post(
    "/compilations",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_admin())],
)
async def create_prompt_compilation(
    body: PromptCompilationCreate,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    await _deny_blind_review_side_channel(request, user=user)
    return await _domain(
        _service(request).create_compilation(
            tenant_id=get_tenant_id(request),
            baseline_tagger_version_id=body.baseline_tagger_version_id,
            gold_set_version_id=body.gold_set_version_id,
            compiler_config=body.compiler.model_dump(mode="json"),
            budget=body.budget.model_dump(mode="json"),
            actor_user_id=user.id,
        )
    )


@router.post(
    "/artifacts/{artifact_id}/decisions",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin())],
)
async def decide_prompt_patches(
    artifact_id: int,
    body: PatchDecisionBatch,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Apply a reviewer's accept/reject decisions and return the resulting artifact.

    Idempotent: an unchanged accepted set resolves to the artifact that already
    exists rather than minting a second candidate.
    """

    await _deny_blind_review_side_channel(request, user=user)
    row = await _domain(
        _service(request).apply_patch_decisions(
            tenant_id=get_tenant_id(request),
            artifact_id=artifact_id,
            decisions=[
                PatchDecision(
                    patch_id=item.patch_id,
                    accepted=item.decision == "accepted",
                    note=item.note,
                )
                for item in body.decisions
            ],
            dropped_demo_ids=body.dropped_demo_ids,
            actor_user_id=user.id,
        )
    )
    return artifact_resource(row)
