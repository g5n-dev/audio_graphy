"""Unit tests for QueryService — dual-channel retrieval + rerank + answer.

Tests: search (happy path, empty results).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.adapters.bundle import AdapterBundle
from audio_graphy.storage.file_index import FileIndex
from audio_graphy.storage.graph_networkx import NetworkXGraphStore
from audio_graphy.storage.mysql_vector import MySQLVectorStore

TENANT = "chang_an"


@pytest.mark.asyncio
class TestQueryService:
    """Tests for QueryService."""

    async def test_search_empty_graph(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        mock_bundle: AdapterBundle,
        vector_store: MySQLVectorStore,
        graph_store: NetworkXGraphStore,
        file_index: FileIndex,
    ) -> None:
        """search returns structured result even with no data."""
        from audio_graphy.services.query import QueryService

        svc = QueryService(
            session_factory,
            mock_bundle,
            vector_store,
            graph_store,
            file_index,
        )
        result = await svc.search(TENANT, "CS75 Plus有什么优惠？", top_k=5)
        assert "query" in result
        assert "answer" in result
        assert "citations" in result
        assert "retrieval_stats" in result
        assert result["query"] == "CS75 Plus有什么优惠？"

    async def test_search_with_time_range(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        mock_bundle: AdapterBundle,
        vector_store: MySQLVectorStore,
        graph_store: NetworkXGraphStore,
        file_index: FileIndex,
    ) -> None:
        """search accepts time_range parameter."""
        from audio_graphy.services.query import QueryService

        svc = QueryService(
            session_factory,
            mock_bundle,
            vector_store,
            graph_store,
            file_index,
        )
        result = await svc.search(
            TENANT,
            "test query",
            time_range=(datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 12, 31, tzinfo=UTC)),
            top_k=3,
        )
        assert result["query"] == "test query"
