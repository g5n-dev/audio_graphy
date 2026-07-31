"""Reception listening-workspace, manual-edit, and provenance endpoints."""

from __future__ import annotations

import asyncio
import os
import stat as stat_module
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import StreamingResponse

from audio_graphy.api.deps import get_current_user, get_session_factory
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.roles import require_write_access
from audio_graphy.auth.tenants import get_tenant_id
from audio_graphy.core.reception_merge import ReceptionProposal
from audio_graphy.errors import APIError, ForbiddenError, NotFoundError
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
    ReceptionAudioOperationCreateRequest,
    ReceptionAudioOperationResponse,
    ReceptionAudioPlanRequest,
    ReceptionAudioPlanResponse,
    ReceptionAudioPlanSourceResponse,
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
    ReceptionWorkspaceCapabilities,
    ReceptionWorkspaceNeighbors,
    ReceptionWorkspaceResponse,
    ReceptionWorkspaceWindow,
    StateTransitionListResponse,
    TranscriptItemResponse,
    WorkspaceCollectionWindow,
)
from audio_graphy.services.reception_audio_operations import (
    ReceptionAudioOperationService,
)
from audio_graphy.services.reception_automation import (
    AutomaticReceptionProposal,
    ReceptionAutomationService,
    ReceptionSplitAcceptance,
)
from audio_graphy.services.receptions import (
    AudioAssembler,
    PlaybackGrantClaims,
    ReceptionAudioAsset,
    ReceptionService,
    ReceptionWorkspace,
    create_playback_grant,
    reception_mapping_playback_geometry,
    verify_playback_grant,
)
from audio_graphy.services.tag_governance import TagGovernanceService

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
        embed_adapter=getattr(
            getattr(request.app.state, "adapter_bundle", None),
            "embed",
            None,
        ),
    )


def _audio_operation_service(request: Request) -> ReceptionAudioOperationService:
    return ReceptionAudioOperationService(
        get_session_factory(request),
        _service(request),
    )


def _audio_operation_response(operation: Any) -> ReceptionAudioOperationResponse:
    error = operation.error_message
    if operation.error_code:
        error = (
            f"{operation.error_code}: {error}"
            if error
            else str(operation.error_code)
        )
    return ReceptionAudioOperationResponse(
        id=operation.id,
        reception_id=operation.reception_id,
        status=operation.status,
        mode=operation.mode,
        progress=operation.progress,
        error=error,
        created_at=operation.created_at,
        updated_at=operation.updated_at,
    )


def _agent_user_id(user: AuthUser) -> int | None:
    return user.id if user.role == "agent" else None


async def _has_active_blind_review(
    request: Request,
    user: AuthUser,
    *,
    reception_id: int | None = None,
) -> bool:
    return await TagGovernanceService(get_session_factory(request)).has_active_blind_review(
        tenant_id=get_tenant_id(request),
        reviewer_user_id=user.id,
        reception_id=reception_id,
    )


async def _forbid_blind_semantic_access(
    request: Request,
    user: AuthUser,
    *,
    reception_id: int | None = None,
    access_kind: str = "reception_semantic_mutation",
) -> None:
    allowed = await TagGovernanceService(
        get_session_factory(request)
    ).record_blind_sensitive_access(
        tenant_id=get_tenant_id(request),
        actor_user_id=user.id,
        access_kind=access_kind,
        reception_id=reception_id,
    )
    if not allowed:
        raise ForbiddenError(
            "Blind review isolation forbids semantic history access or mutation before submission"
        )


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


@dataclass(frozen=True, slots=True)
class _PlaybackLocation:
    url: str
    expires_at: datetime


def _playback_url(
    request: Request,
    user: AuthUser,
    path: str,
    *,
    issued_at: int | None = None,
) -> _PlaybackLocation:
    issued_at = (
        int(datetime.now(UTC).timestamp())
        if issued_at is None
        else issued_at
    )
    grant = create_playback_grant(
        secret=str(request.app.state.settings.jwt_secret),
        subject_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
        path=path,
        now=issued_at,
    )
    claims = verify_playback_grant(
        secret=str(request.app.state.settings.jwt_secret),
        grant=grant,
        expected_path=path,
        now=issued_at,
    )
    return _PlaybackLocation(
        url=f"{path}?{urlencode({'playback_grant': grant})}",
        expires_at=datetime.fromtimestamp(claims.expires_at, UTC),
    )


def _reception_response(
    reception: Reception,
    request: Request,
    user: AuthUser,
    *,
    playback_issued_at: int | None = None,
) -> ReceptionMetadataResponse:
    audio_path = f"/api/v1/receptions/{reception.id}/audio"
    playback = (
        _playback_url(
            request,
            user,
            audio_path,
            issued_at=playback_issued_at,
        )
        if reception.merged_audio_path is not None
        else None
    )
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
        audio_url=playback.url if playback is not None else None,
        playback_expires_at=(
            playback.expires_at if playback is not None else None
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
    *,
    playback_issued_at: int | None = None,
) -> ReceptionRecordingResponse:
    audio_path = (
        f"/api/v1/receptions/{mapping.reception_id}/recordings/{mapping.recording_id}/audio"
    )
    geometry = reception_mapping_playback_geometry(mapping)
    playback = _playback_url(
        request,
        user,
        audio_path,
        issued_at=playback_issued_at,
    )
    return ReceptionRecordingResponse(
        id=mapping.id,
        mapping_id=mapping.id,
        recording_id=mapping.recording_id,
        sequence_no=mapping.sequence_no,
        timeline_start_sec=mapping.timeline_start_sec,
        timeline_end_sec=mapping.timeline_end_sec,
        source_start_sec=mapping.source_start_sec,
        source_end_sec=mapping.source_end_sec,
        source_start_ms=geometry.source_start_ms,
        source_end_ms=geometry.source_end_ms,
        timeline_start_ms=geometry.timeline_start_ms,
        timeline_end_ms=geometry.timeline_end_ms,
        gap_before_ms=geometry.gap_before_ms,
        time_origin_ms=geometry.time_origin_ms,
        legal_source_start_ms=geometry.legal_source_start_ms,
        legal_source_end_ms=geometry.legal_source_end_ms,
        gap_before_sec=mapping.gap_before_sec,
        decision_source=cast(DecisionSource, mapping.decision_source),
        merge_confidence=mapping.merge_confidence,
        merge_reasons=cast(
            dict[str, Any],
            _public_json(mapping.merge_reasons),
        ),
        source_recorded_at=recording.recorded_at,
        audio_url=playback.url,
        playback_expires_at=playback.expires_at,
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
    *,
    redact_semantics: bool = False,
) -> DialogueUnitResponse:
    return DialogueUnitResponse(
        id=unit.id,
        source_recording_id=unit.source_recording_id,
        unit_index=unit.unit_index,
        version=unit.version,
        start_sec=unit.start_sec,
        end_sec=unit.end_sec,
        topic=None if redact_semantics else unit.topic,
        business_stage=None if redact_semantics else unit.business_stage,
        summary=None if redact_semantics else unit.summary,
        boundary_confidence=unit.boundary_confidence,
        stage_confidence=getattr(unit, "stage_confidence", None),
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
    *,
    redact_tag_history: bool = False,
) -> ReceptionWorkspaceResponse:
    if workspace.window is None:
        raise RuntimeError("public workspace responses require a bounded time window")
    window = workspace.window
    can_write = user.role in {"admin", "inspector"}
    streaming_enabled = bool(
        getattr(request.app.state.settings, "enable_streaming", False)
    )
    playback_issued_at = int(datetime.now(UTC).timestamp())

    def collection_window(
        item: Any,
        *,
        redacted: bool = False,
    ) -> WorkspaceCollectionWindow:
        if redacted:
            return WorkspaceCollectionWindow(
                total=0,
                returned=0,
                limit=item.limit,
                truncated=False,
            )
        return WorkspaceCollectionWindow(
            total=item.total,
            returned=item.returned,
            limit=item.limit,
            truncated=item.truncated,
        )

    return ReceptionWorkspaceResponse(
        reception=_reception_response(
            workspace.reception,
            request,
            user,
            playback_issued_at=playback_issued_at,
        ),
        recordings=[
            _recording_response(
                mapping,
                recording,
                request,
                user,
                playback_issued_at=playback_issued_at,
            )
            for mapping, recording in workspace.recordings
        ],
        dialogue_units=[
            _unit_response(
                unit,
                ([] if redact_tag_history else workspace.tag_assignments_by_unit.get(unit.id, [])),
                redact_semantics=redact_tag_history,
            )
            for unit in workspace.dialogue_units
        ],
        state_transitions=(
            []
            if redact_tag_history
            else [_transition_response(transition) for transition in workspace.state_transitions]
        ),
        tag_assignments=(
            [] if redact_tag_history else [_tag_response(tag) for tag in workspace.tag_assignments]
        ),
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
        provenance_events=(
            []
            if redact_tag_history
            else [_provenance_response(event) for event in workspace.provenance_events]
        ),
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
            tag_assignments=collection_window(
                window.tag_assignments,
                redacted=redact_tag_history,
            ),
            state_transitions=collection_window(
                window.state_transitions,
                redacted=redact_tag_history,
            ),
            transcript_items=collection_window(window.transcript_items),
            provenance_events=collection_window(
                window.provenance_events,
                redacted=redact_tag_history,
            ),
        ),
        capabilities=ReceptionWorkspaceCapabilities(
            can_manage_audio=can_write,
            can_run_segmentation=can_write,
            can_edit_dialogue=can_write,
            can_edit_tags=can_write,
            supports_audio_plans=can_write,
            supports_audio_operations=can_write,
            can_cancel_audio_operation=can_write,
            can_stream_audio=streaming_enabled
            and user.role in {"admin", "inspector", "agent"},
        ),
        neighbors=ReceptionWorkspaceNeighbors(
            previous_dialogue_unit=(
                _unit_response(workspace.previous_dialogue_unit, [])
                if workspace.previous_dialogue_unit is not None
                else None
            ),
            next_dialogue_unit=(
                _unit_response(workspace.next_dialogue_unit, [])
                if workspace.next_dialogue_unit is not None
                else None
            ),
        )
        if (
            workspace.previous_dialogue_unit is not None
            or workspace.next_dialogue_unit is not None
        )
        else None,
        active_audio_operation=(
            _audio_operation_response(workspace.active_audio_operation)
            if workspace.active_audio_operation is not None
            else None
        ),
    )


def _mutation_reception_response(
    workspace: ReceptionWorkspace,
    request: Request,
    user: AuthUser,
) -> ReceptionResponse:
    playback_issued_at = int(datetime.now(UTC).timestamp())
    metadata = _reception_response(
        workspace.reception,
        request,
        user,
        playback_issued_at=playback_issued_at,
    )
    return ReceptionResponse(
        **metadata.model_dump(),
        recordings=[
            _recording_response(
                mapping,
                recording,
                request,
                user,
                playback_issued_at=playback_issued_at,
            )
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


def _request_playback_grant_claims(
    request: Request,
) -> PlaybackGrantClaims | None:
    claims = getattr(request.state, "playback_grant_claims", None)
    if isinstance(claims, PlaybackGrantClaims) and claims.path == request.url.path:
        return claims
    grant = request.query_params.get("playback_grant")
    if not grant:
        return None
    try:
        return verify_playback_grant(
            secret=str(request.app.state.settings.jwt_secret),
            grant=grant,
            expected_path=request.url.path,
        )
    except ValueError:
        # Bearer-authenticated direct playback remains valid even if a stale
        # optional grant is present, but no grant-expiry claim is advertised.
        return None


def _audio_contract_headers(
    request: Request,
    asset: ReceptionAudioAsset,
) -> dict[str, str]:
    headers = {
        "X-Time-Origin-Ms": str(asset.time_origin_ms),
        "X-Audio-Time-Origin-Ms": str(asset.time_origin_ms),
        "X-Legal-Source-Start-Ms": str(asset.legal_source_start_ms),
    }
    if asset.legal_source_end_ms is not None:
        headers["X-Legal-Source-End-Ms"] = str(asset.legal_source_end_ms)
        headers["X-Audio-Valid-Source-Range-Ms"] = (
            f"{asset.legal_source_start_ms}-{asset.legal_source_end_ms}"
        )
    claims = _request_playback_grant_claims(request)
    if claims is not None:
        headers["X-Audio-Grant-Expires-At"] = datetime.fromtimestamp(
            claims.expires_at,
            UTC,
        ).isoformat().replace("+00:00", "Z")
    return headers


def _audio_response(
    request: Request,
    asset: ReceptionAudioAsset,
) -> Response:
    contract_headers = _audio_contract_headers(request, asset)
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
                **contract_headers,
            },
        )

    length = end - start + 1
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Cache-Control": "private, no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        **contract_headers,
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
    await _forbid_blind_semantic_access(
        request,
        user,
        reception_id=reception_id,
    )
    workspace = await _service(request).merge_recordings(
        reception_id,
        get_tenant_id(request),
        body,
        actor=f"user:{user.id}",
    )
    return _mutation_reception_response(workspace, request, user)


@router.post(
    "/receptions/{reception_id}/audio-plans",
    response_model=ReceptionAudioPlanResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Validate and sign an immutable reception audio timeline plan",
    dependencies=[Depends(require_write_access())],
)
async def create_reception_audio_plan(
    reception_id: int,
    body: ReceptionAudioPlanRequest,
    request: Request,
) -> ReceptionAudioPlanResponse:
    result = await _audio_operation_service(request).create_plan(
        tenant_id=get_tenant_id(request),
        reception_id=reception_id,
        body=body,
    )
    revision = result.revision
    return ReceptionAudioPlanResponse(
        plan_token=result.token,
        timeline_revision=revision.revision,
        total_duration_ms=revision.total_duration_ms,
        physical_eligible=revision.physical_eligible,
        warnings=list(revision.warnings or []),
        sources=[
            ReceptionAudioPlanSourceResponse(
                mapping_id=int(item["mapping_id"]),
                recording_id=int(item["recording_id"]),
                sequence_no=int(item["sequence_no"]),
                source_start_ms=int(item["source_start_ms"]),
                source_end_ms=int(item["source_end_ms"]),
                gap_before_ms=int(item["gap_before_ms"]),
                timeline_start_ms=int(item["timeline_start_ms"]),
                timeline_end_ms=int(item["timeline_end_ms"]),
            )
            for item in revision.source_manifest or []
        ],
    )


@router.post(
    "/receptions/{reception_id}/audio-operations",
    response_model=ReceptionAudioOperationResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Create an idempotent asynchronous reception audio operation",
    dependencies=[Depends(require_write_access())],
)
async def create_reception_audio_operation(
    reception_id: int,
    body: ReceptionAudioOperationCreateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
) -> ReceptionAudioOperationResponse:
    service = _audio_operation_service(request)
    operation = await service.create_operation(
        tenant_id=get_tenant_id(request),
        reception_id=reception_id,
        plan_token=body.plan_token,
        mode=body.mode,
        expected_version=body.expected_version,
        idempotency_key=idempotency_key,
    )
    if operation.status == "queued":
        # The queued row is committed before dispatch. If this process exits,
        # the lease reconciler can safely claim the same operation later.
        background_tasks.add_task(service.run_operation, int(operation.id))
    return _audio_operation_response(operation)


@router.get(
    "/receptions/{reception_id}/audio-operations/{operation_id}",
    response_model=ReceptionAudioOperationResponse,
    summary="Read durable reception audio operation progress",
)
async def get_reception_audio_operation(
    reception_id: int,
    operation_id: int,
    request: Request,
) -> ReceptionAudioOperationResponse:
    operation = await _audio_operation_service(request).get_operation(
        tenant_id=get_tenant_id(request),
        reception_id=reception_id,
        operation_id=operation_id,
    )
    return _audio_operation_response(operation)


@router.post(
    "/receptions/{reception_id}/audio-operations/{operation_id}/cancel",
    response_model=ReceptionAudioOperationResponse,
    summary="Request cancellation before an audio operation commits",
    dependencies=[Depends(require_write_access())],
)
async def cancel_reception_audio_operation(
    reception_id: int,
    operation_id: int,
    request: Request,
) -> ReceptionAudioOperationResponse:
    operation = await _audio_operation_service(request).cancel_operation(
        tenant_id=get_tenant_id(request),
        reception_id=reception_id,
        operation_id=operation_id,
    )
    return _audio_operation_response(operation)


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
    tenant_id = get_tenant_id(request)
    redact_tag_history = not await TagGovernanceService(
        get_session_factory(request)
    ).record_blind_sensitive_access(
        tenant_id=tenant_id,
        actor_user_id=user.id,
        access_kind="reception_workspace",
        reception_id=reception_id,
    )
    workspace = await _service(request).get_workspace_window(
        reception_id,
        tenant_id,
        window_start_sec=window_start_sec,
        window_size_sec=window_size_sec,
        agent_user_id=_agent_user_id(user),
    )
    return _workspace_response(
        workspace,
        request,
        user,
        redact_tag_history=redact_tag_history,
    )


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
    await _forbid_blind_semantic_access(
        request,
        user,
        reception_id=reception_id,
        access_kind="state_transitions",
    )
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
    await _forbid_blind_semantic_access(
        request,
        user,
        reception_id=reception_id,
    )
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
    await _forbid_blind_semantic_access(
        request,
        user,
        reception_id=reception_id,
    )
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
    await _forbid_blind_semantic_access(
        request,
        user,
        reception_id=reception_id,
    )
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
    await _forbid_blind_semantic_access(
        request,
        user,
        access_kind="provenance",
    )
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
    await _forbid_blind_semantic_access(request, user)
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
