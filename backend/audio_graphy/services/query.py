"""Query service — assembles DualChannelRetriever + Reranker for /query.

See: docs/m3-architecture.md §10.1.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.adapters.bundle import AdapterBundle
from audio_graphy.core.rerank import Reranker
from audio_graphy.core.retrieval import DualChannelRetriever
from audio_graphy.storage.file_index import FileIndex
from audio_graphy.storage.graph_networkx import NetworkXGraphStore
from audio_graphy.storage.mysql_vector import MySQLVectorStore

logger = logging.getLogger(__name__)


class QueryService:
    """Query orchestration — dual-channel retrieval + rerank + answer.

    Args:
        session_factory: async session maker.
        bundle: AdapterBundle.
        vector_store: Global MySQLVectorStore.
        graph_store: Per-tenant NetworkXGraphStore.
        file_index: Per-tenant FileIndex.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        bundle: AdapterBundle,
        vector_store: MySQLVectorStore,
        graph_store: NetworkXGraphStore,
        file_index: FileIndex,
    ) -> None:
        self._session_factory = session_factory
        self._bundle = bundle
        self._vector_store = vector_store
        self._graph_store = graph_store
        self._file_index = file_index

    async def search(
        self,
        tenant_id: str,
        query: str,
        *,
        time_range: tuple[datetime, datetime] | None = None,
        top_k: int = 10,
    ) -> dict[str, Any]:
        """Execute dual-channel retrieval + rerank + answer generation.

        Args:
            tenant_id: Tenant scope.
            query: Natural language query.
            time_range: Optional (start, end) time filter.
            top_k: Max candidates.

        Returns:
            Dict with query, answer, citations, retrieval_stats.
        """
        # Build retriever
        retriever = DualChannelRetriever(
            self._bundle,
            self._vector_store,
            self._graph_store,
            session_factory=self._session_factory,
            file_index=self._file_index,
        )

        # Retrieve
        retrieval_result = await retriever.retrieve(
            query,
            tenant_id=tenant_id,
            top_k=top_k,
            time_range=time_range,
        )

        # Rerank + answer
        reranker = Reranker(
            self._bundle,
            file_index=self._file_index,
            graph_store=self._graph_store,
        )
        rerank_result = await reranker.rerank_and_answer(
            query,
            retrieval_result.candidates,
            time_range=time_range,
        )

        # Build response
        citations_data = []
        for cite in rerank_result.citations:
            citations_data.append(
                {
                    "entity": cite.entity,
                    "chunk_id": cite.chunk_id,
                    "segment_ids": cite.segment_ids,
                    "recording_id": cite.recording_id,
                    "recorded_at": cite.recorded_at.isoformat() if cite.recorded_at else None,
                    "transcript_snippet": cite.transcript_snippet,
                    "confidence": cite.confidence,
                }
            )

        return {
            "query": query,
            "answer": rerank_result.answer,
            "citations": citations_data,
            "retrieval_stats": {
                "naive_hits": retrieval_result.naive_hits,
                "graph_hits": retrieval_result.graph_hits,
                "filtered_by_time": retrieval_result.filtered_by_time,
                "filtered_by_judge": rerank_result.filtered_count,
            },
        }
