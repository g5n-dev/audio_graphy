from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from audio_graphy.core.audio_assembler import (
    AudioAssemblyManifest,
    AudioAssemblySource,
    AudioInputManifest,
)
from audio_graphy.errors import ValidationError
from audio_graphy.models import Base
from audio_graphy.models.reception import Reception, ReceptionRecording
from audio_graphy.models.reception_audio import (
    ReceptionAudioArtifact,
    ReceptionAudioOperation,
    ReceptionTimelineRevision,
)
from audio_graphy.models.recording import Recording
from audio_graphy.services.reception_audio_operations import (
    ReceptionAudioOperationService,
)
from audio_graphy.services.receptions import (
    ReceptionService,
    reception_physical_generation_relative_path,
)


@pytest.fixture
async def artifact_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'audio-artifacts.sqlite3'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


class _PhysicalMergeHarness:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        audio_root: Path,
        *,
        fail_before_build: bool = False,
        fail_after_ready: bool = False,
        fail_during_commit: bool = False,
        wait_for_release: bool = False,
    ) -> None:
        self._factory = factory
        self.audio_root = audio_root
        self.fail_before_build = fail_before_build
        self.fail_after_ready = fail_after_ready
        self.fail_during_commit = fail_during_commit
        self.wait_for_release = wait_for_release
        self.preparing_observed = False
        self.stage_states: list[str] = []
        self.generation_path: Path | None = None
        self.entered_build = asyncio.Event()
        self.release_build = asyncio.Event()

    async def merge_recordings(
        self,
        reception_id: int,
        tenant_id: str,
        body: Any,
        *,
        actor: str,
        timeline_overrides: Any = None,
        physical_generation: str | None = None,
        on_physical_stage: Any = None,
        after_physical_prepare: Any = None,
        before_commit: Any = None,
    ) -> None:
        del actor, timeline_overrides
        assert physical_generation is not None
        assert on_physical_stage is not None
        assert after_physical_prepare is not None
        assert before_commit is not None
        relative = reception_physical_generation_relative_path(
            tenant_id=tenant_id,
            reception_id=reception_id,
            reception_version=body.expected_version + 1,
            generation=physical_generation,
        )
        self.generation_path = self.audio_root / relative
        async with self._factory() as db:
            artifact = (
                await db.execute(
                    select(ReceptionAudioArtifact).where(
                        ReceptionAudioArtifact.reception_id == reception_id,
                        ReceptionAudioArtifact.path == str(self.generation_path),
                    )
                )
            ).scalar_one()
            self.preparing_observed = artifact.state == "PREPARING"
            operation = await db.get(
                ReceptionAudioOperation,
                artifact.operation_id,
            )
            assert operation is not None
            self.stage_states.append(operation.status)
        self.entered_build.set()
        if self.wait_for_release:
            await self.release_build.wait()
        if self.fail_before_build:
            raise RuntimeError("injected assembly failure")

        self.generation_path.parent.mkdir(parents=True, exist_ok=True)
        self.generation_path.write_bytes(b"verified-physical-audio")
        await on_physical_stage("encrypting")
        self.stage_states.append("encrypting")
        await on_physical_stage("verifying")
        self.stage_states.append("verifying")
        prepared = SimpleNamespace(
            merged_audio_path=str(self.generation_path),
            manifest=SimpleNamespace(
                output_sample_rate=16_000,
                output_channels=1,
            ),
        )
        await after_physical_prepare(prepared)
        if self.fail_after_ready:
            raise RuntimeError("injected post-verify failure")

        async with self._factory() as db, db.begin():
            reception = await db.get(Reception, reception_id)
            assert reception is not None
            mapping = (
                await db.execute(
                    select(ReceptionRecording).where(
                        ReceptionRecording.reception_id == reception_id
                    )
                )
            ).scalar_one()
            previous_path = reception.merged_audio_path
            reception.version = body.expected_version + 1
            reception.merge_mode = body.mode
            reception.merged_audio_path = str(self.generation_path)
            await before_commit(
                db,
                reception,
                (mapping,),
                prepared,
                previous_path,
            )
            if self.fail_during_commit:
                raise RuntimeError("injected commit failure")


class _WritingAssembler:
    def __init__(self, audio_root: Path) -> None:
        self._audio_root = audio_root

    async def assemble(
        self,
        sources: Sequence[str | Path | AudioAssemblySource],
        target_relative_path: str | Path,
    ) -> AudioAssemblyManifest:
        assert len(sources) == 1
        request = (
            sources[0]
            if isinstance(sources[0], AudioAssemblySource)
            else AudioAssemblySource(path=sources[0])
        )
        source_end = request.source_end_sec or 1.0
        output = self._audio_root / target_relative_path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"verified-real-service-artifact")
        item = AudioInputManifest(
            path=str(request.path),
            sha256="4" * 64,
            size_bytes=32_044,
            duration_sec=source_end - request.source_start_sec,
            timeline_start_sec=request.gap_before_sec,
            timeline_end_sec=request.gap_before_sec + source_end - request.source_start_sec,
            codec="pcm_s16le",
            sample_rate=16_000,
            channels=1,
            source_start_sec=request.source_start_sec,
            source_end_sec=source_end,
            gap_before_sec=request.gap_before_sec,
        )
        return AudioAssemblyManifest(
            output_path=Path(target_relative_path).as_posix(),
            output_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
            output_bytes=output.stat().st_size,
            total_duration_sec=item.timeline_end_sec,
            command_mode="transcode_pcm",
            inputs=(item,),
            output_sample_rate=16_000,
            output_channels=1,
        )


async def _seed_operation(
    factory: async_sessionmaker[AsyncSession],
    audio_root: Path,
    *,
    tenant_id: str = "tenant-a",
) -> tuple[int, int, int, Path]:
    old_path = (
        audio_root
        / "assembled_audio"
        / tenant_id
        / "receptions"
        / "reception-1"
        / "v1-oldgeneration.wav"
    )
    old_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.write_bytes(b"old-attached-audio")
    now = datetime.now(UTC)
    async with factory() as db, db.begin():
        recording = Recording(
            tenant_id=tenant_id,
            store_id="store-a",
            path=str(audio_root / "source.wav"),
            audio_duration_ms=1_000,
            audio_sha256="a" * 64,
            audio_size_bytes=32044,
            audio_sample_rate=16_000,
            audio_channels=1,
            source_revision=1,
        )
        reception = Reception(
            tenant_id=tenant_id,
            scenario="custom",
            store_id="store-a",
            status="ready",
            merge_mode="physical",
            started_at=now,
            ended_at=now + timedelta(seconds=1),
            merged_audio_path=str(old_path),
            version=1,
        )
        db.add_all([recording, reception])
        await db.flush()
        old_revision = ReceptionTimelineRevision(
            tenant_id=tenant_id,
            reception_id=reception.id,
            revision=1,
            expected_reception_version=1,
            state="ACTIVE",
            plan_signature="b" * 64,
            plan_token_hash="c" * 64,
            source_manifest=[],
            total_duration_ms=1_000,
            physical_eligible=True,
            warnings=[],
            expires_at=now + timedelta(hours=1),
            activated_at=now,
        )
        db.add(old_revision)
        await db.flush()
        reception.active_timeline_revision_id = old_revision.id
        old_operation = ReceptionAudioOperation(
            tenant_id=tenant_id,
            reception_id=reception.id,
            timeline_revision_id=old_revision.id,
            idempotency_key="old-operation",
            mode="physical",
            expected_reception_version=1,
            status="succeeded",
            progress=1.0,
            attempt_count=1,
            finished_at=now,
        )
        db.add(old_operation)
        await db.flush()
        db.add(
            ReceptionAudioArtifact(
                tenant_id=tenant_id,
                reception_id=reception.id,
                timeline_revision_id=old_revision.id,
                operation_id=old_operation.id,
                state="ATTACHED",
                path=str(old_path),
                sha256="d" * 64,
                size_bytes=old_path.stat().st_size,
                duration_ms=1_000,
                sample_rate=16_000,
                channels=1,
                attached_at=now,
            )
        )
        mapping = ReceptionRecording(
            tenant_id=tenant_id,
            reception_id=reception.id,
            recording_id=recording.id,
            sequence_no=0,
            timeline_start_sec=0.0,
            timeline_end_sec=1.0,
            source_start_sec=0.0,
            source_end_sec=1.0,
            source_start_ms=0,
            source_end_ms=1_000,
            timeline_start_ms=0,
            timeline_end_ms=1_000,
            gap_before_ms=0,
            gap_before_sec=0.0,
            decision_source="manual",
            merge_confidence=1.0,
            merge_reasons={},
        )
        db.add(mapping)
        await db.flush()
        revision = ReceptionTimelineRevision(
            tenant_id=tenant_id,
            reception_id=reception.id,
            revision=2,
            expected_reception_version=1,
            state="STAGING",
            plan_signature="e" * 64,
            plan_token_hash="f" * 64,
            source_manifest=[
                {
                    "mapping_id": mapping.id,
                    "recording_id": recording.id,
                    "sequence_no": 0,
                    "source_start_ms": 0,
                    "source_end_ms": 1_000,
                    "gap_before_ms": 0,
                    "timeline_start_ms": 0,
                    "timeline_end_ms": 1_000,
                    "recording_source_revision": 1,
                    "recording_sha256": recording.audio_sha256,
                    "recording_size_bytes": recording.audio_size_bytes,
                }
            ],
            total_duration_ms=1_000,
            physical_eligible=True,
            warnings=[],
            expires_at=now + timedelta(hours=1),
        )
        db.add(revision)
        await db.flush()
        operation = ReceptionAudioOperation(
            tenant_id=tenant_id,
            reception_id=reception.id,
            timeline_revision_id=revision.id,
            idempotency_key="new-operation",
            mode="physical",
            expected_reception_version=1,
            status="queued",
            progress=0.0,
        )
        db.add(operation)
        await db.flush()
        return operation.id, reception.id, revision.id, old_path


@pytest.mark.asyncio
async def test_committed_queued_operation_is_discoverable_by_recovery_dispatcher(
    artifact_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    audio_root = tmp_path / "audio"
    audio_root.mkdir()
    operation_id, _reception_id, _revision_id, _old_path = await _seed_operation(
        artifact_factory,
        audio_root,
    )
    service = ReceptionAudioOperationService(
        artifact_factory,
        SimpleNamespace(),  # type: ignore[arg-type]
    )

    assert await service.pending_operation_ids(limit=10) == [operation_id]


@pytest.mark.asyncio
async def test_physical_generation_is_registered_before_build_and_attached_atomically(
    artifact_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    audio_root = tmp_path / "audio"
    audio_root.mkdir()
    operation_id, reception_id, revision_id, old_path = await _seed_operation(
        artifact_factory,
        audio_root,
    )
    harness = _PhysicalMergeHarness(artifact_factory, audio_root)
    service = ReceptionAudioOperationService(artifact_factory, harness)  # type: ignore[arg-type]

    await service.run_operation(operation_id)

    assert harness.preparing_observed is True
    assert harness.stage_states == [
        "assembling",
        "encrypting",
        "verifying",
    ]
    assert harness.generation_path is not None
    async with artifact_factory() as db:
        operation = await db.get(ReceptionAudioOperation, operation_id)
        reception = await db.get(Reception, reception_id)
        artifacts = list(
            (
                await db.execute(select(ReceptionAudioArtifact).order_by(ReceptionAudioArtifact.id))
            ).scalars()
        )
    assert operation is not None and operation.status == "succeeded"
    assert reception is not None
    assert reception.active_timeline_revision_id == revision_id
    assert reception.merged_audio_path == str(harness.generation_path)
    assert [(artifact.state, artifact.path) for artifact in artifacts] == [
        ("RETIRED", str(old_path)),
        ("ATTACHED", str(harness.generation_path)),
    ]


@pytest.mark.asyncio
async def test_real_reception_service_honours_durable_artifact_callbacks(
    artifact_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    audio_root = tmp_path / "audio-real-service"
    audio_root.mkdir()
    operation_id, reception_id, revision_id, old_path = await _seed_operation(
        artifact_factory,
        audio_root,
    )
    reception_service = ReceptionService(
        artifact_factory,
        audio_root=audio_root,
        audio_assembler=_WritingAssembler(audio_root),  # type: ignore[arg-type]
    )
    service = ReceptionAudioOperationService(artifact_factory, reception_service)

    await service.run_operation(operation_id)

    async with artifact_factory() as db:
        operation = await db.get(ReceptionAudioOperation, operation_id)
        reception = await db.get(Reception, reception_id)
        artifacts = list(
            (
                await db.execute(select(ReceptionAudioArtifact).order_by(ReceptionAudioArtifact.id))
            ).scalars()
        )
    assert operation is not None and operation.status == "succeeded"
    assert reception is not None
    assert reception.active_timeline_revision_id == revision_id
    assert reception.merged_audio_path == artifacts[1].path
    assert artifacts[0].state == "RETIRED"
    assert artifacts[1].state == "ATTACHED"
    assert artifacts[1].sample_rate == 16_000
    assert artifacts[1].channels == 1
    assert not old_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_mode", "expected_state"),
    [
        ("before_build", "FAILED"),
        ("after_ready", "ORPHANED"),
        ("during_commit", "ORPHANED"),
    ],
)
async def test_failure_windows_never_replace_old_attached_artifact(
    artifact_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    failure_mode: str,
    expected_state: str,
) -> None:
    audio_root = tmp_path / f"audio-{failure_mode}"
    audio_root.mkdir()
    operation_id, reception_id, new_revision_id, old_path = await _seed_operation(
        artifact_factory,
        audio_root,
    )
    harness = _PhysicalMergeHarness(
        artifact_factory,
        audio_root,
        fail_before_build=failure_mode == "before_build",
        fail_after_ready=failure_mode == "after_ready",
        fail_during_commit=failure_mode == "during_commit",
    )
    service = ReceptionAudioOperationService(artifact_factory, harness)  # type: ignore[arg-type]

    await service.run_operation(operation_id)

    async with artifact_factory() as db:
        operation = await db.get(ReceptionAudioOperation, operation_id)
        reception = await db.get(Reception, reception_id)
        artifacts = list(
            (
                await db.execute(select(ReceptionAudioArtifact).order_by(ReceptionAudioArtifact.id))
            ).scalars()
        )
    assert operation is not None and operation.status == "failed"
    assert reception is not None
    assert reception.active_timeline_revision_id != new_revision_id
    assert reception.merged_audio_path == str(old_path)
    assert old_path.read_bytes() == b"old-attached-audio"
    assert artifacts[0].state == "ATTACHED"
    assert artifacts[1].state == expected_state
    assert harness.generation_path is not None
    assert not harness.generation_path.exists()


@pytest.mark.asyncio
async def test_artifact_reconciler_is_confined_idempotent_and_repairs_ready_pointer(
    artifact_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    audio_root = tmp_path / "audio-reconcile"
    audio_root.mkdir()
    operation_id, reception_id, revision_id, _old_path = await _seed_operation(
        artifact_factory,
        audio_root,
    )
    now = datetime.now(UTC)
    valid_directory = (
        audio_root / "assembled_audio" / "tenant-a" / "receptions" / f"reception-{reception_id}"
    )
    valid_directory.mkdir(parents=True, exist_ok=True)
    retired_path = valid_directory / "v2-retiredartifact.wav"
    retired_path.write_bytes(b"retired")
    repair_path = valid_directory / "v2-repairartifact.wav"
    repair_path.write_bytes(b"repair")
    preparing_path = valid_directory / "v2-stalepreparing.wav"
    preparing_path.write_bytes(b"partial")
    outside_path = tmp_path / "must-not-delete.wav"
    outside_path.write_bytes(b"private")
    async with artifact_factory() as db, db.begin():
        operation = await db.get(ReceptionAudioOperation, operation_id)
        reception = await db.get(Reception, reception_id)
        assert operation is not None and reception is not None
        operation.status = "succeeded"
        operation.progress = 1.0
        operation.finished_at = now
        reception.active_timeline_revision_id = revision_id
        reception.merged_audio_path = str(repair_path)
        db.add_all(
            [
                ReceptionAudioArtifact(
                    tenant_id="tenant-a",
                    reception_id=reception_id,
                    timeline_revision_id=revision_id,
                    operation_id=operation_id,
                    state="READY",
                    path=str(repair_path),
                    sha256=hashlib.sha256(repair_path.read_bytes()).hexdigest(),
                    size_bytes=repair_path.stat().st_size,
                    duration_ms=1_000,
                    sample_rate=16_000,
                    channels=1,
                    updated_at=now - timedelta(hours=2),
                ),
                ReceptionAudioArtifact(
                    tenant_id="tenant-a",
                    reception_id=reception_id,
                    timeline_revision_id=revision_id,
                    operation_id=operation_id,
                    state="RETIRED",
                    path=str(retired_path),
                    sha256="2" * 64,
                    size_bytes=retired_path.stat().st_size,
                    duration_ms=1_000,
                    sample_rate=16_000,
                    channels=1,
                    retired_at=now - timedelta(hours=2),
                    updated_at=now - timedelta(hours=2),
                ),
                ReceptionAudioArtifact(
                    tenant_id="tenant-a",
                    reception_id=reception_id,
                    timeline_revision_id=revision_id,
                    operation_id=operation_id,
                    state="PREPARING",
                    path=str(preparing_path),
                    updated_at=now - timedelta(hours=2),
                ),
                ReceptionAudioArtifact(
                    tenant_id="tenant-a",
                    reception_id=reception_id,
                    timeline_revision_id=revision_id,
                    operation_id=operation_id,
                    state="RETIRED",
                    path=str(outside_path),
                    sha256="3" * 64,
                    size_bytes=outside_path.stat().st_size,
                    duration_ms=1_000,
                    sample_rate=16_000,
                    channels=1,
                    retired_at=now - timedelta(hours=2),
                    updated_at=now - timedelta(hours=2),
                ),
            ]
        )

    harness = _PhysicalMergeHarness(artifact_factory, audio_root)
    service = ReceptionAudioOperationService(artifact_factory, harness)  # type: ignore[arg-type]
    first_count = await service.reconcile_artifacts(stale_before=now - timedelta(hours=1))
    second_count = await service.reconcile_artifacts(stale_before=now - timedelta(hours=1))

    async with artifact_factory() as db:
        artifacts = list((await db.execute(select(ReceptionAudioArtifact))).scalars())
        revisions = list(
            (
                await db.execute(
                    select(ReceptionTimelineRevision).order_by(ReceptionTimelineRevision.revision)
                )
            ).scalars()
        )
    states_by_path = {artifact.path: artifact.state for artifact in artifacts}
    assert first_count == 3
    assert second_count == 0
    assert states_by_path[str(repair_path)] == "ATTACHED"
    assert states_by_path[str(_old_path)] == "RETIRED"
    assert states_by_path[str(retired_path)] == "DELETED"
    assert states_by_path[str(preparing_path)] == "FAILED"
    assert states_by_path[str(outside_path)] == "RETIRED"
    assert repair_path.is_file()
    assert not retired_path.exists()
    assert not preparing_path.exists()
    assert outside_path.read_bytes() == b"private"
    assert [revision.state for revision in revisions] == [
        "SUPERSEDED",
        "ACTIVE",
    ]


@pytest.mark.asyncio
async def test_artifact_reconciler_never_attaches_or_deletes_corrupt_active_pointer(
    artifact_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    audio_root = tmp_path / "audio-corrupt-pointer"
    audio_root.mkdir()
    operation_id, reception_id, revision_id, _old_path = await _seed_operation(
        artifact_factory,
        audio_root,
    )
    now = datetime.now(UTC)
    artifact_path = (
        audio_root
        / "assembled_audio"
        / "tenant-a"
        / "receptions"
        / f"reception-{reception_id}"
        / "v2-corruptpointer.wav"
    )
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(b"corrupt")
    async with artifact_factory() as db, db.begin():
        operation = await db.get(ReceptionAudioOperation, operation_id)
        reception = await db.get(Reception, reception_id)
        assert operation is not None and reception is not None
        operation.status = "succeeded"
        operation.progress = 1.0
        operation.finished_at = now
        reception.active_timeline_revision_id = revision_id
        reception.merged_audio_path = str(artifact_path)
        db.add(
            ReceptionAudioArtifact(
                tenant_id="tenant-a",
                reception_id=reception_id,
                timeline_revision_id=revision_id,
                operation_id=operation_id,
                state="READY",
                path=str(artifact_path),
                sha256="0" * 64,
                size_bytes=artifact_path.stat().st_size,
                duration_ms=1_000,
                sample_rate=16_000,
                channels=1,
                updated_at=now - timedelta(hours=2),
            )
        )

    harness = _PhysicalMergeHarness(artifact_factory, audio_root)
    service = ReceptionAudioOperationService(artifact_factory, harness)  # type: ignore[arg-type]
    assert await service.reconcile_artifacts(stale_before=now - timedelta(hours=1)) == 0

    async with artifact_factory() as db:
        artifact = (
            await db.execute(
                select(ReceptionAudioArtifact).where(
                    ReceptionAudioArtifact.path == str(artifact_path)
                )
            )
        ).scalar_one()
    assert artifact.state == "READY"
    assert artifact_path.read_bytes() == b"corrupt"


@pytest.mark.asyncio
async def test_long_build_heartbeat_prevents_reclaim(
    artifact_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    audio_root = tmp_path / "audio-heartbeat"
    audio_root.mkdir()
    operation_id, _reception_id, _revision_id, _old_path = await _seed_operation(
        artifact_factory,
        audio_root,
    )
    harness = _PhysicalMergeHarness(
        artifact_factory,
        audio_root,
        fail_before_build=True,
        wait_for_release=True,
    )
    service = ReceptionAudioOperationService(
        artifact_factory,
        harness,  # type: ignore[arg-type]
        lease_sec=0.15,
    )

    running = asyncio.create_task(service.run_operation(operation_id))
    await asyncio.wait_for(harness.entered_build.wait(), timeout=1)
    await asyncio.sleep(0.25)
    assert await service.reconcile_stale() == 0
    harness.release_build.set()
    await running


@pytest.mark.asyncio
async def test_worker_cancellation_cancels_external_build_and_orphans_nothing(
    artifact_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    audio_root = tmp_path / "audio-worker-cancel"
    audio_root.mkdir()
    operation_id, _reception_id, _revision_id, _old_path = await _seed_operation(
        artifact_factory,
        audio_root,
    )
    harness = _PhysicalMergeHarness(
        artifact_factory,
        audio_root,
        wait_for_release=True,
    )
    service = ReceptionAudioOperationService(artifact_factory, harness)  # type: ignore[arg-type]
    running = asyncio.create_task(service.run_operation(operation_id))
    await asyncio.wait_for(harness.entered_build.wait(), timeout=1)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    async with artifact_factory() as db:
        operation = await db.get(ReceptionAudioOperation, operation_id)
        artifact = (
            await db.execute(
                select(ReceptionAudioArtifact).where(
                    ReceptionAudioArtifact.operation_id == operation_id
                )
            )
        ).scalar_one()
    assert operation is not None and operation.status == "failed"
    assert artifact.state == "FAILED"
    assert harness.generation_path is not None
    assert not harness.generation_path.exists()


@pytest.mark.asyncio
async def test_expired_plan_is_durably_cancelled_before_error_is_returned(
    artifact_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    audio_root = tmp_path / "audio-expired-plan"
    audio_root.mkdir()
    _operation_id, reception_id, _revision_id, _old_path = await _seed_operation(
        artifact_factory,
        audio_root,
    )
    token = "expired-plan-token"
    now = datetime.now(UTC)
    async with artifact_factory() as db, db.begin():
        revision = ReceptionTimelineRevision(
            tenant_id="tenant-a",
            reception_id=reception_id,
            revision=3,
            expected_reception_version=1,
            state="STAGING",
            plan_signature="9" * 64,
            plan_token_hash=hashlib.sha256(token.encode()).hexdigest(),
            source_manifest=[],
            total_duration_ms=1_000,
            physical_eligible=False,
            warnings=[],
            expires_at=now - timedelta(seconds=1),
        )
        db.add(revision)
        await db.flush()
        expired_revision_id = revision.id
    harness = _PhysicalMergeHarness(artifact_factory, audio_root)
    service = ReceptionAudioOperationService(artifact_factory, harness)  # type: ignore[arg-type]

    with pytest.raises(ValidationError) as exc_info:
        await service.create_operation(
            tenant_id="tenant-a",
            reception_id=reception_id,
            plan_token=token,
            mode="logical",
            expected_version=1,
            idempotency_key="expired-operation",
        )

    assert exc_info.value.code == "AUDIO_PLAN_EXPIRED"
    async with artifact_factory() as db:
        revision = await db.get(ReceptionTimelineRevision, expired_revision_id)
    assert revision is not None
    assert revision.state == "CANCELLED"


async def _force_operation_terminal(
    factory: async_sessionmaker[AsyncSession],
    *,
    operation_id: int,
    revision_id: int,
    status: str,
    idempotency_key: str,
    plan_token: str,
) -> None:
    """Move a seeded queued operation into a terminal state behind a known key.

    The seeded revision carries an opaque token hash, so the hash is rewritten
    to a token the test controls before replaying ``create_operation``.
    """

    async with factory() as db, db.begin():
        operation = await db.get(ReceptionAudioOperation, operation_id)
        revision = await db.get(ReceptionTimelineRevision, revision_id)
        assert operation is not None and revision is not None
        operation.idempotency_key = idempotency_key
        operation.status = status
        operation.progress = 1.0
        operation.finished_at = datetime.now(UTC)
        revision.plan_token_hash = hashlib.sha256(plan_token.encode()).hexdigest()


@pytest.mark.asyncio
async def test_retry_after_failed_operation_creates_a_new_operation_for_same_key(
    artifact_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A terminal FAILED row must never be replayed as a fake success.

    The workspace derives one deterministic Idempotency-Key per
    (reception, version, plan token), so a user retry after a failure reuses
    the key of the dead operation. The retry must enqueue fresh work.
    """

    audio_root = tmp_path / "audio-failed-retry"
    audio_root.mkdir()
    operation_id, reception_id, revision_id, _old_path = await _seed_operation(
        artifact_factory,
        audio_root,
    )
    token = "retry-plan-token"
    key = "workspace-audio-retry"
    await _force_operation_terminal(
        artifact_factory,
        operation_id=operation_id,
        revision_id=revision_id,
        status="failed",
        idempotency_key=key,
        plan_token=token,
    )
    service = ReceptionAudioOperationService(
        artifact_factory,
        SimpleNamespace(),  # type: ignore[arg-type]
    )

    retried = await service.create_operation(
        tenant_id="tenant-a",
        reception_id=reception_id,
        plan_token=token,
        mode="physical",
        expected_version=1,
        idempotency_key=key,
    )

    assert retried.id != operation_id
    assert retried.status == "queued"
    async with artifact_factory() as db:
        failed = await db.get(ReceptionAudioOperation, operation_id)
    assert failed is not None
    assert failed.status == "failed"
    # The dead row released the client key so future replays hit the new row.
    assert failed.idempotency_key != key
    assert await service.pending_operation_ids(limit=10) == [int(retried.id)]


@pytest.mark.asyncio
async def test_replaying_a_succeeded_operation_returns_the_committed_result(
    artifact_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Replaying a completed request must not run the same plan twice."""

    audio_root = tmp_path / "audio-succeeded-replay"
    audio_root.mkdir()
    operation_id, reception_id, revision_id, _old_path = await _seed_operation(
        artifact_factory,
        audio_root,
    )
    token = "replay-plan-token"
    key = "workspace-audio-replay"
    await _force_operation_terminal(
        artifact_factory,
        operation_id=operation_id,
        revision_id=revision_id,
        status="succeeded",
        idempotency_key=key,
        plan_token=token,
    )
    service = ReceptionAudioOperationService(
        artifact_factory,
        SimpleNamespace(),  # type: ignore[arg-type]
    )

    replayed = await service.create_operation(
        tenant_id="tenant-a",
        reception_id=reception_id,
        plan_token=token,
        mode="physical",
        expected_version=1,
        idempotency_key=key,
    )

    assert replayed.id == operation_id
    assert replayed.status == "succeeded"
    async with artifact_factory() as db:
        operations = list(
            (
                await db.execute(
                    select(ReceptionAudioOperation).where(
                        ReceptionAudioOperation.reception_id == reception_id,
                        ReceptionAudioOperation.idempotency_key == key,
                    )
                )
            ).scalars()
        )
    assert [int(operation.id) for operation in operations] == [operation_id]
