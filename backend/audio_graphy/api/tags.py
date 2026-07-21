"""Tags router — GET/POST tags + recompute.

See: docs/m3-prd.md §4.6.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.adapters.bundle import AdapterBundle
from audio_graphy.api.deps import (
    get_adapters,
    get_current_user,
    get_db,
    get_file_index,
    get_session_factory,
    get_stores,
)
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.roles import require_admin, require_write_access
from audio_graphy.auth.tenants import get_agent_filter, get_tenant_id
from audio_graphy.errors import RecordingNotFoundError, RecordingNotIndexedError
from audio_graphy.models.enums import RecordingStatus
from audio_graphy.models.recording import Recording
from audio_graphy.schemas.tags import (
    RecomputeCreateResponse,
    RecomputeDryRunResponse,
    RecomputeRequest,
    RecomputeTaskResponse,
    TagAutoResponse,
    TagDeltaPreview,
    TagManualResponse,
    TagResultItem,
    TagsListResponse,
)
from audio_graphy.storage.file_index import FileIndex
from audio_graphy.tags.current_view import TagCurrentService
from audio_graphy.tags.facts import TagFactsService
from audio_graphy.tags.recompute import RecomputeService
from audio_graphy.tags.stats import TagStatsService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tags"])


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


@router.get(
    "/recordings/{recording_id}/tags",
    response_model=TagsListResponse,
    summary="Get recording tags",
)
async def get_tags(
    recording_id: int,
    request: Request,
    view: str = "current",
    tag_path: str | None = None,
    db: AsyncSession = Depends(get_db),
    _user: AuthUser = Depends(get_current_user),
) -> TagsListResponse:
    """Get tags for a recording (current / history / facts view).

    Cross-tenant returns 404.
    """
    tenant_id = get_tenant_id(request)
    agent_filter = get_agent_filter(request)

    # Verify recording exists
    rec_stmt = select(Recording).where(
        Recording.id == recording_id,
        Recording.tenant_id == tenant_id,
    )
    if agent_filter is not None:
        rec_stmt = rec_stmt.where(Recording.agent_name == agent_filter)
    rec_result = await db.execute(rec_stmt)
    if rec_result.scalar_one_or_none() is None:
        raise RecordingNotFoundError(detail={"recording_id": recording_id})

    factory = get_session_factory(request)

    if view == "current":
        svc = TagCurrentService(factory)
        tags = await svc.get_current_tags(recording_id, tenant_id)
        tag_data = [
            {
                "tag_path": t.tag_path,
                "tag_value": t.tag_value,
                "version": t.version,
                "prompt_version": t.prompt_version,
            }
            for t in tags
        ]
    elif view == "history":
        facts_svc = TagFactsService(factory)
        facts = await facts_svc.get_history(recording_id, tenant_id, tag_path_prefix=tag_path)
        tag_data = [
            {
                "tag_path": f.tag_path,
                "tag_value": f.tag_value,
                "version": f.version,
                "prompt_version": f.prompt_version,
                "source": f.source,
                "confidence": f.confidence,
                "computed_at": f.computed_at.isoformat() if f.computed_at else None,
                "computed_by": f.computed_by,
            }
            for f in facts
        ]
    else:  # facts
        facts_svc = TagFactsService(factory)
        facts = await facts_svc.get_facts(recording_id, tag_path, tenant_id)
        tag_data = [
            {
                "tag_path": f.tag_path,
                "tag_value": f.tag_value,
                "version": f.version,
                "prompt_version": f.prompt_version,
                "model_version": f.model_version,
                "source": f.source,
                "input_hash": f.input_hash,
                "confidence": f.confidence,
                "computed_at": f.computed_at.isoformat() if f.computed_at else None,
                "computed_by": f.computed_by,
            }
            for f in facts
        ]

    return TagsListResponse(recording_id=recording_id, view=view, tags=tag_data)


@router.post(
    "/recordings/{recording_id}/tags",
    response_model=Any,
    summary="Tag a recording (auto or manual)",
    dependencies=[Depends(require_write_access())],
)
async def post_tags(
    recording_id: int,
    request: Request,
    body: dict[str, Any],
    db: AsyncSession = Depends(get_db),
    current_user: AuthUser = Depends(get_current_user),
) -> Any:
    """Execute tagging on a recording.

    mode=auto: LLM-based auto tagging.
    mode=manual: Manual correction.

    Role: admin / inspector.
    """
    tenant_id = get_tenant_id(request)
    agent_filter = get_agent_filter(request)
    factory = get_session_factory(request)

    # Verify recording exists and is indexed
    rec_stmt = select(Recording).where(
        Recording.id == recording_id,
        Recording.tenant_id == tenant_id,
    )
    if agent_filter is not None:
        rec_stmt = rec_stmt.where(Recording.agent_name == agent_filter)
    rec_result = await db.execute(rec_stmt)
    recording = rec_result.scalar_one_or_none()
    if recording is None:
        raise RecordingNotFoundError(detail={"recording_id": recording_id})
    if recording.status != RecordingStatus.INDEXED.value:
        raise RecordingNotIndexedError(
            detail={"recording_id": recording_id, "status": recording.status}
        )

    mode = body.get("mode", "auto")
    stores = get_stores(request)
    bundle = get_adapters(request)

    if mode == "manual":
        return await _handle_manual_tag(recording, body, current_user, factory, tenant_id, request)
    return await _handle_auto_tag(recording, body, factory, tenant_id, bundle, stores.file_index)


async def _handle_auto_tag(
    recording: Recording,
    body: dict[str, Any],
    factory: async_sessionmaker[AsyncSession],
    tenant_id: str,
    bundle: AdapterBundle,
    file_index: FileIndex,
) -> TagAutoResponse:
    """Handle mode=auto LLM tagging."""
    tag_paths = body.get("tag_paths") or [
        "quality.greeting",
        "quality.closing",
        "sales.product_mention",
    ]
    prompt_version = body.get("prompt_version") or recording.prompt_version or "tag_prompt_v1"

    facts_svc = TagFactsService(factory)
    current_svc = TagCurrentService(factory)
    stats_svc = TagStatsService(factory)

    results: list[TagResultItem] = []
    cached_hits = 0
    llm_calls = 0

    for tag_path in tag_paths:
        cache_key = hashlib.md5(f"{tag_path}:{recording.id}:{prompt_version}".encode()).hexdigest()

        # Check cache
        cached = await file_index.get_llm_cache(cache_key)
        if cached is not None:
            tag_value = cached
            cached_hit = True
            cached_hits += 1
        else:
            messages: list[dict[str, str]] = [
                {
                    "role": "user",
                    "content": (
                        f"请对录音进行质检打标。\n"
                        f"标签路径: {tag_path}\n"
                        f"录音ID: {recording.id}\n"
                        f"请返回 pass 或 fail。"
                    ),
                }
            ]
            response = await bundle.weak_llm.complete(messages=messages, cache_key=cache_key)
            tag_value = response.text.strip().split("\n")[0][:255]
            await file_index.set_llm_cache(cache_key, tag_value)
            cached_hit = False
            llm_calls += 1

        # Get old value for delta
        old_current = await current_svc.get_current_value(recording.id, tag_path, tenant_id)
        old_value = old_current.tag_value if old_current is not None else None

        # Write fact
        fact = await facts_svc.append_fact(
            recording_id=recording.id,
            tag_path=tag_path,
            tag_value=tag_value,
            prompt_version=prompt_version,
            model_version=bundle.weak_llm.model,
            input_hash=cache_key,
            confidence=0.95,
            source="llm",
            computed_by=None,
            tenant_id=tenant_id,
        )
        await current_svc.upsert_current(fact, tenant_id)
        await stats_svc.apply_delta(
            tenant_id=tenant_id,
            store_id=recording.store_id,
            agent_name=recording.agent_name,
            tag_path=tag_path,
            old_value=old_value,
            new_value=tag_value,
        )

        results.append(
            TagResultItem(
                tag_path=tag_path,
                tag_value=tag_value,
                version=fact.version,
                confidence=fact.confidence,
                cached=cached_hit,
            )
        )

    return TagAutoResponse(
        recording_id=recording.id,
        tagged=len(results),
        cached_hits=cached_hits,
        llm_calls=llm_calls,
        results=results,
    )


async def _handle_manual_tag(
    recording: Recording,
    body: dict[str, Any],
    current_user: AuthUser,
    factory: async_sessionmaker[AsyncSession],
    tenant_id: str,
    request: Request,
) -> TagManualResponse:
    """Handle mode=manual correction."""
    tag_path = body["tag_path"]
    tag_value = body["tag_value"]

    facts_svc = TagFactsService(factory)
    current_svc = TagCurrentService(factory)
    stats_svc = TagStatsService(factory)

    old_current = await current_svc.get_current_value(recording.id, tag_path, tenant_id)
    old_value = old_current.tag_value if old_current is not None else None

    fact = await facts_svc.append_fact(
        recording_id=recording.id,
        tag_path=tag_path,
        tag_value=tag_value,
        prompt_version=recording.prompt_version or "manual",
        model_version="manual",
        input_hash=hashlib.md5(f"manual:{recording.id}:{tag_path}".encode()).hexdigest(),
        confidence=1.0,
        source="manual",
        computed_by=current_user.id,
        tenant_id=tenant_id,
    )
    await current_svc.upsert_current(fact, tenant_id)
    await stats_svc.apply_delta(
        tenant_id=tenant_id,
        store_id=recording.store_id,
        agent_name=recording.agent_name,
        tag_path=tag_path,
        old_value=old_value,
        new_value=tag_value,
    )

    return TagManualResponse(
        recording_id=recording.id,
        tag_path=tag_path,
        tag_value=tag_value,
        version=fact.version,
        source="manual",
        computed_by=current_user.id,
    )


@router.post(
    "/tags/recompute",
    response_model=Any,
    summary="Trigger batch recompute",
    dependencies=[Depends(require_admin())],
)
async def recompute(
    request: Request,
    body: RecomputeRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> Any:
    """Trigger batch tag recompute (prompt version switch).

    Role: admin only. Writes audit_log(action="tags.recompute").
    """
    tenant_id = get_tenant_id(request)
    factory = get_session_factory(request)
    bundle = get_adapters(request)
    file_index = get_file_index(request)

    svc = RecomputeService(factory, bundle, file_index)

    if body.dry_run:
        result = await svc.dry_run(
            tenant_id,
            body.prompt_version,
            body.tag_paths,
            body.recording_ids,
        )
        return RecomputeDryRunResponse(
            dry_run=True,
            affected_count=result["affected_count"],
            changed_count=result["changed_count"],
            unchanged_count=result["unchanged_count"],
            changes_preview=[TagDeltaPreview(**c) for c in result["changes_preview"]],
        )

    task = await svc.create_task(
        tenant_id,
        body.prompt_version,
        body.tag_paths,
        body.recording_ids,
    )

    # Execute inline (in production, this would be async via scheduler)
    await svc.execute_task(task.task_id)

    # ---- Audit log (fire-and-forget; Q2 quick win PIPL §14.3) ----
    await _write_audit(
        request,
        tenant_id=tenant_id,
        user_id=current_user.id,
        action="tags.recompute",
        target=f"task:{task.task_id}",
        after={
            "prompt_version": body.prompt_version,
            "total": task.total,
            "changed": task.changed,
        },
    )

    return RecomputeCreateResponse(
        dry_run=False,
        task_id=task.task_id,
        status=task.status,
        affected_count=task.total,
    )


@router.get(
    "/tags/recompute/{task_id}",
    response_model=RecomputeTaskResponse,
    summary="Get recompute task status",
)
async def get_recompute_task(
    task_id: str,
    request: Request,
    _user: AuthUser = Depends(require_admin()),
) -> RecomputeTaskResponse:
    """Get recompute task status by ID.

    Role: admin only.
    """
    tenant_id = get_tenant_id(request)
    factory = get_session_factory(request)
    bundle = get_adapters(request)
    file_index = get_file_index(request)

    svc = RecomputeService(factory, bundle, file_index)
    task = await svc.get_task_status(task_id, tenant_id)

    return RecomputeTaskResponse(
        task_id=task.task_id,
        status=task.status,
        prompt_version=task.prompt_version,
        total=task.total,
        processed=task.processed,
        changed=task.changed,
        cached_hits=task.cached_hits,
        llm_calls=task.llm_calls,
        started_at=task.started_at,
        finished_at=task.finished_at,
        error_message=task.error_message,
    )
