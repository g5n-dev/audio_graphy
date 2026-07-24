"""Unit tests for IngestionService — recording registration, listing, detail, reindex.

Tests: register_recording (happy path, file not found, duplicate),
list_recordings (filters, pagination, sort), get_recording (not found, tenant isolation),
get_recording_detail, trigger_reindex.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.errors import (
    DuplicateRecordingError,
    FileNotFoundError400,
    RecordingNotFoundError,
)
from audio_graphy.models.recording import Recording
from audio_graphy.schemas.recordings import RecordingCreate
from audio_graphy.services.ingestion import IngestionService

TENANT = "chang_an"


@pytest.mark.asyncio
class TestIngestionService:
    """Tests for IngestionService."""

    async def test_register_recording_success(
        self, session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """register_recording creates a queued recording for an existing file."""

        # Create a dummy audio file
        audio_file = tmp_path / "recording.wav"
        audio_file.write_bytes(b"\x00" * 1000)

        svc = IngestionService(session_factory)
        body = RecordingCreate(
            store_id="S001",
            agent_name="张敏",
            path=str(audio_file),
        )
        rec = await svc.register_recording(TENANT, body)
        assert rec.id is not None
        assert rec.tenant_id == TENANT
        assert rec.store_id == "S001"
        assert rec.agent_name == "张敏"
        assert rec.status == "queued"
        assert rec.pipeline_state == "pending"

    async def test_register_recording_file_not_found(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """register_recording raises FileNotFoundError400 for missing file."""

        svc = IngestionService(session_factory)
        body = RecordingCreate(
            store_id="S001",
            agent_name="张敏",
            path="/tmp/nonexistent_audio_file_xyz.wav",
        )
        with pytest.raises(FileNotFoundError400):
            await svc.register_recording(TENANT, body)

    async def test_register_recording_duplicate(
        self, session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """register_recording raises DuplicateRecordingError for same path."""

        audio_file = tmp_path / "dup.wav"
        audio_file.write_bytes(b"\x00" * 1000)

        svc = IngestionService(session_factory)
        body = RecordingCreate(store_id="S001", agent_name="张敏", path=str(audio_file))
        await svc.register_recording(TENANT, body)

        with pytest.raises(DuplicateRecordingError):
            await svc.register_recording(TENANT, body)

    async def test_list_recordings_empty(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """list_recordings returns empty list when no recordings exist."""

        svc = IngestionService(session_factory)
        recordings, total = await svc.list_recordings(TENANT)
        assert recordings == []
        assert total == 0

    async def test_list_recordings_with_data(
        self, session_factory: async_sessionmaker[AsyncSession], seeded_recording: Recording
    ) -> None:
        """list_recordings returns seeded recordings."""

        svc = IngestionService(session_factory)
        recordings, total = await svc.list_recordings(TENANT)
        assert total == 1
        assert len(recordings) == 1
        assert recordings[0].id == seeded_recording.id

    async def test_list_recordings_filter_agent(
        self, session_factory: async_sessionmaker[AsyncSession], seeded_recording: Recording
    ) -> None:
        """list_recordings filters by agent_name."""

        svc = IngestionService(session_factory)
        _recordings, total = await svc.list_recordings(TENANT, agent_name="张敏")
        assert total == 1
        _recordings, total = await svc.list_recordings(TENANT, agent_name="nonexistent")
        assert total == 0

    async def test_list_recordings_filter_store(
        self, session_factory: async_sessionmaker[AsyncSession], seeded_recording: Recording
    ) -> None:
        """list_recordings filters by store_id."""

        svc = IngestionService(session_factory)
        _recordings, total = await svc.list_recordings(TENANT, store_id="S001")
        assert total == 1
        _recordings, total = await svc.list_recordings(TENANT, store_id="WRONG")
        assert total == 0

    async def test_list_recordings_pagination(
        self, session_factory: async_sessionmaker[AsyncSession], seeded_recording: Recording
    ) -> None:
        """list_recordings respects pagination."""

        svc = IngestionService(session_factory)
        recordings, total = await svc.list_recordings(TENANT, page=1, page_size=10)
        assert total == 1
        assert len(recordings) == 1
        # Page 2 should be empty
        recordings, total = await svc.list_recordings(TENANT, page=2, page_size=10)
        assert len(recordings) == 0

    async def test_list_recordings_sort(
        self, session_factory: async_sessionmaker[AsyncSession], seeded_recording: Recording
    ) -> None:
        """list_recordings respects sort parameter."""

        svc = IngestionService(session_factory)
        recordings, _ = await svc.list_recordings(TENANT, sort="recorded_at")
        assert len(recordings) == 1

    async def test_get_recording_found(
        self, session_factory: async_sessionmaker[AsyncSession], seeded_recording: Recording
    ) -> None:
        """get_recording returns the recording."""

        svc = IngestionService(session_factory)
        rec = await svc.get_recording(seeded_recording.id, TENANT)
        assert rec.id == seeded_recording.id

    async def test_get_recording_not_found(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """get_recording raises RecordingNotFoundError."""

        svc = IngestionService(session_factory)
        with pytest.raises(RecordingNotFoundError):
            await svc.get_recording(99999, TENANT)

    async def test_get_recording_tenant_isolation(
        self, session_factory: async_sessionmaker[AsyncSession], seeded_recording: Recording
    ) -> None:
        """get_recording raises when accessed from different tenant."""

        svc = IngestionService(session_factory)
        with pytest.raises(RecordingNotFoundError):
            await svc.get_recording(seeded_recording.id, "byd")

    async def test_get_recording_agent_user_id(
        self, session_factory: async_sessionmaker[AsyncSession], seeded_recording: Recording
    ) -> None:
        """get_recording with stable agent user ID enforces agent isolation."""

        svc = IngestionService(session_factory)
        # Correct agent → found
        rec = await svc.get_recording(seeded_recording.id, TENANT, agent_user_id=41)
        assert rec is not None
        # Wrong agent → not found
        with pytest.raises(RecordingNotFoundError):
            await svc.get_recording(seeded_recording.id, TENANT, agent_user_id=42)

    async def test_get_recording_detail(
        self, session_factory: async_sessionmaker[AsyncSession], seeded_recording: Recording
    ) -> None:
        """get_recording_detail returns recording + summary fields."""

        svc = IngestionService(session_factory)
        detail = await svc.get_recording_detail(seeded_recording.id, TENANT)
        assert detail["recording"].id == seeded_recording.id
        assert detail["segments_count"] == 0
        assert detail["chunks_count"] == 0
        assert detail["current_tags"] == []

    async def test_trigger_reindex(
        self, session_factory: async_sessionmaker[AsyncSession], seeded_recording: Recording
    ) -> None:
        """trigger_reindex resets recording to queued."""

        svc = IngestionService(session_factory)
        rec = await svc.trigger_reindex(seeded_recording.id, TENANT)
        assert rec.status == "queued"
        assert rec.pipeline_state == "pending"
        assert rec.indexed_at is None

    async def test_trigger_reindex_not_found(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """trigger_reindex raises for non-existent recording."""

        svc = IngestionService(session_factory)
        with pytest.raises(RecordingNotFoundError):
            await svc.trigger_reindex(99999, TENANT)
