"""End-to-end safety tests for recording processing generations."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.adapters.bundle import AdapterBundle
from audio_graphy.core.chunker import ChunkerOutput
from audio_graphy.models.enums import RecordingStatus
from audio_graphy.models.pipeline import (
    PIPELINE_RUN_IN_PROGRESS_STATES,
    RecordingPipelineRun,
)
from audio_graphy.models.recording import Recording
from audio_graphy.services.indexing import IndexingService
from audio_graphy.services.ingestion import IngestionService
from audio_graphy.storage.file_index import FileIndex
from audio_graphy.storage.graph_networkx import NetworkXGraphStore
from audio_graphy.storage.mysql_vector import MySQLVectorStore


class _FailingASR:
    async def transcribe(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("ASR unavailable")


class _FailingEmbed:
    dim = 1024
    model = "failing-embed"

    async def embed_texts(self, _texts: Any) -> Any:
        raise RuntimeError("embedding unavailable")


@pytest.mark.asyncio
async def test_verified_silence_is_ready_without_being_indexed(
    session_factory: async_sessionmaker[AsyncSession],
    mock_bundle: AdapterBundle,
    vector_store: MySQLVectorStore,
    graph_store: NetworkXGraphStore,
    file_index: FileIndex,
    seeded_recording: Recording,
) -> None:
    service = IndexingService(
        session_factory,
        mock_bundle,
        vector_store,
        graph_store,
        file_index,
    )

    async def silent_stage(recording: Recording, _run: RecordingPipelineRun) -> ChunkerOutput:
        return ChunkerOutput(recording_id=recording.id, segments=[], chunks=[])

    service._stage_vad_asr_chunk = silent_stage  # type: ignore[method-assign]
    await service.run_pipeline(seeded_recording)

    async with session_factory() as db:
        recording = await db.get(Recording, seeded_recording.id)
        assert recording is not None
        active = await db.get(RecordingPipelineRun, recording.active_pipeline_run_id)
        assert active is not None
        assert active.state == "ready_no_speech"
        assert active.projections_complete()
        assert recording.status == RecordingStatus.READY_NO_SPEECH.value
        assert recording.status != RecordingStatus.INDEXED.value
        assert recording.indexed_at is None


@pytest.mark.asyncio
async def test_successful_retry_has_one_active_generation(
    session_factory: async_sessionmaker[AsyncSession],
    mock_bundle: AdapterBundle,
    vector_store: MySQLVectorStore,
    graph_store: NetworkXGraphStore,
    file_index: FileIndex,
    seeded_recording: Recording,
) -> None:
    service = IndexingService(
        session_factory,
        mock_bundle,
        vector_store,
        graph_store,
        file_index,
    )
    await service.run_pipeline(seeded_recording)

    ingestion = IngestionService(session_factory)
    requeued = await ingestion.trigger_reindex(
        seeded_recording.id,
        str(seeded_recording.tenant_id),
        force=True,
    )
    await service.run_pipeline(requeued)

    async with session_factory() as db:
        recording = await db.get(Recording, seeded_recording.id)
        assert recording is not None
        active = await db.get(RecordingPipelineRun, recording.active_pipeline_run_id)
        assert active is not None
        assert active.state == "ready"
        assert active.generation == 2
        assert active.projections_complete()
        assert recording.status == "indexed"
        assert (
            await db.execute(
                select(func.count(RecordingPipelineRun.id)).where(
                    RecordingPipelineRun.recording_id == recording.id,
                    RecordingPipelineRun.state == "ready",
                )
            )
        ).scalar_one() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_field", "replacement"),
    [
        ("asr", _FailingASR()),
        ("embed", _FailingEmbed()),
    ],
)
async def test_required_ai_stage_failure_never_marks_recording_indexed(
    adapter_field: str,
    replacement: object,
    session_factory: async_sessionmaker[AsyncSession],
    mock_bundle: AdapterBundle,
    vector_store: MySQLVectorStore,
    graph_store: NetworkXGraphStore,
    file_index: FileIndex,
    seeded_recording: Recording,
) -> None:
    broken_bundle = replace(mock_bundle, **{adapter_field: replacement})
    service = IndexingService(
        session_factory,
        broken_bundle,
        vector_store,
        graph_store,
        file_index,
    )

    await service.run_pipeline(seeded_recording)

    async with session_factory() as db:
        recording = await db.get(Recording, seeded_recording.id)
        assert recording is not None
        assert recording.status != "indexed"
        assert recording.active_pipeline_run_id is None
        run = (
            await db.execute(
                select(RecordingPipelineRun).where(
                    RecordingPipelineRun.recording_id == recording.id
                )
            )
        ).scalar_one()
        assert run.state in {"partial", "failed_retryable"}


@pytest.mark.asyncio
async def test_file_index_is_not_acknowledged_until_its_checkpoint_is_durable(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
    mock_bundle: AdapterBundle,
    vector_store: MySQLVectorStore,
    graph_store: NetworkXGraphStore,
    file_index: FileIndex,
    seeded_recording: Recording,
) -> None:
    async def fail_checkpoint() -> None:
        raise OSError("injected file-index checkpoint failure")

    monkeypatch.setattr(file_index, "flush", fail_checkpoint)
    service = IndexingService(
        session_factory,
        mock_bundle,
        vector_store,
        graph_store,
        file_index,
    )

    await service.run_pipeline(seeded_recording)

    async with session_factory() as db:
        recording = await db.get(Recording, seeded_recording.id)
        assert recording is not None
        run = (
            await db.execute(
                select(RecordingPipelineRun).where(
                    RecordingPipelineRun.recording_id == recording.id
                )
            )
        ).scalar_one()
        assert recording.status != RecordingStatus.INDEXED.value
        assert recording.active_pipeline_run_id is None
        assert run.state in {"partial", "failed_retryable"}
        assert "file_index" not in run.completed_projections


@pytest.mark.asyncio
async def test_graph_projection_is_not_acknowledged_when_graphml_publish_fails(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
    mock_bundle: AdapterBundle,
    vector_store: MySQLVectorStore,
    graph_store: NetworkXGraphStore,
    file_index: FileIndex,
    seeded_recording: Recording,
) -> None:
    async def fail_graph_publish() -> None:
        raise OSError("injected GraphML checkpoint failure")

    monkeypatch.setattr(graph_store, "save", fail_graph_publish)
    service = IndexingService(
        session_factory,
        mock_bundle,
        vector_store,
        graph_store,
        file_index,
    )

    await service.run_pipeline(seeded_recording)

    async with session_factory() as db:
        recording = await db.get(Recording, seeded_recording.id)
        assert recording is not None
        run = (
            await db.execute(
                select(RecordingPipelineRun).where(
                    RecordingPipelineRun.recording_id == recording.id
                )
            )
        ).scalar_one()
        assert recording.status != RecordingStatus.INDEXED.value
        assert recording.active_pipeline_run_id is None
        assert run.state in {"partial", "failed_retryable"}
        assert "graph" not in run.completed_projections


@pytest.mark.asyncio
async def test_two_workers_atomically_claim_one_pipeline_run(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
    mock_bundle: AdapterBundle,
    vector_store: MySQLVectorStore,
    tmp_working_dir: Any,
    seeded_recording: Recording,
) -> None:
    from audio_graphy.scheduler import PipelineWorker

    calls: list[tuple[int, int | None]] = []

    async def fake_run(
        _service: IndexingService,
        recording: Recording,
        *,
        pipeline_run_id: int | None = None,
        lease_owner: str | None = None,
        lease_seconds: int = 60,
    ) -> None:
        del lease_owner, lease_seconds
        calls.append((recording.id, pipeline_run_id))
        await asyncio.sleep(0.05)

    monkeypatch.setattr(IndexingService, "run_pipeline", fake_run)
    await IngestionService(session_factory).trigger_reindex(
        seeded_recording.id,
        str(seeded_recording.tenant_id),
        idempotency_key="two-worker-claim",
    )
    workers = [
        PipelineWorker(
            session_factory,
            mock_bundle,
            vector_store,
            {},
            {},
            working_dir=str(tmp_working_dir),
            concurrency=1,
        )
        for _ in range(2)
    ]

    counts = await asyncio.gather(*(worker.poll_once() for worker in workers))

    assert sum(counts) == 1
    assert len(calls) == 1
    assert calls[0][0] == seeded_recording.id
    assert calls[0][1] is not None


@pytest.mark.asyncio
async def test_stale_worker_cannot_mutate_a_run_after_lease_reassignment(
    session_factory: async_sessionmaker[AsyncSession],
    mock_bundle: AdapterBundle,
    vector_store: MySQLVectorStore,
    graph_store: NetworkXGraphStore,
    file_index: FileIndex,
    seeded_recording: Recording,
) -> None:
    entered_stage = asyncio.Event()
    release_stage = asyncio.Event()
    service = IndexingService(
        session_factory,
        mock_bundle,
        vector_store,
        graph_store,
        file_index,
    )

    async def paused_silence(
        recording: Recording,
        _run: RecordingPipelineRun,
    ) -> ChunkerOutput:
        entered_stage.set()
        await release_stage.wait()
        return ChunkerOutput(recording_id=recording.id, segments=[], chunks=[])

    service._stage_vad_asr_chunk = paused_silence  # type: ignore[method-assign]
    stale_worker = asyncio.create_task(
        service.run_pipeline(
            seeded_recording,
            lease_owner="worker-old",
            lease_seconds=10,
        )
    )
    await asyncio.wait_for(entered_stage.wait(), timeout=1)

    async with session_factory() as db, db.begin():
        run = (
            await db.execute(
                select(RecordingPipelineRun)
                .where(
                    RecordingPipelineRun.recording_id == seeded_recording.id
                )
                .with_for_update()
            )
        ).scalar_one()
        run.state = "claimed"
        run.lease_owner = "worker-new"
        run.lease_expires_at = datetime.now(UTC) + timedelta(seconds=60)
        run.attempt_count += 1
        run_id = int(run.id)

    release_stage.set()
    await asyncio.wait_for(stale_worker, timeout=2)

    async with session_factory() as db:
        run = await db.get(RecordingPipelineRun, run_id)
        recording = await db.get(Recording, seeded_recording.id)
        assert run is not None
        assert recording is not None
        assert run.state == "claimed"
        assert run.lease_owner == "worker-new"
        assert recording.active_pipeline_run_id is None
        assert recording.status != RecordingStatus.INDEXED.value


@pytest.mark.asyncio
@pytest.mark.parametrize("expired_state", sorted(PIPELINE_RUN_IN_PROGRESS_STATES))
async def test_worker_reclaims_an_expired_run_from_every_in_progress_stage(
    expired_state: str,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
    mock_bundle: AdapterBundle,
    vector_store: MySQLVectorStore,
    tmp_working_dir: Any,
    seeded_recording: Recording,
) -> None:
    from audio_graphy.scheduler import PipelineWorker

    calls: list[tuple[int, int | None, str | None]] = []

    async def fake_run(
        _service: IndexingService,
        recording: Recording,
        *,
        pipeline_run_id: int | None = None,
        lease_owner: str | None = None,
        lease_seconds: int = 60,
    ) -> None:
        del lease_seconds
        calls.append((recording.id, pipeline_run_id, lease_owner))

    monkeypatch.setattr(IndexingService, "run_pipeline", fake_run)
    queued = await IngestionService(session_factory).queue_reindex(
        seeded_recording.id,
        str(seeded_recording.tenant_id),
        idempotency_key="expired-stage-reclaim",
    )
    async with session_factory() as db, db.begin():
        run = await db.get(RecordingPipelineRun, queued.run.id)
        assert run is not None
        run.state = expired_state
        run.lease_owner = "crashed-worker"
        run.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    worker = PipelineWorker(
        session_factory,
        mock_bundle,
        vector_store,
        {},
        {},
        working_dir=str(tmp_working_dir),
        concurrency=1,
        worker_id="recovery-worker",
    )

    assert await worker.poll_once() == 1
    assert calls == [
        (
            seeded_recording.id,
            queued.run.id,
            "recovery-worker",
        )
    ]
