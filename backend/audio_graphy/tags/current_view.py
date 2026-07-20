"""TagCurrentService — upsert current effective tag view (Layer 2).

Maintains the latest version of each (recording, tag_path) pair.
Updated by upsert after each tag_facts insert.

See: docs/m3-architecture.md §3.2, docs/m3-prd.md TAG-02.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.models.tag_current import TagCurrent
from audio_graphy.models.tag_fact import TagFact

logger = logging.getLogger(__name__)


class TagCurrentService:
    """Current tag view upsert service.

    Args:
        session_factory: async session maker.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def upsert_current(self, fact: TagFact, tenant_id: str) -> TagCurrent:
        """Upsert the current tag for a (recording, tag_path) pair.

        If an entry exists, it is updated. Otherwise, a new row is inserted.

        Args:
            fact: The TagFact that was just appended.
            tenant_id: Tenant scope.

        Returns:
            The upserted TagCurrent ORM object.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(TagCurrent).where(
                    TagCurrent.recording_id == fact.recording_id,
                    TagCurrent.tag_path == fact.tag_path,
                    TagCurrent.tenant_id == tenant_id,
                )
            )
            existing = result.scalar_one_or_none()

            if existing is not None:
                existing.tag_value = fact.tag_value
                existing.version = fact.version
                existing.prompt_version = fact.prompt_version
                await session.commit()
                await session.refresh(existing)
                return existing

            new_current = TagCurrent(
                tenant_id=tenant_id,
                recording_id=fact.recording_id,
                tag_path=fact.tag_path,
                tag_value=fact.tag_value,
                version=fact.version,
                prompt_version=fact.prompt_version,
            )
            session.add(new_current)
            await session.commit()
            await session.refresh(new_current)
            return new_current

    async def get_current_tags(
        self,
        recording_id: int,
        tenant_id: str,
    ) -> list[TagCurrent]:
        """Get all current tags for a recording.

        Args:
            recording_id: Recording ID.
            tenant_id: Tenant scope.

        Returns:
            List of TagCurrent ORM objects.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(TagCurrent).where(
                    TagCurrent.recording_id == recording_id,
                    TagCurrent.tenant_id == tenant_id,
                )
            )
            return list(result.scalars().all())

    async def get_current_value(
        self,
        recording_id: int,
        tag_path: str,
        tenant_id: str,
    ) -> TagCurrent | None:
        """Get the current tag value for a specific path.

        Args:
            recording_id: Recording ID.
            tag_path: Tag path string.
            tenant_id: Tenant scope.

        Returns:
            TagCurrent ORM object or None if not found.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(TagCurrent).where(
                    TagCurrent.recording_id == recording_id,
                    TagCurrent.tag_path == tag_path,
                    TagCurrent.tenant_id == tenant_id,
                )
            )
            return result.scalar_one_or_none()

    async def get_previous_value(
        self,
        recording_id: int,
        tag_path: str,
        tenant_id: str,
        *,
        exclude_version: int | None = None,
    ) -> str | None:
        """Get the previous tag value before the given version.

        Used for stats delta calculation (-old_value).

        Args:
            recording_id: Recording ID.
            tag_path: Tag path string.
            tenant_id: Tenant scope.
            exclude_version: Version to exclude (the new one).

        Returns:
            Previous tag value string, or None if this is the first version.
        """
        async with self._session_factory() as session:
            stmt = (
                select(TagFact)
                .where(
                    TagFact.recording_id == recording_id,
                    TagFact.tag_path == tag_path,
                    TagFact.tenant_id == tenant_id,
                )
                .order_by(TagFact.version.desc())
            )
            if exclude_version is not None:
                stmt = stmt.where(TagFact.version < exclude_version)
            stmt = stmt.limit(1)
            result = await session.execute(stmt)
            prev = result.scalar_one_or_none()
            return prev.tag_value if prev is not None else None
