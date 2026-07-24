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
import logging
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
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

    async def register_recording(
        self,
        tenant_id: str,
        body: RecordingCreate,
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

        # M6: envelope-encrypt audio (best effort; logged on failure).
        encrypted_path: str | None = None
        encryption_meta: dict[str, Any] | None = None
        if self._crypto is not None:
            try:
                cipher_path = f"{validated_path}.enc"
                meta = await asyncio.to_thread(
                    self._crypto.encrypt_file,
                    validated_path,
                    Path(cipher_path),
                )
                if meta.size_bytes > self._max_audio_bytes:
                    raise ValueError("encrypted plaintext exceeded the ingestion size limit")
                encrypted_path = cipher_path
                encryption_meta = {
                    "master_key_id": meta.master_key_id,
                    "data_key_id": meta.data_key_id,
                    "size_bytes": meta.size_bytes,
                    "sha256": meta.sha256,
                }
            except Exception as exc:
                logger.error(
                    "Audio encryption failed for %s: %s",
                    validated_path,
                    exc,
                    exc_info=True,
                )
                Path(f"{validated_path}.enc").unlink(missing_ok=True)
                raise APIError(
                    "Audio encryption failed",
                    code="AUDIO_ENCRYPTION_FAILED",
                    status_code=500,
                ) from exc

        async with self._session_factory() as session:
            # Check duplicate
            existing = await session.execute(
                select(Recording).where(
                    Recording.tenant_id == tenant_id,
                    Recording.path == str(validated_path),
                )
            )
            if existing.scalar_one_or_none() is not None:
                raise DuplicateRecordingError(
                    detail={"path": str(validated_path), "tenant_id": tenant_id},
                )

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
                status=RecordingStatus.QUEUED.value,
                pipeline_state=PipelineState.PENDING.value,
                recorded_at=recorded_at,
                prompt_version=body.prompt_version,
                audio_encrypted_path=encrypted_path,
                audio_encryption_meta=encryption_meta,
            )
            session.add(recording)
            await session.commit()
            await session.refresh(recording)

            # M6: fire-and-forget audit (queue → background flusher).
            if self._audit is not None:
                await self._audit.record(
                    tenant_id=tenant_id,
                    user_id=None,  # system upload
                    action="recording.uploaded",
                    target=f"recording:{recording.id}",
                    after={
                        "path": recording.path,
                        "encrypted": encrypted_path is not None,
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
            # Segments count
            seg_count_result = await session.execute(
                select(func.count()).where(Segment.recording_id == recording_id)
            )
            segments_count = seg_count_result.scalar_one()

            # Chunks count
            chunk_count_result = await session.execute(
                select(func.count()).where(Chunk.recording_id == recording_id)
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
        # Verify recording exists (raises RecordingNotFoundError if not)
        await self.get_recording(recording_id, tenant_id)

        async with self._session_factory() as session:
            # Re-fetch and update
            result = await session.execute(select(Recording).where(Recording.id == recording_id))
            rec = result.scalar_one()
            rec.status = RecordingStatus.QUEUED.value
            rec.pipeline_state = PipelineState.PENDING.value
            rec.indexed_at = None
            await session.commit()
            await session.refresh(rec)
            return rec
