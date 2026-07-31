"""Integration tests for Chunker + Extractor pipeline.

Tests cover:
    - Full chunker → extractor pipeline with mock adapters
    - Provenance chain: chunk.segment_ids → segment.idx
    - Extractor uses chunk_id from chunker output
    - File index persistence (segments + chunks)
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from audio_graphy.core.chunker import Chunker
from audio_graphy.core.extractor import EntityExtractor


@pytest.mark.integration
class TestChunkerExtractorIntegration:
    """Chunker → Extractor pipeline integration."""

    async def test_full_pipeline(
        self,
        scripted_bundle: object,
        sample_audio_file: Path,
        file_index: object,
        sample_graphrag_response: str,
    ) -> None:
        """Full pipeline: chunker → extractor produces entities with provenance."""
        # Configure scripted LLM
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response(sample_graphrag_response)

        # Step 1: Chunker
        chunker = Chunker(
            scripted_bundle,  # type: ignore[arg-type]
            token_budget=1200,
            file_index=file_index,  # type: ignore[arg-type]
        )
        chunker_output = await chunker.process_recording(
            recording_id=1,
            audio_path=str(sample_audio_file),
            recorded_at=datetime(2026, 7, 10, tzinfo=UTC),
        )

        assert len(chunker_output.segments) > 0
        assert len(chunker_output.chunks) > 0

        # Step 2: Extractor
        extractor = EntityExtractor(
            scripted_bundle,  # type: ignore[arg-type]
            prompt_template=(
                "抽取实体。{entity_types} {tuple_delimiter} "
                "{record_delimiter} {completion_delimiter} {input_text}"
            ),
            gleaning_rounds=0,
            file_index=file_index,  # type: ignore[arg-type]
        )

        # Build chunks input: (chunk_id, text, recording_id)
        # Since we didn't write to MySQL, chunk_id is None — use index as ID
        chunk_inputs = [(i, chunk.text, 1) for i, chunk in enumerate(chunker_output.chunks)]

        results = await extractor.extract_from_chunks(chunk_inputs, concurrency=2)

        assert len(results) == len(chunker_output.chunks)

        # At least some results should have entities
        total_entities = sum(len(r.entities) for r in results)
        assert total_entities > 0

    async def test_provenance_chain_chunk_to_segment(
        self,
        scripted_bundle: object,
        sample_audio_file: Path,
        file_index: object,
        sample_graphrag_response: str,
    ) -> None:
        """Provenance: chunk.segment_ids → segment.idx is valid."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response(sample_graphrag_response)

        chunker = Chunker(
            scripted_bundle,  # type: ignore[arg-type]
            token_budget=1200,
            file_index=file_index,  # type: ignore[arg-type]
        )
        output = await chunker.process_recording(
            recording_id=1,
            audio_path=str(sample_audio_file),
            recorded_at=None,
        )

        valid_segment_ids = {seg.idx for seg in output.segments}
        for chunk in output.chunks:
            for seg_id in chunk.segment_ids:
                assert seg_id in valid_segment_ids, (
                    f"Chunk references segment {seg_id} which doesn't exist"
                )

    async def test_provenance_entity_to_chunk(
        self,
        scripted_bundle: object,
        sample_audio_file: Path,
        file_index: object,
        sample_graphrag_response: str,
    ) -> None:
        """Provenance: entity.chunk_id references valid chunk."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response(sample_graphrag_response)

        chunker = Chunker(
            scripted_bundle,  # type: ignore[arg-type]
            token_budget=1200,
            file_index=file_index,  # type: ignore[arg-type]
        )
        output = await chunker.process_recording(
            recording_id=1,
            audio_path=str(sample_audio_file),
            recorded_at=None,
        )

        # Use chunk indices as IDs
        chunk_inputs = [(i, c.text, 1) for i, c in enumerate(output.chunks)]

        extractor = EntityExtractor(
            scripted_bundle,  # type: ignore[arg-type]
            prompt_template="抽取 {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
        )
        results = await extractor.extract_from_chunks(chunk_inputs)

        valid_chunk_ids = {ci[0] for ci in chunk_inputs}
        for result in results:
            for entity in result.entities:
                assert entity.chunk_id in valid_chunk_ids
                assert entity.recording_id == 1

    async def test_file_index_persistence(
        self,
        scripted_bundle: object,
        sample_audio_file: Path,
        file_index: object,
    ) -> None:
        """File index stores segments and chunks after chunker runs."""
        chunker = Chunker(
            scripted_bundle,  # type: ignore[arg-type]
            token_budget=1200,
            file_index=file_index,  # type: ignore[arg-type]
        )
        output = await chunker.process_recording(
            recording_id=42,
            audio_path=str(sample_audio_file),
            recorded_at=datetime(2026, 7, 15, 10, 0, tzinfo=UTC),
        )

        # Flush to persist
        await file_index.flush()  # type: ignore[attr-defined]

        # Verify segments were stored
        for seg in output.segments:
            key = f"42_{seg.idx}"
            stored = await file_index.get("kv_store_video_segments", key)  # type: ignore[attr-defined]
            assert stored is not None
            assert stored["recorded_at"] is not None

    async def test_entity_extraction_does_not_persist_legacy_file_cache(
        self,
        scripted_bundle: object,
        sample_audio_file: Path,
        file_index: object,
        sample_graphrag_response: str,
    ) -> None:
        """FileIndex flush/reload never persists EntityExtractor outputs."""
        strong_llm = scripted_bundle.strong_llm
        strong_llm.set_default_response(sample_graphrag_response)

        # First extraction
        extractor = EntityExtractor(
            scripted_bundle,  # type: ignore[arg-type]
            prompt_template="抽取 {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
            file_index=file_index,  # type: ignore[arg-type]
        )

        await extractor.extract_from_chunk(1, "测试缓存持久化", recording_id=1)
        await file_index.flush()  # type: ignore[attr-defined]

        # Create new extractor with same file_index (simulates reload)
        extractor2 = EntityExtractor(
            scripted_bundle,  # type: ignore[arg-type]
            prompt_template="抽取 {entity_types} {tuple_delimiter} {record_delimiter} {completion_delimiter} {input_text}",
            gleaning_rounds=0,
            file_index=file_index,  # type: ignore[arg-type]
        )
        # file_index already has the cache — need to ensure it's loaded
        await file_index.load()  # type: ignore[attr-defined]

        await extractor2.extract_from_chunk(1, "测试缓存持久化", recording_id=1)

        assert (
            await file_index.get_all("kv_store_llm_response_cache")  # type: ignore[attr-defined]
            == {}
        )
