"""Unit tests for PipelineWorker + create_scheduler — background pipeline worker.

Tests: poll_once (empty, with recording), create_scheduler construction.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.adapters.bundle import AdapterBundle
from audio_graphy.models.recording import Recording
from audio_graphy.storage.mysql_vector import MySQLVectorStore

TENANT = "chang_an"


@pytest.mark.asyncio
class TestPipelineWorker:
    """Tests for PipelineWorker."""

    async def test_poll_once_empty(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        mock_bundle: AdapterBundle,
        vector_store: MySQLVectorStore,
    ) -> None:
        """poll_once returns 0 when no queued recordings exist."""
        from audio_graphy.scheduler import PipelineWorker

        worker = PipelineWorker(
            session_factory,
            mock_bundle,
            vector_store,
            {},
            {},
            poll_seconds=1,
            concurrency=1,
        )
        count = await worker.poll_once()
        assert count == 0

    async def test_poll_once_with_recording(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        mock_bundle: AdapterBundle,
        vector_store: MySQLVectorStore,
        tmp_working_dir: Path,
        seeded_recording: Recording,
    ) -> None:
        """poll_once processes a queued recording and returns 1."""
        from sqlalchemy import select

        from audio_graphy.models.recording import Recording as RecModel
        from audio_graphy.scheduler import PipelineWorker
        from audio_graphy.storage.file_index import FileIndex
        from audio_graphy.storage.graph_networkx import NetworkXGraphStore

        graph_store = NetworkXGraphStore(tmp_working_dir, tenant_id=TENANT)
        file_index = FileIndex(tmp_working_dir, tenant_id=TENANT)

        worker = PipelineWorker(
            session_factory,
            mock_bundle,
            vector_store,
            {TENANT: graph_store},
            {TENANT: file_index},
            poll_seconds=1,
            concurrency=1,
        )
        count = await worker.poll_once()
        assert count == 1

        async with session_factory() as session:
            result = await session.execute(
                select(RecModel).where(RecModel.id == seeded_recording.id)
            )
            rec = result.scalar_one()
            assert rec.status != "queued"

    async def test_poll_once_creates_missing_stores(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        mock_bundle: AdapterBundle,
        vector_store: MySQLVectorStore,
        seeded_recording: Recording,
    ) -> None:
        """poll_once auto-creates graph_store/file_index for new tenants."""
        from audio_graphy.scheduler import PipelineWorker

        graph_stores: dict[str, Any] = {}
        file_indexes: dict[str, Any] = {}

        worker = PipelineWorker(
            session_factory,
            mock_bundle,
            vector_store,
            graph_stores,
            file_indexes,
            poll_seconds=1,
            concurrency=1,
        )
        count = await worker.poll_once()
        assert count == 1
        assert TENANT in graph_stores
        assert TENANT in file_indexes


class TestCreateScheduler:
    """Tests for create_scheduler factory."""

    def test_create_scheduler_returns_scheduler(self, mock_bundle: AdapterBundle) -> None:
        """create_scheduler returns a configured BackgroundScheduler."""
        from apscheduler.schedulers.background import BackgroundScheduler

        from audio_graphy.scheduler import PipelineWorker, create_scheduler

        worker = PipelineWorker.__new__(PipelineWorker)
        worker._poll_seconds = 5
        worker._concurrency = 1

        scheduler = create_scheduler(worker, poll_seconds=10)
        assert isinstance(scheduler, BackgroundScheduler)

        jobs = scheduler.get_jobs()
        assert len(jobs) == 1
        assert jobs[0].id == "pipeline_poll"
        assert jobs[0].trigger.interval.total_seconds() == 10

        scheduler.start()
        scheduler.shutdown(wait=False)
