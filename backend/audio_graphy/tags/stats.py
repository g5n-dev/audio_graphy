"""TagStatsService — delta aggregation for tag statistics (Layer 3).

Maintains multi-dimensional tag count/distribution by applying
incremental deltas: ``-old_value_count +new_value_count``.

See: docs/m3-architecture.md §3.2, docs/m3-prd.md TAG-03.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.models.recording import Recording
from audio_graphy.models.tag_current import TagCurrent
from audio_graphy.models.tag_stat import TagStat

logger = logging.getLogger(__name__)

GroupByField = Literal["store_id", "agent_name", "tag_path", "tag_value"]


class TagStatsService:
    """Tag statistics aggregation service.

    Args:
        session_factory: async session maker.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def apply_delta(
        self,
        tenant_id: str,
        store_id: str,
        agent_name: str | None,
        tag_path: str,
        old_value: str | None,
        new_value: str | None,
    ) -> None:
        """Apply a delta: decrement old value count, increment new value count.

        If old_value == new_value, no change is made.

        Args:
            tenant_id: Tenant scope.
            store_id: Store ID dimension.
            agent_name: Agent name dimension (may be None).
            tag_path: Tag path.
            old_value: Previous tag value (None for first-time).
            new_value: New tag value.
        """
        if old_value == new_value:
            return  # No change

        if old_value is not None:
            await self._decrement(tenant_id, store_id, agent_name, tag_path, old_value)
        if new_value is not None:
            await self._increment(tenant_id, store_id, agent_name, tag_path, new_value)

    async def get_stats(
        self,
        tenant_id: str,
        store_id: str | None,
        agent_name: str | None,
        tag_path_prefix: str | None,
        tag_value: str | None,
        group_by: GroupByField,
        agent_user_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query tag statistics with optional filters and grouping.

        Args:
            tenant_id: Tenant scope.
            store_id: Optional store filter.
            agent_name: Optional agent filter.
            tag_path_prefix: Optional tag path prefix (e.g. "quality.*").
            tag_value: Optional tag value filter.
            group_by: Aggregation dimension.
            agent_user_id: Optional immutable owner scope. When set, statistics
                are derived from canonical current tags joined to recordings,
                rather than the legacy display-name aggregate.

        Returns:
            List of dicts with dimension keys + tag_count.
        """
        if agent_user_id is not None:
            return await self._get_agent_stats(
                tenant_id=tenant_id,
                agent_user_id=agent_user_id,
                store_id=store_id,
                tag_path_prefix=tag_path_prefix,
                tag_value=tag_value,
                group_by=group_by,
            )

        async with self._session_factory() as session:
            stmt = select(TagStat).where(TagStat.tenant_id == tenant_id)

            if store_id is not None:
                stmt = stmt.where(TagStat.store_id == store_id)
            if agent_name is not None:
                stmt = stmt.where(TagStat.agent_name == agent_name)
            if tag_path_prefix is not None:
                if tag_path_prefix.endswith("*"):
                    prefix = tag_path_prefix[:-1]
                    stmt = stmt.where(TagStat.tag_path.like(f"{prefix}%"))
                else:
                    stmt = stmt.where(TagStat.tag_path == tag_path_prefix)
            if tag_value is not None:
                stmt = stmt.where(TagStat.tag_value == tag_value)

            result = await session.execute(stmt)
            rows = result.scalars().all()

        # Group in Python (simpler than dynamic SQL GROUP BY)
        groups: dict[tuple[str | None, ...], int] = {}
        for row in rows:
            key = self._make_group_key(row, group_by)
            groups[key] = groups.get(key, 0) + row.tag_count

        stats_list: list[dict[str, Any]] = []
        for key, count in groups.items():
            entry: dict[str, Any] = {group_by: key[0] if key else None}
            entry["tag_count"] = count
            stats_list.append(entry)

        return stats_list

    async def _get_agent_stats(
        self,
        *,
        tenant_id: str,
        agent_user_id: int,
        store_id: str | None,
        tag_path_prefix: str | None,
        tag_value: str | None,
        group_by: GroupByField,
    ) -> list[dict[str, Any]]:
        """Aggregate current tags behind one immutable recording owner."""

        async with self._session_factory() as session:
            stmt = (
                select(
                    Recording.store_id,
                    Recording.agent_name,
                    TagCurrent.tag_path,
                    TagCurrent.tag_value,
                )
                .join(Recording, Recording.id == TagCurrent.recording_id)
                .where(
                    Recording.tenant_id == tenant_id,
                    Recording.agent_user_id == agent_user_id,
                    TagCurrent.tenant_id == tenant_id,
                )
            )
            if store_id is not None:
                stmt = stmt.where(Recording.store_id == store_id)
            if tag_path_prefix is not None:
                if tag_path_prefix.endswith("*"):
                    prefix = tag_path_prefix[:-1]
                    stmt = stmt.where(TagCurrent.tag_path.like(f"{prefix}%"))
                else:
                    stmt = stmt.where(TagCurrent.tag_path == tag_path_prefix)
            if tag_value is not None:
                stmt = stmt.where(TagCurrent.tag_value == tag_value)

            rows = (await session.execute(stmt)).mappings().all()

        groups: dict[str | None, int] = {}
        for row in rows:
            value = row[group_by]
            groups[value] = groups.get(value, 0) + 1

        return [
            {group_by: value, "tag_count": count}
            for value, count in sorted(
                groups.items(),
                key=lambda item: (item[0] is None, str(item[0])),
            )
        ]

    async def _increment(
        self,
        tenant_id: str,
        store_id: str,
        agent_name: str | None,
        tag_path: str,
        tag_value: str,
    ) -> None:
        """Increment the count for a dimension combination."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(TagStat).where(
                    TagStat.tenant_id == tenant_id,
                    TagStat.store_id == store_id,
                    TagStat.agent_name == agent_name
                    if agent_name is not None
                    else TagStat.agent_name.is_(None),
                    TagStat.tag_path == tag_path,
                    TagStat.tag_value == tag_value,
                )
            )
            existing = result.scalar_one_or_none()

            if existing is not None:
                existing.tag_count += 1
            else:
                session.add(
                    TagStat(
                        tenant_id=tenant_id,
                        store_id=store_id,
                        agent_name=agent_name,
                        tag_path=tag_path,
                        tag_value=tag_value,
                        tag_count=1,
                    )
                )
            await session.commit()

    async def _decrement(
        self,
        tenant_id: str,
        store_id: str,
        agent_name: str | None,
        tag_path: str,
        tag_value: str,
    ) -> None:
        """Decrement the count for a dimension combination (min 0)."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(TagStat).where(
                    TagStat.tenant_id == tenant_id,
                    TagStat.store_id == store_id,
                    TagStat.agent_name == agent_name
                    if agent_name is not None
                    else TagStat.agent_name.is_(None),
                    TagStat.tag_path == tag_path,
                    TagStat.tag_value == tag_value,
                )
            )
            existing = result.scalar_one_or_none()

            if existing is not None:
                existing.tag_count = max(0, existing.tag_count - 1)
                await session.commit()

    @staticmethod
    def _make_group_key(row: TagStat, group_by: GroupByField) -> tuple[str | None, ...]:
        """Make a grouping key from a TagStat row."""
        val = getattr(row, group_by, None)
        return (val,) if val is not None else (None,)
