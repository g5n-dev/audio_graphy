"""Reception listening-workspace, manual-edit, and provenance endpoints."""

from __future__ import annotations

import asyncio
import os
import stat as stat_module
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from audio_graphy.api.deps import get_current_user, get_session_factory
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.roles import require_write_access
from audio_graphy.auth.tenants import get_tenant_id
from audio_graphy.core.reception_merge import ReceptionProposal
from audio_graphy.errors import APIError, NotFoundError
from audio_graphy.models.reception import (
    DialogueStateTransition,
    DialogueTagAssignment,
    DialogueUnit,
    ProvenanceEvent,
    Reception,
    ReceptionRecording,
)
from audio_graphy.models.recording import Recording
from audio_graphy.schemas.receptions import (
    DEFAULT_RECEPTION_TRACE_PAGE_SIZE,
    DEFAULT_WORKSPACE_WINDOW_SIZE_SEC,
    MAX_RECEPTION_TRACE_PAGE_SIZE,
    MAX_WORKSPACE_WINDOW_SIZE_SEC,
    DecisionSource,
    DialogueEditResponse,
    DialogueStateTransitionResponse,
    DialogueTagAssignmentResponse,
    DialogueUnitMergeRequest,
    DialogueUnitResponse,
    DialogueUnitSplitRequest,
    MergeMode,
    ProvenanceEventResponse,
    ProvenanceListResponse,
    ReceptionAutomaticProposalResponse,
    ReceptionCreate,
    ReceptionDiscoveryRequest,
    ReceptionDiscoveryResponse,
    ReceptionListResponse,
    ReceptionMergeProposalItemResponse,
    ReceptionMergeProposalRequest,
    ReceptionMergeProposalResponse,
    ReceptionMergeReasonResponse,
    ReceptionMergeRequest,
    ReceptionMetadataResponse,
    ReceptionProposalAcceptRequest,
    ReceptionRecordingResponse,
    ReceptionResponse,
    ReceptionScenario,
    ReceptionSegmentRequest,
    ReceptionSplitAcceptanceResponse,
    ReceptionStatus,
    ReceptionWorkspaceResponse,
    ReceptionWorkspaceWindow,
    StateTransitionListResponse,
    TranscriptItemResponse,
    WorkspaceCollectionWindow,
)
from audio_graphy.services.reception_automation import (
    AutomaticReceptionProposal,
    ReceptionAutomationService,
    ReceptionSplitAcceptance,
)
from audio_graphy.services.receptions import (
    AudioAssembler,
    ReceptionAudioAsset,
    ReceptionService,
    ReceptionWorkspace,
    create_playback_grant,
)

router = APIRouter(tags=["receptions"])
_STREAM_CHUNK_SIZE = 64 * 1024
_PRIVATE_PATH_KEYS = {
    "merged_audio_path",
    "output_path",
    "path",
    "source_path",
}


def _service(request: Request) -> ReceptionService:
    settings = request.app.state.settings
    assembler = cast(
        AudioAssembler | None,
        getattr(request.app.state, "audio_assembler", None),
    )
    return ReceptionService(
        get_session_factory(request),
        audio_root=Path(settings.working_dir),
        audio_assembler=assembler,
        audio_crypto=getattr(request.app.state, "audio_crypto", None),
    )


def _agent_user_id(user: AuthUser) -> int | None:
    return user.id if user.role == "agent" else None


def _public_json(value: Any) -> Any:
    """Recursively remove persisted filesystem paths from public contracts."""
    if isinstance(value, dict):
        return {
            str(key): _public_json(item)
            for key, item in value.items()
            if str(key) not in _PRIVATE_PATH_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_public_json(item) for item in value]
    return value


def _playback_url(
    request: Request,
    user: AuthUser,
    path: str,
) -> str:
    grant = create_playback_grant(
        secret=str(request.app.state.settings.jwt_secret),
        subject_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
        path=path,
    )
    return f"{path}?{urlencode({'playback_grant': grant})}"


def _reception_response(
    reception: Reception,
    request: Request,
    user: AuthUser,
) -> ReceptionMetadataResponse:
    audio_path = f"/api/v1/receptions/{reception.id}/audio"
    return ReceptionMetadataResponse(
        id=reception.id,
        tenant_id=str(reception.tenant_id),
        external_session_id=reception.external_session_id,
        scenario=cast(ReceptionScenario, reception.scenario),
        store_id=reception.store_id,
        agent_name=reception.agent_name,
        agent_user_id=reception.agent_user_id,
        customer_hash=reception.customer_hash,
        status=cast(ReceptionStatus, reception.status),
        merge_mode=cast(MergeMode, reception.merge_mode),
        merge_confidence=reception.merge_confidence,
        started_at=reception.started_at,
        ended_at=reception.ended_at,
        audio_url=(
            _playback_url(request, user, audio_path)
            if reception.merged_audio_path is not None
            else None
        ),
        version=reception.version,
        created_at=reception.created_at,
        updated_at=reception.updated_at,
    )


def _recording_response(
    mapping: ReceptionRecording,
    recording: Recording,
    request: Request,
    user: AuthUser,
) -> ReceptionRecordingResponse:
    audio_path = (
        f"/api/v1/receptions/{mapping.reception_id}/recordings/{mapping.recording_id}/audio"
    )
    return ReceptionRecordingResponse(
        id=mapping.id,
        recording_id=mapping.recording_id,
        sequence_no=mapping.sequence_no,
        timeline_start_sec=mapping.timeline_start_sec,
        timeline_end_sec=mapping.timeline_end_sec,
        source_start_sec=mapping.source_start_sec,
        source_end_sec=mapping.source_end_sec,
        gap_before_sec=mapping.gap_before_sec,
        decision_source=cast(DecisionSource, mapping.decision_source),
        merge_confidence=mapping.merge_confidence,
        merge_reasons=cast(
            dict[str, Any],
            _public_json(mapping.merge_reasons),
        ),
        source_recorded_at=recording.recorded_at,
        audio_url=_playback_url(request, user, audio_path),
    )


def _tag_response(tag: DialogueTagAssignment) -> DialogueTagAssignmentResponse:
    return DialogueTagAssignmentResponse(
        id=tag.id,
        reception_id=tag.reception_id,
        dialogue_unit_id=tag.dialogue_unit_id,
        group_key=tag.group_key,
        group_version=tag.group_version,
        label_key=tag.label_key,
        label_value=tag.label_value,
        confidence=tag.confidence,
        source=tag.source,
        priority=tag.priority,
        evidence_refs=cast(list[Any], _public_json(tag.evidence_refs)),
        model_run_id=tag.model_run_id,
        is_current=tag.is_current,
        assigned_at=tag.assigned_at,
    )


def _unit_response(
    unit: DialogueUnit,
    tags: list[DialogueTagAssignment],
) -> DialogueUnitResponse:
    return DialogueUnitResponse(
        id=unit.id,
        source_recording_id=unit.source_recording_id,
        unit_index=unit.unit_index,
        version=unit.version,
        start_sec=unit.start_sec,
        end_sec=unit.end_sec,
        topic=unit.topic,
        business_stage=unit.business_stage,
        summary=unit.summary,
        boundary_confidence=unit.boundary_confidence,
        boundary_reasons=cast(
            list[Any],
            _public_json(unit.boundary_reasons),
        ),
        segment_refs=cast(list[Any], _public_json(unit.segment_refs)),
        speaker_refs=cast(list[Any], _public_json(unit.speaker_refs)),
        edit_status=unit.edit_status,
        tag_assignments=[_tag_response(tag) for tag in tags],
    )


def _transition_response(
    transition: DialogueStateTransition,
) -> DialogueStateTransitionResponse:
    return DialogueStateTransitionResponse(
        id=transition.id,
        dialogue_unit_id=transition.dialogue_unit_id,
        sequence_no=transition.sequence_no,
        from_state=transition.from_state,
        to_state=transition.to_state,
        trigger=transition.trigger,
        confidence=transition.confidence,
        evidence_refs=cast(
            list[Any],
            _public_json(transition.evidence_refs),
        ),
        algorithm_version=transition.algorithm_version,
        created_at=transition.created_at,
    )


def _provenance_response(event: ProvenanceEvent) -> ProvenanceEventResponse:
    return ProvenanceEventResponse(
        id=event.id,
        reception_id=event.reception_id,
        object_type=event.object_type,
        object_ref=event.object_ref,
        event_type=event.event_type,
        actor=event.actor,
        algorithm_version=event.algorithm_version,
        parent_refs=cast(list[Any], _public_json(event.parent_refs)),
        evidence_refs=cast(list[Any], _public_json(event.evidence_refs)),
        payload=cast(dict[str, Any], _public_json(event.payload)),
        occurred_at=event.occurred_at,
    )


def _merge_proposal_response(
    proposal: ReceptionProposal,
) -> ReceptionMergeProposalItemResponse:
    return ReceptionMergeProposalItemResponse(
        recording_ids=[int(recording_id) for recording_id in proposal.recording_ids],
        decision=proposal.decision,
        confidence=proposal.confidence,
        reasons=[
            ReceptionMergeReasonResponse(
                code=reason.code,
                contribution=reason.contribution,
                detail=reason.detail,
                hard_constraint=reason.hard_constraint,
            )
            for reason in proposal.reasons
        ],
        manual_override=proposal.manual_override,
    )


def _workspace_response(
    workspace: ReceptionWorkspace,
    request: Request,
    user: AuthUser,
) -> ReceptionWorkspaceResponse:
    if workspace.window is None:
        raise RuntimeError("public workspace responses require a bounded time window")
    window = workspace.window

    def collection_window(
        item: Any,
    ) -> WorkspaceCollectionWindow:
        return WorkspaceCollectionWindow(
            total=item.total,
            returned=item.returned,
            limit=item.limit,
            truncated=item.truncated,
        )

    return ReceptionWorkspaceResponse(
        reception=_reception_response(workspace.reception, request, user),
        recordings=[
            _recording_response(mapping, recording, request, user)
            for mapping, recording in workspace.recordings
        ],
        dialogue_units=[
            _unit_response(
                unit,
                workspace.tag_assignments_by_unit.get(unit.id, []),
            )
            for unit in workspace.dialogue_units
        ],
        state_transitions=[
            _transition_response(transition) for transition in workspace.state_transitions
        ],
        tag_assignments=[_tag_response(tag) for tag in workspace.tag_assignments],
        transcript_items=[
            TranscriptItemResponse(
                segment_id=item.segment_id,
                recording_id=item.recording_id,
                segment_index=item.segment_index,
                source_start_sec=item.source_start_sec,
                source_end_sec=item.source_end_sec,
                timeline_start_sec=item.timeline_start_sec,
                timeline_end_sec=item.timeline_end_sec,
                speaker=item.speaker,
                text=item.text,
                vad_confidence=item.vad_confidence,
            )
            for item in workspace.transcript_items
        ],
        provenance_events=[_provenance_response(event) for event in workspace.provenance_events],
        window=ReceptionWorkspaceWindow(
            start_sec=window.start_sec,
            end_sec=window.end_sec,
            size_sec=window.size_sec,
            reception_duration_sec=window.reception_duration_sec,
            truncated=window.truncated,
            has_previous=window.has_previous,
            has_next=window.has_next,
            previous_start_sec=window.previous_start_sec,
            next_start_sec=window.next_start_sec,
            total_dialogue_units=window.total_dialogue_units,
            protected_dialogue_units=window.protected_dialogue_units,
            dialogue_units=collection_window(window.dialogue_units),
            tag_assignments=collection_window(window.tag_assignments),
            state_transitions=collection_window(window.state_transitions),
            transcript_items=collection_window(window.transcript_items),
            provenance_events=collection_window(window.provenance_events),
        ),
    )


def _mutation_reception_response(
    workspace: ReceptionWorkspace,
    request: Request,
    user: AuthUser,
) -> ReceptionResponse:
    metadata = _reception_response(workspace.reception, request, user)
    return ReceptionResponse(
        **metadata.model_dump(),
        recordings=[
            _recording_response(mapping, recording, request, user)
            for mapping, recording in workspace.recordings
        ],
    )


def _edit_response(workspace: ReceptionWorkspace) -> DialogueEditResponse:
    return DialogueEditResponse(
        reception_id=workspace.reception.id,
        reception_version=workspace.reception.version,
        dialogue_units=[
            _unit_response(
                unit,
                workspace.tag_assignments_by_unit.get(unit.id, []),
            )
            for unit in workspace.dialogue_units
        ],
    )


def _parse_byte_range(range_header: str | None, size: int) -> tuple[int, int, bool]:
    """Parse one RFC 7233 byte range; multiple ranges are deliberately unsupported."""
    if range_header is None:
        return 0, size - 1, False
    if not range_header.startswith("bytes=") or "," in range_header:
        raise ValueError("unsupported byte range")
    spec = range_header.removeprefix("bytes=").strip()
    if "-" not in spec:
        raise ValueError("invalid byte range")
    start_text, end_text = spec.split("-", 1)
    try:
        if not start_text:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                raise ValueError("invalid suffix range")
            start = max(0, size - suffix_length)
            end = size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
            if start < 0 or start >= size or end < start:
                raise ValueError("invalid byte range")
            end = min(end, size - 1)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid byte range") from exc
    return start, end, True


async def _stream_file_descriptor(
    file_descriptor: int,
    *,
    start: int,
    length: int,
) -> AsyncIterator[bytes]:
    try:
        await asyncio.to_thread(os.lseek, file_descriptor, start, os.SEEK_SET)
        remaining = length
        while remaining > 0:
            chunk = await asyncio.to_thread(
                os.read,
                file_descriptor,
                min(_STREAM_CHUNK_SIZE, remaining),
            )
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        os.close(file_descriptor)


def _open_audio_descriptor(asset: ReceptionAudioAsset) -> tuple[int, int]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_descriptor = os.open(asset.path, flags)
        file_stat = os.fstat(file_descriptor)
    except OSError as exc:
        raise NotFoundError("Audio asset not found", code="AUDIO_NOT_FOUND") from exc
    if not stat_module.S_ISREG(file_stat.st_mode) or file_stat.st_size <= 0:
        os.close(file_descriptor)
        raise NotFoundError("Audio asset not found", code="AUDIO_NOT_FOUND")
    if asset.delete_after_open:
        try:
            # The already-open descriptor remains readable on our POSIX
            # deployment while no plaintext pathname remains on disk.
            os.unlink(asset.path)
        except OSError as exc:
            os.close(file_descriptor)
            raise APIError(
                "Private audio cleanup failed",
                code="AUDIO_PRIVATE_CLEANUP_FAILED",
                status_code=503,
            ) from exc
    return file_descriptor, file_stat.st_size


def _audio_response(
    request: Request,
    asset: ReceptionAudioAsset,
) -> Response:
    file_descriptor, size = _open_audio_descriptor(asset)
    try:
        start, end, is_partial = _parse_byte_range(
            request.headers.get("range"),
            size,
        )
    except ValueError:
        os.close(file_descriptor)
        return Response(
            status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Range": f"bytes */{size}",
                "Cache-Control": "private, no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    length = end - start + 1
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Cache-Control": "private, no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }
    if is_partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    response_status = status.HTTP_206_PARTIAL_CONTENT if is_partial else status.HTTP_200_OK
    if request.method == "HEAD":
        os.close(file_descriptor)
        return Response(
            status_code=response_status,
            media_type=asset.media_type,
            headers=headers,
        )
    return StreamingResponse(
        _stream_file_descriptor(
            file_descriptor,
            start=start,
            length=length,
        ),
        status_code=response_status,
        media_type=asset.media_type,
        headers=headers,
    )


@router.post(
    "/receptions",
    response_model=ReceptionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a reception with logical source mappings",
    dependencies=[Depends(require_write_access())],
)
async def create_reception(
    body: ReceptionCreate,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> ReceptionResponse:
    tenant_id = get_tenant_id(request)
    workspace = await _service(request).create_reception(
        tenant_id,
        body,
        actor=f"user:{user.id}",
    )
    return _mutation_reception_response(workspace, request, user)


@router.post(
    "/receptions/proposals",
    response_model=ReceptionMergeProposalResponse,
    summary="Explain candidate recording merge groups without writing",
    dependencies=[Depends(require_write_access())],
)
async def propose_reception_groups(
    body: ReceptionMergeProposalRequest,
    request: Request,
) -> ReceptionMergeProposalResponse:
    result = await _service(request).propose_reception_groups(
        get_tenant_id(request),
        body,
    )
    return ReceptionMergeProposalResponse(
        recording_ids=result.recording_ids,
        proposals=[_merge_proposal_response(proposal) for proposal in result.proposals],
        groups=[_merge_proposal_response(group) for group in result.groups],
    )


@router.post(
    "/receptions/{reception_id}/merge",
    response_model=ReceptionResponse,
    summary="Append or reorder reception recordings",
    dependencies=[Depends(require_write_access())],
)
async def merge_reception_recordings(
    reception_id: int,
    body: ReceptionMergeRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> ReceptionResponse:
    workspace = await _service(request).merge_recordings(
        reception_id,
        get_tenant_id(request),
        body,
        actor=f"user:{user.id}",
    )
    return _mutation_reception_response(workspace, request, user)


@router.get(
    "/receptions/{reception_id}/workspace",
    response_model=ReceptionWorkspaceResponse,
    summary="Load the reception listening workspace",
)
async def get_reception_workspace(
    reception_id: int,
    request: Request,
    window_start_sec: float = Query(default=0.0, ge=0),
    window_size_sec: float = Query(
        default=DEFAULT_WORKSPACE_WINDOW_SIZE_SEC,
        gt=0,
        le=MAX_WORKSPACE_WINDOW_SIZE_SEC,
    ),
    user: AuthUser = Depends(get_current_user),
) -> ReceptionWorkspaceResponse:
    workspace = await _service(request).get_workspace_window(
        reception_id,
        get_tenant_id(request),
        window_start_sec=window_start_sec,
        window_size_sec=window_size_sec,
        agent_user_id=_agent_user_id(user),
    )
    return _workspace_response(workspace, request, user)


@router.post(
    "/receptions/{reception_id}/segment",
    response_model=DialogueEditResponse,
    summary="Derive dialogue units from persisted transcript segments",
    dependencies=[Depends(require_write_access())],
)
async def segment_reception(
    reception_id: int,
    body: ReceptionSegmentRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> DialogueEditResponse:
    workspace = await _service(request).segment_reception(
        reception_id,
        get_tenant_id(request),
        body,
        actor=f"user:{user.id}",
    )
    return _edit_response(workspace)


@router.api_route(
    "/receptions/{reception_id}/audio",
    methods=["GET", "HEAD"],
    summary="Stream physically merged reception audio with Range support",
)
async def stream_reception_audio(
    reception_id: int,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> Response:
    asset = await _service(request).get_audio_asset(
        reception_id,
        get_tenant_id(request),
        agent_user_id=_agent_user_id(user),
    )
    return _audio_response(request, asset)


@router.api_route(
    "/receptions/{reception_id}/recordings/{recording_id}/audio",
    methods=["GET", "HEAD"],
    summary="Stream one reception source recording with Range support",
)
async def stream_reception_recording_audio(
    reception_id: int,
    recording_id: int,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> Response:
    asset = await _service(request).get_audio_asset(
        reception_id,
        get_tenant_id(request),
        recording_id=recording_id,
        agent_user_id=_agent_user_id(user),
    )
    return _audio_response(request, asset)


@router.post(
    "/receptions/{reception_id}/dialogue-units/{unit_id}/split",
    response_model=DialogueEditResponse,
    summary="Manually split a dialogue unit",
    dependencies=[Depends(require_write_access())],
)
async def split_dialogue_unit(
    reception_id: int,
    unit_id: int,
    body: DialogueUnitSplitRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> DialogueEditResponse:
    workspace = await _service(request).split_dialogue_unit(
        reception_id,
        unit_id,
        get_tenant_id(request),
        body,
        actor=f"user:{user.id}",
    )
    return _edit_response(workspace)


@router.post(
    "/receptions/{reception_id}/dialogue-units/{unit_id}/merge",
    response_model=DialogueEditResponse,
    summary="Manually merge adjacent dialogue units",
    dependencies=[Depends(require_write_access())],
)
async def merge_dialogue_units(
    reception_id: int,
    unit_id: int,
    body: DialogueUnitMergeRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> DialogueEditResponse:
    workspace = await _service(request).merge_dialogue_units(
        reception_id,
        unit_id,
        get_tenant_id(request),
        body,
        actor=f"user:{user.id}",
    )
    return _edit_response(workspace)


@router.get(
    "/receptions/{reception_id}/state-transitions",
    response_model=StateTransitionListResponse,
    summary="Get dialogue business-state transitions",
)
async def get_state_transitions(
    reception_id: int,
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(
        default=DEFAULT_RECEPTION_TRACE_PAGE_SIZE,
        ge=1,
        le=MAX_RECEPTION_TRACE_PAGE_SIZE,
    ),
    user: AuthUser = Depends(get_current_user),
) -> StateTransitionListResponse:
    items, total = await _service(request).get_state_transitions(
        reception_id,
        get_tenant_id(request),
        page=page,
        page_size=page_size,
        agent_user_id=_agent_user_id(user),
    )
    return StateTransitionListResponse(
        reception_id=reception_id,
        items=[_transition_response(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
        truncated=len(items) < total,
    )


@router.get(
    "/provenance/{object_type}/{object_ref}",
    response_model=ProvenanceListResponse,
    summary="Get an object's chronological provenance chain",
)
async def get_provenance(
    object_type: str,
    object_ref: str,
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(
        default=DEFAULT_RECEPTION_TRACE_PAGE_SIZE,
        ge=1,
        le=MAX_RECEPTION_TRACE_PAGE_SIZE,
    ),
    user: AuthUser = Depends(get_current_user),
) -> ProvenanceListResponse:
    events, total = await _service(request).get_provenance(
        object_type,
        object_ref,
        get_tenant_id(request),
        page=page,
        page_size=page_size,
        agent_user_id=_agent_user_id(user),
    )
    return ProvenanceListResponse(
        object_type=object_type,
        object_ref=object_ref,
        items=[_provenance_response(event) for event in events],
        total=total,
        page=page,
        page_size=page_size,
        truncated=len(events) < total,
    )


def _automation_service(request: Request) -> ReceptionAutomationService:
    return ReceptionAutomationService(
        get_session_factory(request),
        reception_service=_service(request),
        proposal_secret=str(request.app.state.settings.jwt_secret),
    )


def _automatic_proposal_response(
    item: AutomaticReceptionProposal,
) -> ReceptionAutomaticProposalResponse:
    return ReceptionAutomaticProposalResponse(
        candidate_type=item.candidate_type,
        recording_ids=list(item.recording_ids),
        decision=item.decision,
        confidence=item.confidence,
        reasons=[
            ReceptionMergeReasonResponse(
                code=reason.code,
                contribution=reason.contribution,
                detail=reason.detail,
                hard_constraint=reason.hard_constraint,
            )
            for reason in item.reasons
        ],
        store_id=item.store_id,
        started_at=item.started_at,
        ended_at=item.ended_at,
        duration_status=("available" if item.duration_available else "unavailable"),
        split_at_sec=item.split_at_sec,
        at_segment_id=item.at_segment_id,
        proposal_token=item.proposal_token,
        proposal_expires_at=item.proposal_expires_at,
    )


@router.get(
    "/receptions",
    response_model=ReceptionListResponse,
    summary="List the authenticated reception work queue",
)
async def list_receptions(
    request: Request,
    user: AuthUser = Depends(get_current_user),
    store_id: str | None = Query(default=None, min_length=1, max_length=64),
    reception_status: ReceptionStatus | None = Query(
        default=None,
        alias="status",
    ),
    started_from: datetime | None = Query(default=None),
    started_to: datetime | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> ReceptionListResponse:
    result = await _automation_service(request).list_receptions(
        get_tenant_id(request),
        agent_user_id=_agent_user_id(user),
        store_id=store_id,
        status=reception_status,
        started_from=started_from,
        started_to=started_to,
        page=page,
        page_size=page_size,
    )
    return ReceptionListResponse(
        items=[_reception_response(reception, request, user) for reception in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post(
    "/receptions/proposals/discover",
    response_model=ReceptionDiscoveryResponse,
    summary="Discover explainable merge and long-recording split candidates",
    dependencies=[Depends(require_write_access())],
)
async def discover_reception_proposals(
    body: ReceptionDiscoveryRequest,
    request: Request,
) -> ReceptionDiscoveryResponse:
    result = await _automation_service(request).discover(
        get_tenant_id(request),
        body,
    )
    return ReceptionDiscoveryResponse(
        items=[_automatic_proposal_response(item) for item in result.items],
        total=result.total,
        scanned_recordings=result.scanned_recordings,
        truncated=result.truncated,
    )


@router.post(
    "/receptions/proposals/accept",
    response_model=ReceptionResponse | ReceptionSplitAcceptanceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Accept a proposal and build its complete timeline server-side",
    dependencies=[Depends(require_write_access())],
)
async def accept_reception_proposal(
    body: ReceptionProposalAcceptRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> ReceptionResponse | ReceptionSplitAcceptanceResponse:
    result = await _automation_service(request).accept(
        get_tenant_id(request),
        body,
        actor=f"user:{user.id}",
    )
    if isinstance(result, ReceptionSplitAcceptance):
        return ReceptionSplitAcceptanceResponse(
            candidate_type="recording_split",
            recording_id=result.recording_id,
            split_at_sec=result.split_at_sec,
            at_segment_id=result.at_segment_id,
            source_duration_sec=result.source_duration_sec,
            receptions=[
                _mutation_reception_response(workspace, request, user)
                for workspace in result.workspaces
            ],
            provenance_event_ids=list(result.provenance_event_ids),
        )
    return _mutation_reception_response(result, request, user)


__all__ = ["router"]
