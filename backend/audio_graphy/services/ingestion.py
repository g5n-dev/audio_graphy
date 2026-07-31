"""Ingestion service — recording registration, listing, detail, reindex.

Encapsulates all recording-related business logic so that routers
are thin wrappers.

M6 PIPL §14.3 integration (optional, opt-in via constructor):
    - crypto: AudioCrypto — when provided, audio files are envelope-encrypted
      at upload time and ``audio_encrypted_path`` + ``audio_encryption_meta``
      are populated.
    - pii_scrubber: PIIScrubber — when provided, segments get a scrubbed
      text mirror via ``update_segment_text``.
    - audit: AuditWriter — when provided, ``register_recording`` writes an
      audit record for each upload.

See: docs/m3-architecture.md §10.1, docs/m6-architecture.md §3.6.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.errors import (
    APIError,
    DuplicateRecordingError,
    FileNotFoundError400,
    RecordingNotFoundError,
    ValidationError,
)
from audio_graphy.models.chunk import Chunk
from audio_graphy.models.enums import PipelineState, RecordingStatus
from audio_graphy.models.pipeline import (
    DEFAULT_REQUIRED_PROJECTIONS,
    RecordingPipelineRun,
)
from audio_graphy.models.recording import Recording
from audio_graphy.models.segment import Segment
from audio_graphy.models.tag_current import TagCurrent
from audio_graphy.schemas.recordings import RecordingCreate
from audio_graphy.services.agent_identity import resolve_unique_agent_user_id

if TYPE_CHECKING:
    from audio_graphy.core.audit import AuditWriter
    from audio_graphy.core.crypto import AudioCrypto
    from audio_graphy.core.pii import PIIScrubber

logger = logging.getLogger(__name__)
_AUDIO_EXTENSIONS = frozenset({".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"})
_DEFAULT_MAX_AUDIO_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _SourceFacts:
    sha256: str
    size_bytes: int
    duration_ms: int | None
    sample_rate: int | None
    channels: int | None


@dataclass(frozen=True, slots=True)
class QueuedPipelineRun:
    """Compatibility-safe result for a newly queued processing operation."""

    recording: Recording
    run: RecordingPipelineRun


def _hash_source(path: Path) -> tuple[str, int]:
    """Hash one stable regular file and reject a concurrent source swap."""
    before = path.stat(follow_symlinks=False)
    digest = hashlib.sha256()
    size_bytes = 0
    with path.open("rb") as source:
        opened = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
        ):
            raise RuntimeError("audio source changed before hashing")
        while block := source.read(1024 * 1024):
            digest.update(block)
            size_bytes += len(block)
        after_open = os.fstat(source.fileno())
    after_path = path.stat(follow_symlinks=False)
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after_open = (
        after_open.st_dev,
        after_open.st_ino,
        after_open.st_size,
        after_open.st_mtime_ns,
    )
    identity_after_path = (
        after_path.st_dev,
        after_path.st_ino,
        after_path.st_size,
        after_path.st_mtime_ns,
    )
    if identity_before != identity_after_open or identity_before != identity_after_path:
        raise RuntimeError("audio source changed while hashing")
    return digest.hexdigest(), size_bytes


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


class IngestionService:
    """Recording CRUD + pipeline trigger service.

    Args:
        session_factory: async session maker for DB operations.
        crypto: Optional AudioCrypto; when set, register_recording encrypts
            the audio file and persists audio_encrypted_path.
        pii_scrubber: Optional PIIScrubber; used by update_segment_text.
        audit: Optional AuditWriter for fire-and-forget audit records.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        crypto: AudioCrypto | None = None,
        pii_scrubber: PIIScrubber | None = None,
        audit: AuditWriter | None = None,
        allowed_root: Path | None = None,
        max_audio_bytes: int = _DEFAULT_MAX_AUDIO_BYTES,
    ) -> None:
        if max_audio_bytes <= 0:
            raise ValueError("max_audio_bytes must be positive")
        self._session_factory = session_factory
        self._crypto = crypto
        self._pii_scrubber = pii_scrubber
        self._audit = audit
        self._allowed_root = Path(allowed_root) if allowed_root is not None else None
        self._max_audio_bytes = max_audio_bytes

    def _validate_managed_audio_path(self, raw_path: str) -> Path:
        """Resolve an API-supplied audio path below the managed working root."""
        candidate = Path(raw_path)
        if "\x00" in raw_path:
            raise ValidationError(
                "Audio path is invalid",
                code="AUDIO_PATH_INVALID",
            )
        if self._allowed_root is None:
            if candidate.exists():
                self._validate_audio_size(candidate)
            return candidate
        try:
            root = self._allowed_root.resolve(strict=True)
            unresolved = candidate if candidate.is_absolute() else root / candidate
            resolved = unresolved.resolve(strict=True)
            relative = resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValidationError(
                "Audio path must stay below the configured working directory",
                code="AUDIO_PATH_OUTSIDE_ROOT",
            ) from exc
        cursor = root
        for component in relative.parts:
            cursor = cursor / component
            if cursor.is_symlink():
                raise ValidationError(
                    "Audio path cannot traverse a symbolic link",
                    code="AUDIO_PATH_SYMLINK",
                )
        file_stat = resolved.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_size <= 0
            or file_stat.st_nlink != 1
            or resolved.suffix.casefold() not in _AUDIO_EXTENSIONS
        ):
            raise ValidationError(
                "Audio path must name one non-empty supported regular file",
                code="AUDIO_FILE_INVALID",
            )
        if file_stat.st_size > self._max_audio_bytes:
            raise ValidationError(
                "Audio file exceeds the configured size limit",
                code="AUDIO_FILE_TOO_LARGE",
                detail={
                    "size_bytes": file_stat.st_size,
                    "max_size_bytes": self._max_audio_bytes,
                },
            )
        return resolved

    def _validate_audio_size(self, path: Path) -> None:
        try:
            size_bytes = os.stat(path, follow_symlinks=False).st_size
        except OSError:
            return
        if size_bytes > self._max_audio_bytes:
            raise ValidationError(
                "Audio file exceeds the configured size limit",
                code="AUDIO_FILE_TOO_LARGE",
                detail={
                    "size_bytes": size_bytes,
                    "max_size_bytes": self._max_audio_bytes,
                },
            )

    async def _probe_audio(self, path: Path) -> tuple[int | None, int | None, int | None]:
        """Best-effort media probe; source hash/size remain mandatory facts."""
        command = (
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate,channels,duration:format=duration",
            "-of",
            "json",
            str(path),
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=30)
            if process.returncode != 0:
                return None, None, None
            payload = json.loads(stdout)
            streams = payload.get("streams")
            if not isinstance(streams, list) or not streams:
                return None, None, None
            stream = streams[0]
            if not isinstance(stream, dict):
                return None, None, None
            duration_value = stream.get("duration")
            if duration_value is None and isinstance(payload.get("format"), dict):
                duration_value = payload["format"].get("duration")
            if duration_value is None:
                return None, None, None
            duration = float(duration_value)
            computed_duration_ms = round(duration * 1000)
            duration_ms = (
                computed_duration_ms
                if math.isfinite(duration) and computed_duration_ms >= 0
                else None
            )
            return (
                duration_ms,
                _positive_int(stream.get("sample_rate")),
                _positive_int(stream.get("channels")),
            )
        except (TimeoutError, OSError, ValueError, TypeError, json.JSONDecodeError):
            return None, None, None

    async def _collect_source_facts(self, path: Path) -> _SourceFacts:
        hash_result, probe_result = await asyncio.gather(
            asyncio.to_thread(_hash_source, path),
            self._probe_audio(path),
        )
        sha256, size_bytes = hash_result
        duration_ms, sample_rate, channels = probe_result
        return _SourceFacts(
            sha256=sha256,
            size_bytes=size_bytes,
            duration_ms=duration_ms,
            sample_rate=sample_rate,
            channels=channels,
        )

    @staticmethod
    def _ingestion_config_fingerprint(body: RecordingCreate, path: Path) -> str:
        payload = {
            "store_id": body.store_id,
            "agent_name": body.agent_name,
            "customer_hash": body.customer_hash,
            "path": str(path),
            "recorded_at": body.recorded_at.isoformat() if body.recorded_at else None,
            "prompt_version": body.prompt_version,
            "pipeline_contract": "recording-generation-v1",
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    @staticmethod
    def _normalize_ingestion_key(idempotency_key: str | None) -> str:
        material = idempotency_key if idempotency_key is not None else uuid.uuid4().hex
        return f"ingest:{hashlib.sha256(material.encode()).hexdigest()}"

    async def register_recording(
        self,
        tenant_id: str,
        body: RecordingCreate,
        *,
        idempotency_key: str | None = None,
    ) -> Recording:
        """Register a new recording (creates a queued record for pipeline).

        When AudioCrypto is configured (M6), the plaintext audio file is
        envelope-encrypted to ``<path>.enc`` and ``audio_encrypted_path``
        + ``audio_encryption_meta`` are persisted. The plaintext file is
        left in place (the pipeline worker still expects it); the M6
        retention cron is responsible for removing plaintext copies once
        the recording transitions to ``indexed``.

        Args:
            tenant_id: Tenant scope.
            body: Recording creation data.

        Returns:
            The created Recording ORM object.

        Raises:
            FileNotFoundError400: If the audio path doesn't exist.
            DuplicateRecordingError: If (tenant_id, path) already registered.
        """
        validated_path = self._validate_managed_audio_path(body.path)
        # Direct service callers may opt out of root confinement; retain the
        # established not-found contract for that compatibility path.
        if not os.path.exists(validated_path):
            raise FileNotFoundError400(
                message=f"Audio file not found: {body.path}",
                detail={"path": body.path},
            )
        self._validate_audio_size(validated_path)

        # Determine recorded_at
        recorded_at = body.recorded_at
        if recorded_at is None:
            try:
                mtime = os.path.getmtime(validated_path)
                recorded_at = datetime.fromtimestamp(mtime, tz=UTC)
            except OSError:
                recorded_at = None

        try:
            facts = await self._collect_source_facts(validated_path)
        except (OSError, RuntimeError) as exc:
            raise ValidationError(
                "Audio source changed while it was being registered",
                code="AUDIO_SOURCE_CHANGED",
            ) from exc

        # Unit-level failure tests intentionally omit a database factory. Keep
        # that path fail-closed while still avoiding the historical fixed
        # ``<path>.enc`` overwrite target.
        if self._session_factory is None:
            assert self._crypto is not None
            temporary = Path(f"{validated_path}.pending-{uuid.uuid4().hex}.enc")
            try:
                await asyncio.to_thread(
                    self._crypto.encrypt_file,
                    validated_path,
                    temporary,
                )
                raise RuntimeError("database reservation is unavailable")
            except Exception as exc:
                temporary.unlink(missing_ok=True)
                raise APIError(
                    "Audio encryption failed",
                    code="AUDIO_ENCRYPTION_FAILED",
                    status_code=500,
                ) from exc

        normalized_key = self._normalize_ingestion_key(idempotency_key)
        config_fingerprint = self._ingestion_config_fingerprint(body, validated_path)

        async with self._session_factory() as session:
            # Explicit idempotency is tenant-scoped and independent from file
            # path/content. Legacy direct callers without a key retain the old
            # sequential duplicate-path error contract.
            if idempotency_key is None:
                existing_path = await session.execute(
                    select(Recording).where(
                        Recording.tenant_id == tenant_id,
                        Recording.path == str(validated_path),
                    )
                )
                if existing_path.scalar_one_or_none() is not None:
                    raise DuplicateRecordingError(
                        detail={"path": str(validated_path), "tenant_id": tenant_id},
                    )
            existing_run = (
                await session.execute(
                    select(RecordingPipelineRun).where(
                        RecordingPipelineRun.tenant_id == tenant_id,
                        RecordingPipelineRun.idempotency_key == normalized_key,
                    )
                )
            ).scalar_one_or_none()
            if existing_run is not None:
                if (
                    existing_run.source_fingerprint != facts.sha256
                    or existing_run.config_fingerprint != config_fingerprint
                ):
                    raise ValidationError(
                        "Idempotency key was already used with another recording request",
                        code="IDEMPOTENCY_KEY_REUSED",
                    )
                existing_recording = await session.get(
                    Recording,
                    existing_run.recording_id,
                )
                if existing_recording is None:
                    raise RuntimeError("idempotent recording reservation is corrupt")
                return existing_recording

            agent_user_id = await resolve_unique_agent_user_id(
                session,
                tenant_id=tenant_id,
                agent_name=body.agent_name,
            )
            recording = Recording(
                tenant_id=tenant_id,
                store_id=body.store_id,
                agent_name=body.agent_name,
                agent_user_id=agent_user_id,
                customer_hash=body.customer_hash,
                path=str(validated_path),
                status=(
                    RecordingStatus.PROCESSING.value
                    if self._crypto is not None
                    else RecordingStatus.QUEUED.value
                ),
                pipeline_state=PipelineState.PENDING.value,
                recorded_at=recorded_at,
                prompt_version=body.prompt_version,
                audio_duration_ms=facts.duration_ms,
                audio_sha256=facts.sha256,
                audio_size_bytes=facts.size_bytes,
                audio_sample_rate=facts.sample_rate,
                audio_channels=facts.channels,
                source_revision=1,
            )
            session.add(recording)
            await session.flush()
            run = RecordingPipelineRun(
                tenant_id=tenant_id,
                recording_id=recording.id,
                generation=1,
                idempotency_key=normalized_key,
                source_fingerprint=facts.sha256,
                config_fingerprint=config_fingerprint,
                state="claimed" if self._crypto is not None else "queued",
                lease_owner="ingestion-reservation" if self._crypto is not None else None,
                attempt_count=0,
                required_projections=list(DEFAULT_REQUIRED_PROJECTIONS),
                completed_projections=[],
            )
            session.add(run)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                replay = (
                    await session.execute(
                        select(RecordingPipelineRun).where(
                            RecordingPipelineRun.tenant_id == tenant_id,
                            RecordingPipelineRun.idempotency_key == normalized_key,
                        )
                    )
                ).scalar_one_or_none()
                if replay is None:
                    raise
                replay_recording = await session.get(Recording, replay.recording_id)
                if replay_recording is None:
                    raise
                return replay_recording
            await session.refresh(recording)
            await session.refresh(run)

        encrypted_path: str | None = None
        if self._crypto is not None:
            temporary = Path(
                f"{validated_path}.recording-{recording.id}.source-1."
                f"pending-{uuid.uuid4().hex}.enc"
            )
            published = Path(f"{validated_path}.recording-{recording.id}.source-1.enc")
            try:
                if published.exists():
                    raise RuntimeError("reserved ciphertext target already exists")
                meta = await asyncio.to_thread(
                    self._crypto.encrypt_file,
                    validated_path,
                    temporary,
                )
                if (
                    meta.size_bytes != facts.size_bytes
                    or meta.sha256 != facts.sha256
                    or meta.size_bytes > self._max_audio_bytes
                ):
                    raise RuntimeError("encrypted source identity did not match reservation")
                await asyncio.to_thread(temporary.replace, published)
                async with self._session_factory() as session, session.begin():
                    reserved = (
                        await session.execute(
                            select(Recording)
                            .where(
                                Recording.id == recording.id,
                                Recording.tenant_id == tenant_id,
                            )
                            .with_for_update()
                        )
                    ).scalar_one()
                    reserved_run = (
                        await session.execute(
                            select(RecordingPipelineRun)
                            .where(RecordingPipelineRun.id == run.id)
                            .with_for_update()
                        )
                    ).scalar_one()
                    if (
                        reserved.audio_encrypted_path is not None
                        or reserved.audio_sha256 != facts.sha256
                        or reserved.source_revision != 1
                        or reserved_run.state != "claimed"
                    ):
                        raise RuntimeError("recording reservation changed before publish")
                    reserved.audio_encrypted_path = str(published)
                    reserved.audio_encryption_meta = {
                        "master_key_id": meta.master_key_id,
                        "data_key_id": meta.data_key_id,
                        "size_bytes": meta.size_bytes,
                        "sha256": meta.sha256,
                        "source_revision": 1,
                    }
                    reserved.status = RecordingStatus.QUEUED.value
                    reserved_run.state = "queued"
                    reserved_run.lease_owner = None
                    reserved_run.lease_expires_at = None
                encrypted_path = str(published)
                recording.audio_encrypted_path = encrypted_path
                recording.audio_encryption_meta = {
                    "master_key_id": meta.master_key_id,
                    "data_key_id": meta.data_key_id,
                    "size_bytes": meta.size_bytes,
                    "sha256": meta.sha256,
                    "source_revision": 1,
                }
                recording.status = RecordingStatus.QUEUED.value
            except Exception as exc:
                temporary.unlink(missing_ok=True)
                published.unlink(missing_ok=True)
                async with self._session_factory() as cleanup:
                    cleanup_recording = await cleanup.get(Recording, recording.id)
                    if (
                        cleanup_recording is not None
                        and cleanup_recording.audio_encrypted_path is None
                    ):
                        await cleanup.delete(cleanup_recording)
                        await cleanup.commit()
                logger.error(
                    "Audio encryption failed for %s: %s",
                    validated_path,
                    exc,
                    exc_info=True,
                )
                raise APIError(
                    "Audio encryption failed",
                    code="AUDIO_ENCRYPTION_FAILED",
                    status_code=500,
                ) from exc

        if self._audit is not None:
            await self._audit.record(
                tenant_id=tenant_id,
                user_id=None,
                action="recording.uploaded",
                target=f"recording:{recording.id}",
                after={
                    "path": recording.path,
                    "encrypted": encrypted_path is not None,
                    "audio_sha256": facts.sha256,
                    "source_revision": 1,
                    "pipeline_run_id": run.id,
                },
            )

        logger.info(
            "Registered recording %d for tenant %s (path=%s, encrypted=%s)",
            recording.id,
            tenant_id,
            body.path,
            encrypted_path is not None,
        )
        return recording

    async def update_segment_text(
        self,
        segment: Segment,
        raw_text: str,
    ) -> None:
        """Set ``segment.transcript`` and ``segment.text_scrubbed``.

        Called by the pipeline after ASR completes. When PIIScrubber is
        configured (M6), the scrubbed text is stored alongside the raw
        transcript for the query layer to consume.

        Args:
            segment: Segment ORM instance (must be persisted).
            raw_text: Raw ASR transcript.
        """
        segment.transcript = raw_text
        if self._pii_scrubber is not None:
            segment.text_scrubbed = self._pii_scrubber.scrub_simple(raw_text)
        else:
            segment.text_scrubbed = raw_text

    async def list_recordings(
        self,
        tenant_id: str,
        *,
        agent_user_id: int | None = None,
        store_id: str | None = None,
        status: str | None = None,
        agent_name: str | None = None,
        recorded_from: datetime | None = None,
        recorded_to: datetime | None = None,
        sort: str = "-recorded_at",
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Recording], int]:
        """List recordings with filters and pagination.

        Args:
            tenant_id: Tenant scope.
            agent_user_id: If set, force the immutable recording owner id.
            store_id: Optional store filter.
            status: Optional status filter.
            agent_name: Optional display-name filter for non-agent users.
            recorded_from: Optional recorded_at lower bound.
            recorded_to: Optional recorded_at upper bound.
            sort: Sort field (-recorded_at / recorded_at / -created_at / created_at).
            page: Page number (1-based).
            page_size: Items per page.

        Returns:
            Tuple of (recordings_list, total_count).
        """
        async with self._session_factory() as session:
            stmt = select(Recording).where(Recording.tenant_id == tenant_id)

            # Stable owner scope always overrides mutable display-name filters.
            if agent_user_id is not None:
                stmt = stmt.where(Recording.agent_user_id == agent_user_id)
            elif agent_name is not None:
                stmt = stmt.where(Recording.agent_name == agent_name)
            if store_id is not None:
                stmt = stmt.where(Recording.store_id == store_id)
            if status is not None:
                stmt = stmt.where(Recording.status == status)
            if recorded_from is not None:
                stmt = stmt.where(Recording.recorded_at >= recorded_from)
            if recorded_to is not None:
                stmt = stmt.where(Recording.recorded_at <= recorded_to)

            # Sort
            sort_map: dict[str, Any] = {
                "-recorded_at": Recording.recorded_at.desc(),
                "recorded_at": Recording.recorded_at.asc(),
                "-created_at": Recording.created_at.desc(),
                "created_at": Recording.created_at.asc(),
            }
            order_col = sort_map.get(sort, Recording.recorded_at.desc())
            stmt = stmt.order_by(order_col)

            # Count total
            count_stmt = select(func.count()).select_from(stmt.subquery())
            total_result = await session.execute(count_stmt)
            total = total_result.scalar_one()

            # Paginate
            offset = (page - 1) * page_size
            stmt = stmt.offset(offset).limit(page_size)
            result = await session.execute(stmt)
            recordings = list(result.scalars().all())

        return recordings, total

    async def get_recording(
        self,
        recording_id: int,
        tenant_id: str,
        *,
        agent_user_id: int | None = None,
    ) -> Recording:
        """Get a single recording by ID (with tenant + agent isolation).

        Args:
            recording_id: Recording ID.
            tenant_id: Tenant scope.
            agent_user_id: If set, force the immutable recording owner id.

        Returns:
            Recording ORM object.

        Raises:
            RecordingNotFoundError: If not found or cross-tenant.
        """
        async with self._session_factory() as session:
            stmt = select(Recording).where(
                Recording.id == recording_id,
                Recording.tenant_id == tenant_id,
            )
            if agent_user_id is not None:
                stmt = stmt.where(Recording.agent_user_id == agent_user_id)

            result = await session.execute(stmt)
            recording = result.scalar_one_or_none()

            if recording is None:
                raise RecordingNotFoundError(
                    detail={"recording_id": recording_id},
                )
        return recording

    async def get_recording_detail(
        self,
        recording_id: int,
        tenant_id: str,
        *,
        agent_user_id: int | None = None,
    ) -> dict[str, Any]:
        """Get recording detail with segments_count, chunks_count, current_tags.

        Args:
            recording_id: Recording ID.
            tenant_id: Tenant scope.
            agent_user_id: Optional stable agent owner filter.

        Returns:
            Dict with recording + summary fields.
        """
        recording = await self.get_recording(
            recording_id,
            tenant_id,
            agent_user_id=agent_user_id,
        )

        async with self._session_factory() as session:
            segment_scope = Segment.recording_id == recording_id
            chunk_scope = Chunk.recording_id == recording_id
            if recording.active_pipeline_run_id is not None:
                segment_scope = segment_scope & (
                    Segment.pipeline_run_id == recording.active_pipeline_run_id
                )
                chunk_scope = chunk_scope & (
                    Chunk.pipeline_run_id == recording.active_pipeline_run_id
                )
            # Segments count
            seg_count_result = await session.execute(
                select(func.count()).where(segment_scope)
            )
            segments_count = seg_count_result.scalar_one()

            # Chunks count
            chunk_count_result = await session.execute(
                select(func.count()).where(chunk_scope)
            )
            chunks_count = chunk_count_result.scalar_one()

            # Current tags
            tags_result = await session.execute(
                select(TagCurrent).where(
                    TagCurrent.recording_id == recording_id,
                    TagCurrent.tenant_id == tenant_id,
                )
            )
            current_tags = tags_result.scalars().all()

        return {
            "recording": recording,
            "segments_count": segments_count,
            "chunks_count": chunks_count,
            "current_tags": current_tags,
        }

    async def trigger_reindex(
        self,
        recording_id: int,
        tenant_id: str,
        *,
        force: bool = False,
        idempotency_key: str | None = None,
    ) -> Recording:
        """Reset a recording to queued for re-indexing.

        Args:
            recording_id: Recording ID.
            tenant_id: Tenant scope.
            force: Ignored at service level (pipeline handles dedup).

        Returns:
            Updated Recording ORM object.

        Raises:
            RecordingNotFoundError: If not found.
        """
        queued = await self.queue_reindex(
            recording_id,
            tenant_id,
            force=force,
            idempotency_key=idempotency_key,
        )
        return queued.recording

    async def queue_reindex(
        self,
        recording_id: int,
        tenant_id: str,
        *,
        force: bool = False,
        idempotency_key: str | None = None,
    ) -> QueuedPipelineRun:
        """Append an inactive pipeline generation without hiding the active one."""
        async with self._session_factory() as session, session.begin():
            recording = (
                await session.execute(
                    select(Recording)
                    .where(
                        Recording.id == recording_id,
                        Recording.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if recording is None:
                raise RecordingNotFoundError(detail={"recording_id": recording_id})

            key_material = idempotency_key if idempotency_key is not None else uuid.uuid4().hex
            normalized_key = (
                f"reindex:{recording_id}:"
                f"{hashlib.sha256(key_material.encode()).hexdigest()}"
            )
            existing = (
                await session.execute(
                    select(RecordingPipelineRun).where(
                        RecordingPipelineRun.tenant_id == tenant_id,
                        RecordingPipelineRun.idempotency_key == normalized_key,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return QueuedPipelineRun(recording=recording, run=existing)

            latest_generation = (
                await session.execute(
                    select(func.max(RecordingPipelineRun.generation)).where(
                        RecordingPipelineRun.recording_id == recording_id,
                        RecordingPipelineRun.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            generation = max(1, int(latest_generation or 0) + 1)
            source_fingerprint = recording.audio_sha256 or hashlib.sha256(
                (
                    f"{recording.path}:{recording.audio_size_bytes}:"
                    f"{recording.source_revision}"
                ).encode()
            ).hexdigest()
            config_fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "pipeline_contract": "recording-generation-v1",
                        "prompt_version": recording.prompt_version,
                        "force": force,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            run = RecordingPipelineRun(
                tenant_id=tenant_id,
                recording_id=recording_id,
                generation=generation,
                idempotency_key=normalized_key,
                source_fingerprint=source_fingerprint,
                config_fingerprint=config_fingerprint,
                state="queued",
                required_projections=list(DEFAULT_REQUIRED_PROJECTIONS),
                completed_projections=[],
            )
            session.add(run)
            await session.flush()

            # A failed re-index must never hide the previous READY generation.
            if recording.active_pipeline_run_id is None:
                recording.status = RecordingStatus.QUEUED.value
                recording.pipeline_state = PipelineState.PENDING.value
                recording.indexed_at = None
            return QueuedPipelineRun(recording=recording, run=run)

    async def get_pipeline_run(
        self,
        recording_id: int,
        run_id: int,
        tenant_id: str,
    ) -> RecordingPipelineRun:
        """Return one tenant/recording-scoped pipeline operation."""
        async with self._session_factory() as session:
            run = (
                await session.execute(
                    select(RecordingPipelineRun).where(
                        RecordingPipelineRun.id == run_id,
                        RecordingPipelineRun.recording_id == recording_id,
                        RecordingPipelineRun.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
        if run is None:
            raise RecordingNotFoundError(
                detail={"recording_id": recording_id, "pipeline_run_id": run_id}
            )
        return run
