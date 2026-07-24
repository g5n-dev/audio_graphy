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
def test_derive_writes_evidence_version_and_stable_actor(
    reception_tag_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    reception_id, _unit_id, segment_id = _run(_seed_reception(db_session_factory))
    url = f"/api/v1/receptions/{reception_id}/dialogue-tags/derive"
    body = {
        "group_key": "sales-rules",
        "group_version": "automotive-v1",
        "target_labels": [
            "stage",
            "intent",
            "objection",
            "next_step",
            "compliance_risk",
        ],
    }

    first = reception_tag_client.post(
        url,
        json=body,
        headers=auth_headers["inspector_t1"],
    )
    assert first.status_code == 200
    assert first.json()["assignment_count"] == 5
    assert first.json()["no_op"] is False
    assert {assignment["label_key"] for assignment in first.json()["assignments"]} == set(
        body["target_labels"]
    )
    assert all(
        assignment["source"] == "rule"
        and assignment["confidence"] > 0
        and assignment["evidence_refs"]
        and assignment["evidence_refs"][0]["segment_id"] == segment_id
        and assignment["evidence_refs"][0]["coordinate_space"] == "reception_timeline"
        and assignment["evidence_refs"][0]["source_start_sec"] == 0.0
        and assignment["evidence_refs"][0]["source_end_sec"] == 12.0
        and assignment["evidence_refs"][0]["timeline_start_sec"] == 0.0
        and assignment["evidence_refs"][0]["timeline_end_sec"] == 12.0
        for assignment in first.json()["assignments"]
    )

    repeated = reception_tag_client.post(
        url,
        json=body,
        headers=auth_headers["inspector_t1"],
    )
    assert repeated.status_code == 200
    assert repeated.json()["no_op"] is True
    state = _run(_tag_state(db_session_factory, reception_id))
    assert len(state["tags"]) == 5
    assert all(item.is_current for item in state["tags"])
    assert {event.actor for event in state["events"]} == {"user:2"}
    assert all(
        {"type": "segment", "id": segment_id} in event.parent_refs for event in state["events"]
    )

    upgraded = reception_tag_client.post(
        url,
        json={**body, "group_version": "automotive-v2"},
        headers=auth_headers["admin_t1"],
    )
    assert upgraded.status_code == 200
    state = _run(_tag_state(db_session_factory, reception_id))
    assert len(state["tags"]) == 10
    assert sum(item.is_current for item in state["tags"]) == 5
    assert {item.group_version for item in state["tags"] if item.is_current} == {"automotive-v2"}
    assert {event.actor for event in state["events"]} == {"user:1", "user:2"}


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
    response = reception_tag_client.post(
        f"/api/v1/receptions/{reception_id}/dialogue-tags/derive",
        json={
            "group_key": "legacy-pii",
            "group_version": "v1",
            "target_labels": ["next_step"],
        },
        headers=auth_headers["inspector_t1"],
    )

    assert response.status_code == 200
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
    assert hidden.json()["error"]["code"] == "RECEPTION_NOT_FOUND"


@pytest.mark.integration
def test_derive_requires_new_version_when_evidence_changes(
    reception_tag_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    reception_id, _unit_id, segment_id = _run(_seed_reception(db_session_factory))
    url = f"/api/v1/receptions/{reception_id}/dialogue-tags/derive"
    body = {
        "group_key": "sales-rules",
        "group_version": "immutable-v1",
        "target_labels": ["intent"],
    }
    initial = reception_tag_client.post(
        url,
        json=body,
        headers=auth_headers["admin_t1"],
    )
    assert initial.status_code == 200
    assert initial.json()["assignments"][0]["label_value"] == "high"

    _run(
        _replace_segment_text(
            db_session_factory,
            segment_id,
            "客户说先看车，考虑一下。",
        )
    )
    conflict = reception_tag_client.post(
        url,
        json=body,
        headers=auth_headers["admin_t1"],
    )

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "TAG_VERSION_REUSE_CONFLICT"
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

    response = reception_tag_client.post(
        f"/api/v1/receptions/{reception_id}/dialogue-tags/derive",
        json={"group_version": "split-v1", "target_labels": ["stage"]},
        headers=auth_headers["inspector_t1"],
    )

    assert response.status_code == 200
    evidence = response.json()["assignments"][0]["evidence_refs"][0]
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
    response = reception_tag_client.post(
        f"/api/v1/receptions/{reception_id}/dialogue-tags/derive",
        json={
            "group_version": "v1",
            "target_labels": [
                "stage",
                "intent",
                "objection",
                "next_step",
                "compliance_risk",
            ],
        },
        headers=auth_headers["admin_t1"],
    )

    assert response.status_code == 200
    assert response.json()["assignment_count"] == 0
    assert response.json()["missing"][0]["reason"] == "no_verified_segment_evidence"
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
