"""Stable reception ownership and fail-closed agent authorization tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import _run_async, seed_recording


async def _add_agent(
    factory: Any,
    *,
    user_id: int,
    tenant_id: str,
    name: str,
) -> None:
    from audio_graphy.models import User

    async with factory() as session:
        session.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                name=name,
                email=f"agent-{tenant_id}-{user_id}@example.test",
                role="agent",
                password_hash="mock",
            )
        )
        await session.commit()


async def _rename_user(factory: Any, *, user_id: int, name: str) -> None:
    from audio_graphy.models import User

    async with factory() as session:
        user = await session.get(User, user_id)
        assert user is not None
        user.name = name
        await session.commit()


async def _seed_reception(
    factory: Any,
    *,
    tenant_id: str = "chang_an",
    agent_name: str | None = "agent_ca",
    agent_user_id: int | None,
    with_provenance: bool = False,
) -> int:
    from audio_graphy.models import ProvenanceEvent, Reception

    now = datetime.now(UTC)
    async with factory() as session:
        reception = Reception(
            tenant_id=tenant_id,
            scenario="automotive",
            store_id="S001",
            agent_name=agent_name,
            agent_user_id=agent_user_id,
            customer_hash=None,
            status="ready",
            merge_mode="logical",
            merge_confidence=1.0,
            started_at=now,
            ended_at=now + timedelta(minutes=5),
            version=1,
        )
        session.add(reception)
        await session.flush()
        if with_provenance:
            session.add(
                ProvenanceEvent(
                    tenant_id=tenant_id,
                    reception_id=reception.id,
                    object_type="reception",
                    object_ref=str(reception.id),
                    event_type="created",
                    actor="system",
                    algorithm_version=None,
                    parent_refs=[],
                    evidence_refs=[],
                    payload={"reception_id": reception.id},
                    occurred_at=now,
                )
            )
        await session.commit()
        return reception.id


async def _map_shared_recording_with_provenance(
    factory: Any,
    *,
    recording_id: int,
    reception_ids: tuple[int, int],
) -> None:
    from audio_graphy.models import ProvenanceEvent, ReceptionRecording

    now = datetime.now(UTC)
    async with factory() as session:
        session.add_all(
            [
                ReceptionRecording(
                    tenant_id="chang_an",
                    reception_id=reception_id,
                    recording_id=recording_id,
                    sequence_no=0,
                    timeline_start_sec=0,
                    timeline_end_sec=10,
                    source_start_sec=0,
                    source_end_sec=10,
                    gap_before_sec=0,
                    decision_source="manual",
                    merge_confidence=1,
                    merge_reasons={"seed": True},
                )
                for reception_id in reception_ids
            ]
        )
        session.add(
            ProvenanceEvent(
                tenant_id="chang_an",
                reception_id=None,
                object_type="recording",
                object_ref=str(recording_id),
                event_type="split",
                actor="system",
                algorithm_version="test-v1",
                parent_refs=[],
                evidence_refs=[],
                payload={"child_reception_ids": list(reception_ids)},
                occurred_at=now,
            )
        )
        await session.commit()


async def _add_recording_provenance(factory: Any, *, recording_id: int) -> None:
    from audio_graphy.models import ProvenanceEvent

    async with factory() as session:
        session.add(
            ProvenanceEvent(
                tenant_id="chang_an",
                reception_id=None,
                object_type="recording",
                object_ref=str(recording_id),
                event_type="derived",
                actor="system",
                algorithm_version="test-v1",
                parent_refs=[],
                evidence_refs=[],
                payload={},
                occurred_at=datetime.now(UTC),
            )
        )
        await session.commit()


async def _add_state_transition_provenance(
    factory: Any,
    *,
    reception_id: int,
    tenant_id: str = "chang_an",
) -> int:
    from audio_graphy.models import DialogueStateTransition, ProvenanceEvent

    now = datetime.now(UTC)
    async with factory() as session:
        transition = DialogueStateTransition(
            tenant_id=tenant_id,
            reception_id=reception_id,
            dialogue_unit_id=None,
            sequence_no=0,
            from_state="opening",
            to_state="needs_discovery",
            trigger="test",
            confidence=0.9,
            evidence_refs=[],
            algorithm_version="test-v1",
        )
        session.add(transition)
        await session.flush()
        session.add(
            ProvenanceEvent(
                tenant_id=tenant_id,
                reception_id=reception_id,
                object_type="dialogue_state_transition",
                object_ref=str(transition.id),
                event_type="derived",
                actor="system",
                algorithm_version="test-v1",
                parent_refs=[],
                evidence_refs=[],
                payload={"reception_id": reception_id},
                occurred_at=now,
            )
        )
        await session.commit()
        return transition.id


async def _delete_state_transition(factory: Any, *, transition_id: int) -> None:
    from audio_graphy.models import DialogueStateTransition

    async with factory() as session:
        transition = await session.get(DialogueStateTransition, transition_id)
        assert transition is not None
        await session.delete(transition)
        await session.commit()


def _agent_headers(jwt_manager: Any, *, user_id: int, tenant_id: str) -> dict[str, str]:
    token = jwt_manager.create_access_token(user_id, tenant_id, "agent")
    return {"Authorization": f"Bearer {token}"}


def _create_body(recording_id: int, *, agent_name: str) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "scenario": "automotive",
        "store_id": "S001",
        "agent_name": agent_name,
        "status": "confirmed",
        "merge_mode": "logical",
        "started_at": now.isoformat(),
        "ended_at": (now + timedelta(seconds=10)).isoformat(),
        "recordings": [
            {
                "recording_id": recording_id,
                "sequence_no": 0,
                "timeline_start_sec": 0,
                "timeline_end_sec": 10,
                "source_start_sec": 0,
                "source_end_sec": 10,
            }
        ],
    }


@pytest.mark.integration
def test_same_name_agents_are_isolated_and_rename_does_not_drift(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    jwt_manager: Any,
    db_session_factory: Any,
) -> None:
    _run_async(
        _add_agent(
            db_session_factory,
            user_id=9,
            tenant_id="chang_an",
            name="agent_ca",
        )
    )
    first = _run_async(
        _seed_reception(
            db_session_factory,
            agent_user_id=3,
            with_provenance=True,
        )
    )
    second = _run_async(
        _seed_reception(
            db_session_factory,
            agent_user_id=9,
            with_provenance=True,
        )
    )
    second_headers = _agent_headers(jwt_manager, user_id=9, tenant_id="chang_an")

    first_queue = test_client.get(
        "/api/v1/receptions",
        headers=auth_headers["agent_t1"],
    )
    second_queue = test_client.get("/api/v1/receptions", headers=second_headers)
    assert [item["id"] for item in first_queue.json()["items"]] == [first]
    assert [item["id"] for item in second_queue.json()["items"]] == [second]
    first_insights = test_client.get(
        "/api/v1/reception-tag-insights",
        headers=auth_headers["agent_t1"],
    )
    second_insights = test_client.get(
        "/api/v1/reception-tag-insights",
        headers=second_headers,
    )
    assert first_insights.json()["returned_reception_ids"] == [first]
    assert second_insights.json()["returned_reception_ids"] == [second]

    assert (
        test_client.get(
            f"/api/v1/receptions/{second}/workspace",
            headers=auth_headers["agent_t1"],
        ).status_code
        == 404
    )
    assert (
        test_client.get(
            f"/api/v1/provenance/reception/{second}",
            headers=auth_headers["agent_t1"],
        ).status_code
        == 404
    )

    shared_recording_id = _run_async(seed_recording(db_session_factory, recording_id=101))
    _run_async(
        _map_shared_recording_with_provenance(
            db_session_factory,
            recording_id=shared_recording_id,
            reception_ids=(first, second),
        )
    )
    for headers in (auth_headers["agent_t1"], second_headers):
        assert (
            test_client.get(
                f"/api/v1/provenance/recording/{shared_recording_id}",
                headers=headers,
            ).status_code
            == 404
        )
    assert (
        test_client.get(
            f"/api/v1/provenance/recording/{shared_recording_id}",
            headers=auth_headers["inspector_t1"],
        ).status_code
        == 200
    )

    _run_async(_rename_user(db_session_factory, user_id=3, name="renamed_agent"))
    renamed_queue = test_client.get(
        "/api/v1/receptions",
        headers=auth_headers["agent_t1"],
    )
    assert [item["id"] for item in renamed_queue.json()["items"]] == [first]
    assert (
        test_client.get(
            f"/api/v1/receptions/{first}/workspace",
            headers=second_headers,
        ).status_code
        == 404
    )


@pytest.mark.integration
def test_ambiguous_historical_name_without_stable_id_is_hidden_from_agents(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    jwt_manager: Any,
    db_session_factory: Any,
) -> None:
    _run_async(
        _add_agent(
            db_session_factory,
            user_id=9,
            tenant_id="chang_an",
            name="agent_ca",
        )
    )
    historical = _run_async(
        _seed_reception(
            db_session_factory,
            agent_user_id=None,
            with_provenance=True,
        )
    )
    second_headers = _agent_headers(jwt_manager, user_id=9, tenant_id="chang_an")

    for headers in (auth_headers["agent_t1"], second_headers):
        queue = test_client.get("/api/v1/receptions", headers=headers)
        assert historical not in {item["id"] for item in queue.json()["items"]}
        assert (
            test_client.get(
                f"/api/v1/receptions/{historical}/workspace",
                headers=headers,
            ).status_code
            == 404
        )
        assert (
            test_client.get(
                f"/api/v1/provenance/reception/{historical}",
                headers=headers,
            ).status_code
            == 404
        )

    assert (
        test_client.get(
            f"/api/v1/receptions/{historical}/workspace",
            headers=auth_headers["inspector_t1"],
        ).status_code
        == 200
    )


@pytest.mark.integration
def test_reception_creation_resolves_server_owned_identity_and_rejects_spoofed_id(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    jwt_manager: Any,
    db_session_factory: Any,
) -> None:
    recording_id = _run_async(seed_recording(db_session_factory))
    body = _create_body(recording_id, agent_name="agent_ca")

    created = test_client.post(
        "/api/v1/receptions",
        json=body,
        headers=auth_headers["inspector_t1"],
    )
    assert created.status_code == 201
    assert created.json()["agent_user_id"] == 3
    _run_async(
        _add_recording_provenance(
            db_session_factory,
            recording_id=recording_id,
        )
    )
    assert (
        test_client.get(
            f"/api/v1/provenance/recording/{recording_id}",
            headers=auth_headers["agent_t1"],
        ).status_code
        == 200
    )

    spoofed = dict(body)
    spoofed["agent_user_id"] = 7
    spoofed["external_session_id"] = "spoof-attempt"
    response = test_client.post(
        "/api/v1/receptions",
        json=spoofed,
        headers=auth_headers["inspector_t1"],
    )
    assert response.status_code == 422

    _run_async(
        _add_agent(
            db_session_factory,
            user_id=9,
            tenant_id="chang_an",
            name="agent_ca",
        )
    )
    ambiguous_recording_id = _run_async(seed_recording(db_session_factory, recording_id=102))
    ambiguous = test_client.post(
        "/api/v1/receptions",
        json=_create_body(ambiguous_recording_id, agent_name="agent_ca"),
        headers=auth_headers["inspector_t1"],
    )
    assert ambiguous.status_code == 201
    assert ambiguous.json()["agent_user_id"] is None
    ambiguous_id = ambiguous.json()["id"]
    for headers in (
        auth_headers["agent_t1"],
        _agent_headers(jwt_manager, user_id=9, tenant_id="chang_an"),
    ):
        assert (
            test_client.get(
                f"/api/v1/receptions/{ambiguous_id}/workspace",
                headers=headers,
            ).status_code
            == 404
        )


@pytest.mark.integration
def test_stable_identity_still_requires_matching_tenant(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    reception_id = _run_async(
        _seed_reception(
            db_session_factory,
            tenant_id="byd",
            agent_name="agent_ca",
            agent_user_id=3,
        )
    )

    assert (
        test_client.get(
            f"/api/v1/receptions/{reception_id}/workspace",
            headers=auth_headers["agent_t1"],
        ).status_code
        == 404
    )
    assert (
        test_client.get(
            f"/api/v1/receptions/{reception_id}/workspace",
            headers=auth_headers["agent_t2"],
        ).status_code
        == 404
    )
    assert (
        test_client.get(
            f"/api/v1/receptions/{reception_id}/workspace",
            headers=auth_headers["admin_t2"],
        ).status_code
        == 200
    )


@pytest.mark.integration
def test_agent_can_trace_own_state_transition_but_not_another_agents(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    jwt_manager: Any,
    db_session_factory: Any,
) -> None:
    _run_async(
        _add_agent(
            db_session_factory,
            user_id=9,
            tenant_id="chang_an",
            name="second_agent",
        )
    )
    own_reception_id = _run_async(
        _seed_reception(
            db_session_factory,
            agent_user_id=3,
        )
    )
    other_reception_id = _run_async(
        _seed_reception(
            db_session_factory,
            agent_name="second_agent",
            agent_user_id=9,
        )
    )
    own_transition_id = _run_async(
        _add_state_transition_provenance(
            db_session_factory,
            reception_id=own_reception_id,
        )
    )
    other_transition_id = _run_async(
        _add_state_transition_provenance(
            db_session_factory,
            reception_id=other_reception_id,
        )
    )
    second_headers = _agent_headers(jwt_manager, user_id=9, tenant_id="chang_an")

    own_trace = test_client.get(
        f"/api/v1/provenance/dialogue_state_transition/{own_transition_id}",
        headers=auth_headers["agent_t1"],
    )
    assert own_trace.status_code == 200
    assert own_trace.json()["items"][0]["reception_id"] == own_reception_id
    _run_async(
        _delete_state_transition(
            db_session_factory,
            transition_id=own_transition_id,
        )
    )
    historical_trace = test_client.get(
        f"/api/v1/provenance/dialogue_state_transition/{own_transition_id}",
        headers=auth_headers["agent_t1"],
    )
    assert historical_trace.status_code == 200
    assert historical_trace.json()["items"][0]["reception_id"] == own_reception_id

    assert (
        test_client.get(
            f"/api/v1/provenance/dialogue_state_transition/{other_transition_id}",
            headers=auth_headers["agent_t1"],
        ).status_code
        == 404
    )
    assert (
        test_client.get(
            f"/api/v1/provenance/dialogue_state_transition/{own_transition_id}",
            headers=second_headers,
        ).status_code
        == 404
    )
