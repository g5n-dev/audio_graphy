"""SQLite API tests for reception dialogue-tag production and insights."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from audio_graphy.models.reception import (
    DialogueTagAssignment,
    DialogueUnit,
    ProvenanceEvent,
    Reception,
    ReceptionRecording,
)
from audio_graphy.models.recording import Recording
from audio_graphy.models.segment import Segment


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _seed_reception(
    factory: Any,
    *,
    tenant_id: str = "chang_an",
    scenario: str = "automotive",
    store_id: str = "S001",
    agent_name: str = "agent_ca",
    transcript: str = (
        "客户说我今天就订车，但是价格太高；先安排试驾。销售要求把定金转到个人账户。"
    ),
    started_at: datetime | None = None,
) -> tuple[int, int, int]:
    now = started_at or datetime.now(UTC)
    async with factory() as session:
        recording = Recording(
            tenant_id=tenant_id,
            store_id=store_id,
            agent_name=agent_name,
            customer_hash="customer-hash",
            path=f"/tmp/{tenant_id}-{store_id}.wav",
            status="indexed",
            pipeline_state="done",
            recorded_at=now,
            indexed_at=now,
        )
        session.add(recording)
        await session.flush()
        segment = Segment(
            tenant_id=tenant_id,
            recording_id=recording.id,
            idx=0,
            start_sec=0.0,
            end_sec=12.0,
            transcript=transcript,
            text_scrubbed=transcript,
            speaker="customer",
            vad_conf=0.94,
        )
        session.add(segment)
        await session.flush()
        reception = Reception(
            tenant_id=tenant_id,
            scenario=scenario,
            store_id=store_id,
            agent_name=agent_name,
            agent_user_id=(
                3
                if tenant_id == "chang_an" and agent_name == "agent_ca"
                else 7
                if tenant_id == "byd" and agent_name == "agent_byd"
                else None
            ),
            status="ready",
            merge_mode="logical",
            started_at=now,
            ended_at=now + timedelta(seconds=12),
            version=1,
        )
        session.add(reception)
        await session.flush()
        session.add(
            ReceptionRecording(
                tenant_id=tenant_id,
                reception_id=reception.id,
                recording_id=recording.id,
                sequence_no=0,
                timeline_start_sec=0.0,
                timeline_end_sec=12.0,
                source_start_sec=0.0,
                source_end_sec=12.0,
                gap_before_sec=0.0,
                decision_source="manual",
                merge_confidence=1.0,
                merge_reasons={"seed": True},
            )
        )
        unit = DialogueUnit(
            tenant_id=tenant_id,
            reception_id=reception.id,
            source_recording_id=recording.id,
            unit_index=0,
            version=1,
            start_sec=0.0,
            end_sec=12.0,
            topic="成交推进",
            business_stage="成交推进",
            summary="客户表达购买意向并讨论后续。",
            boundary_confidence=0.91,
            boundary_reasons=[{"code": "seed"}],
            segment_refs=[
                {
                    "recording_id": recording.id,
                    "segment_id": segment.id,
                    "timeline_start_sec": 0.0,
                    "timeline_end_sec": 12.0,
                }
            ],
            speaker_refs=["customer"],
            edit_status="auto",
        )
        session.add(unit)
        await session.commit()
        return reception.id, unit.id, segment.id


async def _tag_state(factory: Any, reception_id: int) -> dict[str, Any]:
    async with factory() as session:
        tags = list(
            (
                await session.execute(
                    select(DialogueTagAssignment)
                    .where(DialogueTagAssignment.reception_id == reception_id)
                    .order_by(DialogueTagAssignment.id)
                )
            )
            .scalars()
            .all()
        )
        events = list(
            (
                await session.execute(
                    select(ProvenanceEvent)
                    .where(
                        ProvenanceEvent.reception_id == reception_id,
                        ProvenanceEvent.object_type == "dialogue_tag_assignment",
                    )
                    .order_by(ProvenanceEvent.id)
                )
            )
            .scalars()
            .all()
        )
        return {"tags": tags, "events": events}


async def _legacy_derive(
    factory: Any,
    *,
    reception_id: int,
    target_labels: list[str],
    group_key: str = "sales-rules",
    group_version: str = "automotive-v1",
    actor: str = "user:1",
) -> Any:
    """Seed legacy projections for compatibility-read/correction tests only."""

    from audio_graphy.schemas.reception_tags import DeriveDialogueTagsRequest
    from audio_graphy.services.reception_tagging import ReceptionTaggingService

    return await ReceptionTaggingService(factory).derive(
        reception_id=reception_id,
        tenant_id="chang_an",
        request=DeriveDialogueTagsRequest(
            group_key=group_key,
            group_version=group_version,
            target_labels=target_labels,
        ),
        actor=actor,
    )


async def _seed_legacy_mapping_recipe(
    factory: Any,
    *,
    target_labels: list[str],
) -> None:
    """Publish the deterministic recipe required by the deprecated HTTP adapter."""

    from audio_graphy.models.tag_governance import (
        LegacyTagMapping,
        TaggerVersion,
        TagSchema,
        TagSchemaVersion,
    )

    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        schema = TagSchema(
            tenant_id="chang_an",
            key="legacy-api-adapter",
            name="Legacy API adapter",
            status="published",
            created_by=1,
        )
        session.add(schema)
        await session.flush()
        version = TagSchemaVersion(
            tenant_id="chang_an",
            schema_id=schema.id,
            version="1",
            definitions=[
                {
                    "key": tag_key,
                    "value_type": "enum",
                    "allowed_values": ["unknown", "present"],
                    "evidence_required": True,
                    "subject_types": ["dialogue_unit"],
                    "scenarios": ["automotive"],
                }
                for tag_key in target_labels
            ],
            checksum="c" * 64,
            status="published",
            created_by=1,
        )
        session.add(version)
        await session.flush()
        schema.active_version_id = version.id
        session.add(
            TaggerVersion(
                tenant_id="chang_an",
                schema_version_id=version.id,
                version="qualified",
                engine="llm",
                prompt_content="Return evidence-bound canonical labels.",
                rule_bundle={"dsl_version": "1", "rules": []},
                model_version="test-model",
                thresholds=dict.fromkeys(target_labels, 0.7),
                config_checksum="d" * 64,
                status="qualified",
                created_by=1,
                qualified_at=now,
            )
        )
        session.add_all(
            [
                LegacyTagMapping(
                    tenant_id="chang_an",
                    legacy_tag_path=f"dialogue_tag_assignments.{tag_key}",
                    schema_version_id=version.id,
                    tag_key=tag_key,
                    mapping={
                        "mode": "identity",
                        "source_subject": "dialogue_unit",
                        "target_subject": "dialogue_unit",
                    },
                    deterministic=True,
                )
                for tag_key in target_labels
            ]
        )


async def _seed_canonical_tag(
    factory: Any,
    *,
    unit_id: int,
    segment_id: int,
    tag_key: str = "intent",
    tag_value: str = "high",
    allowed_values: list[str] | None = None,
) -> tuple[int, int]:
    from audio_graphy.models.tag_governance import TagSchema, TagSchemaVersion
    from audio_graphy.services.tag_governance import TagGovernanceService

    async with factory() as session:
        schema = TagSchema(
            tenant_id="chang_an",
            key="canonical-sales",
            name="接待标准标签",
            status="published",
            created_by=1,
        )
        session.add(schema)
        await session.flush()
        version = TagSchemaVersion(
            tenant_id="chang_an",
            schema_id=schema.id,
            version="1",
            definitions=[
                {
                    "key": tag_key,
                    "value_type": "enum",
                    "allowed_values": allowed_values or ["high", "medium"],
                    "evidence_required": True,
                    "subject_types": ["dialogue_unit"],
                    "scenarios": ["automotive"],
                }
            ],
            checksum="f" * 64,
            status="published",
            created_by=1,
        )
        session.add(version)
        await session.commit()

    fact = await TagGovernanceService(factory).append_assignment(
        tenant_id="chang_an",
        subject_type="dialogue_unit",
        subject_id=unit_id,
        tag_key=tag_key,
        tag_value=tag_value,
        confidence=0.89,
        evidence_refs=[
            {
                "ref_id": f"segment:{segment_id}",
                "kind": "audio",
                "segment_id": segment_id,
                "recording_id": 1,
                "start_sec": 0.0,
                "end_sec": 12.0,
            }
        ],
        source="imported",
        schema_version_id=version.id,
        tagger_version_id=None,
        extraction_run_id=None,
        deployment_id=None,
        input_hash="1" * 64,
        actor_user_id=1,
    )
    return fact.id, version.id


async def _seed_review_schema(
    factory: Any,
    *,
    tag_key: str = "intent",
    allowed_values: list[str] | None = None,
) -> int:
    from audio_graphy.models.tag_governance import TagSchema, TagSchemaVersion

    async with factory() as session, session.begin():
        schema = TagSchema(
            tenant_id="chang_an",
            key="legacy-bootstrap",
            name="历史标签治理接入",
            status="published",
            created_by=1,
        )
        session.add(schema)
        await session.flush()
        version = TagSchemaVersion(
            tenant_id="chang_an",
            schema_id=schema.id,
            version="1",
            definitions=[
                {
                    "key": tag_key,
                    "value_type": "enum",
                    "allowed_values": allowed_values or ["high", "medium"],
                    "evidence_required": True,
                    "subject_types": ["dialogue_unit"],
                    "scenarios": ["automotive"],
                }
            ],
            checksum="e" * 64,
            status="published",
            created_by=1,
        )
        session.add(version)
        await session.flush()
        return version.id


async def _seed_stage_chain(
    factory: Any,
    *,
    reception_id: int,
    unit_id: int,
) -> int:
    from audio_graphy.models.reception import DialogueStateTransition

    async with factory() as session, session.begin():
        first = (
            await session.execute(
                select(DialogueUnit).where(
                    DialogueUnit.id == unit_id,
                    DialogueUnit.reception_id == reception_id,
                )
            )
        ).scalar_one()
        first.end_sec = 6.0
        first.business_stage = "需求了解"
        second = DialogueUnit(
            tenant_id=first.tenant_id,
            reception_id=reception_id,
            source_recording_id=first.source_recording_id,
            unit_index=1,
            version=1,
            start_sec=6.0,
            end_sec=12.0,
            topic="试驾安排",
            business_stage="试驾",
            summary="安排客户试驾。",
            boundary_confidence=0.88,
            boundary_reasons=[{"code": "seed"}],
            segment_refs=list(first.segment_refs),
            speaker_refs=list(first.speaker_refs),
            edit_status="auto",
        )
        session.add(second)
        await session.flush()
        session.add_all(
            [
                DialogueStateTransition(
                    tenant_id=first.tenant_id,
                    reception_id=reception_id,
                    dialogue_unit_id=first.id,
                    sequence_no=0,
                    from_state="__start__",
                    to_state="需求了解",
                    trigger="seed",
                    confidence=0.91,
                    evidence_refs=list(first.segment_refs),
                    algorithm_version="seed-v1",
                ),
                DialogueStateTransition(
                    tenant_id=first.tenant_id,
                    reception_id=reception_id,
                    dialogue_unit_id=second.id,
                    sequence_no=1,
                    from_state="需求了解",
                    to_state="试驾",
                    trigger="seed",
                    confidence=0.88,
                    evidence_refs=list(second.segment_refs),
                    algorithm_version="seed-v1",
                ),
            ]
        )
        return second.id


async def _replace_segment_text(
    factory: Any,
    segment_id: int,
    text: str,
) -> None:
    async with factory() as session:
        segment = await session.get(Segment, segment_id)
        assert segment is not None
        segment.transcript = text
        segment.text_scrubbed = text
        await session.commit()


async def _clear_segment_scrubbed_text(
    factory: Any,
    segment_id: int,
) -> None:
    async with factory() as session:
        segment = await session.get(Segment, segment_id)
        assert segment is not None
        segment.text_scrubbed = None
        await session.commit()


async def _offset_and_split_unit(
    factory: Any,
    reception_id: int,
    unit_id: int,
    segment_id: int,
) -> None:
    async with factory() as session:
        mapping = (
            await session.execute(
                select(ReceptionRecording).where(ReceptionRecording.reception_id == reception_id)
            )
        ).scalar_one()
        mapping.timeline_start_sec = 30.0
        mapping.timeline_end_sec = 42.0
        unit = await session.get(DialogueUnit, unit_id)
        assert unit is not None
        unit.start_sec = 32.0
        unit.end_sec = 36.0
        unit.segment_refs = [
            {
                "recording_id": mapping.recording_id,
                "segment_id": segment_id,
                "source_start_sec": 0.0,
                "source_end_sec": 12.0,
                "timeline_start_sec": 30.0,
                "timeline_end_sec": 42.0,
            }
        ]
        await session.commit()


@pytest.fixture
def reception_tag_client(test_client: TestClient) -> TestClient:
    """Exercise the production create_app router registration."""
    return test_client


@pytest.mark.integration
def test_derive_enqueues_canonical_job_without_legacy_projection_writes(
    reception_tag_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    target_labels = [
        "stage",
        "intent",
        "objection",
        "next_step",
        "compliance_risk",
    ]
    reception_id, unit_id, _segment_id = _run(_seed_reception(db_session_factory))
    _run(
        _seed_legacy_mapping_recipe(
            db_session_factory,
            target_labels=target_labels,
        )
    )
    url = f"/api/v1/receptions/{reception_id}/dialogue-tags/derive"
    body = {
        "group_key": "sales-rules",
        "group_version": "automotive-v1",
        "target_labels": target_labels,
    }

    first = reception_tag_client.post(
        url,
        json=body,
        headers={
            **auth_headers["inspector_t1"],
            "Idempotency-Key": "derive-canonical-1",
        },
    )
    assert first.status_code == 202, first.text
    assert first.json()["status"] == "queued"
    assert set(first.json()["requested_labels"]) == set(target_labels)

    repeated = reception_tag_client.post(
        url,
        json=body,
        headers={
            **auth_headers["inspector_t1"],
            "Idempotency-Key": "derive-canonical-1",
        },
    )
    assert repeated.status_code == 202
    assert repeated.json()["job_id"] == first.json()["job_id"]
    state = _run(_tag_state(db_session_factory, reception_id))
    assert state == {"tags": [], "events": []}

    async def _canonical_job_state() -> tuple[Any, list[Any]]:
        from audio_graphy.models.tag_governance import TagExtractionJob

        async with db_session_factory() as session:
            jobs = list(
                (await session.execute(select(TagExtractionJob).order_by(TagExtractionJob.id)))
                .scalars()
                .all()
            )
            return jobs[0], jobs

    job, jobs = _run(_canonical_job_state())
    assert len(jobs) == 1
    assert job.created_by == 2
    assert job.total_items == 1
    assert job.scope["dialogue_unit_ids"] == [unit_id]
    assert set(job.scope["target_tag_keys"]) == set(target_labels)

    upgraded = reception_tag_client.post(
        url,
        json={**body, "group_version": "automotive-v2"},
        headers={
            **auth_headers["admin_t1"],
            "Idempotency-Key": "derive-canonical-2",
        },
    )
    assert upgraded.status_code == 202
    state = _run(_tag_state(db_session_factory, reception_id))
    assert state == {"tags": [], "events": []}


@pytest.mark.integration
def test_patch_dialogue_tag_appends_manual_version_and_provenance(
    reception_tag_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    reception_id, unit_id, segment_id = _run(_seed_reception(db_session_factory))
    derived = _run(
        _legacy_derive(
            db_session_factory,
            reception_id=reception_id,
            target_labels=["intent"],
            actor="user:2",
        )
    )
    original = derived.assignments[0]

    corrected = reception_tag_client.patch(
        f"/api/v1/receptions/{reception_id}/dialogue-tags/{original.id}",
        json={
            "expected_reception_version": 1,
            "expected_group_version": "automotive-v1",
            "label_value": "medium",
            "reason": "客户仍在比较方案，人工复核为中等意向",
            "evidence_ref_ids": [f"segment:{segment_id}"],
        },
        headers=auth_headers["inspector_t1"],
    )

    assert corrected.status_code == 200, corrected.text
    payload = corrected.json()
    assert payload["reception_id"] == reception_id
    assert payload["reception_version"] == 2
    assert payload["superseded_assignment_id"] == original.id
    assert payload["assignment"]["dialogue_unit_id"] == unit_id
    assert payload["assignment"]["group_key"] == "sales-rules"
    assert payload["assignment"]["group_version"].startswith("automotive-v1-manual-r2")
    assert payload["assignment"]["label_key"] == "intent"
    assert payload["assignment"]["label_value"] == "medium"
    assert payload["assignment"]["source"] == "manual"
    assert payload["assignment"]["confidence"] == 1.0
    assert payload["assignment"]["is_current"] is True
    assert [evidence["ref_id"] for evidence in payload["assignment"]["evidence_refs"]] == [
        f"segment:{segment_id}"
    ]

    state = _run(_tag_state(db_session_factory, reception_id))
    assert len(state["tags"]) == 2
    assert state["tags"][0].id == original.id
    assert state["tags"][0].is_current is False
    assert state["tags"][1].source == "manual"
    assert state["tags"][1].is_current is True
    assert [event.event_type for event in state["events"]] == [
        "derived",
        "superseded",
        "edited",
    ]
    superseded, edited = state["events"][-2:]
    assert superseded.object_ref == str(original.id)
    assert superseded.actor == "user:2"
    assert superseded.payload["superseded_by_assignment_id"] == state["tags"][1].id
    assert edited.object_ref == str(state["tags"][1].id)
    assert edited.actor == "user:2"
    assert edited.payload["reason"] == "客户仍在比较方案，人工复核为中等意向"
    assert edited.payload["before"]["label_value"] == "high"
    assert edited.payload["after"]["label_value"] == "medium"


@pytest.mark.integration
def test_patch_dialogue_tag_validates_lock_evidence_role_and_tenant(
    reception_tag_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    reception_id, _unit_id, segment_id = _run(_seed_reception(db_session_factory))
    derived = _run(
        _legacy_derive(
            db_session_factory,
            reception_id=reception_id,
            target_labels=["intent"],
        )
    )
    assignment_id = derived.assignments[0].id
    url = f"/api/v1/receptions/{reception_id}/dialogue-tags/{assignment_id}"
    valid_body = {
        "expected_reception_version": 1,
        "expected_group_version": "automotive-v1",
        "label_value": "medium",
        "reason": "人工核对",
        "evidence_ref_ids": [f"segment:{segment_id}"],
    }

    forbidden = reception_tag_client.patch(
        url,
        json=valid_body,
        headers=auth_headers["viewer_t1"],
    )
    assert forbidden.status_code == 403

    hidden = reception_tag_client.patch(
        url,
        json=valid_body,
        headers=auth_headers["inspector_t2"],
    )
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "RECEPTION_NOT_FOUND"

    stale_reception = reception_tag_client.patch(
        url,
        json={**valid_body, "expected_reception_version": 99},
        headers=auth_headers["inspector_t1"],
    )
    assert stale_reception.status_code == 409
    assert stale_reception.json()["error"]["code"] == "RECEPTION_VERSION_CONFLICT"

    stale_group = reception_tag_client.patch(
        url,
        json={**valid_body, "expected_group_version": "automotive-v0"},
        headers=auth_headers["inspector_t1"],
    )
    assert stale_group.status_code == 409
    assert stale_group.json()["error"]["code"] == "TAG_GROUP_VERSION_CONFLICT"

    empty_evidence = reception_tag_client.patch(
        url,
        json={**valid_body, "evidence_ref_ids": []},
        headers=auth_headers["inspector_t1"],
    )
    assert empty_evidence.status_code == 422

    missing_reason_body = {key: value for key, value in valid_body.items() if key != "reason"}
    missing_reason = reception_tag_client.patch(
        url,
        json=missing_reason_body,
        headers=auth_headers["inspector_t1"],
    )
    assert missing_reason.status_code == 422

    foreign_evidence = reception_tag_client.patch(
        url,
        json={**valid_body, "evidence_ref_ids": ["segment:not-owned"]},
        headers=auth_headers["inspector_t1"],
    )
    assert foreign_evidence.status_code == 422
    assert foreign_evidence.json()["error"]["code"] == "TAG_EVIDENCE_SUBSET_INVALID"

    state = _run(_tag_state(db_session_factory, reception_id))
    assert len(state["tags"]) == 1
    assert state["tags"][0].is_current is True


@pytest.mark.integration
def test_patch_canonical_workspace_tag_appends_fact_and_moves_current_projection(
    reception_tag_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    from audio_graphy.models.tag_governance import (
        TagAssignmentCurrent,
        TagAssignmentFact,
        TagGovernanceAuditEvent,
    )

    reception_id, unit_id, segment_id = _run(_seed_reception(db_session_factory))
    fact_id, schema_version_id = _run(
        _seed_canonical_tag(
            db_session_factory,
            unit_id=unit_id,
            segment_id=segment_id,
        )
    )
    workspace = reception_tag_client.get(
        f"/api/v1/receptions/{reception_id}/workspace",
        headers=auth_headers["inspector_t1"],
    )
    assert workspace.status_code == 200, workspace.text
    canonical = workspace.json()["tag_assignments"][0]
    assert canonical["id"] == fact_id
    assert canonical["group_key"] == "canonical"
    assert canonical["group_version"] == f"schema:{schema_version_id}|tagger:manual"

    corrected = reception_tag_client.patch(
        f"/api/v1/receptions/{reception_id}/dialogue-tags/{fact_id}",
        json={
            "expected_reception_version": 1,
            "expected_group_version": canonical["group_version"],
            "label_value": "medium",
            "reason": "人工复核 canonical 当前事实",
            "evidence_ref_ids": [f"segment:{segment_id}"],
        },
        headers=auth_headers["inspector_t1"],
    )

    assert corrected.status_code == 200, corrected.text
    payload = corrected.json()
    assert payload["reception_version"] == 2
    assert payload["superseded_assignment_id"] == fact_id
    assert payload["assignment"]["source"] == "manual"
    assert payload["assignment"]["label_value"] == "medium"
    assert payload["assignment"]["model_run_id"].startswith("fact:")

    async def _canonical_state() -> tuple[
        list[TagAssignmentFact],
        TagAssignmentCurrent,
        list[TagGovernanceAuditEvent],
        list[ProvenanceEvent],
        Reception,
    ]:
        async with db_session_factory() as session:
            facts = list(
                (
                    await session.execute(
                        select(TagAssignmentFact).order_by(TagAssignmentFact.revision)
                    )
                )
                .scalars()
                .all()
            )
            current = (await session.execute(select(TagAssignmentCurrent))).scalar_one()
            audits = list(
                (
                    await session.execute(
                        select(TagGovernanceAuditEvent)
                        .where(
                            TagGovernanceAuditEvent.resource_type == "tag_assignment_fact",
                            TagGovernanceAuditEvent.resource_id == facts[-1].id,
                        )
                        .order_by(TagGovernanceAuditEvent.id)
                    )
                )
                .scalars()
                .all()
            )
            events = list(
                (
                    await session.execute(
                        select(ProvenanceEvent)
                        .where(
                            ProvenanceEvent.reception_id == reception_id,
                            ProvenanceEvent.object_type == "tag_assignment_fact",
                        )
                        .order_by(ProvenanceEvent.id)
                    )
                )
                .scalars()
                .all()
            )
            reception = (
                await session.execute(select(Reception).where(Reception.id == reception_id))
            ).scalar_one()
            return facts, current, audits, events, reception

    facts, current, audits, events, reception = _run(_canonical_state())
    assert [fact.tag_value for fact in facts] == ["high", "medium"]
    assert facts[0].tag_value == "high"
    assert facts[1].source == "manual"
    assert facts[1].superseded_fact_id == facts[0].id
    assert current.fact_id == facts[1].id
    assert reception.version == 2
    assert [audit.action for audit in audits] == ["manual_corrected"]
    assert audits[0].payload["reason"] == "人工复核 canonical 当前事实"
    assert audits[0].payload["superseded_fact_id"] == facts[0].id
    assert [event.event_type for event in events] == ["superseded", "edited"]
    assert events[0].object_ref == str(facts[0].id)
    assert events[1].object_ref == str(facts[1].id)
    assert events[1].payload["reason"] == "人工复核 canonical 当前事实"

    refreshed = reception_tag_client.get(
        f"/api/v1/receptions/{reception_id}/workspace",
        headers=auth_headers["viewer_t1"],
    )
    assert refreshed.status_code == 200
    current_tags = refreshed.json()["tag_assignments"]
    assert len(current_tags) == 1
    assert current_tags[0]["id"] == facts[1].id
    assert current_tags[0]["label_value"] == "medium"
    assert current_tags[0]["source"] == "manual"

    stale = reception_tag_client.patch(
        f"/api/v1/receptions/{reception_id}/dialogue-tags/{fact_id}",
        json={
            "expected_reception_version": 2,
            "expected_group_version": canonical["group_version"],
            "label_value": "high",
            "reason": "过期事实不允许覆盖当前投影",
            "evidence_ref_ids": [f"segment:{segment_id}"],
        },
        headers=auth_headers["inspector_t1"],
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "TAG_GROUP_VERSION_CONFLICT"


@pytest.mark.integration
def test_patch_stage_tag_keeps_unit_and_state_chain_in_sync(
    reception_tag_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    from audio_graphy.models.reception import DialogueStateTransition

    reception_id, unit_id, segment_id = _run(_seed_reception(db_session_factory))
    second_unit_id = _run(
        _seed_stage_chain(
            db_session_factory,
            reception_id=reception_id,
            unit_id=unit_id,
        )
    )
    fact_id, schema_version_id = _run(
        _seed_canonical_tag(
            db_session_factory,
            unit_id=unit_id,
            segment_id=segment_id,
            tag_key="stage",
            tag_value="需求了解",
            allowed_values=["需求了解", "报价"],
        )
    )

    async def _transition_snapshot() -> list[dict[str, Any]]:
        async with db_session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(DialogueStateTransition)
                        .where(DialogueStateTransition.reception_id == reception_id)
                        .order_by(DialogueStateTransition.sequence_no)
                    )
                )
                .scalars()
                .all()
            )
            return [
                {
                    "id": row.id,
                    "from_state": row.from_state,
                    "to_state": row.to_state,
                    "trigger": row.trigger,
                    "confidence": row.confidence,
                    "evidence_refs": row.evidence_refs,
                    "algorithm_version": row.algorithm_version,
                }
                for row in rows
            ]

    before_transition_state = _run(_transition_snapshot())
    corrected = reception_tag_client.patch(
        f"/api/v1/receptions/{reception_id}/dialogue-tags/{fact_id}",
        json={
            "expected_reception_version": 1,
            "expected_group_version": (f"schema:{schema_version_id}|tagger:manual"),
            "label_value": "报价",
            "reason": "客户已进入报价沟通阶段",
            "evidence_ref_ids": [f"segment:{segment_id}"],
        },
        headers=auth_headers["admin_t1"],
    )

    assert corrected.status_code == 200, corrected.text

    async def _stage_state() -> tuple[
        list[DialogueUnit],
        list[DialogueStateTransition],
        list[ProvenanceEvent],
    ]:
        async with db_session_factory() as session:
            units = list(
                (
                    await session.execute(
                        select(DialogueUnit)
                        .where(DialogueUnit.reception_id == reception_id)
                        .order_by(DialogueUnit.unit_index)
                    )
                )
                .scalars()
                .all()
            )
            transitions = list(
                (
                    await session.execute(
                        select(DialogueStateTransition)
                        .where(DialogueStateTransition.reception_id == reception_id)
                        .order_by(DialogueStateTransition.sequence_no)
                    )
                )
                .scalars()
                .all()
            )
            events = list(
                (
                    await session.execute(
                        select(ProvenanceEvent)
                        .where(
                            ProvenanceEvent.reception_id == reception_id,
                            ProvenanceEvent.object_type == "dialogue_unit",
                            ProvenanceEvent.object_ref == str(unit_id),
                        )
                        .order_by(ProvenanceEvent.id)
                    )
                )
                .scalars()
                .all()
            )
            return units, transitions, events

    units, transitions, events = _run(_stage_state())
    assert [unit.id for unit in units] == [unit_id, second_unit_id]
    assert units[0].business_stage == "报价"
    assert units[0].version == 2
    assert units[0].edit_status == "manual_edited"
    assert [(item.from_state, item.to_state) for item in transitions] == [
        ("__start__", "报价"),
        ("报价", "试驾"),
    ]
    assert [item.id for item in transitions] == [item["id"] for item in before_transition_state]
    assert transitions[0].trigger == "manual_tag_correction"
    assert transitions[0].algorithm_version == "manual-tag-edit-v1"
    assert transitions[1].trigger == before_transition_state[1]["trigger"]
    assert transitions[1].confidence == before_transition_state[1]["confidence"]
    assert transitions[1].evidence_refs == before_transition_state[1]["evidence_refs"]
    assert transitions[1].algorithm_version == before_transition_state[1]["algorithm_version"]
    assert [event.event_type for event in events] == ["edited"]
    assert events[0].payload["before"]["business_stage"] == "需求了解"
    assert events[0].payload["after"]["business_stage"] == "报价"

    workspace = reception_tag_client.get(
        f"/api/v1/receptions/{reception_id}/workspace",
        headers=auth_headers["viewer_t1"],
    )
    assert workspace.status_code == 200
    workspace_payload = workspace.json()
    current_stage_tag = next(
        item
        for item in workspace_payload["tag_assignments"]
        if item["dialogue_unit_id"] == unit_id and item["label_key"] == "stage"
    )
    current_unit = next(
        item for item in workspace_payload["dialogue_units"] if item["id"] == unit_id
    )
    current_transition = next(
        item
        for item in workspace_payload["state_transitions"]
        if item["dialogue_unit_id"] == unit_id
    )
    assert (
        current_stage_tag["label_value"]
        == current_unit["business_stage"]
        == current_transition["to_state"]
        == "报价"
    )

    insights = reception_tag_client.get(
        "/api/v1/reception-state-insights",
        params={"reception_id": reception_id},
        headers=auth_headers["viewer_t1"],
    )
    assert insights.status_code == 200
    insight_payload = insights.json()
    assert insight_payload["total_transitions"] == 2
    assert {(item["from_state"], item["to_state"]) for item in insight_payload["transitions"]} == {
        ("__start__", "报价"),
        ("报价", "试驾"),
    }


@pytest.mark.integration
def test_patch_legacy_stage_tag_repairs_a_missing_state_chain(
    reception_tag_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    from audio_graphy.models.reception import DialogueStateTransition

    reception_id, unit_id, segment_id = _run(_seed_reception(db_session_factory))
    derived = _run(
        _legacy_derive(
            db_session_factory,
            reception_id=reception_id,
            target_labels=["stage"],
        )
    )
    assignment = derived.assignments[0]

    corrected = reception_tag_client.patch(
        f"/api/v1/receptions/{reception_id}/dialogue-tags/{assignment.id}",
        json={
            "expected_reception_version": 1,
            "expected_group_version": "automotive-v1",
            "label_value": "报价",
            "reason": "补齐缺失状态链",
            "evidence_ref_ids": [f"segment:{segment_id}"],
        },
        headers=auth_headers["inspector_t1"],
    )
    assert corrected.status_code == 200, corrected.text

    async def _repaired_state() -> tuple[
        DialogueUnit,
        list[DialogueStateTransition],
    ]:
        async with db_session_factory() as session:
            unit = (
                await session.execute(select(DialogueUnit).where(DialogueUnit.id == unit_id))
            ).scalar_one()
            transitions = list(
                (
                    await session.execute(
                        select(DialogueStateTransition).where(
                            DialogueStateTransition.reception_id == reception_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            return unit, transitions

    unit, transitions = _run(_repaired_state())
    assert unit.business_stage == "报价"
    assert unit.version == 2
    assert len(transitions) == 1
    assert transitions[0].from_state == "__start__"
    assert transitions[0].to_state == "报价"
    assert transitions[0].trigger == "manual_tag_correction"


@pytest.mark.integration
def test_patch_stage_tag_rolls_back_every_projection_when_schema_is_not_published(
    reception_tag_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    from audio_graphy.models.reception import DialogueStateTransition
    from audio_graphy.models.tag_governance import (
        TagAssignmentCurrent,
        TagAssignmentFact,
        TagSchemaVersion,
    )

    reception_id, unit_id, segment_id = _run(_seed_reception(db_session_factory))
    _run(
        _seed_stage_chain(
            db_session_factory,
            reception_id=reception_id,
            unit_id=unit_id,
        )
    )
    fact_id, schema_version_id = _run(
        _seed_canonical_tag(
            db_session_factory,
            unit_id=unit_id,
            segment_id=segment_id,
            tag_key="stage",
            tag_value="需求了解",
            allowed_values=["需求了解", "报价"],
        )
    )

    async def _unpublish_schema() -> None:
        async with db_session_factory() as session, session.begin():
            schema_version = (
                await session.execute(
                    select(TagSchemaVersion).where(TagSchemaVersion.id == schema_version_id)
                )
            ).scalar_one()
            schema_version.status = "draft"

    _run(_unpublish_schema())
    rejected = reception_tag_client.patch(
        f"/api/v1/receptions/{reception_id}/dialogue-tags/{fact_id}",
        json={
            "expected_reception_version": 1,
            "expected_group_version": (f"schema:{schema_version_id}|tagger:manual"),
            "label_value": "报价",
            "reason": "此修改必须整体回滚",
            "evidence_ref_ids": [f"segment:{segment_id}"],
        },
        headers=auth_headers["admin_t1"],
    )

    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "TAG_ASSIGNMENT_INVALID"

    async def _rollback_state() -> tuple[
        DialogueUnit,
        list[DialogueStateTransition],
        Reception,
        list[TagAssignmentFact],
        TagAssignmentCurrent,
        list[ProvenanceEvent],
    ]:
        async with db_session_factory() as session:
            unit = (
                await session.execute(select(DialogueUnit).where(DialogueUnit.id == unit_id))
            ).scalar_one()
            transitions = list(
                (
                    await session.execute(
                        select(DialogueStateTransition)
                        .where(DialogueStateTransition.reception_id == reception_id)
                        .order_by(DialogueStateTransition.sequence_no)
                    )
                )
                .scalars()
                .all()
            )
            reception = (
                await session.execute(select(Reception).where(Reception.id == reception_id))
            ).scalar_one()
            facts = list(
                (
                    await session.execute(
                        select(TagAssignmentFact).order_by(TagAssignmentFact.revision)
                    )
                )
                .scalars()
                .all()
            )
            current = (await session.execute(select(TagAssignmentCurrent))).scalar_one()
            events = list(
                (
                    await session.execute(
                        select(ProvenanceEvent).where(ProvenanceEvent.reception_id == reception_id)
                    )
                )
                .scalars()
                .all()
            )
            return unit, transitions, reception, facts, current, events

    unit, transitions, reception, facts, current, events = _run(_rollback_state())
    assert unit.business_stage == "需求了解"
    assert unit.version == 1
    assert unit.edit_status == "auto"
    assert [(item.from_state, item.to_state) for item in transitions] == [
        ("__start__", "需求了解"),
        ("需求了解", "试驾"),
    ]
    assert reception.version == 1
    assert [fact.id for fact in facts] == [fact_id]
    assert current.fact_id == fact_id
    assert events == []


@pytest.mark.integration
def test_review_decision_stage_correction_updates_every_reception_projection(
    reception_tag_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    from audio_graphy.models.reception import DialogueStateTransition
    from audio_graphy.models.tag_governance import TagAssignmentCurrent

    reception_id, unit_id, segment_id = _run(_seed_reception(db_session_factory))
    _run(
        _seed_stage_chain(
            db_session_factory,
            reception_id=reception_id,
            unit_id=unit_id,
        )
    )
    fact_id, schema_version_id = _run(
        _seed_canonical_tag(
            db_session_factory,
            unit_id=unit_id,
            segment_id=segment_id,
            tag_key="stage",
            tag_value="需求了解",
            allowed_values=["需求了解", "报价"],
        )
    )
    batch = reception_tag_client.post(
        "/api/v1/tag-reviews/create-batch",
        json={
            "reason": "conflict",
            "subjects": [
                {
                    "subject_type": "dialogue_unit",
                    "subject_id": unit_id,
                    "reception_id": reception_id,
                    "tag_key": "stage",
                    "proposed_value": "需求了解",
                    "schema_version_id": schema_version_id,
                    "evidence_refs": [
                        {
                            "ref_id": f"segment:{segment_id}",
                            "segment_id": segment_id,
                            "recording_id": 1,
                            "start_sec": 0,
                            "end_sec": 12,
                        }
                    ],
                    "priority": 100,
                }
            ],
        },
        headers=auth_headers["inspector_t1"],
    )
    assert batch.status_code == 201, batch.text
    assert batch.json()["items"][0]["proposed_fact_id"] == fact_id
    claimed = reception_tag_client.post(
        f"/api/v1/tag-reviews/{batch.json()['items'][0]['id']}/claim",
        headers=auth_headers["inspector_t1"],
    )
    assert claimed.status_code == 200, claimed.text

    decided = reception_tag_client.post(
        f"/api/v1/tag-reviews/{batch.json()['items'][0]['id']}/decide",
        json={
            "action": "correct",
            "corrected_value": "报价",
            "reason_code": "manual_workspace_correction",
            "note": "客户已进入报价沟通阶段",
            "evidence_refs": [
                {
                    "ref_id": f"segment:{segment_id}",
                    "segment_id": segment_id,
                    "recording_id": 1,
                    "start_sec": 0,
                    "end_sec": 12,
                }
            ],
        },
        headers=auth_headers["inspector_t1"],
    )
    assert decided.status_code == 200, decided.text
    corrected_fact_id = decided.json()["fact"]["id"]
    assert corrected_fact_id != fact_id

    async def _review_state() -> tuple[
        DialogueUnit,
        list[DialogueStateTransition],
        Reception,
        TagAssignmentCurrent,
        list[ProvenanceEvent],
    ]:
        async with db_session_factory() as session:
            unit = (
                await session.execute(select(DialogueUnit).where(DialogueUnit.id == unit_id))
            ).scalar_one()
            transitions = list(
                (
                    await session.execute(
                        select(DialogueStateTransition)
                        .where(DialogueStateTransition.reception_id == reception_id)
                        .order_by(DialogueStateTransition.sequence_no)
                    )
                )
                .scalars()
                .all()
            )
            reception = (
                await session.execute(select(Reception).where(Reception.id == reception_id))
            ).scalar_one()
            current = (await session.execute(select(TagAssignmentCurrent))).scalar_one()
            events = list(
                (
                    await session.execute(
                        select(ProvenanceEvent)
                        .where(ProvenanceEvent.reception_id == reception_id)
                        .order_by(ProvenanceEvent.id)
                    )
                )
                .scalars()
                .all()
            )
            return unit, transitions, reception, current, events

    unit, transitions, reception, current, events = _run(_review_state())
    assert current.fact_id == corrected_fact_id
    assert unit.business_stage == "报价"
    assert unit.version == 2
    assert unit.edit_status == "manual_edited"
    assert reception.version == 2
    assert [(item.from_state, item.to_state) for item in transitions] == [
        ("__start__", "报价"),
        ("报价", "试驾"),
    ]
    assert {(event.object_type, event.event_type) for event in events} >= {
        ("tag_assignment_fact", "superseded"),
        ("tag_assignment_fact", "edited"),
        ("dialogue_unit", "edited"),
    }


@pytest.mark.integration
def test_review_decision_bootstraps_legacy_tag_into_canonical_current(
    reception_tag_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    from audio_graphy.models.tag_governance import (
        TagAssignmentCurrent,
        TagAssignmentFact,
        TagGovernanceAuditEvent,
    )

    reception_id, unit_id, segment_id = _run(_seed_reception(db_session_factory))
    derived = _run(
        _legacy_derive(
            db_session_factory,
            reception_id=reception_id,
            target_labels=["intent"],
            actor="user:2",
        )
    )
    legacy = derived.assignments[0]
    schema_version_id = _run(_seed_review_schema(db_session_factory))
    evidence_refs = [
        {
            "ref_id": f"segment:{segment_id}",
            "kind": "audio",
            "segment_id": segment_id,
            "recording_id": legacy.evidence_refs[0]["recording_id"],
            "start_sec": 0,
            "end_sec": 12,
        }
    ]

    batch = reception_tag_client.post(
        "/api/v1/tag-reviews/create-batch",
        json={
            "reason": "conflict",
            "subjects": [
                {
                    "subject_type": "dialogue_unit",
                    "subject_id": unit_id,
                    "reception_id": reception_id,
                    "tag_key": "intent",
                    "proposed_value": legacy.label_value,
                    "schema_version_id": schema_version_id,
                    "evidence_refs": evidence_refs,
                    "priority": 100,
                }
            ],
        },
        headers=auth_headers["inspector_t1"],
    )
    assert batch.status_code == 201, batch.text
    task = batch.json()["items"][0]
    assert task["proposed_fact_id"] is None
    claimed = reception_tag_client.post(
        f"/api/v1/tag-reviews/{task['id']}/claim",
        headers=auth_headers["inspector_t1"],
    )
    assert claimed.status_code == 200, claimed.text

    decided = reception_tag_client.post(
        f"/api/v1/tag-reviews/{task['id']}/decide",
        json={
            "action": "correct",
            "corrected_value": "medium",
            "reason_code": "manual_workspace_correction",
            "note": "历史标签首次纳入 canonical 治理",
            "evidence_refs": evidence_refs,
        },
        headers=auth_headers["inspector_t1"],
    )
    assert decided.status_code == 200, decided.text
    fact_id = decided.json()["fact"]["id"]

    async def _canonical_state() -> tuple[Any, ...]:
        async with db_session_factory() as session:
            current = (await session.execute(select(TagAssignmentCurrent))).scalar_one()
            fact = (
                await session.execute(
                    select(TagAssignmentFact).where(TagAssignmentFact.id == current.fact_id)
                )
            ).scalar_one()
            legacy_assignment = (
                await session.execute(
                    select(DialogueTagAssignment).where(DialogueTagAssignment.id == legacy.id)
                )
            ).scalar_one()
            audit = (
                await session.execute(
                    select(TagGovernanceAuditEvent).where(
                        TagGovernanceAuditEvent.resource_type == "tag_assignment_fact",
                        TagGovernanceAuditEvent.resource_id == fact.id,
                        TagGovernanceAuditEvent.action == "manual_corrected",
                    )
                )
            ).scalar_one()
            return current, fact, legacy_assignment, audit

    current, fact, legacy_assignment, audit = _run(_canonical_state())
    assert current.fact_id == fact_id
    assert fact.tag_value == "medium"
    assert fact.source == "manual"
    assert fact.superseded_fact_id is None
    assert legacy_assignment.is_current is True
    assert audit.payload["bootstrap_canonical"] is True

    workspace = reception_tag_client.get(
        f"/api/v1/receptions/{reception_id}/workspace",
        headers=auth_headers["inspector_t1"],
    )
    assert workspace.status_code == 200, workspace.text
    intents = [
        item
        for item in workspace.json()["tag_assignments"]
        if item["dialogue_unit_id"] == unit_id and item["label_key"] == "intent"
    ]
    assert len(intents) == 1
    assert intents[0]["label_value"] == "medium"
    assert intents[0]["model_run_id"] == f"fact:{fact_id}"


@pytest.mark.integration
def test_legacy_bootstrap_review_conflicts_when_canonical_current_appears(
    reception_tag_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    from audio_graphy.models.tag_governance import (
        TagAssignmentCurrent,
        TagAssignmentFact,
        TagReviewDecision,
        TagReviewTask,
    )
    from audio_graphy.services.tag_governance import TagGovernanceService

    reception_id, unit_id, segment_id = _run(_seed_reception(db_session_factory))
    schema_version_id = _run(_seed_review_schema(db_session_factory))
    evidence_refs = [
        {
            "ref_id": f"segment:{segment_id}",
            "kind": "audio",
            "segment_id": segment_id,
            "recording_id": 1,
            "start_sec": 0,
            "end_sec": 12,
        }
    ]
    batch = reception_tag_client.post(
        "/api/v1/tag-reviews/create-batch",
        json={
            "reason": "conflict",
            "subjects": [
                {
                    "subject_type": "dialogue_unit",
                    "subject_id": unit_id,
                    "reception_id": reception_id,
                    "tag_key": "intent",
                    "proposed_value": "high",
                    "schema_version_id": schema_version_id,
                    "evidence_refs": evidence_refs,
                }
            ],
        },
        headers=auth_headers["inspector_t1"],
    )
    assert batch.status_code == 201, batch.text
    task = batch.json()["items"][0]
    assert task["proposed_fact_id"] is None

    concurrent_fact = _run(
        TagGovernanceService(db_session_factory).append_assignment(
            tenant_id="chang_an",
            subject_type="dialogue_unit",
            subject_id=unit_id,
            tag_key="intent",
            tag_value="high",
            confidence=0.9,
            evidence_refs=evidence_refs,
            source="imported",
            schema_version_id=schema_version_id,
            tagger_version_id=None,
            extraction_run_id=None,
            deployment_id=None,
            input_hash="9" * 64,
            actor_user_id=1,
        )
    )
    claimed = reception_tag_client.post(
        f"/api/v1/tag-reviews/{task['id']}/claim",
        headers=auth_headers["inspector_t1"],
    )
    assert claimed.status_code == 200, claimed.text

    rejected = reception_tag_client.post(
        f"/api/v1/tag-reviews/{task['id']}/decide",
        json={
            "action": "correct",
            "corrected_value": "medium",
            "reason_code": "manual_workspace_correction",
            "note": "任务创建后出现了新的 canonical 事实",
            "evidence_refs": evidence_refs,
        },
        headers=auth_headers["inspector_t1"],
    )
    assert rejected.status_code == 409

    async def _state() -> tuple[Any, ...]:
        async with db_session_factory() as session:
            current = (await session.execute(select(TagAssignmentCurrent))).scalar_one()
            facts = list(
                (await session.execute(select(TagAssignmentFact).order_by(TagAssignmentFact.id)))
                .scalars()
                .all()
            )
            review_task = (
                await session.execute(select(TagReviewTask).where(TagReviewTask.id == task["id"]))
            ).scalar_one()
            decisions = list((await session.execute(select(TagReviewDecision))).scalars().all())
            reception = (
                await session.execute(select(Reception).where(Reception.id == reception_id))
            ).scalar_one()
            return current, facts, review_task, decisions, reception

    current, facts, review_task, decisions, reception = _run(_state())
    assert current.fact_id == concurrent_fact.id
    assert [fact.id for fact in facts] == [concurrent_fact.id]
    assert review_task.status == "claimed"
    assert decisions == []
    assert reception.version == 1


@pytest.mark.integration
def test_review_decision_stage_correction_rolls_back_as_one_transaction(
    reception_tag_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    from audio_graphy.models.reception import DialogueStateTransition
    from audio_graphy.models.tag_governance import (
        TagAssignmentCurrent,
        TagAssignmentFact,
        TagReviewTask,
        TagSchemaVersion,
    )

    reception_id, unit_id, segment_id = _run(_seed_reception(db_session_factory))
    _run(
        _seed_stage_chain(
            db_session_factory,
            reception_id=reception_id,
            unit_id=unit_id,
        )
    )
    fact_id, schema_version_id = _run(
        _seed_canonical_tag(
            db_session_factory,
            unit_id=unit_id,
            segment_id=segment_id,
            tag_key="stage",
            tag_value="需求了解",
            allowed_values=["需求了解", "报价"],
        )
    )
    batch = reception_tag_client.post(
        "/api/v1/tag-reviews/create-batch",
        json={
            "reason": "conflict",
            "subjects": [
                {
                    "subject_type": "dialogue_unit",
                    "subject_id": unit_id,
                    "tag_key": "stage",
                    "proposed_fact_id": fact_id,
                }
            ],
        },
        headers=auth_headers["admin_t1"],
    )
    assert batch.status_code == 201
    task_id = batch.json()["items"][0]["id"]
    claimed = reception_tag_client.post(
        f"/api/v1/tag-reviews/{task_id}/claim",
        headers=auth_headers["admin_t1"],
    )
    assert claimed.status_code == 200, claimed.text

    async def _unpublish_schema() -> None:
        async with db_session_factory() as session, session.begin():
            schema_version = (
                await session.execute(
                    select(TagSchemaVersion).where(TagSchemaVersion.id == schema_version_id)
                )
            ).scalar_one()
            schema_version.status = "draft"

    _run(_unpublish_schema())
    rejected = reception_tag_client.post(
        f"/api/v1/tag-reviews/{task_id}/decide",
        json={
            "action": "correct",
            "corrected_value": "报价",
            "reason_code": "manual_workspace_correction",
            "note": "必须全部回滚",
            "evidence_refs": [
                {
                    "ref_id": f"segment:{segment_id}",
                    "segment_id": segment_id,
                    "recording_id": 1,
                    "start_sec": 0,
                    "end_sec": 12,
                }
            ],
        },
        headers=auth_headers["admin_t1"],
    )
    assert rejected.status_code == 422

    async def _rollback_state() -> tuple[Any, ...]:
        async with db_session_factory() as session:
            unit = (
                await session.execute(select(DialogueUnit).where(DialogueUnit.id == unit_id))
            ).scalar_one()
            transitions = list(
                (
                    await session.execute(
                        select(DialogueStateTransition)
                        .where(DialogueStateTransition.reception_id == reception_id)
                        .order_by(DialogueStateTransition.sequence_no)
                    )
                )
                .scalars()
                .all()
            )
            reception = (
                await session.execute(select(Reception).where(Reception.id == reception_id))
            ).scalar_one()
            facts = list(
                (
                    await session.execute(
                        select(TagAssignmentFact).order_by(TagAssignmentFact.revision)
                    )
                )
                .scalars()
                .all()
            )
            current = (await session.execute(select(TagAssignmentCurrent))).scalar_one()
            task = (
                await session.execute(select(TagReviewTask).where(TagReviewTask.id == task_id))
            ).scalar_one()
            events = list(
                (
                    await session.execute(
                        select(ProvenanceEvent).where(ProvenanceEvent.reception_id == reception_id)
                    )
                )
                .scalars()
                .all()
            )
            return unit, transitions, reception, facts, current, task, events

    unit, transitions, reception, facts, current, task, events = _run(_rollback_state())
    assert unit.business_stage == "需求了解"
    assert unit.version == 1
    assert [(item.from_state, item.to_state) for item in transitions] == [
        ("__start__", "需求了解"),
        ("需求了解", "试驾"),
    ]
    assert reception.version == 1
    assert [fact.id for fact in facts] == [fact_id]
    assert current.fact_id == fact_id
    assert task.status == "claimed"
    assert events == []


@pytest.mark.integration
def test_derive_preserves_manual_current_assignment(
    reception_tag_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    reception_id, _unit_id, segment_id = _run(_seed_reception(db_session_factory))
    first = _run(
        _legacy_derive(
            db_session_factory,
            reception_id=reception_id,
            target_labels=["intent"],
        )
    )
    automatic = first.assignments[0]
    correction = reception_tag_client.patch(
        f"/api/v1/receptions/{reception_id}/dialogue-tags/{automatic.id}",
        json={
            "expected_reception_version": 1,
            "expected_group_version": "automotive-v1",
            "label_value": "medium",
            "reason": "人工事实优先",
            "evidence_ref_ids": [f"segment:{segment_id}"],
        },
        headers=auth_headers["admin_t1"],
    )
    assert correction.status_code == 200
    manual_id = correction.json()["assignment"]["id"]

    rerun = _run(
        _legacy_derive(
            db_session_factory,
            reception_id=reception_id,
            target_labels=["intent"],
            group_version="automotive-v2",
            actor="user:2",
        )
    )

    assert rerun.no_op is True
    assert rerun.superseded_count == 0
    assert [assignment.id for assignment in rerun.assignments] == [manual_id]
    assert rerun.assignments[0].source == "manual"
    assert rerun.assignments[0].label_value == "medium"
    state = _run(_tag_state(db_session_factory, reception_id))
    assert len(state["tags"]) == 2
    assert [tag.id for tag in state["tags"] if tag.is_current] == [manual_id]
    assert state["tags"][-1].source == "manual"


@pytest.mark.integration
def test_derive_scrubs_legacy_raw_transcript_before_rule_tagging(
    reception_tag_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from audio_graphy.services.reception_tagging import ReceptionRuleTagger

    raw_text = "联系电话 13812345678，客户想先安排试驾。"
    reception_id, _unit_id, segment_id = _run(
        _seed_reception(
            db_session_factory,
            transcript=raw_text,
        )
    )
    _run(_clear_segment_scrubbed_text(db_session_factory, segment_id))
    captured_texts: list[str] = []
    original_derive = ReceptionRuleTagger.derive

    def capture_derive(
        self: ReceptionRuleTagger,
        *,
        scenario: Any,
        unit: Any,
        target_labels: Any,
    ) -> Any:
        captured_texts.extend(segment.text for segment in unit.segments)
        return original_derive(
            self,
            scenario=scenario,
            unit=unit,
            target_labels=target_labels,
        )

    monkeypatch.setattr(ReceptionRuleTagger, "derive", capture_derive)
    _run(
        _legacy_derive(
            db_session_factory,
            reception_id=reception_id,
            target_labels=["next_step"],
            group_key="legacy-pii",
            group_version="v1",
            actor="user:2",
        )
    )

    assert captured_texts == ["联系电话 138****5678，客户想先安排试驾。"]
    assert all("13812345678" not in text for text in captured_texts)


@pytest.mark.integration
def test_derive_enforces_write_role_and_tenant_isolation(
    reception_tag_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    reception_id, _unit_id, _segment_id = _run(_seed_reception(db_session_factory))
    url = f"/api/v1/receptions/{reception_id}/dialogue-tags/derive"
    body = {"group_version": "v1", "target_labels": ["stage"]}

    forbidden = reception_tag_client.post(
        url,
        json=body,
        headers=auth_headers["viewer_t1"],
    )
    assert forbidden.status_code == 403

    hidden = reception_tag_client.post(
        url,
        json=body,
        headers=auth_headers["inspector_t2"],
    )
    assert hidden.status_code == 404
    assert hidden.json()["detail"] == "reception not found"


@pytest.mark.integration
def test_derive_requires_new_version_when_evidence_changes(
    reception_tag_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    reception_id, _unit_id, segment_id = _run(_seed_reception(db_session_factory))
    initial = _run(
        _legacy_derive(
            db_session_factory,
            reception_id=reception_id,
            target_labels=["intent"],
            group_version="immutable-v1",
        )
    )
    assert initial.assignments[0].label_value == "high"

    _run(
        _replace_segment_text(
            db_session_factory,
            segment_id,
            "客户说先看车，考虑一下。",
        )
    )
    from audio_graphy.errors import ConflictError

    with pytest.raises(ConflictError) as conflict:
        _run(
            _legacy_derive(
                db_session_factory,
                reception_id=reception_id,
                target_labels=["intent"],
                group_version="immutable-v1",
            )
        )
    assert conflict.value.code == "TAG_VERSION_REUSE_CONFLICT"
    state = _run(_tag_state(db_session_factory, reception_id))
    assert len(state["tags"]) == 1
    assert state["tags"][0].label_value == "high"
    assert state["tags"][0].is_current is True


@pytest.mark.integration
def test_derive_clips_verified_source_and_timeline_coordinates_after_split(
    reception_tag_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    reception_id, unit_id, segment_id = _run(_seed_reception(db_session_factory))
    _run(
        _offset_and_split_unit(
            db_session_factory,
            reception_id,
            unit_id,
            segment_id,
        )
    )

    response = _run(
        _legacy_derive(
            db_session_factory,
            reception_id=reception_id,
            target_labels=["stage"],
            group_version="split-v1",
            actor="user:2",
        )
    )

    evidence = response.assignments[0].evidence_refs[0]
    assert evidence["coordinate_space"] == "reception_timeline"
    assert evidence["source_start_sec"] == 2.0
    assert evidence["source_end_sec"] == 6.0
    assert evidence["timeline_start_sec"] == 32.0
    assert evidence["timeline_end_sec"] == 36.0
    assert evidence["start_ms"] == 32_000
    assert evidence["end_ms"] == 36_000


@pytest.mark.integration
def test_derive_never_uses_cross_tenant_or_missing_segment_reference(
    reception_tag_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    reception_id, unit_id, _segment_id = _run(_seed_reception(db_session_factory))

    async def _break_reference() -> None:
        async with db_session_factory() as session:
            unit = await session.get(DialogueUnit, unit_id)
            assert unit is not None
            unit.segment_refs = [{"recording_id": 999, "segment_id": 999}]
            await session.commit()

    _run(_break_reference())
    response = _run(
        _legacy_derive(
            db_session_factory,
            reception_id=reception_id,
            target_labels=[
                "stage",
                "intent",
                "objection",
                "next_step",
                "compliance_risk",
            ],
            group_version="v1",
        )
    )

    assert response.assignments == []
    assert response.missing[0].reason == "no_verified_segment_evidence"
    state = _run(_tag_state(db_session_factory, reception_id))
    assert state["tags"] == []
    assert state["events"] == []


async def _seed_manual_groups(
    factory: Any,
    reception_id: int,
    unit_id: int,
    segment_id: int,
) -> None:
    now = datetime.now(UTC)
    evidence = [
        {
            "ref_id": f"segment:{segment_id}",
            "kind": "audio",
            "segment_id": segment_id,
            "recording_id": 1,
            "start_ms": 0,
            "end_ms": 12_000,
        }
    ]
    async with factory() as session:
        session.add_all(
            [
                DialogueTagAssignment(
                    tenant_id="chang_an",
                    reception_id=reception_id,
                    dialogue_unit_id=unit_id,
                    group_key="review-a",
                    group_version="v1",
                    label_key="intent",
                    label_value="high",
                    confidence=0.9,
                    source="manual",
                    priority=20,
                    evidence_refs=evidence,
                    model_run_id="manual-a",
                    is_current=True,
                    assigned_at=now,
                ),
                DialogueTagAssignment(
                    tenant_id="chang_an",
                    reception_id=reception_id,
                    dialogue_unit_id=unit_id,
                    group_key="review-b",
                    group_version="v2",
                    label_key="intent",
                    label_value="medium",
                    confidence=0.7,
                    source="llm",
                    priority=10,
                    evidence_refs=evidence,
                    model_run_id="model-b",
                    is_current=True,
                    assigned_at=now,
                ),
                DialogueTagAssignment(
                    tenant_id="chang_an",
                    reception_id=reception_id,
                    dialogue_unit_id=unit_id,
                    group_key="review-a",
                    group_version="v1",
                    label_key="next_step",
                    label_value="test_drive",
                    confidence=0.88,
                    source="manual",
                    priority=20,
                    evidence_refs=evidence,
                    model_run_id="manual-a",
                    is_current=True,
                    assigned_at=now,
                ),
            ]
        )
        await session.commit()


async def _seed_historical_versions(
    factory: Any,
    reception_id: int,
    unit_id: int,
    segment_id: int,
) -> None:
    now = datetime.now(UTC)
    evidence = [
        {
            "ref_id": f"segment:{segment_id}",
            "kind": "audio",
            "segment_id": segment_id,
            "recording_id": 1,
            "start_ms": 0,
            "end_ms": 12_000,
        }
    ]
    async with factory() as session:
        session.add_all(
            [
                DialogueTagAssignment(
                    tenant_id="chang_an",
                    reception_id=reception_id,
                    dialogue_unit_id=unit_id,
                    group_key="review",
                    group_version="v1",
                    label_key="intent",
                    label_value="high",
                    confidence=1.0,
                    source="manual",
                    priority=5,
                    evidence_refs=evidence,
                    model_run_id="review-v1",
                    is_current=False,
                    assigned_at=now - timedelta(days=1),
                ),
                DialogueTagAssignment(
                    tenant_id="chang_an",
                    reception_id=reception_id,
                    dialogue_unit_id=unit_id,
                    group_key="review",
                    group_version="v2",
                    label_key="intent",
                    label_value="medium",
                    confidence=0.8,
                    source="llm",
                    priority=20,
                    evidence_refs=evidence,
                    model_run_id="review-v2",
                    is_current=True,
                    assigned_at=now,
                ),
                DialogueTagAssignment(
                    tenant_id="chang_an",
                    reception_id=reception_id,
                    dialogue_unit_id=unit_id,
                    group_key="audit",
                    group_version="v3",
                    label_key="intent",
                    label_value="high",
                    confidence=0.95,
                    source="manual",
                    priority=10,
                    evidence_refs=evidence,
                    model_run_id="audit-v3",
                    is_current=True,
                    assigned_at=now,
                ),
            ]
        )
        await session.commit()


@pytest.mark.integration
def test_database_insights_are_filterable_paginated_and_visualization_ready(
    reception_tag_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    first_id, first_unit, first_segment = _run(
        _seed_reception(
            db_session_factory,
            store_id="S001",
            started_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
    )
    second_id, _second_unit, _second_segment = _run(
        _seed_reception(
            db_session_factory,
            store_id="S002",
            started_at=datetime(2026, 7, 2, tzinfo=UTC),
        )
    )
    _run(
        _seed_manual_groups(
            db_session_factory,
            first_id,
            first_unit,
            first_segment,
        )
    )
    # A foreign-tenant row with the same store must never affect totals.
    _run(
        _seed_reception(
            db_session_factory,
            tenant_id="byd",
            store_id="S001",
            agent_name="agent_byd",
        )
    )

    response = reception_tag_client.get(
        "/api/v1/reception-tag-insights",
        params=[
            ("store_id", "S001"),
            ("scenario", "automotive"),
            ("reception_id", str(first_id)),
            ("reception_id", str(second_id)),
            ("page", "1"),
            ("page_size", "1"),
            ("assignment_limit", "100"),
        ],
        headers=auth_headers["viewer_t1"],
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_receptions"] == 1
    assert payload["returned_reception_ids"] == [first_id]
    assert payload["assignment_count"] == 3
    insights = payload["insights"]
    assert insights["overview"]["group_count"] == 2
    assert insights["overview"]["conflict_cells"] == 1
    assert any(row["conflict"] for row in insights["matrix"])
    assert any(row["missing_group_keys"] for row in insights["matrix"])
    assert insights["distributions"]
    assert insights["trends"]
    assert len(payload["evidence_summary"]) == 3
    assert all(item["evidence_refs"] for item in payload["evidence_summary"])
    assert payload["assignment_truncated"] is False
    assert payload["difference_truncated"] is False
    assert payload["evidence_summary_total"] == 3
    assert payload["evidence_summary_count"] == 3
    assert payload["evidence_summary_limit"] == 256
    assert payload["evidence_summary_truncated"] is False
    assert payload["evidence_ref_count"] <= payload["evidence_ref_limit"] == 1_024


@pytest.mark.integration
def test_database_insights_default_to_current_versions_only(
    reception_tag_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    reception_id, unit_id, segment_id = _run(_seed_reception(db_session_factory))
    _run(
        _seed_historical_versions(
            db_session_factory,
            reception_id,
            unit_id,
            segment_id,
        )
    )

    response = reception_tag_client.get(
        "/api/v1/reception-tag-insights",
        params=[("group_key", "review")],
        headers=auth_headers["viewer_t1"],
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["selection_mode"] == "current"
    assert payload["selected_group_ids"] == ["review@v2"]
    assert payload["assignment_count"] == 1
    assert payload["insights"]["groups"][0]["group_id"] == "review@v2"
    assert {item["group_id"] for item in payload["evidence_summary"]} == {"review@v2"}
    assert {
        item["group_key"]
        for item in payload["insights"]["trends"]
        if item["group_key"] != "__merged__"
    } == {"review@v2"}


@pytest.mark.integration
@pytest.mark.parametrize(
    ("strategy", "expected_values", "expected_selected"),
    [
        (
            "union",
            ["high", "medium"],
            ["review@v2", "audit@v3", "review@v1"],
        ),
        ("intersection", [], []),
        ("priority", ["medium"], ["review@v2"]),
        ("manual_wins", ["high"], ["audit@v3"]),
    ],
)
def test_database_insights_compare_exact_historical_versions_without_mixing(
    strategy: str,
    expected_values: list[str],
    expected_selected: list[str],
    reception_tag_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    reception_id, unit_id, segment_id = _run(_seed_reception(db_session_factory))
    _run(
        _seed_historical_versions(
            db_session_factory,
            reception_id,
            unit_id,
            segment_id,
        )
    )

    response = reception_tag_client.get(
        "/api/v1/reception-tag-insights",
        params=[
            ("group_id", "review@v1"),
            ("group_id", "review@v2"),
            ("group_id", "audit@v3"),
            ("merge_strategy", strategy),
        ],
        headers=auth_headers["viewer_t1"],
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["selection_mode"] == "exact_versions"
    assert payload["selected_group_ids"] == [
        "review@v1",
        "review@v2",
        "audit@v3",
    ]
    assert payload["assignment_count"] == 3
    assert [group["group_id"] for group in payload["insights"]["groups"]] == [
        "review@v2",
        "audit@v3",
        "review@v1",
    ]
    matrix_row = payload["insights"]["matrix"][0]
    assert {cell["group"]["group_id"] for cell in matrix_row["cells"]} == {
        "review@v1",
        "review@v2",
        "audit@v3",
    }
    assert matrix_row["conflict"] is True
    assert matrix_row["merged"]["values"] == expected_values
    assert matrix_row["merged"]["selected_group_keys"] == expected_selected
    assert {item["group_id"] for item in payload["evidence_summary"]} == {
        "review@v1",
        "review@v2",
        "audit@v3",
    }
    for collection in (
        payload["insights"]["coverage"],
        payload["insights"]["distributions"],
        payload["insights"]["trends"],
        payload["insights"]["confidence"],
        payload["insights"]["dimension_comparisons"],
    ):
        assert all(
            item["group_key"] == "__merged__" or "@" in item["group_key"] for item in collection
        )


@pytest.mark.integration
def test_exact_version_filters_are_bounded_and_cannot_mix_with_broad_group_keys(
    reception_tag_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
) -> None:
    conflict = reception_tag_client.get(
        "/api/v1/reception-tag-insights",
        params=[
            ("group_key", "review"),
            ("group_id", "review@v1"),
        ],
        headers=auth_headers["admin_t1"],
    )
    assert conflict.status_code == 422
    assert conflict.json()["error"]["code"] == "TAG_INSIGHTS_GROUP_FILTER_CONFLICT"

    too_many = reception_tag_client.get(
        "/api/v1/reception-tag-insights",
        params=[("group_id", f"review@v{index}") for index in range(9)],
        headers=auth_headers["admin_t1"],
    )
    assert too_many.status_code == 422
    assert too_many.json()["error"]["code"] == "TAG_INSIGHTS_FILTER_LIMIT"


@pytest.mark.integration
def test_insights_empty_page_and_agent_scope_do_not_leak_other_agents(
    reception_tag_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    reception_id, unit_id, segment_id = _run(
        _seed_reception(
            db_session_factory,
            agent_name="someone_else",
        )
    )
    _run(
        _seed_manual_groups(
            db_session_factory,
            reception_id,
            unit_id,
            segment_id,
        )
    )

    response = reception_tag_client.get(
        "/api/v1/reception-tag-insights",
        headers=auth_headers["agent_t1"],
    )

    assert response.status_code == 200
    assert response.json()["total_receptions"] == 0
    assert response.json()["insights"] is None
    assert response.json()["evidence_summary"] == []


@pytest.mark.integration
def test_insights_assignment_limit_is_bounded_by_schema(
    reception_tag_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
) -> None:
    response = reception_tag_client.get(
        "/api/v1/reception-tag-insights?assignment_limit=5001",
        headers=auth_headers["admin_t1"],
    )
    assert response.status_code == 422


@pytest.mark.integration
@pytest.mark.parametrize(
    "query",
    [
        "matrix_limit=97",
        "difference_limit=129",
        "evidence_summary_limit=257",
    ],
)
def test_insights_output_budgets_are_bounded_by_schema(
    query: str,
    reception_tag_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
) -> None:
    response = reception_tag_client.get(
        f"/api/v1/reception-tag-insights?{query}",
        headers=auth_headers["admin_t1"],
    )
    assert response.status_code == 422
