"""TagFactsService — append-only tag fact insertion (Layer 1).

Every tag judgment is recorded here with full provenance. Rows are
INSERT-only — never updated or deleted.

See: docs/m3-architecture.md §3.2, docs/m3-prd.md TAG-01.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.models.tag_fact import TagFact

logger = logging.getLogger(__name__)


class TagFactsService:
    """Append-only tag fact writer.

    Args:
        session_factory: async session maker for DB operations.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_next_version(
        self,
        recording_id: int,
        tag_path: str,
        tenant_id: str,
    ) -> int:
        """Get the next version number for a (recording, tag_path) pair.

        Args:
            recording_id: Recording ID.
            tag_path: Tag path string.
            tenant_id: Tenant scope.

        Returns:
            Next version number (starts at 1).
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(TagFact.version)
                .where(
                    TagFact.recording_id == recording_id,
                    TagFact.tag_path == tag_path,
                    TagFact.tenant_id == tenant_id,
                )
                .order_by(TagFact.version.desc())
                .limit(1)
            )
            current_max = result.scalar_one_or_none()
            return (current_max or 0) + 1

    async def append_fact(
        self,
        recording_id: int,
        tag_path: str,
        tag_value: str,
        prompt_version: str,
        model_version: str,
        input_hash: str,
        confidence: float,
        source: str,
        computed_by: int | None,
        tenant_id: str,
    ) -> TagFact:
        """Append a new tag fact row (append-only).

        Args:
            recording_id: Recording ID.
            tag_path: Tag path string.
            tag_value: Tag value string.
            prompt_version: Prompt version used.
            model_version: Model version used.
            input_hash: Input content hash (for reproducibility).
            confidence: Confidence score (0.0-1.0).
            source: "llm" or "manual".
            computed_by: User ID of the tagger (None for system).
            tenant_id: Tenant scope.

        Returns:
            The created TagFact ORM object.
        """
        version = await self.get_next_version(recording_id, tag_path, tenant_id)

        fact = TagFact(
            tenant_id=tenant_id,
            recording_id=recording_id,
            tag_path=tag_path,
            tag_value=tag_value,
            version=version,
            prompt_version=prompt_version,
            model_version=model_version,
            source=source,
            input_hash=input_hash,
            confidence=confidence,
            computed_at=datetime.now(UTC),
            computed_by=computed_by,
        )
        async with self._session_factory() as session:
            session.add(fact)
            await session.commit()
            await session.refresh(fact)

        logger.debug(
            "Appended tag fact: recording=%d path=%s value=%s version=%d",
            recording_id,
            tag_path,
            tag_value,
            version,
        )
        return fact

    async def get_facts(
        self,
        recording_id: int,
        tag_path: str | None,
        tenant_id: str,
    ) -> list[TagFact]:
        """Get tag facts for a recording (optionally filtered by tag_path).

        Args:
            recording_id: Recording ID.
            tag_path: Optional tag path filter.
            tenant_id: Tenant scope.

        Returns:
            List of TagFact ORM objects.
        """
        async with self._session_factory() as session:
            stmt = select(TagFact).where(
                TagFact.recording_id == recording_id,
                TagFact.tenant_id == tenant_id,
            )
            if tag_path is not None:
                stmt = stmt.where(TagFact.tag_path == tag_path)
            stmt = stmt.order_by(TagFact.tag_path, TagFact.version)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_history(
        self,
        recording_id: int,
        tenant_id: str,
        tag_path_prefix: str | None = None,
    ) -> list[TagFact]:
        """Get all tag fact history for a recording.

        Args:
            recording_id: Recording ID.
            tenant_id: Tenant scope.
            tag_path_prefix: Optional prefix filter (e.g. "quality.*").

        Returns:
            List of TagFact ORM objects sorted by tag_path + version.
        """
        async with self._session_factory() as session:
            stmt = select(TagFact).where(
                TagFact.recording_id == recording_id,
                TagFact.tenant_id == tenant_id,
            )
            if tag_path_prefix is not None and tag_path_prefix.endswith("*"):
                prefix = tag_path_prefix[:-1]
                stmt = stmt.where(TagFact.tag_path.like(f"{prefix}%"))
            elif tag_path_prefix is not None:
                stmt = stmt.where(TagFact.tag_path == tag_path_prefix)
            stmt = stmt.order_by(TagFact.tag_path, TagFact.version)
            result = await session.execute(stmt)
            return list(result.scalars().all())
