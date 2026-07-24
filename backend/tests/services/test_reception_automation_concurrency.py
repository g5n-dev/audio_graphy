"""Concurrent proposal-acceptance invariants for ordinary merge groups."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from audio_graphy.core.reception_merge import (
    ManualReceptionConstraints,
    ReceptionMerger,
    ReceptionProposal,
    RecordingCandidate,
)
from audio_graphy.errors import ConflictError
from audio_graphy.models import (
    Base,
    ProvenanceEvent,
    Reception,
    ReceptionRecording,
    Recording,
    Segment,
)
from audio_graphy.schemas.receptions import (
    ReceptionDiscoveryRequest,
    ReceptionProposalAcceptRequest,
)
from audio_graphy.services.reception_automation import ReceptionAutomationService
from audio_graphy.services.receptions import ReceptionService


@pytest_asyncio.fixture
async def concurrent_session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Use independent SQLite connections so BEGIN IMMEDIATE is exercised."""

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'proposal-concurrency.db'}",
        connect_args={"timeout": 5.0},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_recording(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    recorded_at = datetime(2026, 7, 23, 9, 0, tzinfo=UTC)
    async with session_factory() as session, session.begin():
        recording = Recording(
            tenant_id="tenant-a",
            store_id="store-a",
            agent_name=None,
            customer_hash="customer-a",
            path="/tmp/source.wav",
            status="indexed",
            pipeline_state="done",
            recorded_at=recorded_at,
            indexed_at=recorded_at,
        )
        session.add(recording)
        await session.flush()
        session.add(
            Segment(
                tenant_id="tenant-a",
                recording_id=recording.id,
                idx=0,
                start_sec=0.0,
                end_sec=30.0,
                transcript="一次完整接待",
                text_scrubbed="一次完整接待",
                speaker="customer",
                vad_conf=0.99,
            )
        )
        recording_id = recording.id
    return recording_id


async def _seed_dense_short_recordings(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    count: int,
) -> None:
    recorded_at = datetime(2026, 7, 23, 9, 0, tzinfo=UTC)
    async with session_factory() as session, session.begin():
        recordings = [
            Recording(
                tenant_id="tenant-a",
                store_id="store-a",
                agent_name=None,
                customer_hash=None,
                path=f"/tmp/source-{index}.wav",
                status="indexed",
                pipeline_state="done",
                recorded_at=recorded_at + timedelta(milliseconds=index),
                indexed_at=recorded_at,
            )
            for index in range(count)
        ]
        session.add_all(recordings)
        await session.flush()
        session.add_all(
            [
                Segment(
                    tenant_id="tenant-a",
                    recording_id=recording.id,
                    idx=0,
                    start_sec=0.0,
                    end_sec=30.0,
                    transcript="一次短录音",
                    text_scrubbed="一次短录音",
                    speaker="customer",
                    vad_conf=0.99,
                )
                for recording in recordings
            ]
        )


async def _seed_long_recording_over_segment_budget(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    recorded_at = datetime(2026, 7, 23, 9, 0, tzinfo=UTC)
    async with session_factory() as session, session.begin():
        recording = Recording(
            tenant_id="tenant-a",
            store_id="store-a",
            agent_name=None,
            customer_hash=None,
            path="/tmp/long-source.wav",
            status="indexed",
            pipeline_state="done",
            recorded_at=recorded_at,
            indexed_at=recorded_at,
        )
        session.add(recording)
        await session.flush()
        session.add_all(
            [
                Segment(
                    tenant_id="tenant-a",
                    recording_id=recording.id,
                    idx=index,
                    start_sec=float(index * 2),
                    end_sec=float(index * 2 + 1),
                    transcript="您好，欢迎光临",
                    text_scrubbed="您好，欢迎光临",
                    speaker="customer",
                    vad_conf=0.99,
                )
                for index in range(513)
            ]
        )


class _CountingMerger(ReceptionMerger):
    evaluation_count = 0

    def evaluate_pair(
        self,
        left: RecordingCandidate,
        right: RecordingCandidate,
        *,
        constraints: ManualReceptionConstraints | None = None,
    ) -> ReceptionProposal:
        self.evaluation_count += 1
        return super().evaluate_pair(left, right, constraints=constraints)


def _service(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> ReceptionAutomationService:
    return ReceptionAutomationService(
        session_factory,
        reception_service=ReceptionService(session_factory, audio_root=tmp_path),
        proposal_secret="test-proposal-secret",
    )


async def _persistence_counts(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[int, int, int]:
    async with session_factory() as session:
        reception_count = int(await session.scalar(select(func.count(Reception.id))) or 0)
        mapping_count = int(await session.scalar(select(func.count(ReceptionRecording.id))) or 0)
        provenance_count = int(await session.scalar(select(func.count(ProvenanceEvent.id))) or 0)
    return reception_count, mapping_count, provenance_count


@pytest.mark.asyncio
async def test_concurrent_same_idempotency_key_returns_one_reception(
    concurrent_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    recording_id = await _seed_recording(concurrent_session_factory)
    body = ReceptionProposalAcceptRequest(
        scenario="automotive",
        recording_ids=[recording_id],
        external_session_id="proposal-click-1",
    )
    first_service = _service(concurrent_session_factory, tmp_path)
    second_service = _service(concurrent_session_factory, tmp_path)

    first, second = await asyncio.gather(
        first_service.accept("tenant-a", body, actor="user:1"),
        second_service.accept("tenant-a", body, actor="user:1"),
    )

    assert first.reception.id == second.reception.id
    assert await _persistence_counts(concurrent_session_factory) == (1, 1, 1)


@pytest.mark.asyncio
async def test_concurrent_different_keys_creates_once_and_conflicts_once(
    concurrent_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    recording_id = await _seed_recording(concurrent_session_factory)
    services = (
        _service(concurrent_session_factory, tmp_path),
        _service(concurrent_session_factory, tmp_path),
    )
    bodies = (
        ReceptionProposalAcceptRequest(
            scenario="automotive",
            recording_ids=[recording_id],
            external_session_id="proposal-click-a",
        ),
        ReceptionProposalAcceptRequest(
            scenario="automotive",
            recording_ids=[recording_id],
            external_session_id="proposal-click-b",
        ),
    )

    results = await asyncio.gather(
        *(
            service.accept("tenant-a", body, actor="user:1")
            for service, body in zip(services, bodies, strict=True)
        ),
        return_exceptions=True,
    )

    successes = [result for result in results if not isinstance(result, BaseException)]
    conflicts = [result for result in results if isinstance(result, ConflictError)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert conflicts[0].code == "RECORDING_ALREADY_ASSIGNED"
    assert await _persistence_counts(concurrent_session_factory) == (1, 1, 1)


@pytest.mark.asyncio
async def test_creation_failure_rolls_back_reception_mapping_and_provenance(
    concurrent_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    recording_id = await _seed_recording(concurrent_session_factory)
    async with concurrent_session_factory() as session, session.begin():
        await session.execute(
            text(
                """
                CREATE TRIGGER fail_proposal_provenance
                BEFORE INSERT ON provenance_events
                WHEN NEW.actor = 'rollback-test'
                BEGIN
                    SELECT RAISE(ABORT, 'forced provenance failure');
                END
                """
            )
        )

    with pytest.raises(IntegrityError, match="forced provenance failure"):
        await _service(concurrent_session_factory, tmp_path).accept(
            "tenant-a",
            ReceptionProposalAcceptRequest(
                scenario="automotive",
                recording_ids=[recording_id],
            ),
            actor="rollback-test",
        )

    assert await _persistence_counts(concurrent_session_factory) == (0, 0, 0)


@pytest.mark.asyncio
async def test_dense_500_recording_discovery_response_is_bounded(
    concurrent_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    await _seed_dense_short_recordings(concurrent_session_factory, count=500)
    merger = _CountingMerger()

    result = await _service(concurrent_session_factory, tmp_path).discover(
        "tenant-a",
        ReceptionDiscoveryRequest(
            scenario="automotive",
            store_id="store-a",
            recorded_from=datetime(2026, 7, 23, 8, 0, tzinfo=UTC),
            recorded_to=datetime(2026, 7, 23, 10, 0, tzinfo=UTC),
            short_recording_max_sec=60,
            limit=500,
        ),
        merger=merger,
    )

    assert result.scanned_recordings == 500
    assert len(result.items) == 500
    assert result.total > len(result.items)
    assert result.truncated is True
    assert merger.evaluation_count <= 500 * 16


@pytest.mark.asyncio
async def test_discovery_skips_unbounded_segment_scan_and_marks_truncated(
    concurrent_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    await _seed_long_recording_over_segment_budget(concurrent_session_factory)

    result = await _service(concurrent_session_factory, tmp_path).discover(
        "tenant-a",
        ReceptionDiscoveryRequest(
            scenario="automotive",
            store_id="store-a",
            recorded_from=datetime(2026, 7, 23, 8, 0, tzinfo=UTC),
            recorded_to=datetime(2026, 7, 23, 10, 0, tzinfo=UTC),
            short_recording_max_sec=60,
            limit=500,
        ),
    )

    assert result.items == ()
    assert result.total == 0
    assert result.truncated is True
