"""Unit tests for TagStatsService — delta aggregation (Layer 3).

Tests: apply_delta (increment/decrement/noop), get_stats with grouping.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.tags.stats import TagStatsService

TENANT = "chang_an"


@pytest.mark.asyncio
class TestTagStatsService:
    """Tests for TagStatsService."""

    async def test_apply_delta_first_time(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """apply_delta with old=None creates a new stat row with count=1."""

        svc = TagStatsService(session_factory)
        await svc.apply_delta(
            tenant_id=TENANT,
            store_id="S001",
            agent_name="张敏",
            tag_path="quality.greeting",
            old_value=None,
            new_value="pass",
        )
        stats = await svc.get_stats(
            TENANT,
            store_id=None,
            agent_name=None,
            tag_path_prefix=None,
            tag_value=None,
            group_by="tag_value",
        )
        assert len(stats) == 1
        assert stats[0]["tag_count"] == 1

    async def test_apply_delta_noop_same_value(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """apply_delta with old==new is a no-op."""

        svc = TagStatsService(session_factory)
        await svc.apply_delta(
            tenant_id=TENANT,
            store_id="S001",
            agent_name="张敏",
            tag_path="quality.greeting",
            old_value="pass",
            new_value="pass",
        )
        stats = await svc.get_stats(
            TENANT,
            None,
            None,
            None,
            None,
            group_by="tag_value",
        )
        assert len(stats) == 0  # No stat created because old==new

    async def test_apply_delta_change_value(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """apply_delta decrements old and increments new."""

        svc = TagStatsService(session_factory)
        # First: pass
        await svc.apply_delta(
            TENANT,
            "S001",
            "张敏",
            "quality.greeting",
            None,
            "pass",
        )
        # Change to fail
        await svc.apply_delta(
            TENANT,
            "S001",
            "张敏",
            "quality.greeting",
            "pass",
            "fail",
        )
        stats = await svc.get_stats(
            TENANT,
            None,
            None,
            None,
            None,
            group_by="tag_value",
        )
        by_value = {s["tag_value"]: s["tag_count"] for s in stats}
        assert by_value.get("pass") == 0
        assert by_value.get("fail") == 1

    async def test_apply_delta_increment_multiple(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Multiple increments accumulate the count."""

        svc = TagStatsService(session_factory)
        for agent in ["张敏", "李华", "王芳"]:
            await svc.apply_delta(
                TENANT,
                "S001",
                agent,
                "quality.greeting",
                None,
                "pass",
            )
        stats = await svc.get_stats(
            TENANT,
            None,
            None,
            None,
            "pass",
            group_by="agent_name",
        )
        total = sum(s["tag_count"] for s in stats)
        assert total == 3

    async def test_apply_delta_decrement_floor_zero(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Decrement never goes below 0."""

        svc = TagStatsService(session_factory)
        await svc.apply_delta(
            TENANT,
            "S001",
            "张敏",
            "quality.greeting",
            None,
            "pass",
        )
        # Decrement without corresponding increment
        await svc.apply_delta(
            TENANT,
            "S001",
            "张敏",
            "quality.greeting",
            "pass",
            None,
        )
        stats = await svc.get_stats(
            TENANT,
            None,
            None,
            None,
            "pass",
            group_by="tag_value",
        )
        if stats:
            assert stats[0]["tag_count"] == 0

    async def test_get_stats_group_by_store(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """get_stats groups by store_id."""

        svc = TagStatsService(session_factory)
        await svc.apply_delta(TENANT, "S001", "张敏", "a", None, "pass")
        await svc.apply_delta(TENANT, "S001", "李华", "a", None, "pass")
        await svc.apply_delta(TENANT, "S002", "王芳", "a", None, "pass")

        stats = await svc.get_stats(
            TENANT,
            None,
            None,
            None,
            None,
            group_by="store_id",
        )
        by_store = {s["store_id"]: s["tag_count"] for s in stats}
        assert by_store.get("S001") == 2
        assert by_store.get("S002") == 1

    async def test_get_stats_group_by_tag_path(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """get_stats groups by tag_path."""

        svc = TagStatsService(session_factory)
        await svc.apply_delta(TENANT, "S001", "张敏", "quality.greeting", None, "pass")
        await svc.apply_delta(TENANT, "S001", "张敏", "quality.closing", None, "fail")

        stats = await svc.get_stats(
            TENANT,
            None,
            None,
            None,
            None,
            group_by="tag_path",
        )
        paths = {s["tag_path"] for s in stats}
        assert "quality.greeting" in paths
        assert "quality.closing" in paths

    async def test_get_stats_filter_store_id(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """get_stats filters by store_id."""

        svc = TagStatsService(session_factory)
        await svc.apply_delta(TENANT, "S001", "张敏", "a", None, "pass")
        await svc.apply_delta(TENANT, "S002", "李华", "a", None, "pass")

        stats = await svc.get_stats(
            TENANT,
            "S001",
            None,
            None,
            None,
            group_by="tag_value",
        )
        assert len(stats) == 1
        assert stats[0]["tag_count"] == 1

    async def test_get_stats_filter_tag_path_prefix(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """get_stats filters by tag_path_prefix wildcard."""

        svc = TagStatsService(session_factory)
        await svc.apply_delta(TENANT, "S001", "张敏", "quality.greeting", None, "pass")
        await svc.apply_delta(TENANT, "S001", "张敏", "sales.product", None, "CS75")

        stats = await svc.get_stats(
            TENANT,
            None,
            None,
            "quality.*",
            None,
            group_by="tag_value",
        )
        assert len(stats) == 1
        assert stats[0]["tag_value"] == "pass"

    async def test_get_stats_empty(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """get_stats returns empty list when no data."""

        svc = TagStatsService(session_factory)
        stats = await svc.get_stats(
            TENANT,
            None,
            None,
            None,
            None,
            group_by="tag_value",
        )
        assert stats == []

    async def test_tenant_isolation(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Stats are tenant-isolated."""

        svc = TagStatsService(session_factory)
        await svc.apply_delta(TENANT, "S001", "张敏", "a", None, "pass")

        stats_t2 = await svc.get_stats("byd", None, None, None, None, group_by="tag_value")
        assert stats_t2 == []
