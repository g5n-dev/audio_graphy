"""Unit tests for TagFactsService — append-only tag fact layer (Layer 1).

Tests: get_next_version, append_fact, get_facts, get_history.
Uses in-memory SQLite (no MySQL needed).
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.tags.facts import TagFactsService

TENANT = "chang_an"


@pytest.mark.asyncio
class TestTagFactsService:
    """Tests for TagFactsService."""

    async def test_get_next_version_empty(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """get_next_version returns 1 when no facts exist."""
        svc = TagFactsService(session_factory)
        version = await svc.get_next_version(
            recording_id=1, tag_path="quality.greeting", tenant_id=TENANT
        )
        assert version == 1

    async def test_get_next_version_after_insert(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """get_next_version increments after a fact is appended."""
        svc = TagFactsService(session_factory)
        await svc.append_fact(
            recording_id=1,
            tag_path="quality.greeting",
            tag_value="pass",
            prompt_version="v1",
            model_version="test-weak",
            input_hash="abc123",
            confidence=0.95,
            source="llm",
            computed_by=None,
            tenant_id=TENANT,
        )
        version = await svc.get_next_version(1, "quality.greeting", TENANT)
        assert version == 2

    async def test_append_fact_creates_row(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """append_fact inserts a new TagFact with correct fields."""
        svc = TagFactsService(session_factory)
        fact = await svc.append_fact(
            recording_id=1,
            tag_path="quality.greeting",
            tag_value="pass",
            prompt_version="v1",
            model_version="test-weak",
            input_hash="hash001",
            confidence=0.9,
            source="llm",
            computed_by=None,
            tenant_id=TENANT,
        )
        assert fact.id is not None
        assert fact.recording_id == 1
        assert fact.tag_path == "quality.greeting"
        assert fact.tag_value == "pass"
        assert fact.version == 1
        assert fact.prompt_version == "v1"
        assert fact.confidence == pytest.approx(0.9)
        assert fact.source == "llm"
        assert fact.computed_at is not None

    async def test_append_fact_multiple_versions(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Multiple appends produce incrementing version numbers."""
        svc = TagFactsService(session_factory)
        for i in range(3):
            fact = await svc.append_fact(
                recording_id=1,
                tag_path="quality.closing",
                tag_value=f"v{i}",
                prompt_version="v1",
                model_version="test-weak",
                input_hash=f"hash{i}",
                confidence=0.8,
                source="llm",
                computed_by=None,
                tenant_id=TENANT,
            )
            assert fact.version == i + 1

    async def test_append_fact_independent_paths(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Different tag_paths have independent version sequences."""
        svc = TagFactsService(session_factory)
        f1 = await svc.append_fact(
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
        f2 = await svc.append_fact(
            recording_id=1,
            tag_path="quality.closing",
            tag_value="fail",
            prompt_version="v1",
            model_version="m",
            input_hash="h2",
            confidence=0.9,
            source="llm",
            computed_by=None,
            tenant_id=TENANT,
        )
        assert f1.version == 1
        assert f2.version == 1  # Different path → independent versioning

    async def test_get_facts_all(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """get_facts returns all facts for a recording."""
        svc = TagFactsService(session_factory)
        await svc.append_fact(
            recording_id=1,
            tag_path="a",
            tag_value="1",
            prompt_version="v1",
            model_version="m",
            input_hash="h1",
            confidence=0.9,
            source="llm",
            computed_by=None,
            tenant_id=TENANT,
        )
        await svc.append_fact(
            recording_id=1,
            tag_path="b",
            tag_value="2",
            prompt_version="v1",
            model_version="m",
            input_hash="h2",
            confidence=0.9,
            source="llm",
            computed_by=None,
            tenant_id=TENANT,
        )
        facts = await svc.get_facts(1, tag_path=None, tenant_id=TENANT)
        assert len(facts) == 2

    async def test_get_facts_filtered_by_path(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """get_facts filters by tag_path."""
        svc = TagFactsService(session_factory)
        await svc.append_fact(
            recording_id=1,
            tag_path="a",
            tag_value="1",
            prompt_version="v1",
            model_version="m",
            input_hash="h1",
            confidence=0.9,
            source="llm",
            computed_by=None,
            tenant_id=TENANT,
        )
        await svc.append_fact(
            recording_id=1,
            tag_path="b",
            tag_value="2",
            prompt_version="v1",
            model_version="m",
            input_hash="h2",
            confidence=0.9,
            source="llm",
            computed_by=None,
            tenant_id=TENANT,
        )
        facts = await svc.get_facts(1, tag_path="a", tenant_id=TENANT)
        assert len(facts) == 1
        assert facts[0].tag_path == "a"

    async def test_get_history_with_prefix(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """get_history with wildcard prefix matches paths."""
        svc = TagFactsService(session_factory)
        await svc.append_fact(
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
        await svc.append_fact(
            recording_id=1,
            tag_path="quality.closing",
            tag_value="fail",
            prompt_version="v1",
            model_version="m",
            input_hash="h2",
            confidence=0.9,
            source="llm",
            computed_by=None,
            tenant_id=TENANT,
        )
        await svc.append_fact(
            recording_id=1,
            tag_path="sales.product",
            tag_value="CS75",
            prompt_version="v1",
            model_version="m",
            input_hash="h3",
            confidence=0.9,
            source="llm",
            computed_by=None,
            tenant_id=TENANT,
        )
        history = await svc.get_history(1, TENANT, tag_path_prefix="quality.*")
        assert len(history) == 2
        for h in history:
            assert h.tag_path.startswith("quality.")

    async def test_get_history_exact_path(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """get_history without wildcard matches exact path."""
        svc = TagFactsService(session_factory)
        await svc.append_fact(
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
        history = await svc.get_history(1, TENANT, tag_path_prefix="quality.greeting")
        assert len(history) == 1

    async def test_tenant_isolation(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Facts from different tenants are isolated."""
        svc = TagFactsService(session_factory)
        await svc.append_fact(
            recording_id=1,
            tag_path="a",
            tag_value="1",
            prompt_version="v1",
            model_version="m",
            input_hash="h1",
            confidence=0.9,
            source="llm",
            computed_by=None,
            tenant_id=TENANT,
        )
        await svc.append_fact(
            recording_id=2,
            tag_path="a",
            tag_value="2",
            prompt_version="v1",
            model_version="m",
            input_hash="h2",
            confidence=0.9,
            source="llm",
            computed_by=None,
            tenant_id="byd",
        )
        facts_t1 = await svc.get_facts(1, None, TENANT)
        facts_t2 = await svc.get_facts(2, None, "byd")
        assert len(facts_t1) == 1
        assert len(facts_t2) == 1
