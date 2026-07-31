"""Unit tests for QueryService — dual-channel retrieval + rerank + answer.

Tests: search (happy path, empty results).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.adapters.bundle import AdapterBundle
from audio_graphy.core.rerank import RerankResult
from audio_graphy.core.retrieval import RetrievalResult
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

    async def test_search_passes_retriever_keywords_to_reranker(
        self,
        monkeypatch: pytest.MonkeyPatch,
        session_factory: async_sessionmaker[AsyncSession],
        mock_bundle: AdapterBundle,
        vector_store: MySQLVectorStore,
        graph_store: NetworkXGraphStore,
        file_index: FileIndex,
    ) -> None:
        """A query performs keyword extraction once and shares the exact result."""
        from audio_graphy.services import query as query_module

        captured: dict[str, object] = {}

        class StubRetriever:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            async def retrieve(self, query: str, **_kwargs: object) -> RetrievalResult:
                return RetrievalResult(
                    query=query,
                    candidates=[],
                    naive_hits=0,
                    graph_hits=0,
                    filtered_by_time=0,
                    keywords=("shared-keyword",),
                )

        class StubReranker:
            def __init__(self, *_args: object, **kwargs: object) -> None:
                captured["reranker_kwargs"] = kwargs

            async def rerank_and_answer(
                self,
                _query: str,
                _candidates: object,
                *,
                time_range: object = None,
                keywords: object = None,
                tenant_id: object = None,
                permission_scope: object = None,
            ) -> RerankResult:
                captured["keywords"] = keywords
                captured["time_range"] = time_range
                captured["tenant_id"] = tenant_id
                captured["permission_scope"] = permission_scope
                return RerankResult(
                    answer="未找到相关录音片段",
                    citations=[],
                    filtered_count=0,
                    refined_count=0,
                )

        monkeypatch.setattr(query_module, "DualChannelRetriever", StubRetriever)
        monkeypatch.setattr(query_module, "Reranker", StubReranker)
        service = query_module.QueryService(
            session_factory,
            mock_bundle,
            vector_store,
            graph_store,
            file_index,
            enable_batch_judge=True,
        )

        await service.search(
            TENANT,
            "test query",
            user_id=42,
            permission_scope={"role": "manager", "store_ids": ["north"]},
        )

        assert captured["keywords"] == ("shared-keyword",)
        assert captured["tenant_id"] == TENANT
        assert captured["permission_scope"] == {
            "role": "manager",
            "store_ids": ["north"],
        }
        assert captured["reranker_kwargs"] == {
            "file_index": file_index,
            "graph_store": graph_store,
            "enable_batch_judge": True,
        }
