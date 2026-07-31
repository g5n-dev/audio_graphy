"""Reception workspace API contracts.

These tests exercise the vertical slice through the real FastAPI app and the
SQLite API fixture.  No MySQL or audio model service is required.
"""

from __future__ import annotations

import wave
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from audio_graphy.core.audio_assembler import (
    AudioAssemblyManifest,
    AudioAssemblySource,
    AudioInputManifest,
)
from audio_graphy.schemas.receptions import ReceptionMergeRequest
from audio_graphy.services.receptions import (
    ReceptionService,
    ReceptionTimelineSliceOverride,
)
from tests.api.conftest import _run_async, seed_recording


class _WritingAudioAssembler:
    def __init__(self, allowed_root: Path) -> None:
        self.allowed_root = allowed_root
        self.received_sources: list[str | Path | AudioAssemblySource] = []

    async def assemble(
        self,
        sources: Sequence[str | Path | AudioAssemblySource],
        target_relative_path: str | Path,
    ) -> AudioAssemblyManifest:
        assert sources
        self.received_sources = list(sources)
        relative = Path(target_relative_path)
        assert not relative.is_absolute()
        output_path = self.allowed_root / relative
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"RIFF-test-wave")
        cursor = 0.0
        inputs_list: list[AudioInputManifest] = []
        for index, source in enumerate(sources):
            request = (
                source
                if isinstance(source, AudioAssemblySource)
                else AudioAssemblySource(path=source)
            )
            source_end = request.source_end_sec if request.source_end_sec is not None else 10.0
            duration = source_end - request.source_start_sec
            timeline_start = cursor + request.gap_before_sec
            timeline_end = timeline_start + duration
            inputs_list.append(
                AudioInputManifest(
                    path=str(request.path),
                    sha256=f"source-{index}",
                    size_bytes=100,
                    duration_sec=duration,
                    timeline_start_sec=timeline_start,
                    timeline_end_sec=timeline_end,
                    codec="pcm_s16le",
                    sample_rate=16_000,
                    channels=1,
                    source_start_sec=request.source_start_sec,
                    source_end_sec=source_end,
                    gap_before_sec=request.gap_before_sec,
                )
            )
            cursor = timeline_end
        inputs = tuple(inputs_list)
        return AudioAssemblyManifest(
            output_path=relative.as_posix(),
            output_sha256="assembled-sha256",
            output_bytes=output_path.stat().st_size,
            total_duration_sec=cursor,
            command_mode="concat_copy",
            inputs=inputs,
        )


class _FailingAudioAssembler:
    async def assemble(
        self,
        sources: Sequence[str | Path],
        target_relative_path: str | Path,
    ) -> AudioAssemblyManifest:
        raise RuntimeError("ffmpeg worker unavailable")


class _VersionConflictingAudioAssembler(_WritingAudioAssembler):
    def __init__(
        self,
        allowed_root: Path,
        session_factory: Any,
        reception_id: int,
    ) -> None:
        super().__init__(allowed_root)
        self._session_factory = session_factory
        self._reception_id = reception_id

    async def assemble(
        self,
        sources: Sequence[str | Path],
        target_relative_path: str | Path,
    ) -> AudioAssemblyManifest:
        manifest = await super().assemble(sources, target_relative_path)
        from audio_graphy.models import Reception

        async with self._session_factory() as session, session.begin():
            reception = await session.get(Reception, self._reception_id)
            assert reception is not None
            reception.version += 1
        return manifest


def _create_body(
    recording_ids: list[int],
    *,
    agent_name: str = "agent_ca",
) -> dict[str, Any]:
    started_at = datetime(2026, 7, 23, 9, 0, tzinfo=UTC)
    mappings: list[dict[str, Any]] = []
    cursor = 0.0
    for sequence_no, recording_id in enumerate(recording_ids):
        gap = 0.0 if sequence_no == 0 else 1.5
        cursor += gap
        mappings.append(
            {
                "recording_id": recording_id,
                "sequence_no": sequence_no,
                "timeline_start_sec": cursor,
                "timeline_end_sec": cursor + 10.0,
                "source_start_sec": 0.0,
                "source_end_sec": 10.0,
                "gap_before_sec": gap,
                "decision_source": "explicit",
                "merge_confidence": 1.0,
                "merge_reasons": {"external_session_id": "POS-001"},
            }
        )
        cursor += 10.0

    return {
        "external_session_id": "POS-001",
        "scenario": "automotive",
        "store_id": "S001",
        "agent_name": agent_name,
        "customer_hash": "customer-001",
        "status": "confirmed",
        "merge_mode": "both",
        "merge_confidence": 0.98,
        "started_at": started_at.isoformat(),
        "ended_at": (started_at + timedelta(minutes=1)).isoformat(),
        "recordings": mappings,
    }


async def _seed_dialogue_workspace(
    factory: Any,
    *,
    locked: bool = False,
    with_following_unit: bool = False,
) -> tuple[int, int]:
    from audio_graphy.models import (
        DialogueStateTransition,
        DialogueTagAssignment,
        DialogueUnit,
        ProvenanceEvent,
        Reception,
    )

    now = datetime.now(UTC)
    async with factory() as session:
        reception = Reception(
            tenant_id="chang_an",
            external_session_id=None,
            scenario="automotive",
            store_id="S001",
            agent_name="agent_ca",
            agent_user_id=3,
            customer_hash="customer-001",
            status="ready",
            merge_mode="logical",
            merge_confidence=1.0,
            started_at=now,
            ended_at=now + timedelta(minutes=1),
            version=1,
        )
        session.add(reception)
        await session.flush()

        unit = DialogueUnit(
            tenant_id="chang_an",
            reception_id=reception.id,
            source_recording_id=None,
            unit_index=0,
            version=1,
            start_sec=0.0,
            end_sec=40.0,
            topic="车型需求与预算",
            business_stage="需求了解",
            summary="客户关注车型和预算。",
            boundary_confidence=0.93,
            boundary_reasons=["semantic_shift"],
            segment_refs=[{"recording_id": 101, "segment_id": 1}],
            speaker_refs=["agent_ca", "customer"],
            edit_status="locked" if locked else "auto",
        )
        session.add(unit)
        await session.flush()

        rows: list[Any] = [
            DialogueTagAssignment(
                tenant_id="chang_an",
                reception_id=reception.id,
                dialogue_unit_id=unit.id,
                group_key="sales-quality",
                group_version="v1",
                label_key="needs_discovery",
                label_value="pass",
                confidence=0.92,
                source="llm",
                priority=10,
                evidence_refs=[
                    {
                        "kind": "audio",
                        "recording_id": 101,
                        "start_sec": 5.0,
                        "end_sec": 15.0,
                    }
                ],
                model_run_id="run-1",
                is_current=True,
                assigned_at=now,
            ),
            DialogueStateTransition(
                tenant_id="chang_an",
                reception_id=reception.id,
                dialogue_unit_id=unit.id,
                sequence_no=0,
                from_state="接待问候",
                to_state="需求了解",
                trigger="need_detected",
                confidence=0.91,
                evidence_refs=[{"recording_id": 101, "start_sec": 5.0, "end_sec": 8.0}],
                algorithm_version="dialogue-hybrid-v1",
            ),
            ProvenanceEvent(
                tenant_id="chang_an",
                object_type="dialogue_unit",
                object_ref=str(unit.id),
                event_type="derived",
                actor="system",
                algorithm_version="dialogue-hybrid-v1",
                parent_refs=[],
                evidence_refs=[{"recording_id": 101}],
                payload={"seed": True},
                occurred_at=now,
            ),
        ]
        if with_following_unit:
            rows.append(
                DialogueUnit(
                    tenant_id="chang_an",
                    reception_id=reception.id,
                    source_recording_id=None,
                    unit_index=1,
                    version=1,
                    start_sec=40.0,
                    end_sec=60.0,
                    topic="报价方案",
                    business_stage="报价",
                    summary="销售介绍报价。",
                    boundary_confidence=0.9,
                    boundary_reasons=["semantic_shift"],
                    segment_refs=[{"recording_id": 101, "segment_id": 2}],
                    speaker_refs=["agent_ca", "customer"],
                    edit_status="auto",
                )
            )
        session.add_all(rows)
        await session.commit()
        return reception.id, unit.id


async def _seed_governance_current_for_reception(
    factory: Any,
    *,
    reception_id: int,
    tag_key: str = "intent",
    tag_value: str = "purchase",
    tombstone: bool = False,
) -> list[int]:
    from sqlalchemy import select

    from audio_graphy.models import DialogueUnit
    from audio_graphy.models.tag_governance import (
        TagAssignmentCurrent,
        TagAssignmentFact,
    )

    now = datetime.now(UTC)
    async with factory() as session:
        unit_ids = list(
            (
                await session.execute(
                    select(DialogueUnit.id)
                    .where(DialogueUnit.reception_id == reception_id)
                    .order_by(DialogueUnit.unit_index)
                )
            ).scalars()
        )
        for unit_id in unit_ids:
            fact = TagAssignmentFact(
                tenant_id="chang_an",
                subject_type="dialogue_unit",
                subject_id=unit_id,
                reception_id=reception_id,
                dialogue_unit_id=unit_id,
                tag_key=tag_key,
                tag_value=tag_value,
                confidence=0.91,
                evidence_refs=[{"segment_id": unit_id, "start_sec": 1, "end_sec": 2}],
                source="llm",
                schema_version_id=1,
                tagger_version_id=2,
                extraction_run_id=None,
                deployment_id=None,
                input_hash=f"{unit_id:064x}",
                superseded_fact_id=None,
                revision=1,
                tombstone=tombstone,
                actor_user_id=None,
                assigned_at=now,
            )
            session.add(fact)
            await session.flush()
            session.add(
                TagAssignmentCurrent(
                    tenant_id="chang_an",
                    subject_type="dialogue_unit",
                    subject_id=unit_id,
                    tag_key=tag_key,
                    fact_id=fact.id,
                    revision=1,
                )
            )
        await session.commit()
        return unit_ids


async def _seed_long_dialogue_workspace(
    factory: Any,
    *,
    recording_id: int,
    item_count: int,
) -> int:
    """Seed every workspace collection past one legal maximum page."""
    from audio_graphy.models import (
        DialogueStateTransition,
        DialogueTagAssignment,
        DialogueUnit,
        ProvenanceEvent,
        Reception,
        ReceptionRecording,
        Segment,
    )

    now = datetime.now(UTC)
    async with factory() as session:
        reception = Reception(
            tenant_id="chang_an",
            external_session_id=None,
            scenario="automotive",
            store_id="S001",
            agent_name="agent_ca",
            agent_user_id=3,
            customer_hash="customer-long",
            status="ready",
            merge_mode="logical",
            merge_confidence=1.0,
            started_at=now,
            ended_at=now + timedelta(seconds=item_count),
            version=1,
        )
        session.add(reception)
        await session.flush()
        session.add(
            ReceptionRecording(
                tenant_id="chang_an",
                reception_id=reception.id,
                recording_id=recording_id,
                sequence_no=0,
                timeline_start_sec=0.0,
                timeline_end_sec=float(item_count),
                source_start_sec=0.0,
                source_end_sec=float(item_count),
                gap_before_sec=0.0,
                decision_source="explicit",
                merge_confidence=1.0,
                merge_reasons={},
            )
        )
        units = [
            DialogueUnit(
                tenant_id="chang_an",
                reception_id=reception.id,
                source_recording_id=recording_id,
                unit_index=index,
                version=1,
                start_sec=float(index),
                end_sec=float(index) + 0.9,
                topic=f"超长接待单元 {index}",
                business_stage=f"stage-{index % 5}",
                summary="长接待分页边界验证",
                boundary_confidence=0.9,
                boundary_reasons=["performance-boundary"],
                segment_refs=[{"recording_id": recording_id, "segment_id": index + 1}],
                speaker_refs=["agent_ca", "customer"],
                edit_status="auto",
            )
            for index in range(item_count)
        ]
        session.add_all(units)
        await session.flush()

        rows: list[Any] = []
        for index, unit in enumerate(units):
            rows.extend(
                [
                    Segment(
                        tenant_id="chang_an",
                        recording_id=recording_id,
                        idx=index,
                        start_sec=float(index),
                        end_sec=float(index) + 0.8,
                        transcript=f"第 {index} 段转写 " + ("长" * 1_000),
                        speaker="agent_ca" if index % 2 == 0 else "customer",
                        vad_conf=0.95,
                    ),
                    DialogueTagAssignment(
                        tenant_id="chang_an",
                        reception_id=reception.id,
                        dialogue_unit_id=unit.id,
                        group_key="workspace-performance",
                        group_version="v1",
                        label_key="stage",
                        label_value=f"value-{index}",
                        confidence=0.9,
                        source="rule",
                        priority=0,
                        evidence_refs=[
                            {
                                "kind": "audio",
                                "recording_id": recording_id,
                                "start_sec": float(index),
                                "end_sec": float(index) + 0.8,
                            }
                        ],
                        model_run_id="workspace-performance-v1",
                        is_current=True,
                        assigned_at=now + timedelta(microseconds=index),
                    ),
                    DialogueStateTransition(
                        tenant_id="chang_an",
                        reception_id=reception.id,
                        dialogue_unit_id=unit.id,
                        sequence_no=index,
                        from_state=f"stage-{(index - 1) % 5}",
                        to_state=f"stage-{index % 5}",
                        trigger="long_reception_test",
                        confidence=0.9,
                        evidence_refs=[
                            {
                                "recording_id": recording_id,
                                "start_sec": float(index),
                                "end_sec": float(index) + 0.8,
                            }
                        ],
                        algorithm_version="dialogue-hybrid-v1",
                    ),
                    ProvenanceEvent(
                        tenant_id="chang_an",
                        reception_id=reception.id,
                        object_type="dialogue_unit",
                        object_ref=str(unit.id),
                        event_type="derived",
                        actor="system",
                        algorithm_version="dialogue-hybrid-v1",
                        parent_refs=[],
                        evidence_refs=[{"recording_id": recording_id}],
                        payload={"index": index, "summary": "审" * 1_000},
                        occurred_at=now + timedelta(microseconds=index),
                    ),
                ]
            )
        session.add_all(rows)
        await session.commit()
        return reception.id


async def _seed_active_audio_operation(
    factory: Any,
    *,
    reception_id: int,
) -> int:
    from audio_graphy.models import Reception
    from audio_graphy.models.reception_audio import (
        ReceptionAudioOperation,
        ReceptionTimelineRevision,
    )

    async with factory() as session:
        reception = await session.get(Reception, reception_id)
        assert reception is not None
        revision = ReceptionTimelineRevision(
            tenant_id=reception.tenant_id,
            reception_id=reception.id,
            revision=1,
            expected_reception_version=reception.version,
            state="STAGING",
            plan_signature=f"{reception_id:064x}",
            plan_token_hash=f"{reception_id + 1:064x}",
            source_manifest=[],
            total_duration_ms=105_000,
            physical_eligible=True,
            warnings=[],
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        session.add(revision)
        await session.flush()
        operation = ReceptionAudioOperation(
            tenant_id=reception.tenant_id,
            reception_id=reception.id,
            timeline_revision_id=revision.id,
            idempotency_key=f"workspace-active-operation-{reception.id}",
            mode="both",
            expected_reception_version=reception.version,
            status="assembling",
            progress=0.55,
            attempt_count=1,
        )
        session.add(operation)
        await session.commit()
        return operation.id


async def _seed_reception_provenance_events(
    factory: Any,
    *,
    reception_id: int,
    item_count: int,
) -> None:
    from audio_graphy.models import ProvenanceEvent

    now = datetime.now(UTC)
    async with factory() as session:
        session.add_all(
            [
                ProvenanceEvent(
                    tenant_id="chang_an",
                    reception_id=reception_id,
                    object_type="reception",
                    object_ref=str(reception_id),
                    event_type="edited",
                    actor="system",
                    algorithm_version=None,
                    parent_refs=[],
                    evidence_refs=[],
                    payload={"index": index},
                    occurred_at=now + timedelta(microseconds=index),
                )
                for index in range(item_count)
            ]
        )
        await session.commit()


async def _seed_recording_duration(
    factory: Any,
    *,
    recording_id: int,
    duration_sec: float,
    segment_duration_sec: float | None = None,
) -> None:
    from audio_graphy.models import Recording, Segment

    async with factory() as session:
        recording = await session.get(Recording, recording_id)
        assert recording is not None
        recording.audio_duration_ms = round(duration_sec * 1_000)
        session.add(
            Segment(
                tenant_id="chang_an",
                recording_id=recording_id,
                idx=0,
                start_sec=0.0,
                end_sec=(
                    segment_duration_sec
                    if segment_duration_sec is not None
                    else duration_sec
                ),
                transcript="测试录音",
                speaker="agent_ca",
                vad_conf=0.99,
            )
        )
        await session.commit()


async def _seed_conflicting_dialogue_tag(
    factory: Any,
    *,
    reception_id: int,
    dialogue_unit_id: int,
) -> None:
    from audio_graphy.models import DialogueTagAssignment

    async with factory() as session:
        session.add(
            DialogueTagAssignment(
                tenant_id="chang_an",
                reception_id=reception_id,
                dialogue_unit_id=dialogue_unit_id,
                group_key="sales-quality",
                group_version="v1",
                label_key="needs_discovery",
                label_value="fail",
                confidence=0.99,
                source="manual",
                priority=99,
                evidence_refs=[
                    {
                        "recording_id": 101,
                        "timeline_start_sec": 20.0,
                        "timeline_end_sec": 25.0,
                        "coordinate_space": "reception_timeline",
                    }
                ],
                model_run_id=None,
                is_current=True,
                assigned_at=datetime.now(UTC),
            )
        )
        await session.commit()


async def _seed_transcript_segments(
    factory: Any,
    *,
    recording_id: int,
    items: list[tuple[float, float, str]],
) -> None:
    from audio_graphy.models import Segment

    async with factory() as session:
        session.add_all(
            [
                Segment(
                    tenant_id="chang_an",
                    recording_id=recording_id,
                    idx=index,
                    start_sec=start_sec,
                    end_sec=end_sec,
                    transcript=text,
                    speaker="agent_ca" if index % 2 == 0 else "customer",
                    vad_conf=0.98,
                )
                for index, (start_sec, end_sec, text) in enumerate(items)
            ]
        )
        await session.commit()


async def _reception_count(factory: Any) -> int:
    from sqlalchemy import func, select

    from audio_graphy.models import Reception

    async with factory() as session:
        result = await session.execute(select(func.count(Reception.id)))
        return int(result.scalar_one())


async def _set_recording_path(
    factory: Any,
    *,
    recording_id: int,
    path: Path,
) -> None:
    from sqlalchemy import select

    from audio_graphy.models import Recording

    async with factory() as session:
        result = await session.execute(select(Recording).where(Recording.id == recording_id))
        recording = result.scalar_one()
        recording.path = str(path)
        await session.commit()


async def _set_recording_source_facts(
    factory: Any,
    *,
    recording_ids: Sequence[int],
    duration_ms: int = 10_000,
) -> None:
    from sqlalchemy import select

    from audio_graphy.models import Recording

    async with factory() as session:
        result = await session.execute(
            select(Recording).where(Recording.id.in_(recording_ids))
        )
        for index, recording in enumerate(result.scalars()):
            recording.audio_duration_ms = duration_ms
            recording.audio_sha256 = f"{index + 1:064x}"
            recording.audio_size_bytes = duration_ms * 32
            recording.audio_sample_rate = 16_000
            recording.audio_channels = 1
            recording.source_revision = 1
        await session.commit()


async def _set_recording_encryption(
    factory: Any,
    *,
    recording_id: int,
    plaintext_path: Path,
    encrypted_path: Path,
) -> None:
    from sqlalchemy import select

    from audio_graphy.models import Recording

    async with factory() as session:
        result = await session.execute(select(Recording).where(Recording.id == recording_id))
        recording = result.scalar_one()
        recording.path = str(plaintext_path)
        recording.audio_encrypted_path = str(encrypted_path)
        recording.audio_encryption_meta = {"test": True}
        await session.commit()


async def _merged_audio_path(factory: Any, reception_id: int) -> str | None:
    from sqlalchemy import select

    from audio_graphy.models import Reception

    async with factory() as session:
        result = await session.execute(
            select(Reception.merged_audio_path).where(Reception.id == reception_id)
        )
        return result.scalar_one()


async def _set_recording_times(
    factory: Any,
    *,
    values: dict[int, datetime],
) -> None:
    from sqlalchemy import select

    from audio_graphy.models import Recording

    async with factory() as session:
        result = await session.execute(select(Recording).where(Recording.id.in_(values)))
        for recording in result.scalars().all():
            recording.recorded_at = values[recording.id]
        await session.commit()


@pytest.mark.integration
class TestReceptionCreateAndRead:
    def test_create_persists_mapping_and_provenance(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        first = _run_async(seed_recording(db_session_factory, recording_id=101))
        second = _run_async(seed_recording(db_session_factory, recording_id=102))
        _run_async(
            _seed_recording_duration(
                db_session_factory,
                recording_id=first,
                duration_sec=10.0,
                segment_duration_sec=3.0,
            )
        )
        _run_async(
            _seed_recording_duration(
                db_session_factory,
                recording_id=second,
                duration_sec=10.0,
            )
        )

        response = test_client.post(
            "/api/v1/receptions",
            json=_create_body([first, second]),
            headers=auth_headers["inspector_t1"],
        )

        assert response.status_code == 201
        created = response.json()
        assert created["tenant_id"] == "chang_an"
        assert created["version"] == 1
        assert created["status"] == "needs_review"
        assert [item["recording_id"] for item in created["recordings"]] == [101, 102]
        assert created["recordings"][1]["timeline_start_sec"] == 11.5
        assert created["audio_url"] is None
        assert created["playback_expires_at"] is None
        assert "merged_audio_path" not in created
        assert "source_path" not in created["recordings"][0]
        assert created["recordings"][0]["playback_expires_at"] is not None
        assert {
            item["playback_expires_at"] for item in created["recordings"]
        } == {created["recordings"][0]["playback_expires_at"]}
        assert (
            created["recordings"][0]["audio_url"]
            .split("?", 1)[0]
            .endswith(f"/receptions/{created['id']}/recordings/101/audio")
        )

        workspace = test_client.get(
            f"/api/v1/receptions/{created['id']}/workspace",
            headers=auth_headers["agent_t1"],
        )
        assert workspace.status_code == 200
        assert workspace.json()["reception"]["id"] == created["id"]
        assert "source_path" not in workspace.json()["recordings"][0]
        assert "merged_audio_path" not in workspace.json()["reception"]
        assert workspace.json()["dialogue_units"] == []
        assert workspace.json()["transcript_items"][0]["text"] == "测试录音"
        assert workspace.json()["transcript_items"][0]["timeline_end_sec"] == 3.0
        assert workspace.json()["provenance_events"][0]["event_type"] == "created"

        provenance = test_client.get(
            f"/api/v1/provenance/reception/{created['id']}",
            headers=auth_headers["viewer_t1"],
        )
        assert provenance.status_code == 200
        events = provenance.json()["items"]
        assert events[0]["event_type"] == "created"
        assert {ref["recording_id"] for ref in events[0]["evidence_refs"]} == {101, 102}

    def test_create_rejects_invalid_sequence_and_timeline(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        first = _run_async(seed_recording(db_session_factory, recording_id=101))
        second = _run_async(seed_recording(db_session_factory, recording_id=102))
        body = _create_body([first, second])
        body["recordings"][1]["sequence_no"] = 2
        body["recordings"][1]["timeline_start_sec"] = 5.0

        response = test_client.post(
            "/api/v1/receptions",
            json=body,
            headers=auth_headers["admin_t1"],
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_create_rejects_cross_tenant_recording_as_not_found(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        foreign = _run_async(
            seed_recording(
                db_session_factory,
                tenant_id="byd",
                store_id="S001",
                agent_name="agent_byd",
                recording_id=201,
            )
        )

        response = test_client.post(
            "/api/v1/receptions",
            json=_create_body([foreign]),
            headers=auth_headers["admin_t1"],
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "RECORDING_NOT_FOUND"

    def test_create_requires_inspector_or_admin(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        recording_id = _run_async(seed_recording(db_session_factory))
        response = test_client.post(
            "/api/v1/receptions",
            json=_create_body([recording_id]),
            headers=auth_headers["agent_t1"],
        )
        assert response.status_code == 403

    def test_cross_tenant_and_other_agent_reads_are_hidden(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        recording_id = _run_async(seed_recording(db_session_factory))
        create = test_client.post(
            "/api/v1/receptions",
            json=_create_body([recording_id], agent_name="someone_else"),
            headers=auth_headers["admin_t1"],
        )
        reception_id = create.json()["id"]

        cross_tenant = test_client.get(
            f"/api/v1/receptions/{reception_id}/workspace",
            headers=auth_headers["admin_t2"],
        )
        own_scope = test_client.get(
            f"/api/v1/receptions/{reception_id}/workspace",
            headers=auth_headers["agent_t1"],
        )
        assert cross_tenant.status_code == 404
        assert own_scope.status_code == 404

    def test_merge_endpoint_appends_and_reorders_source_recordings(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        first = _run_async(seed_recording(db_session_factory, recording_id=101))
        second = _run_async(seed_recording(db_session_factory, recording_id=102))
        _run_async(
            _seed_recording_duration(
                db_session_factory,
                recording_id=second,
                duration_sec=8.0,
            )
        )
        created = test_client.post(
            "/api/v1/receptions",
            json=_create_body([first]),
            headers=auth_headers["admin_t1"],
        ).json()

        appended = test_client.post(
            f"/api/v1/receptions/{created['id']}/merge",
            json={
                "recording_ids": [first, second],
                "mode": "logical",
                "expected_version": 1,
            },
            headers=auth_headers["inspector_t1"],
        )

        assert appended.status_code == 200
        assert appended.json()["version"] == 2
        assert [item["recording_id"] for item in appended.json()["recordings"]] == [
            first,
            second,
        ]
        assert appended.json()["recordings"][1]["timeline_end_sec"] == 18.0

        reordered = test_client.post(
            f"/api/v1/receptions/{created['id']}/merge",
            json={
                "recording_ids": [second, first],
                "mode": "logical",
                "expected_version": 2,
            },
            headers=auth_headers["admin_t1"],
        )
        assert reordered.status_code == 200
        assert reordered.json()["version"] == 3
        assert [item["recording_id"] for item in reordered.json()["recordings"]] == [
            second,
            first,
        ]
        assert reordered.json()["recordings"][0]["timeline_end_sec"] == 8.0

    def test_physical_merge_without_assembler_returns_503_without_mutation(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        recording_id = _run_async(seed_recording(db_session_factory))
        created = test_client.post(
            "/api/v1/receptions",
            json=_create_body([recording_id]),
            headers=auth_headers["admin_t1"],
        ).json()
        test_client.app.state.audio_assembler = None

        response = test_client.post(
            f"/api/v1/receptions/{created['id']}/merge",
            json={
                "recording_ids": [recording_id],
                "mode": "both",
                "expected_version": 1,
            },
            headers=auth_headers["admin_t1"],
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "AUDIO_ASSEMBLER_UNAVAILABLE"

        workspace = test_client.get(
            f"/api/v1/receptions/{created['id']}/workspace",
            headers=auth_headers["admin_t1"],
        )
        assert workspace.json()["reception"]["version"] == 1
        assert workspace.json()["reception"]["audio_url"] is None

    def test_application_wires_the_real_audio_assembler_by_default(
        self,
        test_client: TestClient,
    ) -> None:
        from audio_graphy.core.audio_assembler import AudioAssembler

        assert isinstance(
            test_client.app.state.audio_assembler,
            AudioAssembler,
        )

    def test_physical_merge_commits_only_after_assembler_writes_safe_output(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        recording_id = _run_async(seed_recording(db_session_factory))
        body = _create_body([recording_id])
        body["merge_mode"] = "logical"
        created = test_client.post(
            "/api/v1/receptions",
            json=body,
            headers=auth_headers["admin_t1"],
        ).json()
        test_client.app.state.audio_assembler = _WritingAudioAssembler(
            Path(test_client.app.state.settings.working_dir)
        )

        response = test_client.post(
            f"/api/v1/receptions/{created['id']}/merge",
            json={
                "recording_ids": [recording_id],
                "mode": "physical",
                "expected_version": 1,
            },
            headers=auth_headers["admin_t1"],
        )

        assert response.status_code == 200
        assert response.json()["version"] == 2
        assert response.json()["merge_mode"] == "physical"
        assert "merged_audio_path" not in response.json()
        assert response.json()["playback_expires_at"] is not None
        assert (
            response.json()["recordings"][0]["playback_expires_at"]
            == response.json()["playback_expires_at"]
        )
        assert (
            response.json()["audio_url"]
            .split("?", 1)[0]
            .endswith(f"/receptions/{created['id']}/audio")
        )
        audio = test_client.get(
            response.json()["audio_url"],
            headers=auth_headers["admin_t1"],
        )
        assert audio.status_code == 200
        assert audio.content == b"RIFF-test-wave"
        assert (
            audio.headers["x-audio-grant-expires-at"]
            == response.json()["playback_expires_at"]
        )
        assert audio.headers["x-time-origin-ms"] == "0"
        assert audio.headers["x-legal-source-start-ms"] == "0"
        assert audio.headers["x-legal-source-end-ms"] == "10000"
        provenance = test_client.get(
            f"/api/v1/provenance/reception/{created['id']}",
            headers=auth_headers["viewer_t1"],
        ).json()["items"]
        manifest = provenance[-1]["payload"]["audio_manifest"]
        assert manifest["output_sha256"] == "assembled-sha256"
        assert manifest["command_mode"] == "concat_copy"
        workspace = test_client.get(
            f"/api/v1/receptions/{created['id']}/workspace",
            headers=auth_headers["admin_t1"],
        ).text
        assert str(test_client.app.state.settings.working_dir) not in workspace
        assert "source_path" not in workspace
        assert "merged_audio_path" not in workspace

    def test_physical_assembler_is_duration_source_for_new_recordings(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        first = _run_async(seed_recording(db_session_factory, recording_id=101))
        second = _run_async(seed_recording(db_session_factory, recording_id=102))
        body = _create_body([first])
        body["merge_mode"] = "logical"
        created = test_client.post(
            "/api/v1/receptions",
            json=body,
            headers=auth_headers["admin_t1"],
        ).json()
        test_client.app.state.audio_assembler = _WritingAudioAssembler(
            Path(test_client.app.state.settings.working_dir)
        )

        response = test_client.post(
            f"/api/v1/receptions/{created['id']}/merge",
            json={
                "recording_ids": [first, second],
                "mode": "both",
                "expected_version": 1,
            },
            headers=auth_headers["admin_t1"],
        )

        assert response.status_code == 200
        assert [mapping["timeline_end_sec"] for mapping in response.json()["recordings"]] == [
            10.0,
            20.0,
        ]

    def test_internal_timeline_override_drives_physical_slice_and_persistence(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        first = _run_async(seed_recording(db_session_factory, recording_id=101))
        second = _run_async(seed_recording(db_session_factory, recording_id=102))
        body = _create_body([first])
        body["merge_mode"] = "logical"
        reception_id = test_client.post(
            "/api/v1/receptions",
            json=body,
            headers=auth_headers["admin_t1"],
        ).json()["id"]
        root = Path(test_client.app.state.settings.working_dir)
        assembler = _WritingAudioAssembler(root)
        service = ReceptionService(
            db_session_factory,
            audio_root=root,
            audio_assembler=assembler,
        )

        workspace = _run_async(
            service.merge_recordings(
                reception_id,
                "chang_an",
                ReceptionMergeRequest(
                    recording_ids=[first, second],
                    mode="both",
                    expected_version=1,
                ),
                actor="system:audio-operation",
                timeline_override={
                    first: ReceptionTimelineSliceOverride(
                        source_start_sec=1.0,
                        source_end_sec=4.0,
                    ),
                    second: ReceptionTimelineSliceOverride(
                        source_start_sec=2.0,
                        source_end_sec=6.0,
                        gap_before_sec=1.5,
                    ),
                },
            )
        )

        assert [
            (
                source.source_start_sec,
                source.source_end_sec,
                source.gap_before_sec,
            )
            for source in assembler.received_sources
            if isinstance(source, AudioAssemblySource)
        ] == [(1.0, 4.0, 0.0), (2.0, 6.0, 1.5)]
        assert [
            (
                mapping.source_start_sec,
                mapping.source_end_sec,
                mapping.gap_before_sec,
                mapping.timeline_start_sec,
                mapping.timeline_end_sec,
            )
            for mapping, _recording in workspace.recordings
        ] == [
            (1.0, 4.0, 0.0, 0.0, 3.0),
            (2.0, 6.0, 1.5, 4.5, 8.5),
        ]

    def test_before_commit_hook_failure_rolls_back_mapping_and_artifact(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        recording_id = _run_async(seed_recording(db_session_factory))
        body = _create_body([recording_id])
        body["merge_mode"] = "logical"
        reception_id = test_client.post(
            "/api/v1/receptions",
            json=body,
            headers=auth_headers["admin_t1"],
        ).json()["id"]
        root = Path(test_client.app.state.settings.working_dir)
        service = ReceptionService(
            db_session_factory,
            audio_root=root,
            audio_assembler=_WritingAudioAssembler(root),
        )
        hook_observations: list[tuple[int, tuple[int, ...], bool, str | None]] = []

        async def fail_before_commit(
            session: Any,
            reception: Any,
            mappings: tuple[Any, ...],
            prepared: Any,
            previous_merged_audio_path: str | None,
        ) -> None:
            hook_observations.append(
                (
                    reception.version,
                    tuple(mapping.id for mapping in mappings),
                    session.in_transaction(),
                    previous_merged_audio_path,
                )
            )
            assert prepared is not None
            raise RuntimeError("injected operation publication failure")

        with pytest.raises(
            RuntimeError,
            match="injected operation publication failure",
        ):
            _run_async(
                service.merge_recordings(
                    reception_id,
                    "chang_an",
                    ReceptionMergeRequest(
                        recording_ids=[recording_id],
                        mode="both",
                        expected_version=1,
                    ),
                    actor="system:audio-operation",
                    timeline_override={
                        recording_id: ReceptionTimelineSliceOverride(
                            source_start_sec=1.0,
                            source_end_sec=4.0,
                        )
                    },
                    before_commit=fail_before_commit,
                )
            )

        assert hook_observations
        version, mapping_ids, in_transaction, old_path = hook_observations[0]
        assert version == 2
        assert all(mapping_id > 0 for mapping_id in mapping_ids)
        assert in_transaction is True
        assert old_path is None
        workspace = test_client.get(
            f"/api/v1/receptions/{reception_id}/workspace",
            headers=auth_headers["admin_t1"],
        ).json()
        assert workspace["reception"]["version"] == 1
        assert workspace["recordings"][0]["source_start_sec"] == 0.0
        generation_dir = (
            root
            / "assembled_audio"
            / "chang_an"
            / "receptions"
            / f"reception-{reception_id}"
        )
        assert not generation_dir.exists() or not any(generation_dir.glob("v2-*.wav*"))

    def test_encrypted_sources_and_merged_audio_leave_no_plaintext_at_rest(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
        tmp_path: Path,
    ) -> None:
        from audio_graphy.core.crypto import AudioCrypto

        root = Path(test_client.app.state.settings.working_dir)
        source = root / "encrypted-source.wav"
        encrypted_source = root / "encrypted-source.wav.enc"
        source.write_bytes(b"RIFF-source")
        crypto = AudioCrypto(tmp_path / "master.key", dev_mode=True)
        crypto.encrypt_file(source, encrypted_source)
        source.unlink()

        recording_id = _run_async(seed_recording(db_session_factory))
        _run_async(
            _set_recording_encryption(
                db_session_factory,
                recording_id=recording_id,
                plaintext_path=source,
                encrypted_path=encrypted_source,
            )
        )
        body = _create_body([recording_id])
        body["merge_mode"] = "logical"
        created = test_client.post(
            "/api/v1/receptions",
            json=body,
            headers=auth_headers["admin_t1"],
        ).json()
        test_client.app.state.audio_crypto = crypto
        test_client.app.state.audio_assembler = _WritingAudioAssembler(root)

        response = test_client.post(
            f"/api/v1/receptions/{created['id']}/merge",
            json={
                "recording_ids": [recording_id],
                "mode": "physical",
                "expected_version": 1,
            },
            headers=auth_headers["admin_t1"],
        )

        assert response.status_code == 200
        persisted_path = _run_async(_merged_audio_path(db_session_factory, created["id"]))
        assert persisted_path is not None
        assert persisted_path.endswith(".wav.enc")
        assert Path(persisted_path).is_file()
        assert not Path(persisted_path.removesuffix(".enc")).exists()
        assert not source.exists()

        streamed = test_client.get(response.json()["audio_url"])
        assert streamed.status_code == 200
        assert streamed.content == b"RIFF-test-wave"
        assert streamed.headers["referrer-policy"] == "no-referrer"
        runtime_dir = root / "runtime_plaintext"
        assert not runtime_dir.exists() or not any(runtime_dir.rglob("audio-*"))

    def test_failed_physical_assembly_rolls_back_workspace_version(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        recording_id = _run_async(seed_recording(db_session_factory))
        body = _create_body([recording_id])
        body["merge_mode"] = "logical"
        created = test_client.post(
            "/api/v1/receptions",
            json=body,
            headers=auth_headers["admin_t1"],
        ).json()
        test_client.app.state.audio_assembler = _FailingAudioAssembler()

        response = test_client.post(
            f"/api/v1/receptions/{created['id']}/merge",
            json={
                "recording_ids": [recording_id],
                "mode": "both",
                "expected_version": 1,
            },
            headers=auth_headers["inspector_t1"],
        )

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "AUDIO_ASSEMBLY_FAILED"
        workspace = test_client.get(
            f"/api/v1/receptions/{created['id']}/workspace",
            headers=auth_headers["admin_t1"],
        ).json()
        assert workspace["reception"]["version"] == 1
        assert workspace["reception"]["audio_url"] is None

    def test_physical_generation_is_deleted_when_snapshot_version_conflicts(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        recording_id = _run_async(seed_recording(db_session_factory))
        body = _create_body([recording_id])
        body["merge_mode"] = "logical"
        created = test_client.post(
            "/api/v1/receptions",
            json=body,
            headers=auth_headers["admin_t1"],
        ).json()
        root = Path(test_client.app.state.settings.working_dir)
        test_client.app.state.audio_assembler = _VersionConflictingAudioAssembler(
            root,
            db_session_factory,
            created["id"],
        )

        response = test_client.post(
            f"/api/v1/receptions/{created['id']}/merge",
            json={
                "recording_ids": [recording_id],
                "mode": "physical",
                "expected_version": 1,
            },
            headers=auth_headers["admin_t1"],
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "VERSION_CONFLICT"
        generation_dir = (
            root / "assembled_audio" / "chang_an" / "receptions" / f"reception-{created['id']}"
        )
        assert not generation_dir.exists() or not any(generation_dir.glob("v2-*.wav*"))

    def test_physical_merge_clips_prefix_mapping_instead_of_using_full_source(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        recording_id = _run_async(seed_recording(db_session_factory))
        body = _create_body([recording_id])
        body["merge_mode"] = "logical"
        body["recordings"][0]["timeline_end_sec"] = 4.0
        body["recordings"][0]["source_start_sec"] = 0.0
        body["recordings"][0]["source_end_sec"] = 4.0
        created = test_client.post(
            "/api/v1/receptions",
            json=body,
            headers=auth_headers["admin_t1"],
        ).json()
        assembler = _WritingAudioAssembler(
            Path(test_client.app.state.settings.working_dir)
        )
        test_client.app.state.audio_assembler = assembler

        response = test_client.post(
            f"/api/v1/receptions/{created['id']}/merge",
            json={
                "recording_ids": [recording_id],
                "mode": "physical",
                "expected_version": 1,
            },
            headers=auth_headers["admin_t1"],
        )

        assert response.status_code == 200
        assert len(assembler.received_sources) == 1
        source = assembler.received_sources[0]
        assert isinstance(source, AudioAssemblySource)
        assert source.source_start_sec == 0.0
        assert source.source_end_sec == 4.0
        assert response.json()["recordings"][0]["source_end_sec"] == 4.0

    def test_switching_to_logical_retires_previous_physical_artifact(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        recording_id = _run_async(seed_recording(db_session_factory))
        body = _create_body([recording_id])
        body["merge_mode"] = "logical"
        created = test_client.post(
            "/api/v1/receptions",
            json=body,
            headers=auth_headers["admin_t1"],
        ).json()
        root = Path(test_client.app.state.settings.working_dir)
        test_client.app.state.audio_assembler = _WritingAudioAssembler(root)
        physical = test_client.post(
            f"/api/v1/receptions/{created['id']}/merge",
            json={
                "recording_ids": [recording_id],
                "mode": "physical",
                "expected_version": 1,
            },
            headers=auth_headers["admin_t1"],
        )
        assert physical.status_code == 200
        persisted = _run_async(_merged_audio_path(db_session_factory, created["id"]))
        assert persisted is not None
        assert Path(persisted).is_file()

        logical = test_client.post(
            f"/api/v1/receptions/{created['id']}/merge",
            json={
                "recording_ids": [recording_id],
                "mode": "logical",
                "expected_version": 2,
            },
            headers=auth_headers["admin_t1"],
        )

        assert logical.status_code == 200
        assert not Path(persisted).exists()

    def test_mapping_change_supersedes_all_derived_workspace_artifacts(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        recording_id = _run_async(seed_recording(db_session_factory, recording_id=101))
        _run_async(
            _seed_recording_duration(
                db_session_factory,
                recording_id=recording_id,
                duration_sec=10.0,
            )
        )
        reception_id, unit_id = _run_async(_seed_dialogue_workspace(db_session_factory))
        before = test_client.get(
            f"/api/v1/receptions/{reception_id}/workspace",
            headers=auth_headers["admin_t1"],
        ).json()
        tag_id = before["tag_assignments"][0]["id"]
        transition_id = before["state_transitions"][0]["id"]

        merged = test_client.post(
            f"/api/v1/receptions/{reception_id}/merge",
            json={
                "recording_ids": [recording_id],
                "mode": "logical",
                "expected_version": 1,
            },
            headers=auth_headers["admin_t1"],
        )

        assert merged.status_code == 200
        assert merged.json()["status"] == "needs_review"
        workspace = test_client.get(
            f"/api/v1/receptions/{reception_id}/workspace",
            headers=auth_headers["admin_t1"],
        ).json()
        assert workspace["dialogue_units"] == []
        assert workspace["tag_assignments"] == []
        assert workspace["state_transitions"] == []

        for object_type, object_ref in (
            ("dialogue_unit", unit_id),
            ("dialogue_tag_assignment", tag_id),
            ("dialogue_state_transition", transition_id),
        ):
            lineage = test_client.get(
                f"/api/v1/provenance/{object_type}/{object_ref}",
                headers=auth_headers["viewer_t1"],
            )
            assert lineage.status_code == 200
            assert lineage.json()["items"][-1]["event_type"] == "superseded"
            assert lineage.json()["items"][-1]["payload"]["reason"] == "reception_timeline_changed"

    def test_mapping_change_refuses_to_destroy_locked_dialogue_units(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        recording_id = _run_async(seed_recording(db_session_factory, recording_id=101))
        _run_async(
            _seed_recording_duration(
                db_session_factory,
                recording_id=recording_id,
                duration_sec=10.0,
            )
        )
        reception_id, _ = _run_async(_seed_dialogue_workspace(db_session_factory, locked=True))

        response = test_client.post(
            f"/api/v1/receptions/{reception_id}/merge",
            json={
                "recording_ids": [recording_id],
                "mode": "logical",
                "expected_version": 1,
            },
            headers=auth_headers["inspector_t1"],
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "LOCKED_DIALOGUE_UNITS_PRESENT"
        workspace = test_client.get(
            f"/api/v1/receptions/{reception_id}/workspace",
            headers=auth_headers["admin_t1"],
        ).json()
        assert workspace["reception"]["version"] == 1
        assert len(workspace["dialogue_units"]) == 1

    def test_geometry_idempotent_merge_keeps_derived_units(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        recording_id = _run_async(seed_recording(db_session_factory, recording_id=101))
        _run_async(
            _seed_transcript_segments(
                db_session_factory,
                recording_id=recording_id,
                items=[(0.0, 1.0, "您好"), (9.0, 9.8, "报价方案")],
            )
        )
        body = _create_body([recording_id])
        body["merge_mode"] = "logical"
        reception_id = test_client.post(
            "/api/v1/receptions",
            json=body,
            headers=auth_headers["admin_t1"],
        ).json()["id"]
        segmented = test_client.post(
            f"/api/v1/receptions/{reception_id}/segment",
            json={"expected_version": 1},
            headers=auth_headers["admin_t1"],
        )
        assert segmented.status_code == 200
        unit_ids = [unit["id"] for unit in segmented.json()["dialogue_units"]]

        merged = test_client.post(
            f"/api/v1/receptions/{reception_id}/merge",
            json={
                "recording_ids": [recording_id],
                "mode": "logical",
                "expected_version": 2,
            },
            headers=auth_headers["admin_t1"],
        )

        assert merged.status_code == 200
        workspace = test_client.get(
            f"/api/v1/receptions/{reception_id}/workspace",
            headers=auth_headers["admin_t1"],
        ).json()
        assert [unit["id"] for unit in workspace["dialogue_units"]] == unit_ids
        assert len(workspace["state_transitions"]) == len(unit_ids)


@pytest.mark.integration
class TestDialogueUnitManualEdits:
    def test_split_is_audited_and_rejects_stale_version(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        reception_id, unit_id = _run_async(_seed_dialogue_workspace(db_session_factory))
        payload = {
            "split_at_sec": 18.0,
            "expected_reception_version": 1,
            "expected_unit_version": 1,
            "reason": "人工确认主题边界",
        }

        response = test_client.post(
            f"/api/v1/receptions/{reception_id}/dialogue-units/{unit_id}/split",
            json=payload,
            headers=auth_headers["inspector_t1"],
        )

        assert response.status_code == 200
        edited = response.json()
        assert edited["reception_version"] == 2
        assert len(edited["dialogue_units"]) == 2
        assert edited["dialogue_units"][0]["end_sec"] == 18.0
        assert edited["dialogue_units"][1]["start_sec"] == 18.0
        assert edited["dialogue_units"][0]["version"] == 2
        assert edited["dialogue_units"][0]["summary"] is None
        assert edited["dialogue_units"][1]["summary"] is None
        assert edited["dialogue_units"][0]["tag_assignments"][0]["evidence_refs"]
        assert edited["dialogue_units"][1]["tag_assignments"] == []
        workspace = test_client.get(
            f"/api/v1/receptions/{reception_id}/workspace",
            headers=auth_headers["viewer_t1"],
        ).json()
        assert len(workspace["state_transitions"]) == 2
        assert workspace["state_transitions"][0]["from_state"] == "__start__"
        assert (
            workspace["state_transitions"][1]["from_state"]
            == workspace["state_transitions"][0]["to_state"]
        )
        assert {
            transition["dialogue_unit_id"] for transition in workspace["state_transitions"]
        } == {unit["id"] for unit in edited["dialogue_units"]}
        assert {
            transition["confidence"] for transition in workspace["state_transitions"]
        } == {1.0}

        stale = test_client.post(
            f"/api/v1/receptions/{reception_id}/dialogue-units/{unit_id}/split",
            json=payload,
            headers=auth_headers["inspector_t1"],
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "VERSION_CONFLICT"

        provenance = test_client.get(
            f"/api/v1/provenance/dialogue_unit/{unit_id}",
            headers=auth_headers["admin_t1"],
        )
        assert provenance.status_code == 200
        assert provenance.json()["items"][-1]["event_type"] == "split"
        assert provenance.json()["items"][-1]["actor"] == "user:2"

        right_id = edited["dialogue_units"][1]["id"]
        right_provenance = test_client.get(
            f"/api/v1/provenance/dialogue_unit/{right_id}",
            headers=auth_headers["viewer_t1"],
        )
        assert right_provenance.status_code == 200
        assert right_provenance.json()["items"][0]["event_type"] == "derived"

    def test_split_invalidates_only_affected_governance_current_and_queues_recompute(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        from sqlalchemy import func, select

        from audio_graphy.models.tag_governance import (
            TagAssignmentCurrent,
            TagAssignmentFact,
            TagExtractionJob,
        )

        reception_id, unit_id = _run_async(
            _seed_dialogue_workspace(
                db_session_factory,
                with_following_unit=True,
            )
        )
        original_unit_ids = _run_async(
            _seed_governance_current_for_reception(
                db_session_factory,
                reception_id=reception_id,
            )
        )

        response = test_client.post(
            f"/api/v1/receptions/{reception_id}/dialogue-units/{unit_id}/split",
            json={
                "split_at_sec": 18.0,
                "expected_reception_version": 1,
                "expected_unit_version": 1,
                "reason": "验证定向标签失效",
            },
            headers=auth_headers["inspector_t1"],
        )

        assert response.status_code == 200
        new_unit_id = next(
            item["id"] for item in response.json()["dialogue_units"] if item["start_sec"] == 18.0
        )

        async def inspect() -> tuple[set[int], int, list[TagExtractionJob]]:
            async with db_session_factory() as session:
                current_ids = set(
                    (
                        await session.execute(
                            select(TagAssignmentCurrent.subject_id).where(
                                TagAssignmentCurrent.tenant_id == "chang_an"
                            )
                        )
                    ).scalars()
                )
                fact_count = int(
                    (
                        await session.execute(
                            select(func.count(TagAssignmentFact.id)).where(
                                TagAssignmentFact.tenant_id == "chang_an"
                            )
                        )
                    ).scalar_one()
                )
                jobs = list(
                    (
                        await session.execute(
                            select(TagExtractionJob).where(TagExtractionJob.tenant_id == "chang_an")
                        )
                    ).scalars()
                )
                return current_ids, fact_count, jobs

        current_ids, fact_count, jobs = _run_async(inspect())
        assert current_ids == {original_unit_ids[1]}
        assert fact_count == 2
        assert len(jobs) == 1
        assert jobs[0].scope["dialogue_unit_ids"] == sorted([unit_id, new_unit_id])
        assert jobs[0].scope["cause"] == "manual_split"

    def test_merge_combines_units_tags_and_provenance(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        reception_id, unit_id = _run_async(_seed_dialogue_workspace(db_session_factory))
        split = test_client.post(
            f"/api/v1/receptions/{reception_id}/dialogue-units/{unit_id}/split",
            json={
                "split_at_sec": 18.0,
                "expected_reception_version": 1,
                "expected_unit_version": 1,
                "reason": "拆分后复核",
            },
            headers=auth_headers["admin_t1"],
        )
        units = split.json()["dialogue_units"]
        right = units[1]

        merged = test_client.post(
            f"/api/v1/receptions/{reception_id}/dialogue-units/{unit_id}/merge",
            json={
                "other_unit_id": right["id"],
                "expected_reception_version": 2,
                "expected_unit_version": 2,
                "expected_other_unit_version": 1,
                "reason": "复核后确认属于同一主题",
            },
            headers=auth_headers["admin_t1"],
        )

        assert merged.status_code == 200
        data = merged.json()
        assert data["reception_version"] == 3
        assert len(data["dialogue_units"]) == 1
        assert data["dialogue_units"][0]["start_sec"] == 0.0
        assert data["dialogue_units"][0]["end_sec"] == 40.0
        assert data["dialogue_units"][0]["tag_assignments"][0]["evidence_refs"]
        workspace = test_client.get(
            f"/api/v1/receptions/{reception_id}/workspace",
            headers=auth_headers["viewer_t1"],
        ).json()
        assert len(workspace["state_transitions"]) == 1
        assert workspace["state_transitions"][0]["from_state"] == "__start__"
        assert (
            workspace["state_transitions"][0]["dialogue_unit_id"] == data["dialogue_units"][0]["id"]
        )

        events = test_client.get(
            f"/api/v1/provenance/dialogue_unit/{unit_id}",
            headers=auth_headers["viewer_t1"],
        ).json()["items"]
        assert [event["event_type"] for event in events][-2:] == ["split", "merged"]
        assert len(events[-1]["parent_refs"]) == 2

        removed_events = test_client.get(
            f"/api/v1/provenance/dialogue_unit/{right['id']}",
            headers=auth_headers["viewer_t1"],
        )
        assert removed_events.status_code == 200
        assert removed_events.json()["items"][-1]["event_type"] == "superseded"
        assert removed_events.json()["items"][-1]["parent_refs"] == [
            {
                "type": "dialogue_unit",
                "id": right["id"],
                "version": 1,
            }
        ]
        own_agent_events = test_client.get(
            f"/api/v1/provenance/dialogue_unit/{right['id']}",
            headers=auth_headers["agent_t1"],
        )
        assert own_agent_events.status_code == 200
        cross_tenant_events = test_client.get(
            f"/api/v1/provenance/dialogue_unit/{right['id']}",
            headers=auth_headers["admin_t2"],
        )
        assert cross_tenant_events.status_code == 404

    def test_merge_resolves_label_conflict_without_mixing_evidence(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        reception_id, unit_id = _run_async(_seed_dialogue_workspace(db_session_factory))
        split = test_client.post(
            f"/api/v1/receptions/{reception_id}/dialogue-units/{unit_id}/split",
            json={
                "split_at_sec": 18.0,
                "expected_reception_version": 1,
                "expected_unit_version": 1,
                "reason": "构造冲突标签",
            },
            headers=auth_headers["admin_t1"],
        )
        right = split.json()["dialogue_units"][1]
        _run_async(
            _seed_conflicting_dialogue_tag(
                db_session_factory,
                reception_id=reception_id,
                dialogue_unit_id=right["id"],
            )
        )

        merged = test_client.post(
            f"/api/v1/receptions/{reception_id}/dialogue-units/{unit_id}/merge",
            json={
                "other_unit_id": right["id"],
                "expected_reception_version": 2,
                "expected_unit_version": 2,
                "expected_other_unit_version": 1,
                "reason": "合并并按人工优先级消解",
            },
            headers=auth_headers["admin_t1"],
        )

        assert merged.status_code == 200
        assignments = merged.json()["dialogue_units"][0]["tag_assignments"]
        assert len(assignments) == 1
        assert assignments[0]["label_value"] == "fail"
        assert assignments[0]["evidence_refs"] == [
            {
                "recording_id": 101,
                "timeline_start_sec": 20.0,
                "timeline_end_sec": 25.0,
                "coordinate_space": "reception_timeline",
            }
        ]
        events = test_client.get(
            f"/api/v1/provenance/dialogue_unit/{unit_id}",
            headers=auth_headers["viewer_t1"],
        ).json()["items"]
        conflict = events[-1]["payload"]["tag_conflicts"][0]
        assert conflict["values"] == ["fail", "pass"]
        assert conflict["selected_value"] == "fail"

    def test_split_keeps_unit_indices_in_timeline_order(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        reception_id, unit_id = _run_async(
            _seed_dialogue_workspace(
                db_session_factory,
                with_following_unit=True,
            )
        )

        response = test_client.post(
            f"/api/v1/receptions/{reception_id}/dialogue-units/{unit_id}/split",
            json={
                "split_at_sec": 18.0,
                "expected_reception_version": 1,
                "expected_unit_version": 1,
                "reason": "插入人工边界",
            },
            headers=auth_headers["admin_t1"],
        )

        assert response.status_code == 200
        units = response.json()["dialogue_units"]
        assert [unit["unit_index"] for unit in units] == [0, 1, 2]
        assert [(unit["start_sec"], unit["end_sec"]) for unit in units] == [
            (0.0, 18.0),
            (18.0, 40.0),
            (40.0, 60.0),
        ]

    def test_locked_unit_cannot_be_split(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        reception_id, unit_id = _run_async(
            _seed_dialogue_workspace(db_session_factory, locked=True)
        )
        response = test_client.post(
            f"/api/v1/receptions/{reception_id}/dialogue-units/{unit_id}/split",
            json={
                "split_at_sec": 18.0,
                "expected_reception_version": 1,
                "expected_unit_version": 1,
                "reason": "attempt",
            },
            headers=auth_headers["inspector_t1"],
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DIALOGUE_UNIT_LOCKED"

    def test_state_transition_and_workspace_include_versioned_evidence(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        reception_id, _ = _run_async(_seed_dialogue_workspace(db_session_factory))

        transitions = test_client.get(
            f"/api/v1/receptions/{reception_id}/state-transitions",
            headers=auth_headers["agent_t1"],
        )
        assert transitions.status_code == 200
        assert transitions.json()["items"][0]["from_state"] == "接待问候"
        assert transitions.json()["items"][0]["evidence_refs"][0]["start_sec"] == 5.0

        workspace = test_client.get(
            f"/api/v1/receptions/{reception_id}/workspace",
            headers=auth_headers["agent_t1"],
        )
        unit = workspace.json()["dialogue_units"][0]
        assert unit["version"] == 1
        assert unit["tag_assignments"][0]["group_version"] == "v1"
        assert unit["tag_assignments"][0]["evidence_refs"][0]["recording_id"] == 101
        assert workspace.json()["tag_assignments"][0]["dialogue_unit_id"] == unit["id"]
        assert workspace.json()["provenance_events"][0]["event_type"] == "derived"
        window = workspace.json()["window"]
        assert window["truncated"] is False
        assert window["dialogue_units"]["total"] == 1
        assert window["dialogue_units"]["returned"] == 1
        assert window["has_next"] is False

    def test_workspace_projects_canonical_current_and_keeps_legacy_supplements(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        reception_id, unit_id = _run_async(_seed_dialogue_workspace(db_session_factory))
        _run_async(
            _seed_governance_current_for_reception(
                db_session_factory,
                reception_id=reception_id,
            )
        )

        response = test_client.get(
            f"/api/v1/receptions/{reception_id}/workspace",
            headers=auth_headers["agent_t1"],
        )

        assert response.status_code == 200
        assignments = response.json()["dialogue_units"][0]["tag_assignments"]
        assert {item["label_key"] for item in assignments} == {
            "intent",
            "needs_discovery",
        }
        canonical = next(item for item in assignments if item["label_key"] == "intent")
        assert canonical["dialogue_unit_id"] == unit_id
        assert canonical["label_value"] == "purchase"
        assert canonical["group_key"] == "canonical"
        assert canonical["group_version"] == "schema:1|tagger:2"
        assert canonical["model_run_id"].startswith("fact:")
        assert canonical["evidence_refs"][0] == {
            "segment_id": unit_id,
            "start_sec": 1,
            "end_sec": 2,
            "ref_id": f"segment:{unit_id}",
            "kind": "audio",
            "coordinate_space": "source",
            "start_ms": 1_000,
            "source_start_ms": 1_000,
            "end_ms": 2_000,
            "source_end_ms": 2_000,
        }
        assert response.json()["window"]["tag_assignments"]["total"] == 2

    def test_workspace_canonical_current_overrides_same_key_legacy_assignment(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        reception_id, _unit_id = _run_async(_seed_dialogue_workspace(db_session_factory))
        _run_async(
            _seed_governance_current_for_reception(
                db_session_factory,
                reception_id=reception_id,
                tag_key="needs_discovery",
                tag_value="canonical-pass",
            )
        )

        response = test_client.get(
            f"/api/v1/receptions/{reception_id}/workspace",
            headers=auth_headers["agent_t1"],
        )

        assert response.status_code == 200
        assignments = response.json()["tag_assignments"]
        assert len(assignments) == 1
        assert assignments[0]["group_key"] == "canonical"
        assert assignments[0]["label_key"] == "needs_discovery"
        assert assignments[0]["label_value"] == "canonical-pass"
        assert response.json()["window"]["tag_assignments"]["total"] == 1

    def test_workspace_tombstone_current_suppresses_same_key_legacy_assignment(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        reception_id, _unit_id = _run_async(_seed_dialogue_workspace(db_session_factory))
        _run_async(
            _seed_governance_current_for_reception(
                db_session_factory,
                reception_id=reception_id,
                tag_key="needs_discovery",
                tag_value="rejected",
                tombstone=True,
            )
        )

        response = test_client.get(
            f"/api/v1/receptions/{reception_id}/workspace",
            headers=auth_headers["agent_t1"],
        )

        assert response.status_code == 200
        assert response.json()["tag_assignments"] == []
        assert response.json()["dialogue_units"][0]["tag_assignments"] == []
        assert response.json()["window"]["tag_assignments"]["total"] == 0

    def test_workspace_can_browse_a_long_reception_by_adjacent_time_windows(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        recording_id = _run_async(seed_recording(db_session_factory, recording_id=909))
        reception_id = _run_async(
            _seed_long_dialogue_workspace(
                db_session_factory,
                recording_id=recording_id,
                item_count=105,
            )
        )
        operation_id = _run_async(
            _seed_active_audio_operation(
                db_session_factory,
                reception_id=reception_id,
            )
        )

        first = test_client.get(
            f"/api/v1/receptions/{reception_id}/workspace",
            params={"window_start_sec": 0, "window_size_sec": 60},
            headers=auth_headers["agent_t1"],
        )

        assert first.status_code == 200
        first_payload = first.json()
        assert len(first.content) < 5 * 1024 * 1024
        assert first_payload["window"]["start_sec"] == 0
        assert first_payload["window"]["end_sec"] == 60
        assert first_payload["window"]["has_previous"] is False
        assert first_payload["window"]["has_next"] is True
        assert first_payload["window"]["next_start_sec"] == 60
        assert first_payload["neighbors"]["previous_dialogue_unit"] is None
        assert first_payload["neighbors"]["next_dialogue_unit"]["unit_index"] == 60
        assert first_payload["active_audio_operation"] == {
            "id": operation_id,
            "reception_id": reception_id,
            "status": "assembling",
            "mode": "both",
            "progress": 0.55,
            "error": None,
            "created_at": first_payload["active_audio_operation"]["created_at"],
            "updated_at": first_payload["active_audio_operation"]["updated_at"],
        }
        assert first_payload["capabilities"] == {
            "can_manage_audio": False,
            "can_run_segmentation": False,
            "can_edit_dialogue": False,
            "can_edit_tags": False,
            "supports_audio_plans": False,
            "supports_audio_operations": False,
            "can_cancel_audio_operation": False,
            "can_stream_audio": False,
        }
        for collection in (
            "dialogue_units",
            "tag_assignments",
            "state_transitions",
            "transcript_items",
            "provenance_events",
        ):
            assert first_payload["window"][collection]["total"] == 60
            assert first_payload["window"][collection]["returned"] == 60
            assert first_payload["window"][collection]["truncated"] is False
            assert len(first_payload[collection]) == 60

        second = test_client.get(
            f"/api/v1/receptions/{reception_id}/workspace",
            params={"window_start_sec": 60, "window_size_sec": 60},
            headers=auth_headers["agent_t1"],
        )

        assert second.status_code == 200
        second_payload = second.json()
        assert second_payload["window"]["start_sec"] == 60
        assert second_payload["window"]["end_sec"] == 105
        assert second_payload["window"]["previous_start_sec"] == 0
        assert second_payload["window"]["has_previous"] is True
        assert second_payload["window"]["has_next"] is False
        assert second_payload["neighbors"]["previous_dialogue_unit"]["unit_index"] == 59
        assert second_payload["neighbors"]["next_dialogue_unit"] is None
        for collection in (
            "dialogue_units",
            "tag_assignments",
            "state_transitions",
            "transcript_items",
            "provenance_events",
        ):
            assert second_payload["window"][collection]["total"] == 45
            assert second_payload["window"][collection]["returned"] == 45
            assert second_payload["window"][collection]["truncated"] is False
            assert len(second_payload[collection]) == 45

        assert {item["id"] for item in first_payload["dialogue_units"]}.isdisjoint(
            item["id"] for item in second_payload["dialogue_units"]
        )
        assert {item["id"] for item in first_payload["tag_assignments"]}.isdisjoint(
            item["id"] for item in second_payload["tag_assignments"]
        )

    def test_workspace_hard_caps_dense_window_collections_and_response_bytes(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        recording_id = _run_async(seed_recording(db_session_factory, recording_id=910))
        reception_id = _run_async(
            _seed_long_dialogue_workspace(
                db_session_factory,
                recording_id=recording_id,
                item_count=105,
            )
        )

        response = test_client.get(
            f"/api/v1/receptions/{reception_id}/workspace",
            params={"window_start_sec": 0, "window_size_sec": 3_600},
            headers=auth_headers["agent_t1"],
        )

        assert response.status_code == 200
        payload = response.json()
        assert len(response.content) < 5 * 1024 * 1024
        assert payload["window"]["truncated"] is True
        assert payload["window"]["dialogue_units"] == {
            "total": 105,
            "returned": 100,
            "limit": 100,
            "truncated": True,
        }
        assert payload["window"]["tag_assignments"] == {
            "total": 105,
            "returned": 100,
            "limit": 200,
            "truncated": True,
        }
        assert payload["window"]["state_transitions"]["returned"] == 100
        assert payload["window"]["provenance_events"]["returned"] == 100
        assert len(payload["dialogue_units"]) == 100
        assert len(payload["tag_assignments"]) == 100
        assert len(payload["state_transitions"]) == 100
        assert len(payload["provenance_events"]) == 100

        returned_unit_ids = {item["id"] for item in payload["dialogue_units"]}
        assert {
            item["dialogue_unit_id"] for item in payload["tag_assignments"]
        } <= returned_unit_ids
        assert {
            item["dialogue_unit_id"]
            for item in payload["state_transitions"]
            if item["dialogue_unit_id"] is not None
        } <= returned_unit_ids
        assert {
            int(item["object_ref"])
            for item in payload["provenance_events"]
            if item["object_type"] == "dialogue_unit"
        } <= returned_unit_ids

    def test_workspace_rejects_windows_above_the_response_budget(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        reception_id, _ = _run_async(_seed_dialogue_workspace(db_session_factory))

        response = test_client.get(
            f"/api/v1/receptions/{reception_id}/workspace",
            params={"window_size_sec": 3_601},
            headers=auth_headers["agent_t1"],
        )

        assert response.status_code == 422

    def test_state_transition_endpoint_queries_directly_with_a_bounded_page(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        recording_id = _run_async(seed_recording(db_session_factory, recording_id=911))
        reception_id = _run_async(
            _seed_long_dialogue_workspace(
                db_session_factory,
                recording_id=recording_id,
                item_count=105,
            )
        )

        first = test_client.get(
            f"/api/v1/receptions/{reception_id}/state-transitions",
            params={"page": 1, "page_size": 100},
            headers=auth_headers["agent_t1"],
        )
        second = test_client.get(
            f"/api/v1/receptions/{reception_id}/state-transitions",
            params={"page": 2, "page_size": 100},
            headers=auth_headers["agent_t1"],
        )

        assert first.status_code == 200
        assert first.json()["total"] == 105
        assert first.json()["truncated"] is True
        assert len(first.json()["items"]) == 100
        assert second.status_code == 200
        assert second.json()["total"] == 105
        assert second.json()["truncated"] is True
        assert len(second.json()["items"]) == 5

        over_budget = test_client.get(
            f"/api/v1/receptions/{reception_id}/state-transitions",
            params={"page_size": 201},
            headers=auth_headers["agent_t1"],
        )
        assert over_budget.status_code == 422

    def test_provenance_endpoint_pages_an_append_only_chain(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        reception_id, _ = _run_async(_seed_dialogue_workspace(db_session_factory))
        _run_async(
            _seed_reception_provenance_events(
                db_session_factory,
                reception_id=reception_id,
                item_count=105,
            )
        )

        first = test_client.get(
            f"/api/v1/provenance/reception/{reception_id}",
            params={"page": 1, "page_size": 100},
            headers=auth_headers["agent_t1"],
        )
        second = test_client.get(
            f"/api/v1/provenance/reception/{reception_id}",
            params={"page": 2, "page_size": 100},
            headers=auth_headers["agent_t1"],
        )

        assert first.status_code == 200
        assert first.json()["total"] == 105
        assert first.json()["truncated"] is True
        assert len(first.json()["items"]) == 100
        assert second.status_code == 200
        assert second.json()["total"] == 105
        assert second.json()["truncated"] is True
        assert len(second.json()["items"]) == 5

        over_budget = test_client.get(
            f"/api/v1/provenance/reception/{reception_id}",
            params={"page_size": 201},
            headers=auth_headers["agent_t1"],
        )
        assert over_budget.status_code == 422


@pytest.mark.integration
class TestReceptionAutomaticSegmentation:
    def test_segment_uses_persisted_transcript_and_writes_transitions_and_lineage(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        recording_id = _run_async(seed_recording(db_session_factory, recording_id=101))
        _run_async(
            _seed_transcript_segments(
                db_session_factory,
                recording_id=recording_id,
                items=[
                    (0.0, 0.5, "您好，欢迎光临，联系电话 13812345678"),
                    (9.0, 9.8, "这款车价格有优惠，可以安排试驾"),
                ],
            )
        )
        body = _create_body([recording_id])
        body["merge_mode"] = "logical"
        created = test_client.post(
            "/api/v1/receptions",
            json=body,
            headers=auth_headers["admin_t1"],
        ).json()

        segmented = test_client.post(
            f"/api/v1/receptions/{created['id']}/segment",
            json={
                "expected_version": 1,
                "replace_auto": False,
                "algorithm_version": "dialogue-hybrid-v1",
            },
            headers=auth_headers["inspector_t1"],
        )

        assert segmented.status_code == 200
        assert segmented.json()["reception_version"] == 2
        units = segmented.json()["dialogue_units"]
        assert len(units) == 2
        assert [unit["unit_index"] for unit in units] == [0, 1]
        assert units[0]["summary"] == "您好，欢迎光临，联系电话 138****5678"
        assert "13812345678" not in units[0]["summary"]
        assert units[1]["segment_refs"][0]["recording_id"] == recording_id
        assert units[1]["segment_refs"][0]["timeline_start_sec"] == 9.0

        workspace = test_client.get(
            f"/api/v1/receptions/{created['id']}/workspace",
            headers=auth_headers["agent_t1"],
        ).json()
        assert len(workspace["state_transitions"]) == 2
        assert workspace["transcript_items"][0]["text"] == ("您好，欢迎光临，联系电话 138****5678")
        assert "13812345678" not in workspace["transcript_items"][0]["text"]
        assert workspace["state_transitions"][0]["from_state"] == "__start__"
        assert (
            workspace["state_transitions"][1]["evidence_refs"][0]["segment_id"]
            == workspace["transcript_items"][1]["segment_id"]
        )
        derived = [
            event for event in workspace["provenance_events"] if event["event_type"] == "derived"
        ]
        assert any(event["object_type"] == "dialogue_unit" for event in derived)
        assert any(event["object_type"] == "reception" for event in derived)

        stale = test_client.post(
            f"/api/v1/receptions/{created['id']}/segment",
            json={"expected_version": 1, "replace_auto": True},
            headers=auth_headers["admin_t1"],
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "VERSION_CONFLICT"

    def test_segment_replace_auto_is_explicit_and_locked_units_are_protected(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        recording_id = _run_async(seed_recording(db_session_factory, recording_id=101))
        _run_async(
            _seed_transcript_segments(
                db_session_factory,
                recording_id=recording_id,
                items=[(0.0, 1.0, "您好"), (9.2, 9.9, "报价和金融方案")],
            )
        )
        body = _create_body([recording_id])
        body["merge_mode"] = "logical"
        reception_id = test_client.post(
            "/api/v1/receptions",
            json=body,
            headers=auth_headers["admin_t1"],
        ).json()["id"]
        first = test_client.post(
            f"/api/v1/receptions/{reception_id}/segment",
            json={"expected_version": 1},
            headers=auth_headers["admin_t1"],
        )
        assert first.status_code == 200

        implicit_replace = test_client.post(
            f"/api/v1/receptions/{reception_id}/segment",
            json={"expected_version": 2, "replace_auto": False},
            headers=auth_headers["admin_t1"],
        )
        assert implicit_replace.status_code == 409
        assert implicit_replace.json()["error"]["code"] == "DIALOGUE_UNITS_EXIST"

        explicit_replace = test_client.post(
            f"/api/v1/receptions/{reception_id}/segment",
            json={"expected_version": 2, "replace_auto": True},
            headers=auth_headers["inspector_t1"],
        )
        assert explicit_replace.status_code == 200
        assert explicit_replace.json()["reception_version"] == 3

        locked_reception_id, _ = _run_async(
            _seed_dialogue_workspace(db_session_factory, locked=True)
        )
        locked = test_client.post(
            f"/api/v1/receptions/{locked_reception_id}/segment",
            json={"expected_version": 1, "replace_auto": True},
            headers=auth_headers["admin_t1"],
        )
        assert locked.status_code == 409
        assert locked.json()["error"]["code"] == "LOCKED_DIALOGUE_UNITS_PRESENT"

    def test_segment_requires_real_segments_and_write_role(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        recording_id = _run_async(seed_recording(db_session_factory, recording_id=101))
        body = _create_body([recording_id])
        body["merge_mode"] = "logical"
        reception_id = test_client.post(
            "/api/v1/receptions",
            json=body,
            headers=auth_headers["admin_t1"],
        ).json()["id"]

        forbidden = test_client.post(
            f"/api/v1/receptions/{reception_id}/segment",
            json={"expected_version": 1},
            headers=auth_headers["agent_t1"],
        )
        assert forbidden.status_code == 403

        missing = test_client.post(
            f"/api/v1/receptions/{reception_id}/segment",
            json={"expected_version": 1},
            headers=auth_headers["admin_t1"],
        )
        assert missing.status_code == 422
        assert missing.json()["error"]["code"] == "NO_SEGMENTS_FOR_RECEPTION"


@pytest.mark.integration
class TestReceptionMergeProposals:
    def test_proposal_explains_groups_without_writing_a_reception(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        first = _run_async(seed_recording(db_session_factory, recording_id=101))
        second = _run_async(seed_recording(db_session_factory, recording_id=102))
        _run_async(
            _seed_recording_duration(
                db_session_factory,
                recording_id=first,
                duration_sec=5.0,
            )
        )
        _run_async(
            _seed_recording_duration(
                db_session_factory,
                recording_id=second,
                duration_sec=6.0,
            )
        )
        proposal_start = datetime(2026, 7, 23, 9, 0, tzinfo=UTC)
        _run_async(
            _set_recording_times(
                db_session_factory,
                values={
                    first: proposal_start,
                    second: proposal_start + timedelta(seconds=7),
                },
            )
        )

        response = test_client.post(
            "/api/v1/receptions/proposals",
            json={"recording_ids": [first, second]},
            headers=auth_headers["inspector_t1"],
        )

        assert response.status_code == 200
        data = response.json()
        assert data["recording_ids"] == [first, second]
        assert data["proposals"][0]["decision"] == "merge"
        assert "same_customer_voiceprint" in {
            reason["code"] for reason in data["proposals"][0]["reasons"]
        }
        assert data["groups"][0]["recording_ids"] == [first, second]
        assert _run_async(_reception_count(db_session_factory)) == 0

    def test_proposal_hard_rejects_cross_tenant_and_cross_store_merge(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        local = _run_async(seed_recording(db_session_factory, recording_id=101))
        other_store = _run_async(
            seed_recording(
                db_session_factory,
                recording_id=102,
                store_id="S002",
            )
        )
        foreign = _run_async(
            seed_recording(
                db_session_factory,
                recording_id=201,
                tenant_id="byd",
                store_id="S001",
                agent_name="agent_byd",
            )
        )
        for recording_id in (local, other_store):
            _run_async(
                _seed_recording_duration(
                    db_session_factory,
                    recording_id=recording_id,
                    duration_sec=5.0,
                )
            )

        rejected = test_client.post(
            "/api/v1/receptions/proposals",
            json={"recording_ids": [local, other_store]},
            headers=auth_headers["admin_t1"],
        )
        assert rejected.status_code == 200
        assert rejected.json()["proposals"][0]["decision"] == "reject"
        assert rejected.json()["proposals"][0]["reasons"][0]["code"] == "store_mismatch"
        assert rejected.json()["groups"] == []

        forced = test_client.post(
            "/api/v1/receptions/proposals",
            json={
                "recording_ids": [local, other_store],
                "force_merge": [[local, other_store]],
            },
            headers=auth_headers["admin_t1"],
        )
        assert forced.status_code == 422
        assert forced.json()["error"]["code"] == "RECEPTION_STORE_MISMATCH"

        hidden = test_client.post(
            "/api/v1/receptions/proposals",
            json={"recording_ids": [local, foreign]},
            headers=auth_headers["admin_t1"],
        )
        assert hidden.status_code == 404
        assert hidden.json()["error"]["code"] == "RECORDING_NOT_FOUND"

    def test_manual_force_split_overrides_an_automatic_merge(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        first = _run_async(seed_recording(db_session_factory, recording_id=101))
        second = _run_async(seed_recording(db_session_factory, recording_id=102))
        for recording_id in (first, second):
            _run_async(
                _seed_recording_duration(
                    db_session_factory,
                    recording_id=recording_id,
                    duration_sec=5.0,
                )
            )
        proposal_start = datetime(2026, 7, 23, 9, 0, tzinfo=UTC)
        _run_async(
            _set_recording_times(
                db_session_factory,
                values={
                    first: proposal_start,
                    second: proposal_start + timedelta(seconds=6),
                },
            )
        )

        response = test_client.post(
            "/api/v1/receptions/proposals",
            json={
                "recording_ids": [first, second],
                "force_split": [[first, second]],
            },
            headers=auth_headers["admin_t1"],
        )

        assert response.status_code == 200
        proposal = response.json()["proposals"][0]
        assert proposal["decision"] == "reject"
        assert proposal["manual_override"] is True
        assert proposal["reasons"][0]["code"] == "manual_force_split"
        assert response.json()["groups"] == []


@pytest.mark.integration
class TestReceptionAudioStreaming:
    def test_unknown_duration_partial_mapping_fails_closed_to_clipped_playback(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        recording_id = _run_async(seed_recording(db_session_factory, recording_id=102))
        audio_path = Path(test_client.app.state.settings.working_dir) / "unknown-source.wav"
        with wave.open(str(audio_path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16_000)
            output.writeframes(b"\x01\x00" * 16_000)
        _run_async(
            _set_recording_path(
                db_session_factory,
                recording_id=recording_id,
                path=audio_path,
            )
        )
        body = _create_body([recording_id])
        body["merge_mode"] = "logical"
        body["recordings"][0].update(
            {
                "timeline_end_sec": 0.5,
                "source_end_sec": 0.5,
                "merge_reasons": {},
            }
        )
        created = test_client.post(
            "/api/v1/receptions",
            json=body,
            headers=auth_headers["admin_t1"],
        ).json()

        streamed = test_client.get(
            created["recordings"][0]["audio_url"],
            headers=auth_headers["agent_t1"],
        )

        assert streamed.status_code == 200
        clipped_path = Path(test_client.app.state.settings.working_dir) / "unknown-clip.wav"
        clipped_path.write_bytes(streamed.content)
        try:
            with wave.open(str(clipped_path), "rb") as clipped:
                assert clipped.getnframes() == 8_000
        finally:
            clipped_path.unlink(missing_ok=True)

    def test_recording_split_playback_is_materialized_to_its_legal_source_span(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        recording_id = _run_async(seed_recording(db_session_factory, recording_id=101))
        audio_path = Path(test_client.app.state.settings.working_dir) / "split-source.wav"
        with wave.open(str(audio_path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16_000)
            output.writeframes(b"\x01\x00" * 16_000)
        _run_async(
            _set_recording_path(
                db_session_factory,
                recording_id=recording_id,
                path=audio_path,
            )
        )
        body = _create_body([recording_id])
        body["merge_mode"] = "logical"
        body["recordings"][0].update(
            {
                "timeline_end_sec": 0.5,
                "source_start_sec": 0.25,
                "source_end_sec": 0.75,
                "merge_reasons": {"candidate_type": "recording_split"},
            }
        )
        created = test_client.post(
            "/api/v1/receptions",
            json=body,
            headers=auth_headers["admin_t1"],
        ).json()

        source_contract = created["recordings"][0]
        streamed = test_client.get(
            source_contract["audio_url"],
            headers=auth_headers["agent_t1"],
        )

        assert streamed.status_code == 200
        assert source_contract["time_origin_ms"] == -250
        assert source_contract["legal_source_start_ms"] == 250
        assert source_contract["legal_source_end_ms"] == 750
        assert streamed.headers["x-time-origin-ms"] == "-250"
        assert streamed.headers["x-legal-source-start-ms"] == "250"
        assert streamed.headers["x-legal-source-end-ms"] == "750"
        clipped_path = Path(test_client.app.state.settings.working_dir) / "clipped-response.wav"
        clipped_path.write_bytes(streamed.content)
        try:
            with wave.open(str(clipped_path), "rb") as clipped:
                assert clipped.getframerate() == 16_000
                assert clipped.getnframes() == 8_000
        finally:
            clipped_path.unlink(missing_ok=True)

    def test_audio_url_supports_full_and_single_range_requests(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        recording_id = _run_async(seed_recording(db_session_factory, recording_id=101))
        audio_path = Path(test_client.app.state.settings.working_dir) / "tenant-a" / "source.wav"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"0123456789")
        _run_async(
            _set_recording_path(
                db_session_factory,
                recording_id=recording_id,
                path=audio_path,
            )
        )
        _run_async(
            _seed_recording_duration(
                db_session_factory,
                recording_id=recording_id,
                duration_sec=10.0,
            )
        )
        body = _create_body([recording_id])
        body["merge_mode"] = "logical"
        created = test_client.post(
            "/api/v1/receptions",
            json=body,
            headers=auth_headers["admin_t1"],
        ).json()
        source_contract = created["recordings"][0]
        assert source_contract["playback_expires_at"] is not None
        assert source_contract["source_start_ms"] == 0
        assert source_contract["source_end_ms"] == 10_000
        assert source_contract["timeline_start_ms"] == 0
        assert source_contract["timeline_end_ms"] == 10_000
        assert source_contract["gap_before_ms"] == 0
        assert source_contract["time_origin_ms"] == 0
        assert source_contract["legal_source_start_ms"] == 0
        assert source_contract["legal_source_end_ms"] == 10_000
        audio_url = source_contract["audio_url"]

        full = test_client.get(audio_url, headers=auth_headers["agent_t1"])
        assert full.status_code == 200
        assert full.content == b"0123456789"
        assert full.headers["accept-ranges"] == "bytes"
        assert full.headers["content-type"].startswith("audio/wav")
        assert full.headers["content-length"] == "10"
        assert (
            full.headers["x-audio-grant-expires-at"]
            == source_contract["playback_expires_at"]
        )
        assert full.headers["x-time-origin-ms"] == "0"
        assert full.headers["x-legal-source-start-ms"] == "0"
        assert full.headers["x-legal-source-end-ms"] == "10000"

        partial = test_client.get(
            audio_url,
            headers={**auth_headers["agent_t1"], "Range": "bytes=2-5"},
        )
        assert partial.status_code == 206
        assert partial.content == b"2345"
        assert partial.headers["content-range"] == "bytes 2-5/10"
        assert partial.headers["content-length"] == "4"
        assert (
            partial.headers["x-audio-grant-expires-at"]
            == source_contract["playback_expires_at"]
        )

        suffix = test_client.get(
            audio_url,
            headers={**auth_headers["admin_t1"], "Range": "bytes=-3"},
        )
        assert suffix.status_code == 206
        assert suffix.content == b"789"

        native_audio = test_client.get(
            audio_url,
            headers={"Range": "bytes=4-7"},
        )
        assert native_audio.status_code == 206
        assert native_audio.content == b"4567"
        assert native_audio.headers["content-range"] == "bytes 4-7/10"

    def test_playback_grant_rejects_expiry_tampering_and_cross_tenant_scope(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        import time

        from audio_graphy.services.receptions import create_playback_grant

        recording_id = _run_async(seed_recording(db_session_factory, recording_id=101))
        audio_path = Path(test_client.app.state.settings.working_dir) / "signed-source.wav"
        audio_path.write_bytes(b"0123456789")
        _run_async(
            _set_recording_path(
                db_session_factory,
                recording_id=recording_id,
                path=audio_path,
            )
        )
        body = _create_body([recording_id])
        body["merge_mode"] = "logical"
        created = test_client.post(
            "/api/v1/receptions",
            json=body,
            headers=auth_headers["admin_t1"],
        ).json()
        signed_url = created["recordings"][0]["audio_url"]
        resource_path = signed_url.split("?", 1)[0]
        grant = signed_url.rsplit("=", 1)[1]

        tampered_last = "A" if grant[-1] != "A" else "B"
        tampered = test_client.get(f"{resource_path}?playback_grant={grant[:-1]}{tampered_last}")
        assert tampered.status_code == 401

        expired_grant = create_playback_grant(
            secret=str(test_client.app.state.settings.jwt_secret),
            subject_id=1,
            tenant_id="chang_an",
            role="admin",
            path=resource_path,
            now=int(time.time()) - 1_801,
        )
        expired = test_client.get(f"{resource_path}?playback_grant={expired_grant}")
        assert expired.status_code == 401

        foreign_grant = create_playback_grant(
            secret=str(test_client.app.state.settings.jwt_secret),
            subject_id=5,
            tenant_id="byd",
            role="admin",
            path=resource_path,
        )
        cross_tenant = test_client.get(f"{resource_path}?playback_grant={foreign_grant}")
        assert cross_tenant.status_code == 404

        changed_path = resource_path.replace(
            f"/recordings/{recording_id}/",
            "/recordings/999/",
        )
        wrong_resource = test_client.get(f"{changed_path}?playback_grant={grant}")
        assert wrong_resource.status_code == 401

    def test_audio_stream_rejects_multiple_ranges_and_cross_tenant_access(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        recording_id = _run_async(seed_recording(db_session_factory, recording_id=101))
        audio_path = Path(test_client.app.state.settings.working_dir) / "source.wav"
        audio_path.write_bytes(b"0123456789")
        _run_async(
            _set_recording_path(
                db_session_factory,
                recording_id=recording_id,
                path=audio_path,
            )
        )
        _run_async(
            _seed_recording_duration(
                db_session_factory,
                recording_id=recording_id,
                duration_sec=10.0,
            )
        )
        body = _create_body([recording_id])
        body["merge_mode"] = "logical"
        created = test_client.post(
            "/api/v1/receptions",
            json=body,
            headers=auth_headers["admin_t1"],
        ).json()
        audio_url = created["recordings"][0]["audio_url"]

        invalid = test_client.get(
            audio_url,
            headers={**auth_headers["admin_t1"], "Range": "bytes=0-1,4-5"},
        )
        assert invalid.status_code == 416
        assert invalid.headers["content-range"] == "bytes */10"

        cross_tenant = test_client.get(
            audio_url,
            headers=auth_headers["admin_t2"],
        )
        assert cross_tenant.status_code == 404

    def test_audio_stream_hides_database_paths_outside_configured_root(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
        tmp_path: Path,
    ) -> None:
        recording_id = _run_async(seed_recording(db_session_factory, recording_id=101))
        outside = tmp_path / "outside.wav"
        outside.write_bytes(b"sensitive-audio")
        _run_async(
            _set_recording_path(
                db_session_factory,
                recording_id=recording_id,
                path=outside,
            )
        )
        body = _create_body([recording_id])
        body["merge_mode"] = "logical"
        created = test_client.post(
            "/api/v1/receptions",
            json=body,
            headers=auth_headers["admin_t1"],
        ).json()

        response = test_client.get(
            created["recordings"][0]["audio_url"],
            headers=auth_headers["admin_t1"],
        )
        assert response.status_code == 404
        assert str(outside) not in response.text


class TestReceptionAudioPlanOperations:
    def test_audio_plan_fails_closed_without_verified_source_duration(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        recording_id = _run_async(
            seed_recording(db_session_factory, recording_id=200)
        )
        body = _create_body([recording_id])
        body["merge_mode"] = "logical"
        created_response = test_client.post(
            "/api/v1/receptions",
            json=body,
            headers=auth_headers["admin_t1"],
        )
        assert created_response.status_code == 201
        created = created_response.json()

        response = test_client.post(
            f"/api/v1/receptions/{created['id']}/audio-plans",
            json={
                "expected_version": 1,
                "sources": [
                    {
                        "mapping_id": created["recordings"][0]["mapping_id"],
                        "gap_before_ms": 0,
                    }
                ],
            },
            headers=auth_headers["admin_t1"],
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "RECORDING_DURATION_UNAVAILABLE"

    def test_signed_plan_and_logical_operation_publish_one_revision(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        import time

        first_id = _run_async(
            seed_recording(db_session_factory, recording_id=201)
        )
        second_id = _run_async(
            seed_recording(db_session_factory, recording_id=202)
        )
        _run_async(
            _set_recording_source_facts(
                db_session_factory,
                recording_ids=[first_id, second_id],
            )
        )
        body = _create_body([first_id, second_id])
        body["merge_mode"] = "logical"
        created_response = test_client.post(
            "/api/v1/receptions",
            json=body,
            headers=auth_headers["admin_t1"],
        )
        assert created_response.status_code == 201
        created = created_response.json()
        reception_id = created["id"]
        mapping_by_recording = {
            int(item["recording_id"]): int(item["mapping_id"])
            for item in created["recordings"]
        }

        plan_response = test_client.post(
            f"/api/v1/receptions/{reception_id}/audio-plans",
            json={
                "expected_version": 1,
                "sources": [
                    {
                        "mapping_id": mapping_by_recording[second_id],
                        "gap_before_ms": 0,
                    },
                    {
                        "mapping_id": mapping_by_recording[first_id],
                        "gap_before_ms": 2_500,
                    },
                ],
            },
            headers=auth_headers["admin_t1"],
        )
        assert plan_response.status_code == 201, plan_response.text
        plan = plan_response.json()
        assert plan["physical_eligible"] is True
        assert plan["total_duration_ms"] == 22_500
        assert [
            (
                source["recording_id"],
                source["timeline_start_ms"],
                source["timeline_end_ms"],
            )
            for source in plan["sources"]
        ] == [
            (second_id, 0, 10_000),
            (first_id, 12_500, 22_500),
        ]

        operation_headers = {
            **auth_headers["admin_t1"],
            "Idempotency-Key": "audio-plan-operation-201-202",
        }
        operation_response = test_client.post(
            f"/api/v1/receptions/{reception_id}/audio-operations",
            json={
                "plan_token": plan["plan_token"],
                "mode": "logical",
                "expected_version": 1,
            },
            headers=operation_headers,
        )
        assert operation_response.status_code == 202, operation_response.text
        operation_id = int(operation_response.json()["id"])

        operation: dict[str, Any] = operation_response.json()
        for _attempt in range(100):
            polled = test_client.get(
                f"/api/v1/receptions/{reception_id}/audio-operations/{operation_id}",
                headers=auth_headers["admin_t1"],
            )
            assert polled.status_code == 200
            operation = polled.json()
            if operation["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.01)
        assert operation["status"] == "succeeded", operation
        assert operation["progress"] == 1.0

        duplicate = test_client.post(
            f"/api/v1/receptions/{reception_id}/audio-operations",
            json={
                "plan_token": plan["plan_token"],
                "mode": "logical",
                "expected_version": 1,
            },
            headers=operation_headers,
        )
        assert duplicate.status_code == 202
        assert int(duplicate.json()["id"]) == operation_id

        mismatched_replay = test_client.post(
            f"/api/v1/receptions/{reception_id}/audio-operations",
            json={
                "plan_token": plan["plan_token"],
                "mode": "both",
                "expected_version": 1,
            },
            headers=operation_headers,
        )
        assert mismatched_replay.status_code == 409
        assert (
            mismatched_replay.json()["error"]["code"]
            == "IDEMPOTENCY_KEY_REUSED"
        )

        workspace_response = test_client.get(
            f"/api/v1/receptions/{reception_id}/workspace",
            headers=auth_headers["admin_t1"],
        )
        assert workspace_response.status_code == 200
        workspace = workspace_response.json()
        assert workspace["reception"]["version"] == 2
        assert [
            (
                int(item["recording_id"]),
                item["timeline_start_ms"],
                item["timeline_end_ms"],
                item["gap_before_ms"],
            )
            for item in workspace["recordings"]
        ] == [
            (second_id, 0, 10_000, 0),
            (first_id, 12_500, 22_500, 2_500),
        ]

    def test_audio_plan_rejects_stale_version_and_viewer(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        recording_id = _run_async(
            seed_recording(db_session_factory, recording_id=203)
        )
        _run_async(
            _set_recording_source_facts(
                db_session_factory,
                recording_ids=[recording_id],
            )
        )
        body = _create_body([recording_id])
        body["merge_mode"] = "logical"
        created = test_client.post(
            "/api/v1/receptions",
            json=body,
            headers=auth_headers["admin_t1"],
        ).json()
        endpoint = f"/api/v1/receptions/{created['id']}/audio-plans"
        request_body = {
            "expected_version": 99,
            "sources": [
                {
                    "mapping_id": created["recordings"][0]["mapping_id"],
                    "gap_before_ms": 0,
                }
            ],
        }

        stale = test_client.post(
            endpoint,
            json=request_body,
            headers=auth_headers["admin_t1"],
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "RECEPTION_VERSION_CONFLICT"
        assert stale.json()["error"]["detail"]["expected_version"] == 99
        assert stale.json()["error"]["detail"]["actual_version"] == 1

        forbidden = test_client.post(
            endpoint,
            json={**request_body, "expected_version": 1},
            headers=auth_headers["viewer_t1"],
        )
        assert forbidden.status_code == 403
