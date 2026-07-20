"""Ingestion service — recording registration, listing, detail, reindex.

Encapsulates all recording-related business logic so that routers
are thin wrappers.

See: docs/m3-architecture.md §10.1.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.errors import (
    DuplicateRecordingError,
    FileNotFoundError400,
    RecordingNotFoundError,
)
from audio_graphy.models.chunk import Chunk
from audio_graphy.models.enums import PipelineState, RecordingStatus
from audio_graphy.models.recording import Recording
from audio_graphy.models.segment import Segment
from audio_graphy.models.tag_current import TagCurrent
from audio_graphy.schemas.recordings import RecordingCreate

logger = logging.getLogger(__name__)


class IngestionService:
    """Recording CRUD + pipeline trigger service.

    Args:
        session_factory: async session maker for DB operations.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def register_recording(
        self,
        tenant_id: str,
        body: RecordingCreate,
    ) -> Recording:
        """Register a new recording (creates a queued record for pipeline).

        Args:
            tenant_id: Tenant scope.
            body: Recording creation data.

        Returns:
            The created Recording ORM object.

        Raises:
            FileNotFoundError400: If the audio path doesn't exist.
            DuplicateRecordingError: If (tenant_id, path) already registered.
        """
        # Check file exists
        if not os.path.exists(body.path):
            raise FileNotFoundError400(
                message=f"Audio file not found: {body.path}",
                detail={"path": body.path},
            )

        # Determine recorded_at
        recorded_at = body.recorded_at
        if recorded_at is None:
            try:
                mtime = os.path.getmtime(body.path)
                recorded_at = datetime.fromtimestamp(mtime, tz=UTC)
            except OSError:
                recorded_at = None

        async with self._session_factory() as session:
            # Check duplicate
            existing = await session.execute(
                select(Recording).where(
                    Recording.tenant_id == tenant_id,
                    Recording.path == body.path,
                )
            )
            if existing.scalar_one_or_none() is not None:
                raise DuplicateRecordingError(
                    detail={"path": body.path, "tenant_id": tenant_id},
                )

            recording = Recording(
                tenant_id=tenant_id,
                store_id=body.store_id,
                agent_name=body.agent_name,
                customer_hash=body.customer_hash,
                path=body.path,
                status=RecordingStatus.QUEUED.value,
                pipeline_state=PipelineState.PENDING.value,
                recorded_at=recorded_at,
                prompt_version=body.prompt_version,
            )
            session.add(recording)
            await session.commit()
            await session.refresh(recording)

        logger.info(
            "Registered recording %d for tenant %s (path=%s)",
            recording.id,
            tenant_id,
            body.path,
        )
        return recording

    async def list_recordings(
        self,
        tenant_id: str,
        *,
        agent_filter: str | None = None,
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
            agent_filter: If set (agent role), force agent_name = this value.
            store_id: Optional store filter.
            status: Optional status filter.
            agent_name: Optional agent name filter (overridden by agent_filter).
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

            # Agent filter takes priority
            effective_agent = agent_filter if agent_filter is not None else agent_name
            if effective_agent is not None:
                stmt = stmt.where(Recording.agent_name == effective_agent)
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
        agent_filter: str | None = None,
    ) -> Recording:
        """Get a single recording by ID (with tenant + agent isolation).

        Args:
            recording_id: Recording ID.
            tenant_id: Tenant scope.
            agent_filter: If set (agent role), force agent_name = this value.

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
            if agent_filter is not None:
                stmt = stmt.where(Recording.agent_name == agent_filter)

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
        agent_filter: str | None = None,
    ) -> dict[str, Any]:
        """Get recording detail with segments_count, chunks_count, current_tags.

        Args:
            recording_id: Recording ID.
            tenant_id: Tenant scope.
            agent_filter: Optional agent filter.

        Returns:
            Dict with recording + summary fields.
        """
        recording = await self.get_recording(recording_id, tenant_id, agent_filter=agent_filter)

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
