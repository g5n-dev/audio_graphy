"""Persistent, resumable reception automation pipeline contracts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from audio_graphy.errors import ConflictError
from audio_graphy.models import (
    Base,
    DialogueTagAssignment,
    DialogueUnit,
    Reception,
    ReceptionAutomationRun,
    ReceptionRecording,
    Recording,
    Segment,
)
from audio_graphy.schemas.reception_pipeline import ReceptionAutomationRequest
from audio_graphy.services.reception_pipeline import ReceptionAutomationPipeline
from audio_graphy.services.reception_tagging import ReceptionTaggingService
from audio_graphy.services.receptions import ReceptionService


@pytest_asyncio.fixture
async def concurrent_pipeline_session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Give lease owner and competitor independent SQLite connections."""

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'pipeline-concurrency.db'}",
        connect_args={"timeout": 5.0},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_reception(
    session_factory: Any,
    tmp_path: Path,
    *,
    with_segments: bool,
) -> int:
    audio_path = tmp_path / "source.wav"
    audio_path.write_bytes(b"RIFF-pipeline-test")
    recorded_at = datetime(2026, 7, 23, 9, 0, tzinfo=UTC)
    async with session_factory() as session:
        recording = Recording(
            tenant_id="chang_an",
            store_id="S001",
            agent_name="agent_ca",
            customer_hash="customer-a",
            path=str(audio_path),
            status="indexed",
            pipeline_state="done",
            recorded_at=recorded_at,
            indexed_at=recorded_at,
        )
        session.add(recording)
        await session.flush()
        reception = Reception(
            tenant_id="chang_an",
            scenario="automotive",
            store_id="S001",
            agent_name="agent_ca",
            customer_hash="customer-a",
            status="confirmed",
            merge_mode="logical",
            merge_confidence=1.0,
            started_at=recorded_at,
            ended_at=recorded_at.replace(minute=1),
            version=1,
        )
        session.add(reception)
        await session.flush()
        session.add(
            ReceptionRecording(
                tenant_id="chang_an",
                reception_id=reception.id,
                recording_id=recording.id,
                sequence_no=0,
                timeline_start_sec=0,
                timeline_end_sec=60,
                source_start_sec=0,
                source_end_sec=60,
                gap_before_sec=0,
                decision_source="auto",
                merge_confidence=1,
                merge_reasons={"candidate_type": "merge_group"},
            )
        )
        if with_segments:
            session.add_all(
                [
                    Segment(
                        tenant_id="chang_an",
                        recording_id=recording.id,
                        idx=0,
                        start_sec=0,
                        end_sec=25,
                        transcript="您好，想了解哪款车？客户想先看看 SUV。",
                        text_scrubbed="您好，想了解哪款车？客户想先看看 SUV。",
                        speaker="agent_ca",
                        vad_conf=0.99,
                    ),
                    Segment(
                        tenant_id="chang_an",
                        recording_id=recording.id,
                        idx=1,
                        start_sec=25,
                        end_sec=60,
                        transcript="价格有点高，安排试驾后再决定。",
                        text_scrubbed="价格有点高，安排试驾后再决定。",
                        speaker="customer",
                        vad_conf=0.99,
                    ),
                ]
            )
        await session.commit()
        return reception.id


def _pipeline(session_factory: Any, tmp_path: Path) -> ReceptionAutomationPipeline:
    return ReceptionAutomationPipeline(
        session_factory,
        reception_service=ReceptionService(
            session_factory,
            audio_root=tmp_path,
        ),
        tagging_service=ReceptionTaggingService(session_factory),
    )


@pytest.mark.asyncio
async def test_pipeline_reaches_ready_and_persists_each_checkpoint(
    session_factory: Any,
    tmp_path: Path,
) -> None:
    reception_id = await _seed_reception(
        session_factory,
        tmp_path,
        with_segments=True,
    )

    result = await _pipeline(session_factory, tmp_path).run(
        reception_id=reception_id,
        tenant_id="chang_an",
        request=ReceptionAutomationRequest(),
        actor="user:1",
    )

    assert result.status == "ready"
    assert result.stage == "ready"
    assert result.attempt_count == 1
    assert result.checkpoints["segmentation"]["status"] == "completed"
    assert result.checkpoints["tagging"]["status"] == "completed"
    async with session_factory() as session:
        reception = await session.get(Reception, reception_id)
        assert reception is not None
        assert reception.status == "ready"
        assert (
            await session.scalar(
                select(func.count(DialogueUnit.id)).where(DialogueUnit.reception_id == reception_id)
            )
            or 0
        ) > 0
        assert (
            await session.scalar(
                select(func.count(DialogueTagAssignment.id)).where(
                    DialogueTagAssignment.reception_id == reception_id,
                    DialogueTagAssignment.group_key == "reception-rules",
                    DialogueTagAssignment.group_version == "rules-v1",
                )
            )
            or 0
        ) > 0


@pytest.mark.asyncio
async def test_completed_pipeline_is_idempotent_without_incrementing_attempts(
    session_factory: Any,
    tmp_path: Path,
) -> None:
    reception_id = await _seed_reception(
        session_factory,
        tmp_path,
        with_segments=True,
    )
    pipeline = _pipeline(session_factory, tmp_path)
    first = await pipeline.run(
        reception_id=reception_id,
        tenant_id="chang_an",
        request=ReceptionAutomationRequest(),
        actor="user:1",
    )
    second = await pipeline.run(
        reception_id=reception_id,
        tenant_id="chang_an",
        request=ReceptionAutomationRequest(),
        actor="user:1",
    )

    assert second.id == first.id
    assert second.status == "ready"
    assert second.attempt_count == 1


@pytest.mark.asyncio
async def test_failed_pipeline_resumes_after_segments_are_available(
    session_factory: Any,
    tmp_path: Path,
) -> None:
    reception_id = await _seed_reception(
        session_factory,
        tmp_path,
        with_segments=False,
    )
    pipeline = _pipeline(session_factory, tmp_path)

    failed = await pipeline.run(
        reception_id=reception_id,
        tenant_id="chang_an",
        request=ReceptionAutomationRequest(),
        actor="user:1",
        raise_on_failure=False,
    )

    assert failed.status == "failed"
    assert failed.stage == "segmentation"
    assert failed.last_error_code == "NO_SEGMENTS_FOR_RECEPTION"

    async with session_factory() as session:
        mapping = (
            await session.execute(
                select(ReceptionRecording).where(ReceptionRecording.reception_id == reception_id)
            )
        ).scalar_one()
        session.add(
            Segment(
                tenant_id="chang_an",
                recording_id=mapping.recording_id,
                idx=0,
                start_sec=0,
                end_sec=60,
                transcript="客户觉得价格高，想安排试驾。",
                text_scrubbed="客户觉得价格高，想安排试驾。",
                speaker="customer",
                vad_conf=0.99,
            )
        )
        await session.commit()

    completed = await pipeline.run(
        reception_id=reception_id,
        tenant_id="chang_an",
        request=ReceptionAutomationRequest(),
        actor="user:1",
    )

    assert completed.status == "ready"
    assert completed.attempt_count == 2
    assert completed.last_error_code is None
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(ReceptionAutomationRun).where(
                    ReceptionAutomationRun.reception_id == reception_id
                )
            )
        ).scalars()
        assert len(list(rows)) == 1


@pytest.mark.asyncio
async def test_long_merge_stage_heartbeat_prevents_concurrent_reclaim(
    concurrent_pipeline_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = concurrent_pipeline_session_factory
    reception_id = await _seed_reception(
        session_factory,
        tmp_path,
        with_segments=True,
    )
    async with session_factory() as session, session.begin():
        reception = await session.get(Reception, reception_id)
        assert reception is not None
        reception.merge_mode = "physical"

    entered = asyncio.Event()
    release = asyncio.Event()
    reception_service = ReceptionService(session_factory, audio_root=tmp_path)

    async def slow_merge(*_args: object, **_kwargs: object):
        entered.set()
        await release.wait()
        return await reception_service.get_workspace(reception_id, "chang_an")

    monkeypatch.setattr(reception_service, "merge_recordings", slow_merge)
    lease_ttl = timedelta(milliseconds=120)
    pipeline = ReceptionAutomationPipeline(
        session_factory,
        reception_service=reception_service,
        tagging_service=ReceptionTaggingService(session_factory),
        lease_ttl=lease_ttl,
        lease_heartbeat_seconds=0.02,
    )
    running = asyncio.create_task(
        pipeline.run(
            reception_id=reception_id,
            tenant_id="chang_an",
            request=ReceptionAutomationRequest(),
            actor="user:1",
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    await asyncio.sleep(0.18)

    competitor = ReceptionAutomationPipeline(
        session_factory,
        reception_service=ReceptionService(session_factory, audio_root=tmp_path),
        tagging_service=ReceptionTaggingService(session_factory),
        lease_ttl=lease_ttl,
        lease_heartbeat_seconds=0.02,
    )
    with pytest.raises(ConflictError) as conflict:
        await competitor.run(
            reception_id=reception_id,
            tenant_id="chang_an",
            request=ReceptionAutomationRequest(),
            actor="user:2",
        )
    assert conflict.value.code == "RECEPTION_AUTOMATION_ALREADY_RUNNING"

    release.set()
    result = await asyncio.wait_for(running, timeout=2.0)
    assert result.status == "ready"
    assert result.attempt_count == 1


@pytest.mark.asyncio
async def test_heartbeat_owner_token_loss_cancels_stage_without_stale_mutation(
    concurrent_pipeline_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = concurrent_pipeline_session_factory
    reception_id = await _seed_reception(
        session_factory,
        tmp_path,
        with_segments=True,
    )
    async with session_factory() as session, session.begin():
        reception = await session.get(Reception, reception_id)
        assert reception is not None
        reception.merge_mode = "physical"

    entered = asyncio.Event()
    cancelled = asyncio.Event()
    reception_service = ReceptionService(session_factory, audio_root=tmp_path)

    async def blocked_merge(*_args: object, **_kwargs: object):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(reception_service, "merge_recordings", blocked_merge)
    pipeline = ReceptionAutomationPipeline(
        session_factory,
        reception_service=reception_service,
        tagging_service=ReceptionTaggingService(session_factory),
        lease_ttl=timedelta(seconds=1),
        lease_heartbeat_seconds=0.02,
    )
    running = asyncio.create_task(
        pipeline.run(
            reception_id=reception_id,
            tenant_id="chang_an",
            request=ReceptionAutomationRequest(),
            actor="user:1",
            raise_on_failure=False,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    async with session_factory() as session, session.begin():
        await session.execute(
            update(ReceptionAutomationRun)
            .where(ReceptionAutomationRun.reception_id == reception_id)
            .values(lease_token="replacement-owner")
        )

    with pytest.raises(ConflictError) as conflict:
        await asyncio.wait_for(running, timeout=1.0)
    assert conflict.value.code == "RECEPTION_AUTOMATION_LEASE_LOST"
    await asyncio.wait_for(cancelled.wait(), timeout=1.0)

    async with session_factory() as session:
        run = (
            await session.execute(
                select(ReceptionAutomationRun).where(
                    ReceptionAutomationRun.reception_id == reception_id
                )
            )
        ).scalar_one()
        assert run.status == "running"
        assert run.stage == "merge"
        assert run.lease_token == "replacement-owner"
