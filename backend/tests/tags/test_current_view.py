"""Unit tests for TagCurrentService — current tag view upsert (Layer 2).

Tests: upsert_current (insert + update), get_current_tags, get_current_value,
get_previous_value.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.tags.current_view import TagCurrentService
from audio_graphy.tags.facts import TagFactsService

TENANT = "chang_an"


@pytest.mark.asyncio
class TestTagCurrentService:
    """Tests for TagCurrentService."""

    async def test_upsert_insert_new(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """upsert_current inserts when no existing entry."""

        facts_svc = TagFactsService(session_factory)
        current_svc = TagCurrentService(session_factory)

        fact = await facts_svc.append_fact(
            recording_id=1,
            tag_path="quality.greeting",
            tag_value="pass",
            prompt_version="v1",
            model_version="m",
            input_hash="h1",
            confidence=0.9,
            source="llm",
            computed_by=None,
            tenant_id=TENANT,
        )
        result = await current_svc.upsert_current(fact, TENANT)
        assert result.id is not None
        assert result.tag_value == "pass"
        assert result.version == 1

    async def test_upsert_update_existing(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """upsert_current updates when entry already exists."""

        facts_svc = TagFactsService(session_factory)
        current_svc = TagCurrentService(session_factory)

        # First version
        fact1 = await facts_svc.append_fact(
            recording_id=1,
            tag_path="quality.greeting",
            tag_value="pass",
            prompt_version="v1",
            model_version="m",
            input_hash="h1",
            confidence=0.9,
            source="llm",
            computed_by=None,
            tenant_id=TENANT,
        )
        await current_svc.upsert_current(fact1, TENANT)

        # Second version (update)
        fact2 = await facts_svc.append_fact(
            recording_id=1,
            tag_path="quality.greeting",
            tag_value="fail",
            prompt_version="v2",
            model_version="m",
            input_hash="h2",
            confidence=0.8,
            source="llm",
            computed_by=None,
            tenant_id=TENANT,
        )
        updated = await current_svc.upsert_current(fact2, TENANT)
        assert updated.tag_value == "fail"
        assert updated.version == 2
        assert updated.prompt_version == "v2"

    async def test_get_current_tags(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """get_current_tags returns all current tags for a recording."""

        facts_svc = TagFactsService(session_factory)
        current_svc = TagCurrentService(session_factory)

        for path in ["quality.greeting", "quality.closing", "sales.product"]:
            f = await facts_svc.append_fact(
                recording_id=1,
                tag_path=path,
                tag_value="pass",
                prompt_version="v1",
                model_version="m",
                input_hash=path,
                confidence=0.9,
                source="llm",
                computed_by=None,
                tenant_id=TENANT,
            )
            await current_svc.upsert_current(f, TENANT)

        tags = await current_svc.get_current_tags(1, TENANT)
        assert len(tags) == 3

    async def test_get_current_value_found(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """get_current_value returns the current value for a specific path."""

        facts_svc = TagFactsService(session_factory)
        current_svc = TagCurrentService(session_factory)

        fact = await facts_svc.append_fact(
            recording_id=1,
            tag_path="quality.greeting",
            tag_value="pass",
            prompt_version="v1",
            model_version="m",
            input_hash="h1",
            confidence=0.9,
            source="llm",
            computed_by=None,
            tenant_id=TENANT,
        )
        await current_svc.upsert_current(fact, TENANT)

        result = await current_svc.get_current_value(1, "quality.greeting", TENANT)
        assert result is not None
        assert result.tag_value == "pass"

    async def test_get_current_value_not_found(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """get_current_value returns None when no tag exists."""

        current_svc = TagCurrentService(session_factory)
        result = await current_svc.get_current_value(999, "nonexistent", TENANT)
        assert result is None

    async def test_get_previous_value_first_version(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """get_previous_value returns None for the first version."""

        facts_svc = TagFactsService(session_factory)
        current_svc = TagCurrentService(session_factory)

        fact = await facts_svc.append_fact(
            recording_id=1,
            tag_path="quality.greeting",
            tag_value="pass",
            prompt_version="v1",
            model_version="m",
            input_hash="h1",
            confidence=0.9,
            source="llm",
            computed_by=None,
            tenant_id=TENANT,
        )
        assert fact.version == 1
        prev = await current_svc.get_previous_value(
            1, "quality.greeting", TENANT, exclude_version=1
        )
        assert prev is None

    async def test_get_previous_value_with_history(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """get_previous_value returns the prior value when version > 1."""

        facts_svc = TagFactsService(session_factory)
        current_svc = TagCurrentService(session_factory)

        # v1 = pass
        await facts_svc.append_fact(
            recording_id=1,
            tag_path="quality.greeting",
            tag_value="pass",
            prompt_version="v1",
            model_version="m",
            input_hash="h1",
            confidence=0.9,
            source="llm",
            computed_by=None,
            tenant_id=TENANT,
        )
        # v2 = fail
        await facts_svc.append_fact(
            recording_id=1,
            tag_path="quality.greeting",
            tag_value="fail",
            prompt_version="v2",
            model_version="m",
            input_hash="h2",
            confidence=0.9,
            source="llm",
            computed_by=None,
            tenant_id=TENANT,
        )
        prev = await current_svc.get_previous_value(
            1, "quality.greeting", TENANT, exclude_version=2
        )
        assert prev == "pass"

    async def test_tenant_isolation(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Current tags are tenant-isolated."""

        facts_svc = TagFactsService(session_factory)
        current_svc = TagCurrentService(session_factory)

        fact = await facts_svc.append_fact(
            recording_id=1,
            tag_path="a",
            tag_value="pass",
            prompt_version="v1",
            model_version="m",
            input_hash="h1",
            confidence=0.9,
            source="llm",
            computed_by=None,
            tenant_id=TENANT,
        )
        await current_svc.upsert_current(fact, TENANT)

        tags_t2 = await current_svc.get_current_tags(1, "byd")
        assert len(tags_t2) == 0
