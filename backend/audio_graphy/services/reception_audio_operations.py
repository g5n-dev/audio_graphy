"""Durable plan/operation layer for reception timeline and physical audio changes."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.core.audio_timeline import (
    AudioTimelinePlanner,
    AudioTimelineSource,
    milliseconds_to_seconds,
    seconds_to_milliseconds,
    verified_recording_duration_ms,
)
from audio_graphy.errors import ConflictError, NotFoundError, ValidationError
from audio_graphy.models.reception import (
    DialogueStateTransition,
    DialogueTagAssignment,
    DialogueUnit,
    Reception,
    ReceptionRecording,
)
from audio_graphy.models.reception_audio import (
    ReceptionAudioArtifact,
    ReceptionAudioOperation,
    ReceptionTimelineRevision,
)
from audio_graphy.models.recording import Recording
from audio_graphy.schemas.receptions import ReceptionAudioPlanRequest, ReceptionMergeRequest
from audio_graphy.services.receptions import (
    ReceptionService,
    ReceptionTimelineSliceOverride,
    reception_physical_generation_relative_path,
    resolve_safe_audio_output,
)

_ACTIVE_OPERATION_STATES = (
    "claimed",
    "probing",
    "slicing",
    "assembling",
    "encrypting",
    "verifying",
    "committing",
)
_GENERATED_ARTIFACT_NAME = re.compile(r"v[1-9][0-9]*-[A-Za-z0-9_-]{8,96}\.wav(?:\.enc)?")


@dataclass(frozen=True, slots=True)
class AudioPlanResult:
    token: str
    revision: ReceptionTimelineRevision


class _AudioOperationCancelledError(RuntimeError):
    pass


class ReceptionAudioOperationService:
    """Create immutable plans, atomically claim operations, and publish pointers."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        reception_service: ReceptionService,
        *,
        plan_ttl_sec: int = 900,
        lease_sec: float = 120,
    ) -> None:
        self._session_factory = session_factory
        self._reception_service = reception_service
        self._plan_ttl_sec = plan_ttl_sec
        self._lease_sec = lease_sec

    async def create_plan(
        self,
        *,
        tenant_id: str,
        reception_id: int,
        body: ReceptionAudioPlanRequest,
    ) -> AudioPlanResult:
        now = datetime.now(UTC)
        async with self._session_factory() as db, db.begin():
            reception = (
                await db.execute(
                    select(Reception)
                    .where(
                        Reception.id == reception_id,
                        Reception.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if reception is None:
                raise NotFoundError("Reception not found", code="RECEPTION_NOT_FOUND")
            if int(reception.version) != body.expected_version:
                raise ConflictError(
                    "Reception version changed",
                    code="RECEPTION_VERSION_CONFLICT",
                    detail={
                        "expected_version": body.expected_version,
                        "actual_version": int(reception.version),
                    },
                )

            mapping_ids = [source.mapping_id for source in body.sources]
            mappings = list(
                (
                    await db.execute(
                        select(ReceptionRecording, Recording)
                        .join(Recording, Recording.id == ReceptionRecording.recording_id)
                        .where(
                            ReceptionRecording.tenant_id == tenant_id,
                            ReceptionRecording.reception_id == reception_id,
                            ReceptionRecording.id.in_(mapping_ids),
                            Recording.tenant_id == tenant_id,
                        )
                    )
                ).all()
            )
            by_mapping_id = {
                int(mapping.id): (mapping, recording) for mapping, recording in mappings
            }
            if len(by_mapping_id) != len(mapping_ids):
                raise ValidationError(
                    "One or more mapping IDs do not belong to this reception",
                    code="RECEPTION_MAPPING_NOT_FOUND",
                )
            active_mapping_count = int(
                (
                    await db.execute(
                        select(func.count())
                        .select_from(ReceptionRecording)
                        .where(
                            ReceptionRecording.tenant_id == tenant_id,
                            ReceptionRecording.reception_id == reception_id,
                        )
                    )
                ).scalar_one()
            )
            if active_mapping_count != len(mapping_ids):
                raise ValidationError(
                    "Audio plans must contain every active source mapping",
                    code="RECEPTION_PLAN_INCOMPLETE",
                )

            timeline_inputs: list[AudioTimelineSource] = []
            physical_eligible = True
            warnings: list[str] = []
            current_geometry: list[tuple[int, int, int]] = []
            for requested in body.sources:
                mapping, recording = by_mapping_id[requested.mapping_id]
                source_start_ms = (
                    int(mapping.source_start_ms)
                    if int(getattr(mapping, "source_start_ms", 0) or 0) > 0
                    else seconds_to_milliseconds(mapping.source_start_sec)
                )
                source_end_ms = (
                    int(mapping.source_end_ms)
                    if getattr(mapping, "source_end_ms", None) is not None
                    else (
                        seconds_to_milliseconds(mapping.source_end_sec)
                        if mapping.source_end_sec is not None
                        else None
                    )
                )
                verified_ms = verified_recording_duration_ms(recording)
                if verified_ms is None:
                    raise ValidationError(
                        "Source duration is unavailable",
                        code="RECORDING_DURATION_UNAVAILABLE",
                        detail={"recording_id": int(recording.id)},
                    )
                if source_end_ms is None:
                    source_end_ms = verified_ms
                source_revision = int(getattr(recording, "source_revision", 0) or 0)
                source_sha256 = getattr(recording, "audio_sha256", None)
                source_size_bytes = getattr(recording, "audio_size_bytes", None)
                if (
                    source_revision <= 0
                    or not source_sha256
                    or source_size_bytes is None
                    or int(source_size_bytes) <= 0
                ):
                    physical_eligible = False
                    warnings.append(f"recording:{int(recording.id)}:source_identity_unavailable")
                if source_end_ms > verified_ms:
                    raise ValidationError(
                        "Source slice exceeds verified recording duration",
                        code="RECEPTION_SOURCE_OUT_OF_BOUNDS",
                        detail={
                            "recording_id": int(recording.id),
                            "source_end_ms": source_end_ms,
                            "verified_duration_ms": verified_ms,
                        },
                    )
                timeline_inputs.append(
                    AudioTimelineSource(
                        source_id=int(mapping.id),
                        source_start_ms=source_start_ms,
                        source_end_ms=source_end_ms,
                        verified_duration_ms=verified_ms,
                        gap_before_ms=requested.gap_before_ms,
                    )
                )
                current_geometry.append(
                    (
                        int(mapping.id),
                        int(mapping.sequence_no),
                        seconds_to_milliseconds(mapping.gap_before_sec),
                    )
                )

            timeline = AudioTimelinePlanner().plan(timeline_inputs)
            manifest: list[dict[str, Any]] = []
            planned_geometry: list[tuple[int, int, int]] = []
            for planned in timeline.slices:
                mapping, recording = by_mapping_id[int(planned.source_id)]
                item = {
                    "mapping_id": int(mapping.id),
                    "recording_id": int(recording.id),
                    "sequence_no": int(planned.sequence_no),
                    "source_start_ms": int(planned.source_start_ms),
                    "source_end_ms": int(planned.source_end_ms),
                    "gap_before_ms": int(planned.gap_before_ms),
                    "timeline_start_ms": int(planned.timeline_start_ms),
                    "timeline_end_ms": int(planned.timeline_end_ms),
                    "recording_source_revision": int(getattr(recording, "source_revision", 0) or 0),
                    "recording_sha256": getattr(recording, "audio_sha256", None),
                    "recording_size_bytes": getattr(recording, "audio_size_bytes", None),
                }
                manifest.append(item)
                planned_geometry.append(
                    (
                        int(mapping.id),
                        int(planned.sequence_no),
                        int(planned.gap_before_ms),
                    )
                )
            if current_geometry != planned_geometry:
                warnings.append("timeline_geometry_changed")

            signature_payload = {
                "tenant_id": tenant_id,
                "reception_id": reception_id,
                "expected_version": body.expected_version,
                "sources": manifest,
            }
            signature = hashlib.sha256(
                json.dumps(
                    signature_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            latest_revision = int(
                (
                    await db.execute(
                        select(func.max(ReceptionTimelineRevision.revision)).where(
                            ReceptionTimelineRevision.reception_id == reception_id,
                            ReceptionTimelineRevision.tenant_id == tenant_id,
                        )
                    )
                ).scalar_one_or_none()
                or 0
            )
            revision = ReceptionTimelineRevision(
                tenant_id=tenant_id,
                reception_id=reception_id,
                revision=latest_revision + 1,
                expected_reception_version=body.expected_version,
                state="STAGING",
                plan_signature=signature,
                plan_token_hash=token_hash,
                source_manifest=manifest,
                total_duration_ms=int(timeline.total_duration_ms),
                physical_eligible=physical_eligible,
                warnings=sorted(set(warnings)),
                expires_at=now + timedelta(seconds=self._plan_ttl_sec),
            )
            db.add(revision)
            await db.flush()
            return AudioPlanResult(token=token, revision=revision)

    async def create_operation(
        self,
        *,
        tenant_id: str,
        reception_id: int,
        plan_token: str,
        mode: str,
        expected_version: int,
        idempotency_key: str,
    ) -> ReceptionAudioOperation:
        if not idempotency_key or len(idempotency_key) > 128:
            raise ValidationError(
                "A bounded Idempotency-Key header is required",
                code="IDEMPOTENCY_KEY_REQUIRED",
            )
        now = datetime.now(UTC)
        token_hash = hashlib.sha256(plan_token.encode("utf-8")).hexdigest()
        async with self._session_factory() as db:
            await db.begin()
            reception = (
                await db.execute(
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
                )
            revision = (
                await db.execute(
                    select(ReceptionTimelineRevision)
                    .where(
                        ReceptionTimelineRevision.tenant_id == tenant_id,
                        ReceptionTimelineRevision.reception_id == reception_id,
                        ReceptionTimelineRevision.plan_token_hash == token_hash,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if revision is None:
                raise ValidationError(
                    "Audio plan is invalid",
                    code="AUDIO_PLAN_INVALID",
                )
            existing = (
                await db.execute(
                    select(ReceptionAudioOperation)
                    .where(
                        ReceptionAudioOperation.tenant_id == tenant_id,
                        ReceptionAudioOperation.reception_id == reception_id,
                        ReceptionAudioOperation.idempotency_key == idempotency_key,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if existing is not None and existing.status in {"failed", "cancelled"}:
                # The workspace derives a deterministic key from
                # (reception, version, plan token), so a user's retry after a
                # failure arrives with the SAME key. Returning the dead row
                # would make the retry a silent no-op, so archive the key on
                # the terminal row and fall through to create a fresh
                # operation. The id prefix keeps the unique index satisfied
                # even when the truncated original keys collide.
                existing.idempotency_key = (
                    f"superseded:{int(existing.id)}:{existing.idempotency_key}"[:128]
                )
                # The rename must reach the database before the retry row is
                # inserted below, or the unique (tenant, reception, key) index
                # rejects the insert inside the same flush.
                await db.flush()
            elif existing is not None:
                if (
                    int(existing.timeline_revision_id) != int(revision.id)
                    or existing.mode != mode
                    or int(existing.expected_reception_version) != expected_version
                ):
                    raise ConflictError(
                        "Idempotency-Key was already used for a different request",
                        code="IDEMPOTENCY_KEY_REUSED",
                        detail={"operation_id": int(existing.id)},
                    )
                # Dedupe in-flight requests and replay a succeeded operation's
                # committed result; a completed plan must never run twice.
                await db.commit()
                return existing

            if revision.state != "STAGING":
                raise ValidationError("Audio plan is invalid", code="AUDIO_PLAN_INVALID")
            expires_at = revision.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= now:
                revision.state = "CANCELLED"
                # The rejection is observable outside this transaction, so its
                # terminal plan state must be committed before raising it.
                await db.commit()
                raise ValidationError("Audio plan expired", code="AUDIO_PLAN_EXPIRED")
            if int(revision.expected_reception_version) != expected_version:
                raise ConflictError(
                    "Audio plan and request versions differ",
                    code="RECEPTION_VERSION_CONFLICT",
                )
            if int(reception.version) != expected_version:
                raise ConflictError(
                    "Reception version changed",
                    code="RECEPTION_VERSION_CONFLICT",
                    detail={
                        "expected_version": expected_version,
                        "actual_version": int(reception.version),
                    },
                )
            if mode in {"physical", "both"} and not revision.physical_eligible:
                raise ValidationError(
                    "One or more sources are not physically eligible",
                    code="AUDIO_PLAN_NOT_PHYSICAL",
                    detail={"warnings": list(revision.warnings or [])},
                )
            if mode == "physical" and "timeline_geometry_changed" in (revision.warnings or []):
                raise ValidationError(
                    "Physical mode cannot publish new timeline geometry",
                    code="PHYSICAL_MODE_GEOMETRY_CHANGE",
                )
            active_operation = (
                await db.execute(
                    select(ReceptionAudioOperation.id)
                    .where(
                        ReceptionAudioOperation.tenant_id == tenant_id,
                        ReceptionAudioOperation.reception_id == reception_id,
                        ReceptionAudioOperation.status.in_(
                            (
                                "queued",
                                "claimed",
                                "probing",
                                "slicing",
                                "assembling",
                                "encrypting",
                                "verifying",
                                "committing",
                            )
                        ),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if active_operation is not None:
                raise ConflictError(
                    "Another audio operation is active",
                    code="AUDIO_OPERATION_ACTIVE",
                    detail={"operation_id": int(active_operation)},
                )
            operation = ReceptionAudioOperation(
                tenant_id=tenant_id,
                reception_id=reception_id,
                timeline_revision_id=int(revision.id),
                idempotency_key=idempotency_key,
                mode=mode,
                expected_reception_version=expected_version,
                status="queued",
                progress=0.0,
            )
            db.add(operation)
            await db.flush()
            await db.commit()
            return operation

    async def get_operation(
        self,
        *,
        tenant_id: str,
        reception_id: int,
        operation_id: int,
    ) -> ReceptionAudioOperation:
        async with self._session_factory() as db:
            operation = (
                await db.execute(
                    select(ReceptionAudioOperation).where(
                        ReceptionAudioOperation.id == operation_id,
                        ReceptionAudioOperation.tenant_id == tenant_id,
                        ReceptionAudioOperation.reception_id == reception_id,
                    )
                )
            ).scalar_one_or_none()
            if operation is None:
                raise NotFoundError(
                    "Audio operation not found",
                    code="AUDIO_OPERATION_NOT_FOUND",
                )
            return operation

    async def pending_operation_ids(self, *, limit: int = 16) -> list[int]:
        """Return committed queued work for the lifecycle recovery dispatcher.

        This lookup deliberately does not claim rows. ``run_operation`` owns
        the conditional ``queued -> claimed`` update, so multiple application
        instances may discover the same ID while only one can execute it.
        """
        if limit <= 0 or limit > 1_000:
            raise ValueError("limit must be between 1 and 1000")
        async with self._session_factory() as db:
            return [
                int(operation_id)
                for operation_id in (
                    await db.execute(
                        select(ReceptionAudioOperation.id)
                        .where(ReceptionAudioOperation.status == "queued")
                        .order_by(
                            ReceptionAudioOperation.created_at,
                            ReceptionAudioOperation.id,
                        )
                        .limit(limit)
                    )
                ).scalars()
            ]

    async def cancel_operation(
        self,
        *,
        tenant_id: str,
        reception_id: int,
        operation_id: int,
    ) -> ReceptionAudioOperation:
        async with self._session_factory() as db, db.begin():
            operation = (
                await db.execute(
                    select(ReceptionAudioOperation)
                    .where(
                        ReceptionAudioOperation.id == operation_id,
                        ReceptionAudioOperation.tenant_id == tenant_id,
                        ReceptionAudioOperation.reception_id == reception_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if operation is None:
                raise NotFoundError(
                    "Audio operation not found",
                    code="AUDIO_OPERATION_NOT_FOUND",
                )
            if operation.status in {"succeeded", "failed", "cancelled"}:
                return operation
            operation.cancel_requested = True
            if operation.status in {"queued", "claimed", "probing"}:
                operation.status = "cancelled"
                operation.progress = 1.0
                operation.finished_at = datetime.now(UTC)
            return operation

    async def run_operation(self, operation_id: int) -> None:
        lease_token = secrets.token_hex(16)
        now = datetime.now(UTC)
        async with self._session_factory() as db, db.begin():
            claim = await db.execute(
                update(ReceptionAudioOperation)
                .where(
                    ReceptionAudioOperation.id == operation_id,
                    ReceptionAudioOperation.status == "queued",
                )
                .values(
                    status="claimed",
                    progress=0.05,
                    attempt_count=ReceptionAudioOperation.attempt_count + 1,
                    lease_token=lease_token,
                    lease_expires_at=now + timedelta(seconds=self._lease_sec),
                )
            )
            if getattr(claim, "rowcount", 0) != 1:
                return

        heartbeat_stop = asyncio.Event()
        lease_lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat_lease(
                operation_id,
                lease_token,
                heartbeat_stop,
                lease_lost,
            )
        )
        merge_task: asyncio.Task[Any] | None = None
        try:
            operation, revision = await self._load_claimed(operation_id, lease_token)
            await self._set_stage(operation_id, lease_token, "probing", 0.15)
            if operation.cancel_requested:
                await self._cancel_claimed(operation_id, lease_token)
                return
            manifest = list(revision.source_manifest or [])
            recording_ids = [int(item["recording_id"]) for item in manifest]
            physical_generation: str | None = None
            preparing_path: Path | None = None
            if operation.mode in {"physical", "both"}:
                await self._set_stage(operation_id, lease_token, "slicing", 0.3)
                await self._set_stage(operation_id, lease_token, "assembling", 0.45)
                physical_generation = (
                    f"op{operation.id}-a{operation.attempt_count}-{secrets.token_hex(8)}"
                )
                preparing_path = await self._register_preparing_artifact(
                    operation=operation,
                    revision=revision,
                    lease_token=lease_token,
                    generation=physical_generation,
                )

            operation_mode = cast(
                Literal["logical", "physical", "both"],
                operation.mode,
            )
            merge_body = ReceptionMergeRequest(
                recording_ids=recording_ids,
                mode=operation_mode,
                expected_version=operation.expected_reception_version,
            )
            kwargs: dict[str, Any] = {"actor": f"audio-operation:{operation_id}"}
            parameters = inspect.signature(self._reception_service.merge_recordings).parameters
            timeline_override = {
                int(item["recording_id"]): ReceptionTimelineSliceOverride(
                    source_start_sec=milliseconds_to_seconds(int(item["source_start_ms"])),
                    source_end_sec=milliseconds_to_seconds(int(item["source_end_ms"])),
                    gap_before_sec=milliseconds_to_seconds(int(item["gap_before_ms"])),
                )
                for item in manifest
            }
            if "timeline_overrides" in parameters:
                kwargs["timeline_overrides"] = timeline_override
            elif "timeline_override" in parameters:
                kwargs["timeline_override"] = timeline_override
            elif "timeline_geometry_changed" in (revision.warnings or []):
                raise RuntimeError("Reception service does not support timeline overrides")

            async def mark_physical_ready(prepared: Any) -> None:
                assert preparing_path is not None
                await self._mark_artifact_ready(
                    operation_id=operation_id,
                    lease_token=lease_token,
                    preparing_path=preparing_path,
                    prepared=prepared,
                    duration_ms=int(revision.total_duration_ms),
                )

            async def advance_physical_stage(status: str) -> None:
                progress_by_status = {
                    "encrypting": 0.65,
                    "verifying": 0.8,
                }
                try:
                    progress = progress_by_status[status]
                except KeyError as exc:
                    raise RuntimeError(f"unsupported physical audio stage: {status}") from exc
                await self._set_stage(
                    operation_id,
                    lease_token,
                    status,
                    progress,
                )

            async def publish_before_commit(
                db: AsyncSession,
                reception: Reception,
                final_mappings: tuple[ReceptionRecording, ...],
                prepared: Any,
                previous_merged_audio_path: str | None,
            ) -> None:
                await self._publish_in_session(
                    db,
                    operation_id=operation_id,
                    lease_token=lease_token,
                    revision_id=int(revision.id),
                    manifest=manifest,
                    reception=reception,
                    final_mappings=final_mappings,
                    prepared=prepared,
                    previous_merged_audio_path=previous_merged_audio_path,
                )

            if "before_commit" not in parameters:
                raise RuntimeError("Reception service cannot publish an operation atomically")
            kwargs["before_commit"] = publish_before_commit
            if operation.mode in {"physical", "both"}:
                if (
                    "physical_generation" not in parameters
                    or "on_physical_stage" not in parameters
                    or "after_physical_prepare" not in parameters
                ):
                    raise RuntimeError(
                        "Reception service cannot register a physical artifact durably"
                    )
                kwargs["physical_generation"] = physical_generation
                kwargs["on_physical_stage"] = advance_physical_stage
                kwargs["after_physical_prepare"] = mark_physical_ready
            else:
                await self._set_stage(
                    operation_id,
                    lease_token,
                    "verifying",
                    0.8,
                )
            merge_task = asyncio.create_task(
                self._reception_service.merge_recordings(
                    operation.reception_id,
                    str(operation.tenant_id),
                    merge_body,
                    **kwargs,
                )
            )
            lease_waiter = asyncio.create_task(lease_lost.wait())
            done, _pending = await asyncio.wait(
                (merge_task, lease_waiter),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if merge_task in done:
                lease_waiter.cancel()
                await asyncio.gather(lease_waiter, return_exceptions=True)
                await merge_task
            else:
                merge_task.cancel()
                await asyncio.gather(merge_task, return_exceptions=True)
                raise RuntimeError("audio operation lease renewal failed during external work")
        except asyncio.CancelledError:
            if merge_task is not None and not merge_task.done():
                merge_task.cancel()
                await asyncio.gather(merge_task, return_exceptions=True)
            await self._fail_operation(
                operation_id,
                lease_token,
                RuntimeError("audio operation worker was cancelled"),
            )
            raise
        except _AudioOperationCancelledError:
            await self._cancel_claimed(operation_id, lease_token)
        except Exception as exc:
            await self._fail_operation(operation_id, lease_token, exc)
        finally:
            if merge_task is not None and not merge_task.done():
                merge_task.cancel()
                await asyncio.gather(merge_task, return_exceptions=True)
            heartbeat_stop.set()
            await heartbeat

    async def _heartbeat_lease(
        self,
        operation_id: int,
        lease_token: str,
        stop: asyncio.Event,
        lease_lost: asyncio.Event,
    ) -> None:
        """Renew ownership while ffmpeg/encryption runs outside a DB transaction."""
        interval = max(0.05, min(self._lease_sec / 3, 30.0))
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                async with self._session_factory() as db, db.begin():
                    result = await db.execute(
                        update(ReceptionAudioOperation)
                        .where(
                            ReceptionAudioOperation.id == operation_id,
                            ReceptionAudioOperation.lease_token == lease_token,
                            ReceptionAudioOperation.status.in_(_ACTIVE_OPERATION_STATES),
                        )
                        .values(
                            lease_expires_at=datetime.now(UTC) + timedelta(seconds=self._lease_sec)
                        )
                    )
                    if getattr(result, "rowcount", 0) != 1:
                        lease_lost.set()
                        return
            except Exception:
                # A failed renewal must not make the worker appear healthy.
                # Let the lease expire; every subsequent stage/commit uses the
                # token CAS and therefore fails closed if another worker wins.
                lease_lost.set()
                return

    async def _register_preparing_artifact(
        self,
        *,
        operation: ReceptionAudioOperation,
        revision: ReceptionTimelineRevision,
        lease_token: str,
        generation: str,
    ) -> Path:
        relative_path = reception_physical_generation_relative_path(
            tenant_id=str(operation.tenant_id),
            reception_id=int(operation.reception_id),
            reception_version=int(operation.expected_reception_version) + 1,
            generation=generation,
        )
        target_path = resolve_safe_audio_output(
            self._reception_service.audio_root,
            relative_path,
        )
        async with self._session_factory() as db, db.begin():
            claimed = (
                await db.execute(
                    select(ReceptionAudioOperation)
                    .where(
                        ReceptionAudioOperation.id == operation.id,
                        ReceptionAudioOperation.tenant_id == operation.tenant_id,
                        ReceptionAudioOperation.lease_token == lease_token,
                        ReceptionAudioOperation.status == "assembling",
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if claimed is None:
                raise RuntimeError("audio operation lease was lost before artifact preparation")
            db.add(
                ReceptionAudioArtifact(
                    tenant_id=str(operation.tenant_id),
                    reception_id=int(operation.reception_id),
                    timeline_revision_id=int(revision.id),
                    operation_id=int(operation.id),
                    state="PREPARING",
                    path=str(target_path),
                )
            )
            await db.flush()
        return target_path

    async def _mark_artifact_ready(
        self,
        *,
        operation_id: int,
        lease_token: str,
        preparing_path: Path,
        prepared: Any,
        duration_ms: int,
    ) -> None:
        prepared_path = Path(str(prepared.merged_audio_path))
        allowed_paths = {
            preparing_path,
            Path(f"{preparing_path}.enc"),
        }
        if prepared_path not in allowed_paths or not prepared_path.is_file():
            raise RuntimeError("prepared physical artifact does not match its reserved generation")
        size_bytes = prepared_path.stat().st_size
        if size_bytes <= 0:
            raise RuntimeError("prepared physical artifact is empty")
        sha256 = await asyncio.to_thread(_sha256_file, prepared_path)
        sample_rate = getattr(prepared.manifest, "output_sample_rate", None)
        channels = getattr(prepared.manifest, "output_channels", None)
        async with self._session_factory() as db, db.begin():
            operation = (
                await db.execute(
                    select(ReceptionAudioOperation)
                    .where(
                        ReceptionAudioOperation.id == operation_id,
                        ReceptionAudioOperation.lease_token == lease_token,
                        ReceptionAudioOperation.status == "verifying",
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if operation is None:
                raise RuntimeError("audio operation lease was lost while verifying artifact")
            artifact = (
                await db.execute(
                    select(ReceptionAudioArtifact)
                    .where(
                        ReceptionAudioArtifact.tenant_id == operation.tenant_id,
                        ReceptionAudioArtifact.reception_id == operation.reception_id,
                        ReceptionAudioArtifact.operation_id == operation_id,
                        ReceptionAudioArtifact.path == str(preparing_path),
                        ReceptionAudioArtifact.state == "PREPARING",
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if artifact is None:
                raise RuntimeError("preparing audio artifact disappeared")
            artifact.state = "READY"
            artifact.path = str(prepared_path)
            artifact.sha256 = sha256
            artifact.size_bytes = size_bytes
            artifact.duration_ms = duration_ms
            artifact.sample_rate = int(sample_rate) if sample_rate is not None else None
            artifact.channels = int(channels) if channels is not None else None

    async def reconcile_stale(self, *, max_attempts: int = 3) -> int:
        """Requeue expired leases; terminally fail exhausted operations."""
        now = datetime.now(UTC)
        cleanup: list[tuple[str, int, str]] = []
        async with self._session_factory() as db, db.begin():
            rows = list(
                (
                    await db.execute(
                        select(ReceptionAudioOperation)
                        .where(
                            ReceptionAudioOperation.status.in_(_ACTIVE_OPERATION_STATES),
                            ReceptionAudioOperation.lease_expires_at < now,
                        )
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            for row in rows:
                artifacts = list(
                    (
                        await db.execute(
                            select(ReceptionAudioArtifact)
                            .where(
                                ReceptionAudioArtifact.tenant_id == row.tenant_id,
                                ReceptionAudioArtifact.reception_id == row.reception_id,
                                ReceptionAudioArtifact.operation_id == row.id,
                                ReceptionAudioArtifact.state.in_(("PREPARING", "READY")),
                            )
                            .with_for_update()
                        )
                    ).scalars()
                )
                for artifact in artifacts:
                    artifact.state = "FAILED" if artifact.state == "PREPARING" else "ORPHANED"
                    cleanup.append(
                        (
                            str(artifact.tenant_id),
                            int(artifact.reception_id),
                            str(artifact.path),
                        )
                    )
                if row.attempt_count >= max_attempts:
                    row.status = "failed"
                    row.error_code = "LEASE_EXHAUSTED"
                    row.error_message = "operation lease expired too many times"
                    row.finished_at = now
                else:
                    row.status = "queued"
                    row.progress = 0.0
                    row.lease_token = None
                    row.lease_expires_at = None
        for tenant_id, reception_id, path in cleanup:
            await self._delete_artifact_generation(
                tenant_id=tenant_id,
                reception_id=reception_id,
                persisted_path=path,
            )
        return len(rows)

    async def reconcile_artifacts(
        self,
        *,
        stale_before: datetime | None = None,
        limit: int = 100,
    ) -> int:
        """Repair committed pointers and reclaim only confined stale generations."""
        if limit <= 0 or limit > 1_000:
            raise ValueError("artifact reconciliation limit must be between 1 and 1000")
        now = datetime.now(UTC)
        cutoff = stale_before or now - timedelta(minutes=15)
        reconciled = 0
        async with self._session_factory() as db, db.begin():
            artifacts = list(
                (
                    await db.execute(
                        select(ReceptionAudioArtifact)
                        .where(
                            ReceptionAudioArtifact.state.in_(
                                (
                                    "PREPARING",
                                    "READY",
                                    "RETIRED",
                                    "FAILED",
                                    "ORPHANED",
                                )
                            ),
                            ReceptionAudioArtifact.updated_at <= cutoff,
                        )
                        .order_by(ReceptionAudioArtifact.updated_at)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            for artifact in artifacts:
                operation = await db.get(ReceptionAudioOperation, artifact.operation_id)
                reception = await db.get(Reception, artifact.reception_id)
                if (
                    operation is not None
                    and (
                        operation.tenant_id != artifact.tenant_id
                        or operation.reception_id != artifact.reception_id
                    )
                ) or (reception is not None and reception.tenant_id != artifact.tenant_id):
                    continue
                pointer_matches = (
                    reception is not None
                    and reception.active_timeline_revision_id == artifact.timeline_revision_id
                    and reception.merged_audio_path == artifact.path
                )
                if pointer_matches:
                    revision = await db.get(
                        ReceptionTimelineRevision,
                        artifact.timeline_revision_id,
                    )
                    if (
                        artifact.state == "READY"
                        and operation is not None
                        and revision is not None
                        and revision.tenant_id == artifact.tenant_id
                        and revision.reception_id == artifact.reception_id
                        and (
                            operation.status == "succeeded"
                            or (
                                operation.status in _ACTIVE_OPERATION_STATES
                                and _aware_utc(operation.lease_expires_at) <= now
                            )
                        )
                        and await self._artifact_file_is_intact(artifact)
                    ):
                        await db.execute(
                            update(ReceptionAudioArtifact)
                            .where(
                                ReceptionAudioArtifact.tenant_id == artifact.tenant_id,
                                ReceptionAudioArtifact.reception_id == artifact.reception_id,
                                ReceptionAudioArtifact.state == "ATTACHED",
                                ReceptionAudioArtifact.id != artifact.id,
                            )
                            .values(state="RETIRED", retired_at=now)
                        )
                        await db.execute(
                            update(ReceptionTimelineRevision)
                            .where(
                                ReceptionTimelineRevision.tenant_id == artifact.tenant_id,
                                ReceptionTimelineRevision.reception_id == artifact.reception_id,
                                ReceptionTimelineRevision.state == "ACTIVE",
                                ReceptionTimelineRevision.id != revision.id,
                            )
                            .values(state="SUPERSEDED")
                        )
                        revision.state = "ACTIVE"
                        revision.activated_at = revision.activated_at or now
                        artifact.state = "ATTACHED"
                        artifact.attached_at = operation.finished_at or now
                        if operation.status != "succeeded":
                            operation.status = "succeeded"
                            operation.progress = 1.0
                            operation.finished_at = now
                            operation.lease_token = None
                            operation.lease_expires_at = None
                        reconciled += 1
                    # A DB pointer is authoritative.  A corrupt or unexpected
                    # state requires operator repair; never make it worse by
                    # deleting the referenced bytes.
                    continue
                if (
                    artifact.state in {"PREPARING", "READY"}
                    and operation is not None
                    and operation.status in _ACTIVE_OPERATION_STATES
                    and _aware_utc(operation.lease_expires_at) > now
                ):
                    continue
                deleted = await self._delete_artifact_generation(
                    tenant_id=str(artifact.tenant_id),
                    reception_id=int(artifact.reception_id),
                    persisted_path=str(artifact.path),
                )
                if not deleted:
                    continue
                if artifact.state == "PREPARING":
                    artifact.state = "FAILED"
                elif artifact.state == "READY":
                    artifact.state = "ORPHANED"
                else:
                    artifact.state = "DELETED"
                reconciled += 1
        return reconciled

    async def _artifact_file_is_intact(
        self,
        artifact: ReceptionAudioArtifact,
    ) -> bool:
        candidates = _confined_artifact_generation_paths(
            self._reception_service.audio_root,
            tenant_id=str(artifact.tenant_id),
            reception_id=int(artifact.reception_id),
            persisted_path=str(artifact.path),
        )
        if (
            candidates is None
            or not candidates[0].is_file()
            or artifact.sha256 is None
            or artifact.size_bytes is None
        ):
            return False
        size_bytes = candidates[0].stat().st_size
        if size_bytes != artifact.size_bytes:
            return False
        return await asyncio.to_thread(_sha256_file, candidates[0]) == artifact.sha256

    async def _load_claimed(
        self,
        operation_id: int,
        lease_token: str,
    ) -> tuple[ReceptionAudioOperation, ReceptionTimelineRevision]:
        async with self._session_factory() as db:
            operation = (
                await db.execute(
                    select(ReceptionAudioOperation).where(
                        ReceptionAudioOperation.id == operation_id,
                        ReceptionAudioOperation.lease_token == lease_token,
                    )
                )
            ).scalar_one()
            revision = await db.get(
                ReceptionTimelineRevision,
                operation.timeline_revision_id,
            )
            if revision is None:
                raise RuntimeError("audio plan revision disappeared")
            return operation, revision

    async def _set_stage(
        self,
        operation_id: int,
        lease_token: str,
        status: str,
        progress: float,
    ) -> None:
        allowed_predecessors: dict[str, tuple[str, ...]] = {
            "probing": ("claimed",),
            "slicing": ("probing",),
            "assembling": ("slicing",),
            "encrypting": ("assembling",),
            "verifying": ("probing", "assembling", "encrypting"),
        }
        predecessors = allowed_predecessors.get(status)
        if predecessors is None:
            raise ValueError(f"unsupported audio operation stage: {status}")
        async with self._session_factory() as db, db.begin():
            result = await db.execute(
                update(ReceptionAudioOperation)
                .where(
                    ReceptionAudioOperation.id == operation_id,
                    ReceptionAudioOperation.lease_token == lease_token,
                    ReceptionAudioOperation.status.in_(predecessors),
                )
                .values(
                    status=status,
                    progress=progress,
                    lease_expires_at=datetime.now(UTC) + timedelta(seconds=self._lease_sec),
                )
            )
            if getattr(result, "rowcount", 0) != 1:
                raise RuntimeError("audio operation lease was lost")

    async def _cancel_claimed(self, operation_id: int, lease_token: str) -> None:
        cleanup: list[tuple[str, int, str]] = []
        async with self._session_factory() as db, db.begin():
            operation = (
                await db.execute(
                    select(ReceptionAudioOperation)
                    .where(
                        ReceptionAudioOperation.id == operation_id,
                        ReceptionAudioOperation.lease_token == lease_token,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if operation is None:
                return
            artifacts = list(
                (
                    await db.execute(
                        select(ReceptionAudioArtifact)
                        .where(
                            ReceptionAudioArtifact.tenant_id == operation.tenant_id,
                            ReceptionAudioArtifact.reception_id == operation.reception_id,
                            ReceptionAudioArtifact.operation_id == operation_id,
                            ReceptionAudioArtifact.state.in_(("PREPARING", "READY")),
                        )
                        .with_for_update()
                    )
                ).scalars()
            )
            for artifact in artifacts:
                artifact.state = "FAILED" if artifact.state == "PREPARING" else "ORPHANED"
                cleanup.append(
                    (
                        str(artifact.tenant_id),
                        int(artifact.reception_id),
                        str(artifact.path),
                    )
                )
            await db.execute(
                update(ReceptionAudioOperation)
                .where(
                    ReceptionAudioOperation.id == operation_id,
                    ReceptionAudioOperation.lease_token == lease_token,
                )
                .values(
                    status="cancelled",
                    progress=1.0,
                    finished_at=datetime.now(UTC),
                    lease_token=None,
                    lease_expires_at=None,
                )
            )
        for tenant_id, reception_id, path in cleanup:
            await self._delete_artifact_generation(
                tenant_id=tenant_id,
                reception_id=reception_id,
                persisted_path=path,
            )

    async def _publish_in_session(
        self,
        db: AsyncSession,
        *,
        operation_id: int,
        lease_token: str,
        revision_id: int,
        manifest: list[dict[str, Any]],
        reception: Reception,
        final_mappings: tuple[ReceptionRecording, ...],
        prepared: Any,
        previous_merged_audio_path: str | None,
    ) -> None:
        """Attach a generation inside ReceptionService's optimistic transaction."""

        now = datetime.now(UTC)
        operation = (
            await db.execute(
                select(ReceptionAudioOperation)
                .where(
                    ReceptionAudioOperation.id == operation_id,
                    ReceptionAudioOperation.lease_token == lease_token,
                    ReceptionAudioOperation.status == "verifying",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if operation is None:
            raise RuntimeError("audio operation lease was lost before commit")
        if operation.cancel_requested:
            raise _AudioOperationCancelledError
        revision = await db.get(
            ReceptionTimelineRevision,
            revision_id,
            with_for_update=True,
        )
        if revision is None or revision.state != "STAGING":
            raise RuntimeError("audio timeline revision is not publishable")
        if reception.id != operation.reception_id or reception.tenant_id != operation.tenant_id:
            raise RuntimeError("audio operation publication scope changed")

        recording_ids = {int(item["recording_id"]) for item in manifest}
        source_rows = list(
            (
                await db.execute(
                    select(Recording)
                    .where(
                        Recording.tenant_id == operation.tenant_id,
                        Recording.id.in_(recording_ids),
                    )
                    .with_for_update()
                )
            ).scalars()
        )
        sources_by_id = {int(recording.id): recording for recording in source_rows}
        if set(sources_by_id) != recording_ids:
            raise RuntimeError("signed audio source disappeared before commit")
        for item in manifest:
            recording = sources_by_id[int(item["recording_id"])]
            signed_revision = int(item.get("recording_source_revision") or 0)
            if int(getattr(recording, "source_revision", 0) or 0) != signed_revision:
                raise RuntimeError("signed audio source revision changed before commit")
            for manifest_key, model_field in (
                ("recording_sha256", "audio_sha256"),
                ("recording_size_bytes", "audio_size_bytes"),
            ):
                signed_value = item.get(manifest_key)
                if signed_value is not None and getattr(recording, model_field) != signed_value:
                    raise RuntimeError(f"signed audio source {model_field} changed before commit")
            verified_duration_ms = verified_recording_duration_ms(recording)
            if verified_duration_ms is None or int(item["source_end_ms"]) > verified_duration_ms:
                raise RuntimeError("signed audio source duration is unavailable or changed")

        operation.status = "committing"
        operation.progress = 0.9
        await db.execute(
            update(ReceptionTimelineRevision)
            .where(
                ReceptionTimelineRevision.tenant_id == operation.tenant_id,
                ReceptionTimelineRevision.reception_id == operation.reception_id,
                ReceptionTimelineRevision.state == "ACTIVE",
                ReceptionTimelineRevision.id != revision_id,
            )
            .values(state="SUPERSEDED")
        )
        revision.state = "ACTIVE"
        revision.activated_at = now
        reception.active_timeline_revision_id = revision_id

        by_recording_id = {int(item["recording_id"]): item for item in manifest}
        if {int(mapping.recording_id) for mapping in final_mappings} != set(by_recording_id):
            raise RuntimeError("committed reception sources differ from the signed plan")
        for mapping in final_mappings:
            item = by_recording_id[int(mapping.recording_id)]
            actual_geometry = (
                seconds_to_milliseconds(mapping.source_start_sec),
                (
                    seconds_to_milliseconds(mapping.source_end_sec)
                    if mapping.source_end_sec is not None
                    else None
                ),
                seconds_to_milliseconds(mapping.timeline_start_sec),
                seconds_to_milliseconds(mapping.timeline_end_sec),
                seconds_to_milliseconds(mapping.gap_before_sec),
            )
            signed_geometry = (
                int(item["source_start_ms"]),
                int(item["source_end_ms"]),
                int(item["timeline_start_ms"]),
                int(item["timeline_end_ms"]),
                int(item["gap_before_ms"]),
            )
            if actual_geometry != signed_geometry:
                raise RuntimeError("committed reception geometry differs from the signed plan")
            mapping.timeline_revision_id = revision_id
            mapping.source_start_ms = signed_geometry[0]
            mapping.source_end_ms = signed_geometry[1]
            mapping.timeline_start_ms = signed_geometry[2]
            mapping.timeline_end_ms = signed_geometry[3]
            mapping.gap_before_ms = signed_geometry[4]

        for model in (DialogueUnit, DialogueTagAssignment, DialogueStateTransition):
            await db.execute(
                update(model)
                .where(
                    model.tenant_id == operation.tenant_id,
                    model.reception_id == operation.reception_id,
                )
                .values(timeline_revision_id=revision_id)
            )

        if prepared is not None and operation.mode in {"physical", "both"}:
            artifact_path = Path(str(prepared.merged_audio_path))
            if not artifact_path.is_file():
                raise RuntimeError("prepared physical artifact disappeared before commit")
            artifact_size = artifact_path.stat().st_size
            artifact_hash = await asyncio.to_thread(_sha256_file, artifact_path)
            artifact = (
                await db.execute(
                    select(ReceptionAudioArtifact)
                    .where(
                        ReceptionAudioArtifact.tenant_id == operation.tenant_id,
                        ReceptionAudioArtifact.reception_id == operation.reception_id,
                        ReceptionAudioArtifact.operation_id == operation.id,
                        ReceptionAudioArtifact.state == "READY",
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if artifact is None:
                raise RuntimeError("verified physical artifact is not READY")
            if (
                artifact.path != str(artifact_path)
                or artifact.size_bytes != artifact_size
                or artifact.sha256 != artifact_hash
                or artifact.timeline_revision_id != revision_id
            ):
                raise RuntimeError("verified physical artifact changed before attachment")
            await db.execute(
                update(ReceptionAudioArtifact)
                .where(
                    ReceptionAudioArtifact.tenant_id == operation.tenant_id,
                    ReceptionAudioArtifact.reception_id == operation.reception_id,
                    ReceptionAudioArtifact.state == "ATTACHED",
                    ReceptionAudioArtifact.id != artifact.id,
                )
                .values(state="RETIRED", retired_at=now)
            )
            artifact.state = "ATTACHED"
            artifact.attached_at = now
        elif operation.mode in {"physical", "both"}:
            raise RuntimeError("physical operation committed without a verified artifact")
        elif previous_merged_audio_path != reception.merged_audio_path:
            await db.execute(
                update(ReceptionAudioArtifact)
                .where(
                    ReceptionAudioArtifact.tenant_id == operation.tenant_id,
                    ReceptionAudioArtifact.reception_id == operation.reception_id,
                    ReceptionAudioArtifact.state == "ATTACHED",
                )
                .values(state="RETIRED", retired_at=now)
            )

        operation.status = "succeeded"
        operation.progress = 1.0
        operation.finished_at = now
        operation.lease_token = None
        operation.lease_expires_at = None

    async def _fail_operation(
        self,
        operation_id: int,
        lease_token: str,
        exc: Exception,
    ) -> None:
        cleanup: list[tuple[str, int, str]] = []
        async with self._session_factory() as db, db.begin():
            operation = (
                await db.execute(
                    select(ReceptionAudioOperation)
                    .where(
                        ReceptionAudioOperation.id == operation_id,
                        ReceptionAudioOperation.lease_token == lease_token,
                        ReceptionAudioOperation.status.not_in(("succeeded", "cancelled")),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if operation is None:
                return
            artifacts = list(
                (
                    await db.execute(
                        select(ReceptionAudioArtifact)
                        .where(
                            ReceptionAudioArtifact.tenant_id == operation.tenant_id,
                            ReceptionAudioArtifact.reception_id == operation.reception_id,
                            ReceptionAudioArtifact.operation_id == operation_id,
                            ReceptionAudioArtifact.state.in_(("PREPARING", "READY")),
                        )
                        .with_for_update()
                    )
                ).scalars()
            )
            for artifact in artifacts:
                artifact.state = "FAILED" if artifact.state == "PREPARING" else "ORPHANED"
                cleanup.append(
                    (
                        str(artifact.tenant_id),
                        int(artifact.reception_id),
                        str(artifact.path),
                    )
                )
            await db.execute(
                update(ReceptionAudioOperation)
                .where(
                    ReceptionAudioOperation.id == operation_id,
                    ReceptionAudioOperation.lease_token == lease_token,
                    ReceptionAudioOperation.status.not_in(("succeeded", "cancelled")),
                )
                .values(
                    status="failed",
                    progress=1.0,
                    error_code=type(exc).__name__[:64],
                    error_message=str(exc)[:1000],
                    finished_at=datetime.now(UTC),
                    lease_token=None,
                    lease_expires_at=None,
                )
            )
        for tenant_id, reception_id, path in cleanup:
            await self._delete_artifact_generation(
                tenant_id=tenant_id,
                reception_id=reception_id,
                persisted_path=path,
            )

    async def _delete_artifact_generation(
        self,
        *,
        tenant_id: str,
        reception_id: int,
        persisted_path: str,
    ) -> bool:
        candidates = _confined_artifact_generation_paths(
            self._reception_service.audio_root,
            tenant_id=tenant_id,
            reception_id=reception_id,
            persisted_path=persisted_path,
        )
        if candidates is None:
            return False
        try:
            for candidate in candidates:
                await asyncio.to_thread(candidate.unlink, missing_ok=True)
        except OSError:
            return False
        return True


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _aware_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _confined_artifact_generation_paths(
    audio_root: Path,
    *,
    tenant_id: str,
    reception_id: int,
    persisted_path: str,
) -> tuple[Path, ...] | None:
    """Resolve only this tenant/reception's generated WAV generation."""
    try:
        root = audio_root.resolve(strict=True)
        expected_relative_parent = Path(
            f"assembled_audio/{tenant_id}/receptions/reception-{reception_id}"
        )
        tenant_component = Path(tenant_id)
        if (
            expected_relative_parent.is_absolute()
            or ".." in expected_relative_parent.parts
            or not tenant_id
            or "\x00" in tenant_id
            or tenant_component.is_absolute()
            or len(tenant_component.parts) != 1
            or tenant_component.name in {".", ".."}
        ):
            return None
        raw = Path(persisted_path)
        absolute = raw if raw.is_absolute() else root / raw
        lexical_relative = absolute.relative_to(root)
        if (
            ".." in lexical_relative.parts
            or lexical_relative.parent != expected_relative_parent
            or _GENERATED_ARTIFACT_NAME.fullmatch(lexical_relative.name) is None
        ):
            return None
        cursor = root
        for part in lexical_relative.parts:
            cursor /= part
            if cursor.is_symlink():
                return None
        resolved = absolute.resolve(strict=False)
        expected_parent = (root / expected_relative_parent).resolve(strict=False)
        if resolved.parent != expected_parent:
            return None
        if resolved.name.endswith(".wav.enc"):
            plaintext = Path(str(resolved)[: -len(".enc")])
            return (resolved, plaintext)
        encrypted = Path(f"{resolved}.enc")
        return (resolved, encrypted)
    except (OSError, RuntimeError, ValueError):
        return None


__all__ = ["AudioPlanResult", "ReceptionAudioOperationService"]
