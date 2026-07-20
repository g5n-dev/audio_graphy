"""Unit tests for IndexingService — pipeline orchestration.

Tests: run_pipeline (happy path with mock adapters, error handling),
stage methods, _update_state.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.adapters.bundle import AdapterBundle
from audio_graphy.models.recording import Recording
from audio_graphy.storage.file_index import FileIndex
from audio_graphy.storage.graph_networkx import NetworkXGraphStore
from audio_graphy.storage.mysql_vector import MySQLVectorStore

TENANT = "chang_an"


@pytest.mark.asyncio
class TestIndexingService:
    """Tests for IndexingService."""

    async def test_run_pipeline_success(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        mock_bundle: AdapterBundle,
        vector_store: MySQLVectorStore,
        graph_store: NetworkXGraphStore,
        file_index: FileIndex,
        seeded_recording: Recording,
    ) -> None:
        """run_pipeline processes a recording end-to-end with mock adapters."""
        from sqlalchemy import select

        from audio_graphy.models.enums import PipelineState, RecordingStatus
        from audio_graphy.services.indexing import IndexingService

        svc = IndexingService(
            session_factory,
            mock_bundle,
            vector_store,
            graph_store,
            file_index,
        )
        await svc.run_pipeline(seeded_recording)

        async with session_factory() as session:
            result = await session.execute(
                select(Recording).where(Recording.id == seeded_recording.id)
            )
            rec = result.scalar_one()
            assert rec.status == RecordingStatus.INDEXED.value
            assert rec.pipeline_state == PipelineState.DONE.value
            assert rec.indexed_at is not None

    async def test_run_pipeline_sets_processing(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        mock_bundle: AdapterBundle,
        vector_store: MySQLVectorStore,
        graph_store: NetworkXGraphStore,
        file_index: FileIndex,
        seeded_recording: Recording,
    ) -> None:
        """run_pipeline sets status to processing at the start."""
        from sqlalchemy import select

        from audio_graphy.models.enums import PipelineState, RecordingStatus
        from audio_graphy.models.recording import Recording as RecModel
        from audio_graphy.services.indexing import IndexingService

        svc = IndexingService(
            session_factory,
            mock_bundle,
            vector_store,
            graph_store,
            file_index,
        )

        call_count = 0

        async def failing_stage(recording: RecModel) -> None:
            nonlocal call_count
            call_count += 1
            async with session_factory() as session:
                result = await session.execute(select(RecModel).where(RecModel.id == recording.id))
                r = result.scalar_one()
                assert r.status == RecordingStatus.PROCESSING.value
            raise RuntimeError("intentional failure")

        svc._stage_vad_asr_chunk = failing_stage  # type: ignore[method-assign]
        await svc.run_pipeline(seeded_recording)

        assert call_count == 1

        async with session_factory() as session:
            result = await session.execute(
                select(Recording).where(Recording.id == seeded_recording.id)
            )
            rec = result.scalar_one()
            assert rec.status == RecordingStatus.FAILED.value
            assert rec.pipeline_state == PipelineState.ERROR.value

    async def test_run_pipeline_error_sets_failed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        mock_bundle: AdapterBundle,
        vector_store: MySQLVectorStore,
        graph_store: NetworkXGraphStore,
        file_index: FileIndex,
        seeded_recording: Recording,
    ) -> None:
        """run_pipeline sets status=failed on exception."""
        from sqlalchemy import select

        from audio_graphy.models.enums import PipelineState, RecordingStatus
        from audio_graphy.services.indexing import IndexingService

        svc = IndexingService(
            session_factory,
            mock_bundle,
            vector_store,
            graph_store,
            file_index,
        )

        async def failing_extract(recording: Any) -> list[Any]:
            raise ValueError("extract failed")

        svc._stage_extract = failing_extract  # type: ignore[method-assign]
        await svc.run_pipeline(seeded_recording)

        async with session_factory() as session:
            result = await session.execute(
                select(Recording).where(Recording.id == seeded_recording.id)
            )
            rec = result.scalar_one()
            assert rec.status == RecordingStatus.FAILED.value
            assert rec.pipeline_state == PipelineState.ERROR.value

    async def test_update_state(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        mock_bundle: AdapterBundle,
        vector_store: MySQLVectorStore,
        graph_store: NetworkXGraphStore,
        file_index: FileIndex,
        seeded_recording: Recording,
    ) -> None:
        """_update_state correctly updates recording status and pipeline_state."""
        from sqlalchemy import select

        from audio_graphy.models.enums import PipelineState, RecordingStatus
        from audio_graphy.services.indexing import IndexingService

        svc = IndexingService(
            session_factory,
            mock_bundle,
            vector_store,
            graph_store,
            file_index,
        )
        await svc._update_state(
            seeded_recording.id,
            RecordingStatus.PROCESSING.value,
            PipelineState.EXTRACTION.value,
        )

        async with session_factory() as session:
            result = await session.execute(
                select(Recording).where(Recording.id == seeded_recording.id)
            )
            rec = result.scalar_one()
            assert rec.status == RecordingStatus.PROCESSING.value
            assert rec.pipeline_state == PipelineState.EXTRACTION.value

    async def test_update_state_nonexistent_recording(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        mock_bundle: AdapterBundle,
        vector_store: MySQLVectorStore,
        graph_store: NetworkXGraphStore,
        file_index: FileIndex,
    ) -> None:
        """_update_state is a no-op for non-existent recording."""
        from audio_graphy.services.indexing import IndexingService

        svc = IndexingService(
            session_factory,
            mock_bundle,
            vector_store,
            graph_store,
            file_index,
        )
        await svc._update_state(99999, "processing", "extraction")

    async def test_stage_extract_no_chunks(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        mock_bundle: AdapterBundle,
        vector_store: MySQLVectorStore,
        graph_store: NetworkXGraphStore,
        file_index: FileIndex,
        seeded_recording: Recording,
    ) -> None:
        """_stage_extract returns empty list when no chunks exist."""
        from audio_graphy.services.indexing import IndexingService

        svc = IndexingService(
            session_factory,
            mock_bundle,
            vector_store,
            graph_store,
            file_index,
        )
        result = await svc._stage_extract(seeded_recording)
        assert result == []

    async def test_stage_graph_merge_empty_extractions(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        mock_bundle: AdapterBundle,
        vector_store: MySQLVectorStore,
        graph_store: NetworkXGraphStore,
        file_index: FileIndex,
        seeded_recording: Recording,
    ) -> None:
        """_stage_graph_merge is a no-op with empty extractions."""
        from audio_graphy.services.indexing import IndexingService

        svc = IndexingService(
            session_factory,
            mock_bundle,
            vector_store,
            graph_store,
            file_index,
        )
        await svc._stage_graph_merge(seeded_recording, [])
