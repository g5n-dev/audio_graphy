"""Endpoints for durable, resumable reception automation."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from fastapi import APIRouter, Depends, Request

from audio_graphy.api.deps import get_current_user, get_session_factory
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.roles import require_write_access
from audio_graphy.auth.tenants import get_tenant_id
from audio_graphy.models.reception import ReceptionAutomationRun
from audio_graphy.schemas.reception_pipeline import (
    ReceptionAutomationRequest,
    ReceptionAutomationResponse,
    ReceptionAutomationStage,
    ReceptionAutomationStatus,
)
from audio_graphy.services.reception_pipeline import ReceptionAutomationPipeline
from audio_graphy.services.reception_tagging import ReceptionTaggingService
from audio_graphy.services.receptions import AudioAssembler, ReceptionService

router = APIRouter(tags=["reception automation"])


def _service(request: Request) -> ReceptionAutomationPipeline:
    session_factory = get_session_factory(request)
    assembler = cast(
        AudioAssembler | None,
        getattr(request.app.state, "audio_assembler", None),
    )
    reception_service = ReceptionService(
        session_factory,
        audio_root=Path(request.app.state.settings.working_dir),
        audio_assembler=assembler,
        audio_crypto=getattr(request.app.state, "audio_crypto", None),
    )
    return ReceptionAutomationPipeline(
        session_factory,
        reception_service=reception_service,
        tagging_service=ReceptionTaggingService(session_factory),
    )


def _response(run: ReceptionAutomationRun) -> ReceptionAutomationResponse:
    return ReceptionAutomationResponse(
        id=run.id,
        reception_id=run.reception_id,
        status=cast(ReceptionAutomationStatus, run.status),
        stage=cast(ReceptionAutomationStage, run.stage),
        attempt_count=run.attempt_count,
        checkpoints=run.checkpoints,
        segmentation_algorithm=run.segmentation_algorithm,
        tag_group_key=run.tag_group_key,
        tag_group_version=run.tag_group_version,
        target_labels=run.target_labels,
        tag_priority=run.tag_priority,
        last_error_code=run.last_error_code,
        last_error_message=run.last_error_message,
        created_at=run.created_at,
        updated_at=run.updated_at,
        finished_at=run.finished_at,
    )


@router.post(
    "/receptions/{reception_id}/automation/run",
    response_model=ReceptionAutomationResponse,
    summary="Run or resume the reception automation state machine",
    dependencies=[Depends(require_write_access())],
)
async def run_reception_automation(
    reception_id: int,
    body: ReceptionAutomationRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> ReceptionAutomationResponse:
    run = await _service(request).run(
        reception_id=reception_id,
        tenant_id=get_tenant_id(request),
        request=body,
        actor=f"user:{user.id}",
        raise_on_failure=False,
    )
    return _response(run)


@router.get(
    "/receptions/{reception_id}/automation",
    response_model=ReceptionAutomationResponse,
    summary="Read one reception automation checkpoint",
)
async def get_reception_automation(
    reception_id: int,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> ReceptionAutomationResponse:
    run = await _service(request).get(
        reception_id=reception_id,
        tenant_id=get_tenant_id(request),
        agent_user_id=user.id if user.role == "agent" else None,
    )
    return _response(run)


__all__ = ["router"]
