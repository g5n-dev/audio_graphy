"""Unit tests for RecomputeService — prompt version switch recomputation.

Tests: create_task, dry_run, execute_task, get_task_status.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.adapters.bundle import AdapterBundle
from audio_graphy.errors import TaskNotFoundError
from audio_graphy.storage.file_index import FileIndex
from audio_graphy.tags.recompute import RecomputeService

TENANT = "chang_an"


@pytest.mark.asyncio
class TestRecomputeService:
    """Tests for RecomputeService."""

    async def test_create_task(
        self, session_factory, mock_bundle, file_index, seeded_recording
    ) -> None:
        """create_task creates a RecomputeTask row with correct total."""

        svc = RecomputeService(session_factory, mock_bundle, file_index)
        task = await svc.create_task(
            tenant_id=TENANT,
            prompt_version="v2",
            tag_paths=["quality.greeting"],
            recording_ids=None,
        )
        assert task.task_id is not None
        assert task.task_id.startswith("recompute-")
        assert task.status == "pending"
        assert task.prompt_version == "v2"
        assert task.total >= 1  # The seeded recording has prompt_version="v1"

    async def test_create_task_with_recording_filter(
        self, session_factory, mock_bundle, file_index, seeded_recording
    ) -> None:
        """create_task respects recording_ids filter."""

        svc = RecomputeService(session_factory, mock_bundle, file_index)
        task = await svc.create_task(
            tenant_id=TENANT,
            prompt_version="v2",
            tag_paths=None,
            recording_ids=[999],  # Non-existent recording
        )
        assert task.total == 0  # No recordings match the filter

    async def test_dry_run(
        self, session_factory, mock_bundle, file_index, seeded_recording
    ) -> None:
        """dry_run returns a diff dict without writing."""

        svc = RecomputeService(session_factory, mock_bundle, file_index)
        result = await svc.dry_run(
            tenant_id=TENANT,
            prompt_version="v2",
            tag_paths=["quality.greeting", "quality.closing"],
            recording_ids=None,
        )
        assert result["dry_run"] is True
        assert "affected_count" in result
        assert "changed_count" in result
        assert "unchanged_count" in result
        assert "changes_preview" in result
        assert result["affected_count"] >= 1

    async def test_dry_run_no_recordings(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        mock_bundle: AdapterBundle,
        file_index: FileIndex,
    ) -> None:
        """dry_run returns zero counts when no recordings are affected."""

        svc = RecomputeService(session_factory, mock_bundle, file_index)
        result = await svc.dry_run(
            tenant_id=TENANT,
            prompt_version="v2",
            tag_paths=None,
            recording_ids=None,
        )
        assert result["affected_count"] == 0
        assert result["changed_count"] == 0

    async def test_get_task_status_not_found(
        self, session_factory, mock_bundle, file_index
    ) -> None:
        """get_task_status raises TaskNotFoundError for unknown task."""

        svc = RecomputeService(session_factory, mock_bundle, file_index)
        with pytest.raises(TaskNotFoundError):
            await svc.get_task_status("nonexistent-task", TENANT)

    async def test_get_task_status_found(
        self, session_factory, mock_bundle, file_index, seeded_recording
    ) -> None:
        """get_task_status returns the task."""

        svc = RecomputeService(session_factory, mock_bundle, file_index)
        task = await svc.create_task(TENANT, "v2", None, None)
        fetched = await svc.get_task_status(task.task_id, TENANT)
        assert fetched.task_id == task.task_id

    async def test_get_task_status_cross_tenant(
        self, session_factory, mock_bundle, file_index, seeded_recording
    ) -> None:
        """get_task_status raises when accessed from different tenant."""

        svc = RecomputeService(session_factory, mock_bundle, file_index)
        task = await svc.create_task(TENANT, "v2", None, None)
        with pytest.raises(TaskNotFoundError):
            await svc.get_task_status(task.task_id, "byd")

    async def test_execute_task_not_found(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        mock_bundle: AdapterBundle,
        file_index: FileIndex,
    ) -> None:
        """execute_task raises TaskNotFoundError for unknown task."""

        svc = RecomputeService(session_factory, mock_bundle, file_index)
        with pytest.raises(TaskNotFoundError):
            await svc.execute_task("nonexistent-task")

    async def test_execute_task_success(
        self, session_factory, mock_bundle, file_index, seeded_recording
    ) -> None:
        """execute_task processes recordings and marks done."""

        svc = RecomputeService(session_factory, mock_bundle, file_index)
        task = await svc.create_task(TENANT, "v2", None, None)
        await svc.execute_task(task.task_id)

        fetched = await svc.get_task_status(task.task_id, TENANT)
        assert fetched.status == "done"
        assert fetched.processed >= 1
        assert fetched.finished_at is not None

    async def test_compute_tag_value_with_cache(
        self, session_factory, mock_bundle, file_index, seeded_recording
    ) -> None:
        """_compute_tag_value_with_cache calls LLM then caches."""

        svc = RecomputeService(session_factory, mock_bundle, file_index)
        # First call — LLM
        value1, cached1 = await svc._compute_tag_value_with_cache(
            seeded_recording, "quality.greeting", "v2"
        )
        assert isinstance(value1, str)
        assert cached1 is False

        # Second call — should hit cache
        value2, cached2 = await svc._compute_tag_value_with_cache(
            seeded_recording, "quality.greeting", "v2"
        )
        assert cached2 is True
        assert value2 == value1
