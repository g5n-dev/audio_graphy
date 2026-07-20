"""End-to-end indexing integration test — recording → chunks → entities → graph.

Tests the full indexing pipeline (AC-1 through AC-8):
    AC-1: 1 recording → VAD → ASR → chunks → entities → graph
    AC-2: segments table written correctly
    AC-3: chunks table written correctly
    AC-6: GraphML file generated
    AC-7: file_index JSON files generated
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from audio_graphy.core.chunker import Chunker
from audio_graphy.core.extractor import EntityExtractor
from audio_graphy.core.graph import GraphBuilder
from audio_graphy.core.types import COMPLETION_DELIMITER, RECORD_DELIMITER, TUPLE_DELIMITER
from audio_graphy.models.recording import Recording


@pytest.mark.e2e
class TestE2EIndexing:
    """End-to-end indexing pipeline."""

    @staticmethod
    async def _create_recording(session_factory: Any, tenant_id: str = "default") -> int:
        """Create a recording in MySQL and return its ID."""
        async with session_factory() as session:
            rec = Recording(
                tenant_id=tenant_id,
                store_id="store_001",
                agent_name="张敏",
                path="/tmp/test_audio.wav",
                status="queued",
                pipeline_state="pending",
                recorded_at=datetime(2026, 7, 10, 14, 0, tzinfo=UTC),
            )
            session.add(rec)
            await session.flush()
            rec_id = rec.id
            await session.commit()
            return rec_id

    async def test_full_indexing_pipeline(
        self,
        scripted_bundle: Any,
        sample_audio_file: Path,
        async_session_factory: Any,
        file_index: Any,
        graph_store: Any,
        vector_store: Any,
    ) -> None:
        """AC-1: Full pipeline recording → chunks → entities → graph."""
        # Create recording
        rec_id = await self._create_recording(async_session_factory)

        # Configure scripted LLM for entity extraction
        strong_llm = scripted_bundle.strong_llm
        extraction_response = (
            f'("实体"{TUPLE_DELIMITER}CS75 Plus{TUPLE_DELIMITER}车型{TUPLE_DELIMITER}热门SUV)'
            f"{RECORD_DELIMITER}"
            f'("实体"{TUPLE_DELIMITER}张敏{TUPLE_DELIMITER}坐席{TUPLE_DELIMITER}销售顾问)'
            f"{RECORD_DELIMITER}"
            f'("关系"{TUPLE_DELIMITER}张敏{TUPLE_DELIMITER}推荐{TUPLE_DELIMITER}CS75 Plus{TUPLE_DELIMITER}坐席推荐了CS75 Plus)'
            f"{COMPLETION_DELIMITER}"
        )
        strong_llm.set_default_response(extraction_response)

        # Step 1: Chunker
        chunker = Chunker(
            scripted_bundle,  # type: ignore[arg-type]
            token_budget=1200,
            session_factory=async_session_factory,
            file_index=file_index,
        )
        chunker_output = await chunker.process_recording(
            recording_id=rec_id,
            audio_path=str(sample_audio_file),
            recorded_at=datetime(2026, 7, 10, tzinfo=UTC),
        )

        # AC-2: segments written
        assert len(chunker_output.segments) > 0
        for i, seg in enumerate(chunker_output.segments):
            assert seg.idx == i

        # AC-3: chunks written with content_hash
        assert len(chunker_output.chunks) > 0
        for chunk in chunker_output.chunks:
            assert chunk.chunk_id is not None  # DB ID assigned
            assert len(chunk.content_hash) == 64
            assert chunk.token_n > 0

        # Step 2: Embed chunks
        chunk_texts = [c.text for c in chunker_output.chunks]
        embeddings = await scripted_bundle.embed.embed_texts(chunk_texts)
        for chunk, emb in zip(chunker_output.chunks, embeddings, strict=True):
            assert chunk.chunk_id is not None
            await vector_store.upsert_chunk_vector("default", chunk.chunk_id, emb.vector)

        # Step 3: Extractor
        extractor = EntityExtractor(
            scripted_bundle,  # type: ignore[arg-type]
            prompt_template=(
                "抽取实体。{entity_types} {tuple_delimiter} "
                "{record_delimiter} {completion_delimiter} {input_text}"
            ),
            gleaning_rounds=0,
            file_index=file_index,
        )
        chunk_inputs = [
            (c.chunk_id, c.text, rec_id) for c in chunker_output.chunks if c.chunk_id is not None
        ]
        extraction_results = await extractor.extract_from_chunks(chunk_inputs)

        assert len(extraction_results) == len(chunker_output.chunks)
        total_entities = sum(len(r.entities) for r in extraction_results)
        assert total_entities > 0

        # Step 4: Graph builder
        builder = GraphBuilder(
            graph_store,
            bundle=scripted_bundle,  # type: ignore[arg-type]
            vector_store=vector_store,
        )
        snapshot = await builder.build_from_extractions(extraction_results)

        # AC-1: GraphSnapshot non-empty
        assert snapshot.total_entities > 0
        assert snapshot.total_relations > 0

        # AC-6: GraphML file generated
        assert await graph_store.has_graph()

        # AC-7: file_index JSON files generated
        await file_index.flush()
        assert (file_index.working_path / "kv_store_video_segments.json").exists()
        assert (file_index.working_path / "kv_store_text_chunks.json").exists()

    async def test_segments_written_to_mysql(
        self,
        scripted_bundle: Any,
        sample_audio_file: Path,
        async_session_factory: Any,
    ) -> None:
        """AC-2: Segments table written correctly."""
        rec_id = await self._create_recording(async_session_factory)

        chunker = Chunker(
            scripted_bundle,  # type: ignore[arg-type]
            token_budget=1200,
            session_factory=async_session_factory,
        )
        output = await chunker.process_recording(
            recording_id=rec_id,
            audio_path=str(sample_audio_file),
            recorded_at=None,
        )

        # Verify segments in MySQL
        from sqlalchemy import select

        from audio_graphy.models.segment import Segment

        async with async_session_factory() as session:
            stmt = select(Segment).where(Segment.recording_id == rec_id)
            result = await session.execute(stmt)
            db_segments = result.scalars().all()

        assert len(db_segments) == len(output.segments)
        for db_seg, out_seg in zip(db_segments, output.segments, strict=True):
            assert db_seg.idx == out_seg.idx
            assert db_seg.start_sec == out_seg.start_sec

    async def test_chunks_written_to_mysql(
        self,
        scripted_bundle: Any,
        sample_audio_file: Path,
        async_session_factory: Any,
    ) -> None:
        """AC-3: Chunks table written with unique content_hash."""
        rec_id = await self._create_recording(async_session_factory)

        chunker = Chunker(
            scripted_bundle,  # type: ignore[arg-type]
            token_budget=1200,
            session_factory=async_session_factory,
        )
        output = await chunker.process_recording(
            recording_id=rec_id,
            audio_path=str(sample_audio_file),
            recorded_at=None,
        )

        from sqlalchemy import select

        from audio_graphy.models.chunk import Chunk as ChunkModel

        async with async_session_factory() as session:
            stmt = select(ChunkModel).where(ChunkModel.recording_id == rec_id)
            result = await session.execute(stmt)
            db_chunks = result.scalars().all()

        assert len(db_chunks) == len(output.chunks)
        # content_hash unique
        hashes = [c.content_hash for c in db_chunks]
        assert len(hashes) == len(set(hashes))
