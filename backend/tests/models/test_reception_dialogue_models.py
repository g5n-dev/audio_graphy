"""Persistence contract for receptions, dialogue units, tags and provenance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from audio_graphy.models import (
    Base,
    DialogueStateTransition,
    DialogueTagAssignment,
    DialogueUnit,
    ProvenanceEvent,
    Reception,
    ReceptionRecording,
)
from audio_graphy.models.recording import Recording

EXPECTED_DIALOGUE_TABLES = {
    "receptions",
    "reception_recordings",
    "dialogue_units",
    "dialogue_state_transitions",
    "dialogue_tag_assignments",
    "provenance_events",
}


@pytest.mark.integration
class TestReceptionDialogueMetadata:
    def test_all_dialogue_tables_are_registered(self) -> None:
        assert set(Base.metadata.tables) >= EXPECTED_DIALOGUE_TABLES

    def test_query_indexes_cover_workspace_and_matrix_access(self) -> None:
        reception_indexes = {index.name for index in Reception.__table__.indexes}
        unit_indexes = {index.name for index in DialogueUnit.__table__.indexes}
        tag_indexes = {index.name for index in DialogueTagAssignment.__table__.indexes}

        assert "ix_receptions_tenant_store_start" in reception_indexes
        assert "ix_dialogue_units_reception_timeline" in unit_indexes
        assert "ix_dialogue_tags_matrix" in tag_indexes
        assert {index.name for index in ProvenanceEvent.__table__.indexes} >= {
            "ix_provenance_events_reception"
        }

    def test_recording_erasure_cascades_only_the_reception_mapping(self) -> None:
        recording_fk = next(
            foreign_key
            for foreign_key in ReceptionRecording.__table__.foreign_keys
            if foreign_key.column.table.name == "recordings"
        )
        assert recording_fk.ondelete == "CASCADE"


@pytest.mark.integration
class TestReceptionDialoguePersistence:
    def test_complete_workspace_graph_round_trips(self, db_session: Session) -> None:
        started_at = datetime.now(UTC)
        recording = Recording(
            tenant_id="tenant-a",
            store_id="gold-001",
            agent_name="sales-7",
            customer_hash="customer-hash",
            path="/audio/clip-1.wav",
            status="indexed",
            pipeline_state="done",
            recorded_at=started_at,
        )
        db_session.add(recording)
        db_session.flush()

        reception = Reception(
            tenant_id="tenant-a",
            external_session_id="POS-20260723-001",
            scenario="gold",
            store_id="gold-001",
            agent_name="sales-7",
            customer_hash="customer-hash",
            status="confirmed",
            merge_mode="both",
            merge_confidence=0.98,
            started_at=started_at,
            ended_at=started_at + timedelta(minutes=8),
            merged_audio_path="/audio/receptions/r-1.wav",
            version=1,
        )
        db_session.add(reception)
        db_session.flush()

        link = ReceptionRecording(
            tenant_id="tenant-a",
            reception_id=reception.id,
            recording_id=recording.id,
            sequence_no=0,
            timeline_start_sec=0.0,
            timeline_end_sec=480.0,
            source_start_sec=0.0,
            source_end_sec=480.0,
            gap_before_sec=0.0,
            decision_source="explicit",
            merge_confidence=1.0,
            merge_reasons={"session_id": "exact"},
        )
        unit = DialogueUnit(
            tenant_id="tenant-a",
            reception_id=reception.id,
            source_recording_id=recording.id,
            unit_index=0,
            version=1,
            start_sec=0.0,
            end_sec=45.0,
            topic="到店选购足金手镯",
            business_stage="需求了解",
            summary="客户关注预算与克重。",
            boundary_confidence=0.93,
            boundary_reasons=["long_pause", "semantic_shift"],
            segment_refs=[{"recording_id": recording.id, "segment_id": 1}],
            speaker_refs=["customer", "sales"],
            edit_status="auto",
        )
        db_session.add_all([link, unit])
        db_session.flush()

        transition = DialogueStateTransition(
            tenant_id="tenant-a",
            reception_id=reception.id,
            dialogue_unit_id=unit.id,
            sequence_no=0,
            from_state="接待问候",
            to_state="需求了解",
            trigger="customer_need_detected",
            confidence=0.91,
            evidence_refs=[{"recording_id": recording.id, "start_sec": 12.0}],
            algorithm_version="dialogue-hybrid-v1",
        )
        tag = DialogueTagAssignment(
            tenant_id="tenant-a",
            reception_id=reception.id,
            dialogue_unit_id=unit.id,
            group_key="sales-intent",
            group_version="v2",
            label_key="budget_band",
            label_value="5000-10000",
            confidence=0.88,
            source="llm",
            priority=20,
            evidence_refs=[
                {
                    "kind": "audio",
                    "recording_id": recording.id,
                    "start_sec": 20.0,
                    "end_sec": 25.0,
                }
            ],
            model_run_id="run-20260723",
            is_current=True,
            assigned_at=started_at,
        )
        provenance = ProvenanceEvent(
            tenant_id="tenant-a",
            object_type="dialogue_tag_assignment",
            object_ref="pending-tag",
            event_type="derived",
            actor="system",
            algorithm_version="tag-v2",
            parent_refs=[{"type": "dialogue_unit", "id": unit.id}],
            evidence_refs=tag.evidence_refs,
            payload={"prompt_version": "gold-sales-v2"},
            occurred_at=started_at,
        )
        db_session.add_all([transition, tag, provenance])
        db_session.commit()

        loaded = db_session.scalar(select(Reception).where(Reception.id == reception.id))
        assert loaded is not None
        assert loaded.recordings[0].recording_id == recording.id
        assert loaded.dialogue_units[0].business_stage == "需求了解"
        assert loaded.dialogue_units[0].tag_assignments[0].label_value == "5000-10000"
        assert loaded.state_transitions[0].to_state == "需求了解"

    def test_schema_contains_time_and_confidence_checks(self) -> None:
        reception_checks = {
            constraint.name
            for constraint in Reception.__table__.constraints
            if constraint.name is not None
        }
        unit_checks = {
            constraint.name
            for constraint in DialogueUnit.__table__.constraints
            if constraint.name is not None
        }

        assert "ck_receptions_time_order" in reception_checks
        assert "ck_receptions_merge_confidence" in reception_checks
        assert "ck_dialogue_units_time_order" in unit_checks
