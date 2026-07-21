"""Query service — assembles DualChannelRetriever + Reranker for /query.

M6 PIPL §14.3 integration (optional, opt-in via constructor):
    - pii_scrubber: PIIScrubber — when set, the answer + citation snippets
      are PII-scrubbed before being returned to the caller (defensive — the
      transcripts in DB are already scrubbed at ingestion time).
    - audit: AuditWriter — when set, ``search`` writes a ``query.answered``
      audit record for each query.

See: docs/m3-architecture.md §10.1, docs/m6-architecture.md §3.7.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.adapters.bundle import AdapterBundle
from audio_graphy.core.rerank import Reranker
from audio_graphy.core.retrieval import DualChannelRetriever
from audio_graphy.storage.file_index import FileIndex
from audio_graphy.storage.graph_networkx import NetworkXGraphStore
from audio_graphy.storage.mysql_vector import MySQLVectorStore

if TYPE_CHECKING:
    from audio_graphy.core.audit import AuditWriter
    from audio_graphy.core.pii import PIIScrubber

logger = logging.getLogger(__name__)


class QueryService:
    """Query orchestration — dual-channel retrieval + rerank + answer.

    Args:
        session_factory: async session maker.
        bundle: AdapterBundle.
        vector_store: Global MySQLVectorStore.
        graph_store: Per-tenant NetworkXGraphStore.
        file_index: Per-tenant FileIndex.
        pii_scrubber: Optional PIIScrubber; when set, the answer + citation
            snippets are scrubbed before return (M6 PIPL §14.3).
        audit: Optional AuditWriter for fire-and-forget audit records.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        bundle: AdapterBundle,
        vector_store: MySQLVectorStore,
        graph_store: NetworkXGraphStore,
        file_index: FileIndex,
        *,
        pii_scrubber: PIIScrubber | None = None,
        audit: AuditWriter | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._bundle = bundle
        self._vector_store = vector_store
        self._graph_store = graph_store
        self._file_index = file_index
        self._pii_scrubber = pii_scrubber
        self._audit = audit

    async def search(
        self,
        tenant_id: str,
        query: str,
        *,
        time_range: tuple[datetime, datetime] | None = None,
        top_k: int = 10,
        user_id: int | None = None,
    ) -> dict[str, Any]:
        """Execute dual-channel retrieval + rerank + answer generation.

        Args:
            tenant_id: Tenant scope.
            query: Natural language query.
            time_range: Optional (start, end) time filter.
            top_k: Max candidates.
            user_id: Optional acting user (for audit attribution).

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

        # M6: PII scrubbing (defensive — LLM may regenerate PII from context).
        answer_text = rerank_result.answer
        pii_redacted_count = 0
        if self._pii_scrubber is not None and answer_text:
            scrub = self._pii_scrubber.scrub(answer_text)
            answer_text = scrub.text
            pii_redacted_count = len(scrub.redactions)

        # Build response
        citations_data: list[dict[str, Any]] = []
        for cite in rerank_result.citations:
            snippet = cite.transcript_snippet
            if self._pii_scrubber is not None and snippet:
                snippet = self._pii_scrubber.scrub_simple(snippet)
            citations_data.append(
                {
                    "entity": cite.entity,
                    "chunk_id": cite.chunk_id,
                    "segment_ids": cite.segment_ids,
                    "recording_id": cite.recording_id,
                    "recorded_at": cite.recorded_at.isoformat() if cite.recorded_at else None,
                    "transcript_snippet": snippet,
                    "confidence": cite.confidence,
                }
            )

        result: dict[str, Any] = {
            "query": query,
            "answer": answer_text,
            "citations": citations_data,
            "retrieval_stats": {
                "naive_hits": retrieval_result.naive_hits,
                "graph_hits": retrieval_result.graph_hits,
                "filtered_by_time": retrieval_result.filtered_by_time,
                "filtered_by_judge": rerank_result.filtered_count,
            },
        }
        if self._pii_scrubber is not None:
            result["pii_redacted_count"] = pii_redacted_count

        # M6: audit fire-and-forget.
        if self._audit is not None:
            await self._audit.record(
                tenant_id=tenant_id,
                user_id=user_id,
                action="query.answered",
                target=f"query:{query[:60]}",
                after={
                    "answer_len": len(answer_text or ""),
                    "pii_redacted_count": pii_redacted_count,
                },
            )

        return result
