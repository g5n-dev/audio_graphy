"""Open API — the machine-to-machine surface for external systems.

Three verbs, authenticated by API key (``Authorization: Bearer agk_…`` or
``X-API-Key``), never by a user JWT:

    POST /api/v1/open/recordings
        Multipart upload of one audio file. ``external_ref`` is the caller's
        own id and the idempotency key: re-POSTing the same reference returns
        the recording that already exists. An optional ``callback_url``
        registers a signed push for the terminal state.

    GET /api/v1/open/recordings/{external_ref}/status
        Computation status by the caller's reference — status, pipeline
        state, and the latest run's stage/error when one exists.

The auth middleware passes ``/api/v1/open/`` through untouched; every route
here therefore carries ``Depends(require_api_key)`` and a test pins that no
route can ship without it.

Payload discipline: responses and callbacks carry ids, states and error codes,
never transcript text or audio. The caller fetches content through the
credentialed detail APIs — this surface is an event channel, not a data
export (PIPL boundary).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import anyio
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from audio_graphy.api.deps import get_db
from audio_graphy.errors import APIError, ValidationError
from audio_graphy.models.integration import ApiKey, IntegrationUpload
from audio_graphy.models.pipeline import RecordingPipelineRun
from audio_graphy.models.recording import Recording
from audio_graphy.schemas.recordings import RecordingCreate, RecordingStatusValue
from audio_graphy.services.integration import hash_api_key, validate_callback_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/open", tags=["open"])

_LAST_USED_REFRESH = 60.0  # seconds between last_used_at writes, to avoid a write per request


@dataclass(frozen=True, slots=True)
class ApiKeyPrincipal:
    """What an authenticated machine caller is: a key inside a tenant."""

    api_key_id: int
    tenant_id: str


def _extract_key(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer agk_"):
        return header.removeprefix("Bearer ").strip()
    candidate = request.headers.get("X-API-Key", "").strip()
    return candidate or None


async def require_api_key(
    request: Request, session: AsyncSession = Depends(get_db)
) -> ApiKeyPrincipal:
    """Authenticate a machine caller. 401 on anything but an active key."""

    plaintext = _extract_key(request)
    if plaintext is None:
        raise APIError(
            "Missing API key (Authorization: Bearer agk_… or X-API-Key)",
            code="API_KEY_REQUIRED",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    row = (
        await session.execute(select(ApiKey).where(ApiKey.key_hash == hash_api_key(plaintext)))
    ).scalar_one_or_none()
    if row is None or not row.active:
        # One message for unknown and revoked: distinguishing them tells an
        # attacker which guesses were once valid.
        raise APIError(
            "Invalid API key",
            code="API_KEY_INVALID",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    now = datetime.now(UTC)
    last_used = row.last_used_at
    if last_used is not None and last_used.tzinfo is None:
        # SQLite hands timestamps back naive; they were written as UTC.
        last_used = last_used.replace(tzinfo=UTC)
    if last_used is None or (now - last_used).total_seconds() > _LAST_USED_REFRESH:
        await session.execute(update(ApiKey).where(ApiKey.id == row.id).values(last_used_at=now))
        await session.commit()
    return ApiKeyPrincipal(api_key_id=row.id, tenant_id=row.tenant_id)


def _ingestion_service(request: Request) -> Any:
    # Deferred import: api.recordings assembles IngestionService with crypto,
    # scrubber, audit and root confinement; reusing its factory keeps the open
    # path byte-identical to the operator path.
    from audio_graphy.api.recordings import _service

    return _service(request)


@router.post("/recordings", status_code=status.HTTP_201_CREATED, summary="Upload one recording")
async def upload_recording(
    request: Request,
    audio: UploadFile = File(...),
    external_ref: str = Form(min_length=1, max_length=128),
    store_id: str = Form(min_length=1, max_length=64),
    agent_name: str | None = Form(default=None, max_length=255),
    recorded_at: datetime | None = Form(default=None),
    callback_url: str | None = Form(default=None, max_length=512),
    principal: ApiKeyPrincipal = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    settings = request.app.state.settings
    if callback_url is not None:
        try:
            validate_callback_url(
                callback_url,
                allow_private=bool(
                    getattr(settings, "integration_allow_private_callback_urls", False)
                ),
            )
        except ValueError as exc:
            raise ValidationError(str(exc), code="CALLBACK_URL_REJECTED") from exc

    existing = await _upload_by_ref(session, principal.tenant_id, external_ref)
    if existing is not None:
        # Idempotent replay: same reference, same answer, nothing re-ingested.
        recording = await session.get(Recording, existing.recording_id)
        return _upload_response(existing, recording, replay=True)

    # Stream to the managed root under a name the caller cannot influence.
    # The pipeline reads from this path; retention handles plaintext cleanup
    # exactly as it does for operator-registered recordings.
    suffix = Path(audio.filename or "upload.bin").suffix[:16] or ".bin"
    relative = (
        Path("open_uploads")
        / principal.tenant_id
        / f"{datetime.now(UTC):%Y%m}"
        / f"{uuid.uuid4().hex}{suffix}"
    )
    target = Path(settings.working_dir) / relative
    target.parent.mkdir(parents=True, exist_ok=True)

    max_bytes = int(getattr(settings, "max_audio_bytes", 512 * 1024 * 1024))
    written = 0
    try:
        async with await anyio.open_file(target, "wb") as sink:
            while chunk := await audio.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise APIError(
                        f"audio exceeds {max_bytes} bytes",
                        code="AUDIO_TOO_LARGE",
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    )
                await sink.write(chunk)
        if written == 0:
            raise ValidationError("empty audio upload", code="AUDIO_EMPTY")

        service = _ingestion_service(request)
        recording = await service.register_recording(
            principal.tenant_id,
            RecordingCreate(
                store_id=store_id,
                path=str(relative),
                agent_name=agent_name,
                recorded_at=recorded_at,
            ),
            idempotency_key=f"open-upload:{external_ref}",
        )

        upload = IntegrationUpload(
            tenant_id=principal.tenant_id,
            external_ref=external_ref,
            recording_id=recording.id,
            api_key_id=principal.api_key_id,
            callback_url=callback_url,
        )
        session.add(upload)
        try:
            await session.commit()
        except IntegrityError:
            # Two concurrent first uploads of one reference: the winner's row
            # is the record; this request's file has already been registered,
            # so answer with the winner rather than inventing a conflict the
            # caller cannot act on.
            await session.rollback()
            winner = await _upload_by_ref(session, principal.tenant_id, external_ref)
            if winner is None:
                raise
            recording = await session.get(Recording, winner.recording_id)
            return _upload_response(winner, recording, replay=True)
        return _upload_response(upload, recording, replay=False)
    except BaseException:
        # The upload row owns the file's reason to exist; without one, remove it.
        if not await _file_is_registered(session, principal.tenant_id, relative):
            with anyio.CancelScope(shield=True):
                await anyio.Path(target).unlink(missing_ok=True)
        raise


async def _file_is_registered(session: AsyncSession, tenant_id: str, relative: Path) -> bool:
    return (
        await session.execute(
            select(Recording.id).where(
                Recording.tenant_id == tenant_id,
                Recording.path == str(relative),
            )
        )
    ).scalar_one_or_none() is not None


async def _upload_by_ref(
    session: AsyncSession, tenant_id: str, external_ref: str
) -> IntegrationUpload | None:
    return (
        await session.execute(
            select(IntegrationUpload).where(
                IntegrationUpload.tenant_id == tenant_id,
                IntegrationUpload.external_ref == external_ref,
            )
        )
    ).scalar_one_or_none()


def _upload_response(
    upload: IntegrationUpload, recording: Recording | None, *, replay: bool
) -> dict[str, Any]:
    return {
        "external_ref": upload.external_ref,
        "recording_id": upload.recording_id,
        "status": recording.status if recording is not None else "queued",
        "callback_registered": upload.callback_url is not None,
        "replayed": replay,
    }


@router.get("/recordings/{external_ref}/status", summary="Computation status by external_ref")
async def computation_status(
    external_ref: str,
    principal: ApiKeyPrincipal = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    upload = await _upload_by_ref(session, principal.tenant_id, external_ref)
    if upload is None:
        raise APIError(
            "unknown external_ref",
            code="EXTERNAL_REF_NOT_FOUND",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    recording = await session.get(Recording, upload.recording_id)
    if recording is None:
        raise APIError(
            "recording no longer exists (erased)",
            code="RECORDING_ERASED",
            status_code=status.HTTP_410_GONE,
        )
    run = (
        await session.execute(
            select(RecordingPipelineRun)
            .where(
                RecordingPipelineRun.tenant_id == principal.tenant_id,
                RecordingPipelineRun.recording_id == recording.id,
            )
            .order_by(RecordingPipelineRun.generation.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    body: dict[str, Any] = {
        "external_ref": external_ref,
        "recording_id": recording.id,
        "status": cast(RecordingStatusValue, recording.status),
        "pipeline_state": recording.pipeline_state,
        "indexed_at": recording.indexed_at,
        "terminal": recording.status in {"indexed", "ready_no_speech", "failed"},
    }
    if run is not None:
        body["run"] = {
            "generation": run.generation,
            "state": run.state,
            "attempt_count": run.attempt_count,
            "error_code": run.error_code,
            "error_message": run.error_message,
            "finished_at": run.finished_at,
        }
    return body
