"""Durable, resumable automation for one accepted business reception."""

from __future__ import annotations

import asyncio
import contextlib
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.errors import APIError, ConflictError, NotFoundError
from audio_graphy.models.reception import (
    ProvenanceEvent,
    Reception,
    ReceptionAutomationRun,
)
from audio_graphy.schemas.reception_pipeline import ReceptionAutomationRequest
from audio_graphy.schemas.reception_tags import DeriveDialogueTagsRequest
from audio_graphy.schemas.receptions import (
    MergeMode,
    ReceptionMergeRequest,
    ReceptionSegmentRequest,
)
from audio_graphy.services.reception_tagging import ReceptionTaggingService
from audio_graphy.services.receptions import ReceptionService

_LEASE_TTL = timedelta(minutes=15)


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _checkpoint(
    *,
    status: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "at": datetime.now(UTC).isoformat(),
        "detail": detail or {},
    }


class ReceptionAutomationPipeline:
    """Run merge, segmentation, and tagging with durable checkpoints.

    Each stage owns its own transaction. A short database lease prevents two
    callers from processing the same reception concurrently, while completed
    stage checkpoints make retries safe after process crashes.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        reception_service: ReceptionService,
        tagging_service: ReceptionTaggingService,
        lease_ttl: timedelta = _LEASE_TTL,
        lease_heartbeat_seconds: float | None = None,
    ) -> None:
        lease_ttl_seconds = lease_ttl.total_seconds()
        if lease_ttl_seconds <= 0:
            raise ValueError("lease_ttl must be positive")
        heartbeat_seconds = (
            min(60.0, lease_ttl_seconds / 3)
            if lease_heartbeat_seconds is None
            else lease_heartbeat_seconds
        )
        if not 0 < heartbeat_seconds < lease_ttl_seconds:
            raise ValueError("lease heartbeat must be positive and shorter than lease_ttl")
        self._session_factory = session_factory
        self._reception_service = reception_service
        self._tagging_service = tagging_service
        self._lease_ttl = lease_ttl
        self._lease_heartbeat_seconds = heartbeat_seconds

    @staticmethod
    def _configuration_matches(
        run: ReceptionAutomationRun,
        request: ReceptionAutomationRequest,
    ) -> bool:
        return (
            run.segmentation_algorithm == request.segmentation_algorithm
            and run.tag_group_key == request.tag_group_key
            and run.tag_group_version == request.tag_group_version
            and list(run.target_labels) == list(request.target_labels)
            and run.tag_priority == request.tag_priority
        )

    async def _claim(
        self,
        *,
        reception_id: int,
        tenant_id: str,
        request: ReceptionAutomationRequest,
    ) -> tuple[ReceptionAutomationRun, str | None]:
        now = datetime.now(UTC)
        token = uuid4().hex
        async with self._session_factory() as session, session.begin():
            reception = (
                await session.execute(
                    select(Reception)
                    .where(
                        Reception.id == reception_id,
                        Reception.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if reception is None:
                raise NotFoundError(
                    "Reception not found",
                    code="RECEPTION_NOT_FOUND",
                    detail={"reception_id": reception_id},
                )
            if reception.status in {"archived", "split"}:
                raise ConflictError(
                    "Reception cannot enter automation in its current state",
                    code="RECEPTION_AUTOMATION_STATE_INVALID",
                    detail={
                        "reception_id": reception_id,
                        "status": reception.status,
                    },
                )

            run = (
                await session.execute(
                    select(ReceptionAutomationRun)
                    .where(
                        ReceptionAutomationRun.reception_id == reception_id,
                        ReceptionAutomationRun.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if run is None:
                run = ReceptionAutomationRun(
                    tenant_id=tenant_id,
                    reception_id=reception_id,
                    status="pending",
                    stage="merge",
                    attempt_count=0,
                    checkpoints={},
                    segmentation_algorithm=request.segmentation_algorithm,
                    tag_group_key=request.tag_group_key,
                    tag_group_version=request.tag_group_version,
                    target_labels=list(request.target_labels),
                    tag_priority=request.tag_priority,
                )
                session.add(run)
                await session.flush()
            elif not self._configuration_matches(run, request):
                raise ConflictError(
                    "Automation configuration is immutable after the first attempt",
                    code="RECEPTION_AUTOMATION_CONFIG_CONFLICT",
                    detail={
                        "reception_id": reception_id,
                        "run_id": run.id,
                    },
                )

            if run.status == "ready":
                return run, None

            lease_expires_at = _aware_utc(run.lease_expires_at)
            if (
                run.status == "running"
                and run.lease_token is not None
                and lease_expires_at is not None
                and lease_expires_at > now
            ):
                raise ConflictError(
                    "Reception automation is already running",
                    code="RECEPTION_AUTOMATION_ALREADY_RUNNING",
                    detail={
                        "reception_id": reception_id,
                        "stage": run.stage,
                        "lease_expires_at": lease_expires_at.isoformat(),
                    },
                )

            run.status = "running"
            run.attempt_count += 1
            run.lease_token = token
            run.lease_expires_at = now + self._lease_ttl
            run.last_error_code = None
            run.last_error_message = None
            run.finished_at = None
            if reception.status != "processing":
                reception.status = "processing"
        return run, token

    async def _load_run(
        self,
        *,
        reception_id: int,
        tenant_id: str,
    ) -> ReceptionAutomationRun:
        async with self._session_factory() as session:
            run = (
                await session.execute(
                    select(ReceptionAutomationRun).where(
                        ReceptionAutomationRun.reception_id == reception_id,
                        ReceptionAutomationRun.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if run is None:
                raise NotFoundError(
                    "Reception automation run not found",
                    code="RECEPTION_AUTOMATION_NOT_FOUND",
                    detail={"reception_id": reception_id},
                )
            return run

    async def get(
        self,
        *,
        reception_id: int,
        tenant_id: str,
        agent_user_id: int | None = None,
    ) -> ReceptionAutomationRun:
        """Load pipeline state after authorizing the reception slice."""
        async with self._session_factory() as session:
            reception_query = select(Reception.id).where(
                Reception.id == reception_id,
                Reception.tenant_id == tenant_id,
            )
            if agent_user_id is not None:
                reception_query = reception_query.where(
                    Reception.agent_user_id == agent_user_id,
                )
            if (await session.execute(reception_query)).scalar_one_or_none() is None:
                raise NotFoundError(
                    "Reception not found",
                    code="RECEPTION_NOT_FOUND",
                    detail={"reception_id": reception_id},
                )
        return await self._load_run(
            reception_id=reception_id,
            tenant_id=tenant_id,
        )

    async def _mark_checkpoint(
        self,
        *,
        reception_id: int,
        tenant_id: str,
        lease_token: str,
        stage: str,
        next_stage: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            run = (
                await session.execute(
                    select(ReceptionAutomationRun)
                    .where(
                        ReceptionAutomationRun.reception_id == reception_id,
                        ReceptionAutomationRun.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one()
            if run.status != "running" or run.lease_token != lease_token:
                raise ConflictError(
                    "Reception automation lease was lost",
                    code="RECEPTION_AUTOMATION_LEASE_LOST",
                    detail={"reception_id": reception_id, "stage": stage},
                )
            checkpoints = deepcopy(run.checkpoints)
            checkpoints[stage] = _checkpoint(
                status="completed",
                detail=detail,
            )
            run.checkpoints = checkpoints
            run.stage = next_stage
            run.lease_expires_at = datetime.now(UTC) + self._lease_ttl

    async def _renew_lease(
        self,
        *,
        reception_id: int,
        tenant_id: str,
        lease_token: str,
    ) -> None:
        """Conditionally extend only the currently owned running lease."""

        new_expiry = datetime.now(UTC) + self._lease_ttl
        async with self._session_factory() as session, session.begin():
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(ReceptionAutomationRun)
                    .where(
                        ReceptionAutomationRun.reception_id == reception_id,
                        ReceptionAutomationRun.tenant_id == tenant_id,
                        ReceptionAutomationRun.status == "running",
                        ReceptionAutomationRun.lease_token == lease_token,
                    )
                    .values(lease_expires_at=new_expiry)
                ),
            )
            if result.rowcount != 1:
                raise ConflictError(
                    "Reception automation lease was lost",
                    code="RECEPTION_AUTOMATION_LEASE_LOST",
                    detail={"reception_id": reception_id},
                )

    async def _lease_heartbeat(
        self,
        *,
        reception_id: int,
        tenant_id: str,
        lease_token: str,
        stop: asyncio.Event,
    ) -> None:
        """Renew until stopped; token loss propagates to cancel the active stage."""

        while True:
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=self._lease_heartbeat_seconds,
                )
            except TimeoutError:
                try:
                    await self._renew_lease(
                        reception_id=reception_id,
                        tenant_id=tenant_id,
                        lease_token=lease_token,
                    )
                except ConflictError:
                    # Finalization clears the token in a separate transaction.
                    # If that won the race after ``stop`` was set, a zero-row
                    # heartbeat update is expected rather than lease loss.
                    if stop.is_set():
                        return
                    raise
            else:
                return

    async def _mark_failed(
        self,
        *,
        reception_id: int,
        tenant_id: str,
        lease_token: str,
        error: Exception,
    ) -> ReceptionAutomationRun:
        code = error.code if isinstance(error, APIError) else "RECEPTION_AUTOMATION_FAILED"
        message = error.message if isinstance(error, APIError) else str(error)
        async with self._session_factory() as session, session.begin():
            run = (
                await session.execute(
                    select(ReceptionAutomationRun)
                    .where(
                        ReceptionAutomationRun.reception_id == reception_id,
                        ReceptionAutomationRun.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one()
            if run.lease_token == lease_token:
                checkpoints = deepcopy(run.checkpoints)
                checkpoints[run.stage] = _checkpoint(
                    status="failed",
                    detail={"code": code, "message": message[:500]},
                )
                run.checkpoints = checkpoints
                run.status = "failed"
                run.last_error_code = code
                run.last_error_message = message[:2_000]
                run.lease_token = None
                run.lease_expires_at = None
                run.finished_at = datetime.now(UTC)
                reception = (
                    await session.execute(
                        select(Reception)
                        .where(
                            Reception.id == reception_id,
                            Reception.tenant_id == tenant_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one()
                if reception.status == "processing":
                    reception.status = "needs_review"
            return run

    async def _mark_ready(
        self,
        *,
        reception_id: int,
        tenant_id: str,
        lease_token: str,
        actor: str,
    ) -> ReceptionAutomationRun:
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            run = (
                await session.execute(
                    select(ReceptionAutomationRun)
                    .where(
                        ReceptionAutomationRun.reception_id == reception_id,
                        ReceptionAutomationRun.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one()
            if run.status != "running" or run.lease_token != lease_token:
                raise ConflictError(
                    "Reception automation lease was lost",
                    code="RECEPTION_AUTOMATION_LEASE_LOST",
                    detail={"reception_id": reception_id, "stage": run.stage},
                )
            reception = (
                await session.execute(
                    select(Reception)
                    .where(
                        Reception.id == reception_id,
                        Reception.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one()
            previous_status = reception.status
            reception.status = "ready"
            reception.version += 1
            run.status = "ready"
            run.stage = "ready"
            run.lease_token = None
            run.lease_expires_at = None
            run.last_error_code = None
            run.last_error_message = None
            run.finished_at = now
            session.add(
                ProvenanceEvent(
                    tenant_id=tenant_id,
                    reception_id=reception_id,
                    object_type="reception_automation_run",
                    object_ref=str(run.id),
                    event_type="derived",
                    actor=actor,
                    algorithm_version=run.segmentation_algorithm,
                    parent_refs=[
                        {
                            "type": "reception",
                            "id": reception_id,
                            "version": reception.version - 1,
                        }
                    ],
                    evidence_refs=[],
                    payload={
                        "operation": "automation_ready",
                        "previous_status": previous_status,
                        "attempt_count": run.attempt_count,
                        "checkpoints": deepcopy(run.checkpoints),
                        "tag_group": (f"{run.tag_group_key}@{run.tag_group_version}"),
                        "version": reception.version,
                    },
                    occurred_at=now,
                )
            )
            return run

    async def _advance_claimed(
        self,
        *,
        reception_id: int,
        tenant_id: str,
        request: ReceptionAutomationRequest,
        actor: str,
        lease_token: str,
        heartbeat_stop: asyncio.Event,
    ) -> ReceptionAutomationRun:
        run = await self._load_run(
            reception_id=reception_id,
            tenant_id=tenant_id,
        )
        if run.stage == "merge":
            workspace = await self._reception_service.get_workspace(
                reception_id,
                tenant_id,
            )
            merge_detail: dict[str, Any] = {
                "mode": workspace.reception.merge_mode,
                "recording_count": len(workspace.recordings),
            }
            if (
                workspace.reception.merge_mode in {"physical", "both"}
                and workspace.reception.merged_audio_path is None
            ):
                workspace = await self._reception_service.merge_recordings(
                    reception_id,
                    tenant_id,
                    ReceptionMergeRequest(
                        recording_ids=[
                            mapping.recording_id for mapping, _recording in workspace.recordings
                        ],
                        mode=cast(MergeMode, workspace.reception.merge_mode),
                        expected_version=workspace.reception.version,
                    ),
                    actor=actor,
                )
                merge_detail["physical_audio"] = "published"
            else:
                merge_detail["physical_audio"] = (
                    "already_published"
                    if workspace.reception.merged_audio_path
                    else "not_requested"
                )
            await self._mark_checkpoint(
                reception_id=reception_id,
                tenant_id=tenant_id,
                lease_token=lease_token,
                stage="merge",
                next_stage="segmentation",
                detail=merge_detail,
            )
            run = await self._load_run(
                reception_id=reception_id,
                tenant_id=tenant_id,
            )

        if run.stage == "segmentation":
            workspace = await self._reception_service.get_workspace(
                reception_id,
                tenant_id,
            )
            if workspace.dialogue_units:
                segmentation_detail = {
                    "dialogue_unit_count": len(workspace.dialogue_units),
                    "reused": True,
                }
            else:
                workspace = await self._reception_service.segment_reception(
                    reception_id,
                    tenant_id,
                    ReceptionSegmentRequest(
                        expected_version=workspace.reception.version,
                        replace_auto=False,
                        algorithm_version=request.segmentation_algorithm,
                    ),
                    actor=actor,
                )
                segmentation_detail = {
                    "dialogue_unit_count": len(workspace.dialogue_units),
                    "reused": False,
                }
            await self._mark_checkpoint(
                reception_id=reception_id,
                tenant_id=tenant_id,
                lease_token=lease_token,
                stage="segmentation",
                next_stage="tagging",
                detail=segmentation_detail,
            )
            run = await self._load_run(
                reception_id=reception_id,
                tenant_id=tenant_id,
            )

        if run.stage == "tagging":
            tagging_result = await self._tagging_service.derive(
                reception_id=reception_id,
                tenant_id=tenant_id,
                request=DeriveDialogueTagsRequest(
                    group_key=request.tag_group_key,
                    group_version=request.tag_group_version,
                    target_labels=request.target_labels,
                    priority=request.tag_priority,
                ),
                actor=actor,
            )
            await self._mark_checkpoint(
                reception_id=reception_id,
                tenant_id=tenant_id,
                lease_token=lease_token,
                stage="tagging",
                next_stage="ready",
                detail={
                    "assignment_count": len(tagging_result.assignments),
                    "missing_count": len(tagging_result.missing),
                    "no_op": tagging_result.no_op,
                },
            )

        heartbeat_stop.set()
        return await self._mark_ready(
            reception_id=reception_id,
            tenant_id=tenant_id,
            lease_token=lease_token,
            actor=actor,
        )

    async def _advance_with_heartbeat(
        self,
        *,
        reception_id: int,
        tenant_id: str,
        request: ReceptionAutomationRequest,
        actor: str,
        lease_token: str,
    ) -> ReceptionAutomationRun:
        stop = asyncio.Event()
        advance_task = asyncio.create_task(
            self._advance_claimed(
                reception_id=reception_id,
                tenant_id=tenant_id,
                request=request,
                actor=actor,
                lease_token=lease_token,
                heartbeat_stop=stop,
            )
        )
        heartbeat_task = asyncio.create_task(
            self._lease_heartbeat(
                reception_id=reception_id,
                tenant_id=tenant_id,
                lease_token=lease_token,
                stop=stop,
            )
        )
        try:
            done, _pending = await asyncio.wait(
                {advance_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                try:
                    await heartbeat_task
                except Exception:
                    advance_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await advance_task
                    raise
                return await advance_task

            stop.set()
            await heartbeat_task
            return await advance_task
        finally:
            stop.set()
            for task in (advance_task, heartbeat_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                advance_task,
                heartbeat_task,
                return_exceptions=True,
            )

    async def run(
        self,
        *,
        reception_id: int,
        tenant_id: str,
        request: ReceptionAutomationRequest,
        actor: str,
        raise_on_failure: bool = True,
    ) -> ReceptionAutomationRun:
        """Claim and advance a workflow, resuming from its last checkpoint."""
        claimed, lease_token = await self._claim(
            reception_id=reception_id,
            tenant_id=tenant_id,
            request=request,
        )
        if lease_token is None:
            return claimed

        try:
            return await self._advance_with_heartbeat(
                reception_id=reception_id,
                tenant_id=tenant_id,
                request=request,
                actor=actor,
                lease_token=lease_token,
            )
        except Exception as exc:
            lease_lost = (
                isinstance(exc, ConflictError) and exc.code == "RECEPTION_AUTOMATION_LEASE_LOST"
            )
            failed = await self._mark_failed(
                reception_id=reception_id,
                tenant_id=tenant_id,
                lease_token=lease_token,
                error=exc,
            )
            if lease_lost or raise_on_failure:
                raise
            return failed


__all__ = ["ReceptionAutomationPipeline"]
