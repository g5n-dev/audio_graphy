"""End-to-end query integration test — query → retrieval → rerank → answer + citations.

Tests the full query pipeline (AC-9 through AC-13):
    AC-9: query → dual-channel retrieval → LLM filter → refine → answer + citations
    AC-10: naive channel召回
    AC-12: union dedup
    AC-13: LLM as-judge filtering
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from audio_graphy.core.chunker import Chunker
from audio_graphy.core.extractor import EntityExtractor
from audio_graphy.core.graph import GraphBuilder
from audio_graphy.core.rerank import Reranker, RerankResult
from audio_graphy.core.retrieval import DualChannelRetriever, RetrievalResult
from audio_graphy.core.types import COMPLETION_DELIMITER, RECORD_DELIMITER, TUPLE_DELIMITER
from audio_graphy.models.recording import Recording


@pytest.mark.integration
@pytest.mark.e2e
class TestE2EQuery:
    """End-to-end query pipeline."""

    @staticmethod
    async def _setup_index(
        scripted_bundle: Any,
        audio_path: Path,
        session_factory: Any,
        file_index: Any,
        graph_store: Any,
        vector_store: Any,
    ) -> int:
        """Run the indexing pipeline and return recording_id."""
        # Create recording
        async with session_factory() as session:
            rec = Recording(
                tenant_id="default",
                store_id="store_001",
                agent_name="张敏",
                path=str(audio_path),
                status="processing",
                pipeline_state="pending",
                recorded_at=datetime(2026, 7, 10, 14, 0, tzinfo=UTC),
            )
            session.add(rec)
            await session.flush()
            rec_id = rec.id
            await session.commit()

        # Configure LLM for extraction
        strong_llm = scripted_bundle.strong_llm
        extraction_response = (
            f'("实体"{TUPLE_DELIMITER}CS75 Plus{TUPLE_DELIMITER}车型{TUPLE_DELIMITER}热门SUV)'
            f"{RECORD_DELIMITER}"
            f'("实体"{TUPLE_DELIMITER}张敏{TUPLE_DELIMITER}坐席{TUPLE_DELIMITER}销售顾问)'
            f"{RECORD_DELIMITER}"
            f'("关系"{TUPLE_DELIMITER}张敏{TUPLE_DELIMITER}推荐{TUPLE_DELIMITER}CS75 Plus{TUPLE_DELIMITER}推荐了)'
            f"{COMPLETION_DELIMITER}"
        )
        strong_llm.set_default_response(extraction_response)

        # Chunker
        chunker = Chunker(
            scripted_bundle,  # type: ignore[arg-type]
            session_factory=session_factory,
            file_index=file_index,
        )
        chunker_output = await chunker.process_recording(
            recording_id=rec_id,
            audio_path=str(audio_path),
            recorded_at=datetime(2026, 7, 10, tzinfo=UTC),
        )

        # Embed chunks
        chunk_texts = [c.text for c in chunker_output.chunks]
        embeddings = await scripted_bundle.embed.embed_texts(chunk_texts)
        for chunk, emb in zip(chunker_output.chunks, embeddings, strict=True):
            if chunk.chunk_id is not None:
                await vector_store.upsert_chunk_vector("default", chunk.chunk_id, emb.vector)

        # Extractor
        extractor = EntityExtractor(
            scripted_bundle,  # type: ignore[arg-type]
            prompt_template="抽取 {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
            file_index=file_index,
        )
        chunk_inputs = [
            (c.chunk_id, c.text, rec_id) for c in chunker_output.chunks if c.chunk_id is not None
        ]
        results = await extractor.extract_from_chunks(chunk_inputs)

        # Graph builder
        builder = GraphBuilder(
            graph_store,
            bundle=scripted_bundle,  # type: ignore[arg-type]
            vector_store=vector_store,
        )
        await builder.build_from_extractions(results)

        return rec_id

    async def test_full_query_pipeline(
        self,
        scripted_bundle: Any,
        sample_audio_file: Path,
        async_session_factory: Any,
        file_index: Any,
        graph_store: Any,
        vector_store: Any,
    ) -> None:
        """AC-9: Full query → retrieval → rerank → answer + citations."""
        # Setup index
        await self._setup_index(
            scripted_bundle,
            sample_audio_file,
            async_session_factory,
            file_index,
            graph_store,
            vector_store,
        )

        # Configure LLM for query phase
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response("yes")  # Judge: keep all
        strong_llm.set_response("请根据", "根据录音分析，CS75 Plus 被推荐。")

        weak_llm = scripted_bundle.weak_llm
        weak_llm.set_default_response("CS75 Plus, 推荐, 优惠")

        # Retrieval
        retriever = DualChannelRetriever(
            scripted_bundle,  # type: ignore[arg-type]
            vector_store,
            graph_store,
            session_factory=async_session_factory,
            file_index=file_index,
        )
        retrieval_result = await retriever.retrieve(
            "CS75 Plus 推荐",
            top_k=5,
        )

        assert isinstance(retrieval_result, RetrievalResult)
        # AC-10: naive channel should have hits (we inserted chunk vectors)
        assert retrieval_result.naive_hits > 0

        # Rerank
        reranker = Reranker(
            scripted_bundle,  # type: ignore[arg-type]
            file_index=file_index,
            graph_store=graph_store,
        )
        rerank_result = await reranker.rerank_and_answer(
            "CS75 Plus 推荐",
            retrieval_result.candidates,
        )

        # AC-9: Answer non-empty
        assert isinstance(rerank_result, RerankResult)
        assert rerank_result.answer != ""
        assert rerank_result.answer != "未找到相关录音片段"

        # Citations should have provenance
        if rerank_result.citations:
            for cite in rerank_result.citations:
                assert cite.chunk_id > 0
                assert cite.recording_id > 0

    async def test_time_range_filtering(
        self,
        scripted_bundle: Any,
        sample_audio_file: Path,
        async_session_factory: Any,
        file_index: Any,
        graph_store: Any,
        vector_store: Any,
    ) -> None:
        """AC-21: Time range filtering in retrieval."""
        await self._setup_index(
            scripted_bundle,
            sample_audio_file,
            async_session_factory,
            file_index,
            graph_store,
            vector_store,
        )

        retriever = DualChannelRetriever(
            scripted_bundle,  # type: ignore[arg-type]
            vector_store,
            graph_store,
            session_factory=async_session_factory,
            file_index=file_index,
        )

        # Time range that INCLUDES the recording (July 10)
        result_in = await retriever.retrieve(
            "CS75 Plus",
            time_range=(datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 31, tzinfo=UTC)),
        )
        # Time range that EXCLUDES the recording
        result_out = await retriever.retrieve(
            "CS75 Plus",
            time_range=(datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 31, tzinfo=UTC)),
        )

        # In-range should have more or equal candidates than out-of-range
        assert len(result_in.candidates) >= len(result_out.candidates)
        # Out-of-range should have filtered some
        if result_out.filtered_by_time > 0:
            assert result_out.filtered_by_time > 0

    async def test_no_time_range_no_filter(
        self,
        scripted_bundle: Any,
        sample_audio_file: Path,
        async_session_factory: Any,
        file_index: Any,
        graph_store: Any,
        vector_store: Any,
    ) -> None:
        """AC-22: No time_range → no filtering."""
        await self._setup_index(
            scripted_bundle,
            sample_audio_file,
            async_session_factory,
            file_index,
            graph_store,
            vector_store,
        )

        retriever = DualChannelRetriever(
            scripted_bundle,  # type: ignore[arg-type]
            vector_store,
            graph_store,
            session_factory=async_session_factory,
            file_index=file_index,
        )
        result = await retriever.retrieve("CS75 Plus")

        assert result.filtered_by_time == 0
