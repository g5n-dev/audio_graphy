"""Prompts router — CRUD + activate.

See: docs/m3-prd.md §4.7.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from audio_graphy.api.deps import get_current_user, get_db, get_session_factory
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.roles import require_admin
from audio_graphy.auth.tenants import get_tenant_id
from audio_graphy.errors import DuplicatePromptVersionError, PromptNotFoundError
from audio_graphy.models.prompt import Prompt
from audio_graphy.models.reception import DialogueUnit
from audio_graphy.models.recording import Recording
from audio_graphy.models.user import User
from audio_graphy.schemas.prompts import (
    ActivateRequest,
    ActivateResponse,
    PromptCreate,
    PromptListItem,
    PromptListResponse,
    PromptResponse,
)
from audio_graphy.services.legacy_tag_compatibility import (
    LEGACY_RECORDING_DEFAULT_TAG_PATHS,
    CanonicalLegacyTarget,
    LegacyTagCompatibilityService,
)
from audio_graphy.services.tag_governance import GovernanceConflictError, GovernanceError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/prompts", tags=["prompts"])


def _normalized_prompt_content(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


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


@router.get("", response_model=PromptListResponse, summary="List prompts")
async def list_prompts(
    name: str | None = Query(default=None),
    active_only: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
    _user: AuthUser = Depends(get_current_user),
) -> PromptListResponse:
    """List prompt versions with optional filters."""
    stmt = select(Prompt)
    if name is not None:
        stmt = stmt.where(Prompt.name == name)
    if active_only:
        stmt = stmt.where(Prompt.active == True)  # noqa: E712
    stmt = stmt.order_by(Prompt.name, Prompt.id)
    result = await db.execute(stmt)
    prompts = result.scalars().all()

    items = [
        PromptListItem(
            id=p.id,
            name=p.name,
            version=p.version,
            active=p.active,
            changelog=p.changelog,
            created_by=p.created_by,
            created_at=p.created_at,
        )
        for p in prompts
    ]
    return PromptListResponse(items=items)


@router.post(
    "",
    response_model=PromptResponse,
    status_code=201,
    summary="Create prompt version",
    dependencies=[Depends(require_admin())],
)
async def create_prompt(
    body: PromptCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: AuthUser = Depends(get_current_user),
) -> PromptResponse:
    """Create a new prompt version.

    Role: admin only.
    """
    if body.activate:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "legacy create-and-activate cannot prove a canonical serving recipe; "
                "create the Prompt, run canonical dry-run/evaluation, deploy the "
                "candidate, then activate with candidate_tagger_version_id"
            ),
        )
    # Check duplicate (name, version)
    existing = await db.execute(
        select(Prompt).where(
            Prompt.name == body.name,
            Prompt.version == body.version,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise DuplicatePromptVersionError(
            detail={"name": body.name, "version": body.version},
        )

    prompt = Prompt(
        name=body.name,
        version=body.version,
        content=body.content,
        changelog=body.changelog,
        active=False,
        created_by=current_user.id,
    )
    db.add(prompt)
    await db.commit()
    await db.refresh(prompt)

    return PromptResponse(
        id=prompt.id,
        name=prompt.name,
        version=prompt.version,
        content=prompt.content,
        changelog=prompt.changelog,
        active=prompt.active,
        created_by=prompt.created_by,
        created_at=prompt.created_at,
    )


@router.get("/{prompt_id}", response_model=PromptResponse, summary="Get prompt detail")
async def get_prompt(
    prompt_id: int,
    db: AsyncSession = Depends(get_db),
    _user: AuthUser = Depends(get_current_user),
) -> PromptResponse:
    """Get prompt detail (with content)."""
    result = await db.execute(select(Prompt).where(Prompt.id == prompt_id))
    prompt = result.scalar_one_or_none()
    if prompt is None:
        raise PromptNotFoundError(detail={"prompt_id": prompt_id})

    return PromptResponse(
        id=prompt.id,
        name=prompt.name,
        version=prompt.version,
        content=prompt.content,
        changelog=prompt.changelog,
        active=prompt.active,
        created_by=prompt.created_by,
        created_at=prompt.created_at,
    )


@router.post(
    "/{prompt_id}/activate",
    response_model=ActivateResponse,
    summary="Activate prompt version",
    dependencies=[Depends(require_admin())],
)
async def activate_prompt(
    prompt_id: int,
    request: Request,
    body: ActivateRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: AuthUser = Depends(get_current_user),
) -> ActivateResponse:
    """Switch the active prompt version (optionally trigger recompute).

    Role: admin only. Writes audit_log(action="prompt.activate").
    """
    tenant_id = get_tenant_id(request)
    # Get the prompt
    result = await db.execute(
        select(Prompt)
        .join(User, User.id == Prompt.created_by)
        .where(
            Prompt.id == prompt_id,
            User.tenant_id == tenant_id,
        )
    )
    prompt = result.scalar_one_or_none()
    if prompt is None:
        raise PromptNotFoundError(detail={"prompt_id": prompt_id})

    # Find previous active version
    prev_result = await db.execute(
        select(Prompt)
        .join(User, User.id == Prompt.created_by)
        .where(
            Prompt.name == prompt.name,
            Prompt.active == True,  # noqa: E712
            User.tenant_id == tenant_id,
        )
        .order_by(Prompt.id.desc())
        .limit(1)
    )
    prev_active = prev_result.scalar_one_or_none()
    prev_active_id = prev_active.id if prev_active is not None else None
    prompt_unchanged = prev_active is not None and _normalized_prompt_content(
        prompt.content
    ) == _normalized_prompt_content(prev_active.content)

    if body.dry_run:
        if prompt_unchanged:
            affected_count = len(
                (
                    await db.execute(
                        select(DialogueUnit.id).where(DialogueUnit.tenant_id == tenant_id)
                    )
                )
                .scalars()
                .all()
            )
            return ActivateResponse(
                prompt_id=prompt.id,
                name=prompt.name,
                version=prompt.version,
                active=False,
                previous_active_id=prev_active_id,
                affected_count=affected_count,
                sampled_count=0,
                estimated_tokens=0,
                estimated_provider_calls=0,
                provider_calls=0,
                provider_tokens=0,
                changed_count=0,
                quality_gate_status="unchanged_prompt",
                message="Prompt content is unchanged; canonical dry-run made zero Provider calls.",
            )

        from audio_graphy.api.deps import get_adapters, get_file_index
        from audio_graphy.tags.recompute import RecomputeService

        factory = get_session_factory(request)
        bundle = get_adapters(request)
        file_index = get_file_index(request)
        dry_compatibility = LegacyTagCompatibilityService(factory)
        svc = RecomputeService(
            factory,
            bundle,
            file_index,
            enable_hybrid_rule_short_circuit=bool(
                getattr(request.app.state.settings, "enable_hybrid_rule_short_circuit", True)
            ),
        )
        try:
            dry_target = await dry_compatibility.resolve_prompt_scope(
                tenant_id=tenant_id,
                legacy_paths=list(LEGACY_RECORDING_DEFAULT_TAG_PATHS),
            )
            dry_result: dict[str, Any] = await svc.dry_run_prompt_candidate(
                tenant_id=tenant_id,
                prompt_id=prompt.id,
                prompt_version=prompt.version,
                prompt_content=prompt.content,
                resolved_target=dry_target,
                actor_user_id=current_user.id,
                sample_limit=body.sample_limit,
                max_provider_tokens=body.max_provider_tokens,
                max_provider_calls=body.max_provider_calls,
            )
        except GovernanceError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Canonical Prompt dry-run failed closed: {exc}",
            ) from exc
        return ActivateResponse(
            prompt_id=prompt.id,
            name=prompt.name,
            version=prompt.version,
            active=False,
            message="Dry run completed",
            affected_count=dry_result["affected_count"],
            sampled_count=dry_result["sampled_count"],
            estimated_tokens=dry_result["estimated_tokens"],
            estimated_provider_calls=dry_result["estimated_provider_calls"],
            provider_calls=dry_result["provider_calls"],
            provider_tokens=dry_result["provider_tokens"],
            changed_count=dry_result["changed_count"],
            candidate_tagger_version_id=dry_result["candidate_tagger_version_id"],
            quality_gate_status=dry_result["quality_gate_status"],
        )

    factory = get_session_factory(request)
    compatibility: LegacyTagCompatibilityService | None = None
    recording_ids: list[int] = []
    resolved_target: CanonicalLegacyTarget | None = None
    if not prompt_unchanged:
        if body.candidate_tagger_version_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "candidate_tagger_version_id is required for changed Prompt content; "
                    "run canonical dry-run, pass quality evaluation, and deploy it first"
                ),
            )
        compatibility = LegacyTagCompatibilityService(factory)
        try:
            resolved_target = await compatibility.resolve_prompt_scope(
                tenant_id=tenant_id,
                legacy_paths=list(LEGACY_RECORDING_DEFAULT_TAG_PATHS),
            )
            resolved_target = await compatibility.validate_prompt_candidate(
                tenant_id=tenant_id,
                resolved_target=resolved_target,
                candidate_tagger_version_id=body.candidate_tagger_version_id,
                prompt_content=prompt.content,
            )
        except GovernanceConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Canonical Prompt activation failed closed: {exc}",
            ) from exc

    if body.trigger_recompute and resolved_target is not None:
        async with factory() as session:
            recording_ids = list(
                (
                    await session.execute(
                        select(Recording.id).where(Recording.tenant_id == tenant_id)
                    )
                )
                .scalars()
                .all()
            )

    # Activate: deactivate old, activate new
    tenant_prompt_ids = list(
        (
            await db.execute(
                select(Prompt.id)
                .join(User, User.id == Prompt.created_by)
                .where(
                    Prompt.name == prompt.name,
                    User.tenant_id == tenant_id,
                )
            )
        )
        .scalars()
        .all()
    )
    await db.execute(
        update(Prompt)
        .where(
            Prompt.id.in_(tenant_prompt_ids),
            Prompt.active == True,  # noqa: E712
        )
        .values(active=False)
    )
    await db.execute(update(Prompt).where(Prompt.id == prompt_id).values(active=True))
    await db.commit()
    await db.refresh(prompt)

    # Trigger recompute
    recompute_task_id = None
    affected_count = len(resolved_target.dialogue_unit_ids) if resolved_target is not None else 0

    if (
        compatibility is not None
        and resolved_target is not None
        and resolved_target.dialogue_unit_ids
        and body.trigger_recompute
    ):
        job = await compatibility.enqueue_recordings(
            tenant_id=tenant_id,
            recording_ids=recording_ids,
            legacy_paths=list(LEGACY_RECORDING_DEFAULT_TAG_PATHS),
            actor_user_id=current_user.id,
            operation="legacy_prompt_activation",
            idempotency_key=(
                f"legacy-prompt-activation-{prompt.id}-{resolved_target.tagger_version_id}"
            ),
            resolved_target=resolved_target,
            prompt_id=prompt.id,
        )
        affected_count = job.total_items
        recompute_task_id = str(job.id)
        response.status_code = status.HTTP_202_ACCEPTED

    # ---- Audit log (fire-and-forget; Q2 quick win PIPL §14.3) ----
    await _write_audit(
        request,
        tenant_id=tenant_id,
        user_id=current_user.id,
        action="prompt.activate",
        target=f"prompt:{prompt.id}",
        before={"previous_active_id": prev_active_id},
        after={
            "new_active_id": prompt.id,
            "version": prompt.version,
            "trigger_recompute": bool(recompute_task_id),
        },
    )

    return ActivateResponse(
        prompt_id=prompt.id,
        name=prompt.name,
        version=prompt.version,
        active=True,
        previous_active_id=prev_active_id,
        recompute_task_id=recompute_task_id,
        successor=(
            f"/api/v1/tag-jobs/{recompute_task_id}" if recompute_task_id is not None else None
        ),
        affected_count=affected_count,
        candidate_tagger_version_id=(
            resolved_target.tagger_version_id if resolved_target is not None else None
        ),
        quality_gate_status=(
            "production_bound" if resolved_target is not None else "unchanged_prompt"
        ),
        message=(
            "Prompt activated. Canonical recompute task created."
            if recompute_task_id
            else (
                "Prompt content is unchanged; activated with zero Provider calls."
                if prompt_unchanged
                else "Prompt activated and bound to the production TaggerVersion."
            )
        ),
    )
