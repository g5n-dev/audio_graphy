"""Prompts router — CRUD + activate.

See: docs/m3-prd.md §4.7.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from audio_graphy.api.deps import get_current_user, get_db, get_session_factory
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.roles import require_admin
from audio_graphy.auth.tenants import get_tenant_id
from audio_graphy.errors import DuplicatePromptVersionError, PromptNotFoundError
from audio_graphy.models.prompt import Prompt
from audio_graphy.schemas.prompts import (
    ActivateRequest,
    ActivateResponse,
    PromptCreate,
    PromptListItem,
    PromptListResponse,
    PromptResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/prompts", tags=["prompts"])


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

    # Optionally activate
    if body.activate:
        factory = get_session_factory(request)
        async with factory() as session:
            # Deactivate old active versions of the same name
            await session.execute(
                update(Prompt)
                .where(Prompt.name == body.name, Prompt.active == True)  # noqa: E712
                .values(active=False)
            )
            # Activate the new one
            await session.execute(update(Prompt).where(Prompt.id == prompt.id).values(active=True))
            await session.commit()
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
    "/{prompt_id}/activate", response_model=ActivateResponse, summary="Activate prompt version",
    dependencies=[Depends(require_admin())],
)
async def activate_prompt(
    prompt_id: int,
    request: Request,
    body: ActivateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: AuthUser = Depends(get_current_user),
) -> ActivateResponse:
    """Switch the active prompt version (optionally trigger recompute).

    Role: admin only. Writes audit_log(action="prompt.activate").
    """
    # Get the prompt
    result = await db.execute(select(Prompt).where(Prompt.id == prompt_id))
    prompt = result.scalar_one_or_none()
    if prompt is None:
        raise PromptNotFoundError(detail={"prompt_id": prompt_id})

    # Find previous active version
    prev_result = await db.execute(
        select(Prompt).where(
            Prompt.name == prompt.name,
            Prompt.active == True,  # noqa: E712
        )
    )
    prev_active = prev_result.scalar_one_or_none()
    prev_active_id = prev_active.id if prev_active is not None else None

    if body.dry_run:
        # Dry run: compute diff without writing
        from audio_graphy.api.deps import get_adapters, get_file_index

        tenant_id = get_tenant_id(request)
        factory = get_session_factory(request)
        bundle = get_adapters(request)
        file_index = get_file_index(request)

        from audio_graphy.tags.recompute import RecomputeService

        svc = RecomputeService(factory, bundle, file_index)
        dry_result: dict[str, Any] = await svc.dry_run(
            tenant_id,
            prompt.version,
            None,
            None,
        )
        return ActivateResponse(
            prompt_id=prompt.id,
            name=prompt.name,
            version=prompt.version,
            active=False,
            message="Dry run completed",
            affected_count=dry_result["affected_count"],
        )

    # Activate: deactivate old, activate new
    factory = get_session_factory(request)
    async with factory() as session:
        # Deactivate old versions of the same name
        await session.execute(
            update(Prompt)
            .where(Prompt.name == prompt.name, Prompt.active == True)  # noqa: E712
            .values(active=False)
        )
        # Activate the new one
        await session.execute(update(Prompt).where(Prompt.id == prompt_id).values(active=True))
        await session.commit()
    await db.refresh(prompt)

    # Trigger recompute
    recompute_task_id = None
    affected_count = 0

    if body.trigger_recompute:
        from audio_graphy.api.deps import get_adapters, get_file_index

        tenant_id = get_tenant_id(request)
        bundle = get_adapters(request)
        file_index = get_file_index(request)

        from audio_graphy.tags.recompute import RecomputeService

        svc = RecomputeService(factory, bundle, file_index)
        task = await svc.create_task(tenant_id, prompt.version, None, None)
        affected_count = task.total
        recompute_task_id = task.task_id

        # Execute inline
        await svc.execute_task(task.task_id)

    # ---- Audit log (fire-and-forget; Q2 quick win PIPL §14.3) ----
    await _write_audit(
        request,
        tenant_id=get_tenant_id(request),
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
        affected_count=affected_count,
        message="Prompt activated. Recompute task created."
        if recompute_task_id
        else "Prompt activated.",
    )
