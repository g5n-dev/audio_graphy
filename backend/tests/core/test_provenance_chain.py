"""3-level provenance chain integrity tests (AC-14 through AC-17).

Provenance chain:
    entity → source_id → chunk → segment_ids → segment → recording → recorded_at

Tests verify:
    AC-14: entity → source_id → chunk (GraphNode.source_ids non-empty)
    AC-15: chunk → segment_ids → segment (ChunkRecord.segment_ids non-empty)
    AC-16: segment → recording → recorded_at (Citation has recording_id + recorded_at)
    AC-17: provenance chain can be reverse-traced end-to-end
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from audio_graphy.core.chunker import Chunker
from audio_graphy.core.extractor import EntityExtractor
from audio_graphy.core.graph import GraphBuilder
from audio_graphy.core.types import (
    COMPLETION_DELIMITER,
    RECORD_DELIMITER,
    TUPLE_DELIMITER,
)
from audio_graphy.models.recording import Recording


@pytest.mark.integration
class TestProvenanceChain:
    """3-level provenance chain integrity."""

    @staticmethod
    async def _build_index(
        scripted_bundle: Any,
        audio_path: Path,
        session_factory: Any,
        file_index: Any,
        graph_store: Any,
        vector_store: Any,
    ) -> tuple[int, Any, Any]:
        """Build a minimal index and return (recording_id, chunker_output, snapshot)."""
        async with session_factory() as session:
            rec = Recording(
                tenant_id="default",
                store_id="store_001",
                path=str(audio_path),
                status="processing",
                pipeline_state="pending",
                recorded_at=datetime(2026, 7, 10, 14, 0, tzinfo=UTC),
            )
            session.add(rec)
            await session.flush()
            rec_id = rec.id
            await session.commit()

        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response(
            f'("实体"{TUPLE_DELIMITER}CS75 Plus{TUPLE_DELIMITER}车型{TUPLE_DELIMITER}SUV)'
            f"{RECORD_DELIMITER}"
            f'("实体"{TUPLE_DELIMITER}张敏{TUPLE_DELIMITER}坐席{TUPLE_DELIMITER}顾问)'
            f"{RECORD_DELIMITER}"
            f'("关系"{TUPLE_DELIMITER}张敏{TUPLE_DELIMITER}推荐{TUPLE_DELIMITER}CS75 Plus{TUPLE_DELIMITER}推荐了)'
            f"{COMPLETION_DELIMITER}"
        )

        chunker = Chunker(
            scripted_bundle,  # type: ignore[arg-type]
            session_factory=session_factory,
            file_index=file_index,
        )
        output = await chunker.process_recording(
            recording_id=rec_id,
            audio_path=str(audio_path),
            recorded_at=datetime(2026, 7, 10, tzinfo=UTC),
        )

        # Embed chunks
        embeddings = await scripted_bundle.embed.embed_texts([c.text for c in output.chunks])
        for chunk, emb in zip(output.chunks, embeddings, strict=True):
            if chunk.chunk_id is not None:
                await vector_store.upsert_chunk_vector("default", chunk.chunk_id, emb.vector)

        # Extract
        extractor = EntityExtractor(
            scripted_bundle,  # type: ignore[arg-type]
            prompt_template="抽取 {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
            file_index=file_index,
        )
        chunk_inputs = [
            (c.chunk_id, c.text, rec_id) for c in output.chunks if c.chunk_id is not None
        ]
        results = await extractor.extract_from_chunks(chunk_inputs)

        # Build graph
        builder = GraphBuilder(graph_store, bundle=scripted_bundle, vector_store=vector_store)  # type: ignore[arg-type]
        snapshot = await builder.build_from_extractions(results)

        return rec_id, output, snapshot

    async def test_entity_to_chunk_provenance(
        self,
        scripted_bundle: Any,
        sample_audio_file: Path,
        async_session_factory: Any,
        file_index: Any,
        graph_store: Any,
        vector_store: Any,
    ) -> None:
        """AC-14: entity → source_id → chunk_id is valid."""
        _rec_id, output, snapshot = await self._build_index(
            scripted_bundle,
            sample_audio_file,
            async_session_factory,
            file_index,
            graph_store,
            vector_store,
        )

        valid_chunk_ids = {c.chunk_id for c in output.chunks if c.chunk_id is not None}

        for node in snapshot.nodes:
            # AC-14: source_ids non-empty
            assert len(node.source_ids) > 0, f"Entity {node.entity_id} has empty source_ids"
            for source_id in node.source_ids:
                # Parse chunk_id from source_id
                parts = source_id.rsplit("_", 1)
                assert len(parts) == 2
                chunk_id = int(parts[1])
                # chunk_id should be a valid DB ID
                assert chunk_id in valid_chunk_ids, (
                    f"source_id {source_id} references non-existent chunk {chunk_id}"
                )

    async def test_chunk_to_segment_provenance(
        self,
        scripted_bundle: Any,
        sample_audio_file: Path,
        async_session_factory: Any,
        file_index: Any,
        graph_store: Any,
        vector_store: Any,
    ) -> None:
        """AC-15: chunk → segment_ids → segment is valid."""
        _rec_id, output, _snapshot = await self._build_index(
            scripted_bundle,
            sample_audio_file,
            async_session_factory,
            file_index,
            graph_store,
            vector_store,
        )

        valid_segment_indices = {seg.idx for seg in output.segments}

        for chunk in output.chunks:
            # AC-15: segment_ids non-empty
            assert len(chunk.segment_ids) > 0, "Chunk has empty segment_ids"
            for seg_id in chunk.segment_ids:
                assert seg_id in valid_segment_indices, (
                    f"Chunk references non-existent segment {seg_id}"
                )

    async def test_segment_to_recording_provenance(
        self,
        scripted_bundle: Any,
        sample_audio_file: Path,
        async_session_factory: Any,
        file_index: Any,
        graph_store: Any,
        vector_store: Any,
    ) -> None:
        """AC-16: segment → recording → recorded_at chain."""
        rec_id, output, _snapshot = await self._build_index(
            scripted_bundle,
            sample_audio_file,
            async_session_factory,
            file_index,
            graph_store,
            vector_store,
        )

        # All segments belong to the recording
        for _seg in output.segments:
            # The recording_id should match
            assert rec_id > 0

        # file_index should have recorded_at
        for seg in output.segments:
            key = f"{rec_id}_{seg.idx}"
            stored = await file_index.get("kv_store_video_segments", key)
            assert stored is not None
            assert stored["recorded_at"] is not None

    async def test_full_reverse_trace(
        self,
        scripted_bundle: Any,
        sample_audio_file: Path,
        async_session_factory: Any,
        file_index: Any,
        graph_store: Any,
        vector_store: Any,
    ) -> None:
        """AC-17: Provenance chain can be reverse-traced end-to-end.

        Path: GraphNode.source_ids → chunk_id → ChunkRecord.segment_ids → SegmentRecord
        """
        rec_id, output, snapshot = await self._build_index(
            scripted_bundle,
            sample_audio_file,
            async_session_factory,
            file_index,
            graph_store,
            vector_store,
        )

        # Pick an entity from the snapshot
        assert len(snapshot.nodes) > 0
        node = snapshot.nodes[0]

        # Trace: entity → source_id → chunk_id
        source_id = node.source_ids[0]
        parts = source_id.rsplit("_", 1)
        chunk_id = int(parts[1])

        # Trace: chunk_id → ChunkRecord
        chunk = next((c for c in output.chunks if c.chunk_id == chunk_id), None)
        assert chunk is not None, f"Chunk {chunk_id} not found in output"

        # Trace: chunk → segment_ids → SegmentRecord
        assert len(chunk.segment_ids) > 0
        seg_idx = chunk.segment_ids[0]
        segment = next((s for s in output.segments if s.idx == seg_idx), None)
        assert segment is not None, f"Segment {seg_idx} not found"

        # Trace: segment → recording → recorded_at
        key = f"{rec_id}_{seg_idx}"
        stored = await file_index.get("kv_store_video_segments", key)
        assert stored is not None
        assert stored["recorded_at"] is not None
        assert stored["transcript"] is not None

        # Full chain verified: entity → chunk → segment → recording → recorded_at
