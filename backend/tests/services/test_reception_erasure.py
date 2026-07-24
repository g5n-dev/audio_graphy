"""Privacy-safe reception invalidation after source erasure."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

from audio_graphy.models.reception import (
    DialogueStateTransition,
    DialogueTagAssignment,
    DialogueUnit,
    ProvenanceEvent,
    Reception,
    ReceptionAutomationRun,
    ReceptionRecording,
)
from audio_graphy.models.recording import Recording
from audio_graphy.services.reception_erasure import (
    erase_reception_artifacts,
    invalidate_receptions_for_recording,
)


@pytest.mark.unit
async def test_erasure_clears_stale_reception_derivatives(
    session_factory: Any,
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        first = Recording(
            tenant_id="tenant-a",
            store_id="store-1",
            path="/data/first.wav",
            status="indexed",
            pipeline_state="done",
        )
        second = Recording(
            tenant_id="tenant-a",
            store_id="store-1",
            path="/data/second.wav",
            status="indexed",
            pipeline_state="done",
        )
        session.add_all([first, second])
        await session.flush()
        reception = Reception(
            tenant_id="tenant-a",
            scenario="gold",
            store_id="store-1",
            status="ready",
            merge_mode="both",
            started_at=now,
            ended_at=now + timedelta(minutes=2),
            merged_audio_path="/data/assembled.wav",
            version=3,
        )
        session.add(reception)
        await session.flush()
        session.add(
            ReceptionAutomationRun(
                tenant_id="tenant-a",
                reception_id=reception.id,
                status="ready",
                stage="ready",
                attempt_count=2,
                checkpoints={"merge": {"version": 3}},
                target_labels=["stage"],
                lease_token="stale-lease",
                lease_expires_at=now + timedelta(minutes=10),
                last_error_code="OLD",
                last_error_message="old detail",
                finished_at=now,
            )
        )
        session.add_all(
            [
                ReceptionRecording(
                    tenant_id="tenant-a",
                    reception_id=reception.id,
                    recording_id=first.id,
                    sequence_no=0,
                    timeline_start_sec=0,
                    timeline_end_sec=60,
                    source_start_sec=0,
                    source_end_sec=60,
                    gap_before_sec=0,
                    decision_source="auto",
                    merge_reasons={},
                ),
                ReceptionRecording(
                    tenant_id="tenant-a",
                    reception_id=reception.id,
                    recording_id=second.id,
                    sequence_no=1,
                    timeline_start_sec=60,
                    timeline_end_sec=120,
                    source_start_sec=0,
                    source_end_sec=60,
                    gap_before_sec=0,
                    decision_source="auto",
                    merge_reasons={},
                ),
            ]
        )
        unit = DialogueUnit(
            tenant_id="tenant-a",
            reception_id=reception.id,
            source_recording_id=first.id,
            unit_index=0,
            start_sec=0,
            end_sec=20,
            segment_refs=[],
            speaker_refs=[],
            boundary_reasons=[],
        )
        session.add(unit)
        await session.flush()
        session.add_all(
            [
                DialogueStateTransition(
                    tenant_id="tenant-a",
                    reception_id=reception.id,
                    dialogue_unit_id=unit.id,
                    sequence_no=0,
                    from_state="greeting",
                    to_state="needs",
                    trigger="test",
                    confidence=0.9,
                    evidence_refs=[],
                    algorithm_version="test",
                ),
                DialogueTagAssignment(
                    tenant_id="tenant-a",
                    reception_id=reception.id,
                    dialogue_unit_id=unit.id,
                    group_key="intent",
                    group_version="v1",
                    label_key="target",
                    label_value="yes",
                    source="rule",
                    evidence_refs=[],
                    assigned_at=now,
                ),
            ]
        )
        session.add(
            ProvenanceEvent(
                tenant_id="tenant-a",
                reception_id=reception.id,
                object_type="reception",
                object_ref=str(reception.id),
                event_type="derived",
                actor="system",
                parent_refs=[],
                evidence_refs=[
                    {
                        "recording_id": first.id,
                        "text_excerpt": "customer personal evidence",
                    }
                ],
                payload={"source_path": "/data/first.wav"},
                occurred_at=now,
            )
        )
        await session.commit()
        first_id = first.id
        second_id = second.id
        reception_id = reception.id

    async with session_factory() as session, session.begin():
        paths = await invalidate_receptions_for_recording(
            session,
            tenant_id="tenant-a",
            recording_id=first_id,
            actor="dsar:user-1",
        )

    assert paths == ["/data/assembled.wav"]
    async with session_factory() as session:
        loaded = await session.get(Reception, reception_id)
        assert loaded is not None
        assert loaded.status == "needs_review"
        assert loaded.version == 4
        assert loaded.merged_audio_path is None

        automation_run = await session.scalar(
            select(ReceptionAutomationRun).where(
                ReceptionAutomationRun.reception_id == reception_id
            )
        )
        assert automation_run is not None
        assert automation_run.status == "pending"
        assert automation_run.stage == "merge"
        assert automation_run.checkpoints == {}
        assert automation_run.lease_token is None
        assert automation_run.lease_expires_at is None
        assert automation_run.last_error_code is None
        assert automation_run.last_error_message is None
        assert automation_run.finished_at is None

        remaining_recordings = set(
            (
                await session.execute(
                    select(ReceptionRecording.recording_id).where(
                        ReceptionRecording.reception_id == reception_id
                    )
                )
            ).scalars()
        )
        assert remaining_recordings == {second_id}
        for model in (
            DialogueUnit,
            DialogueStateTransition,
            DialogueTagAssignment,
        ):
            count = await session.scalar(
                select(func.count()).select_from(model).where(model.reception_id == reception_id)
            )
            assert count == 0

        events = await session.execute(
            select(ProvenanceEvent).where(
                ProvenanceEvent.object_type == "reception",
                ProvenanceEvent.object_ref == str(reception_id),
            )
        )
        reception_events = list(events.scalars())
        assert len(reception_events) == 1
        event = reception_events[0]
        assert event.event_type == "deleted"
        assert event.evidence_refs == []
        assert event.payload["reason"] == "source_recording_erased"
        assert event.payload["derivatives_cleared"] is True
        assert event.payload["automation_invalidated"] is True
        assert event.reception_id == reception_id


@pytest.mark.unit
def test_artifact_erasure_is_confined_to_generated_audio(
    tmp_path: Any,
) -> None:
    generated = tmp_path / "assembled_audio" / "tenant-a" / "r1.wav"
    generated.parent.mkdir(parents=True)
    generated.write_bytes(b"audio")
    outside = tmp_path.parent / "must-not-delete.wav"
    outside.write_bytes(b"outside")

    assert erase_reception_artifacts(
        [str(generated)],
        allowed_root=tmp_path,
    ) == [generated]
    assert not generated.exists()

    with pytest.raises(ValueError, match="escapes"):
        erase_reception_artifacts([str(outside)], allowed_root=tmp_path)
    assert outside.exists()
