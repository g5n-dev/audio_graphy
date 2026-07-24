"""Cross-reception dialogue-state insight API integration tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient


def _run_async(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _seed_state_insight_dataset(factory: Any) -> dict[str, int]:
    from audio_graphy.models import DialogueStateTransition, Reception

    started_at = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    async with factory() as session:
        own = Reception(
            tenant_id="chang_an",
            external_session_id="state-insight-own",
            scenario="gold",
            store_id="S001",
            agent_name="agent_ca",
            agent_user_id=3,
            customer_hash="customer-own",
            status="ready",
            merge_mode="logical",
            merge_confidence=0.95,
            started_at=started_at,
            ended_at=started_at + timedelta(minutes=30),
            version=1,
        )
        colleague = Reception(
            tenant_id="chang_an",
            external_session_id="state-insight-colleague",
            scenario="automotive",
            store_id="S002",
            agent_name="agent_other",
            agent_user_id=2,
            customer_hash="customer-colleague",
            status="ready",
            merge_mode="logical",
            merge_confidence=0.9,
            started_at=started_at + timedelta(days=1),
            ended_at=started_at + timedelta(days=1, minutes=40),
            version=1,
        )
        other_tenant = Reception(
            tenant_id="byd",
            external_session_id="state-insight-other-tenant",
            scenario="automotive",
            store_id="S001",
            agent_name="agent_byd",
            agent_user_id=7,
            customer_hash="customer-other-tenant",
            status="ready",
            merge_mode="logical",
            merge_confidence=0.92,
            started_at=started_at,
            ended_at=started_at + timedelta(minutes=20),
            version=1,
        )
        session.add_all([own, colleague, other_tenant])
        await session.flush()

        session.add_all(
            [
                DialogueStateTransition(
                    tenant_id="chang_an",
                    reception_id=own.id,
                    dialogue_unit_id=None,
                    sequence_no=0,
                    from_state="接待问候",
                    to_state="需求探索",
                    trigger="主动询问",
                    confidence=0.8,
                    evidence_refs=[{"segment_id": 1}, {"segment_id": 2}],
                    algorithm_version="state-v1",
                ),
                DialogueStateTransition(
                    tenant_id="chang_an",
                    reception_id=own.id,
                    dialogue_unit_id=None,
                    sequence_no=1,
                    from_state="需求探索",
                    to_state="方案报价",
                    trigger="客户询价",
                    confidence=0.9,
                    evidence_refs=[{"segment_id": 3}],
                    algorithm_version="state-v1",
                ),
                DialogueStateTransition(
                    tenant_id="chang_an",
                    reception_id=own.id,
                    dialogue_unit_id=None,
                    sequence_no=2,
                    from_state="需求探索",
                    to_state="方案报价",
                    trigger="客户询价",
                    confidence=0.7,
                    evidence_refs=[],
                    algorithm_version="state-v1",
                ),
                DialogueStateTransition(
                    tenant_id="chang_an",
                    reception_id=colleague.id,
                    dialogue_unit_id=None,
                    sequence_no=0,
                    from_state="接待问候",
                    to_state="需求探索",
                    trigger="主动询问",
                    confidence=0.6,
                    evidence_refs=[{"segment_id": 4}],
                    algorithm_version="state-v1",
                ),
                DialogueStateTransition(
                    tenant_id="chang_an",
                    reception_id=colleague.id,
                    dialogue_unit_id=None,
                    sequence_no=1,
                    from_state="需求探索",
                    to_state="成交收口",
                    trigger="客户确认",
                    confidence=0.5,
                    evidence_refs=[{"segment_id": 5}],
                    algorithm_version="state-v1",
                ),
                DialogueStateTransition(
                    tenant_id="byd",
                    reception_id=other_tenant.id,
                    dialogue_unit_id=None,
                    sequence_no=0,
                    from_state="接待问候",
                    to_state="跨租户机密",
                    trigger="不应可见",
                    confidence=1.0,
                    evidence_refs=[{"secret": True}],
                    algorithm_version="state-v1",
                ),
            ]
        )
        await session.commit()
        return {
            "own": own.id,
            "colleague": colleague.id,
            "other_tenant": other_tenant.id,
        }


async def _seed_budget_dataset(factory: Any) -> None:
    from audio_graphy.models import DialogueStateTransition, Reception

    started_at = datetime(2026, 7, 10, 9, 0, tzinfo=UTC)
    async with factory() as session:
        receptions: list[Reception] = []
        for index in range(7):
            reception = Reception(
                tenant_id="chang_an",
                external_session_id=f"state-insight-budget-{index}",
                scenario="gold",
                store_id="BUDGET",
                agent_name=f"agent-{index}",
                agent_user_id=None,
                customer_hash=f"budget-customer-{index}",
                status="ready",
                merge_mode="logical",
                started_at=started_at + timedelta(minutes=index),
                ended_at=started_at + timedelta(minutes=index + 1),
                version=1,
            )
            session.add(reception)
            receptions.append(reception)
        await session.flush()

        for index in range(205):
            session.add(
                DialogueStateTransition(
                    tenant_id="chang_an",
                    reception_id=receptions[0].id,
                    dialogue_unit_id=None,
                    sequence_no=index,
                    from_state=f"from-{index:03d}",
                    to_state=f"to-{index:03d}",
                    trigger=f"trigger-{index % 8}",
                    confidence=0.75,
                    evidence_refs=[{"segment_id": index}],
                    algorithm_version="state-v1",
                )
            )
        for reception_index, reception in enumerate(receptions[1:], start=1):
            session.add(
                DialogueStateTransition(
                    tenant_id="chang_an",
                    reception_id=reception.id,
                    dialogue_unit_id=None,
                    sequence_no=0,
                    from_state="from-000",
                    to_state="to-000",
                    trigger=f"extra-trigger-{reception_index}",
                    confidence=0.5,
                    evidence_refs=[{"segment_id": reception_index}],
                    algorithm_version="state-v1",
                )
            )
        await session.commit()


def test_state_insights_aggregate_real_transitions(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    ids = _run_async(_seed_state_insight_dataset(db_session_factory))

    response = test_client.get(
        "/api/v1/reception-state-insights",
        headers=auth_headers["viewer_t1"],
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_receptions"] == 2
    assert payload["total_transitions"] == 5
    assert payload["returned_transitions"] == 3
    assert payload["transition_limit"] == 100
    assert payload["truncated"] is False
    assert payload["generated_at"].endswith("Z")

    stages = {item["state"]: item for item in payload["stages"]}
    assert stages["需求探索"] == {
        "state": "需求探索",
        "count": 5,
        "reception_count": 2,
        "incoming_count": 2,
        "outgoing_count": 3,
        "average_confidence": 0.7,
    }

    transitions = {(item["from_state"], item["to_state"]): item for item in payload["transitions"]}
    quote = transitions[("需求探索", "方案报价")]
    assert quote["count"] == 2
    assert quote["average_confidence"] == 0.8
    assert quote["evidence_count"] == 1
    assert quote["top_triggers"] == [{"trigger": "客户询价", "count": 2}]
    assert quote["sample_reception_ids"] == [ids["own"]]
    assert "跨租户机密" not in stages


def test_state_insights_support_all_reception_filters(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    ids = _run_async(_seed_state_insight_dataset(db_session_factory))

    response = test_client.get(
        "/api/v1/reception-state-insights",
        params={
            "store_id": "S001",
            "agent_name": "agent_ca",
            "scenario": "gold",
            "started_from": "2026-07-01T08:00:00Z",
            "started_to": "2026-07-01T10:00:00Z",
            "reception_id": ids["own"],
        },
        headers=auth_headers["admin_t1"],
    )

    assert response.status_code == 200
    assert response.json()["total_receptions"] == 1
    assert response.json()["total_transitions"] == 3
    assert {item["sample_reception_ids"][0] for item in response.json()["transitions"]} == {
        ids["own"]
    }


def test_state_insights_support_repeated_multi_value_filters(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    ids = _run_async(_seed_state_insight_dataset(db_session_factory))

    response = test_client.get(
        "/api/v1/reception-state-insights",
        params=[
            ("store_id", "S001"),
            ("store_id", "S002"),
            ("agent_name", "agent_ca"),
            ("agent_name", "agent_other"),
            ("scenario", "gold"),
            ("scenario", "automotive"),
            ("reception_id", str(ids["own"])),
            ("reception_id", str(ids["colleague"])),
        ],
        headers=auth_headers["admin_t1"],
    )

    assert response.status_code == 200
    assert response.json()["total_receptions"] == 2
    assert response.json()["total_transitions"] == 5


def test_state_insights_are_tenant_and_agent_scoped(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    ids = _run_async(_seed_state_insight_dataset(db_session_factory))

    agent = test_client.get(
        "/api/v1/reception-state-insights",
        headers=auth_headers["agent_t1"],
    )
    attempted_overreach = test_client.get(
        "/api/v1/reception-state-insights",
        params={"reception_id": ids["colleague"]},
        headers=auth_headers["agent_t1"],
    )
    other_tenant = test_client.get(
        "/api/v1/reception-state-insights",
        headers=auth_headers["viewer_t2"],
    )

    assert agent.status_code == 200
    assert agent.json()["total_receptions"] == 1
    assert agent.json()["total_transitions"] == 3
    assert attempted_overreach.status_code == 200
    assert attempted_overreach.json()["total_receptions"] == 0
    assert attempted_overreach.json()["total_transitions"] == 0
    assert other_tenant.status_code == 200
    assert other_tenant.json()["total_receptions"] == 1
    assert other_tenant.json()["stages"][-1]["state"] == "跨租户机密"


def test_state_insights_enforce_output_budgets(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    _run_async(_seed_budget_dataset(db_session_factory))

    response = test_client.get(
        "/api/v1/reception-state-insights",
        params={"store_id": "BUDGET", "transition_limit": 200},
        headers=auth_headers["viewer_t1"],
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_receptions"] == 7
    assert payload["total_transitions"] == 211
    assert payload["returned_transitions"] == 200
    assert len(payload["transitions"]) == 200
    assert len(payload["stages"]) == payload["stage_limit"] == 64
    assert payload["truncated"] is True
    busiest = payload["transitions"][0]
    assert busiest["from_state"] == "from-000"
    assert len(busiest["top_triggers"]) == 5
    assert len(busiest["sample_reception_ids"]) == 5

    over_budget = test_client.get(
        "/api/v1/reception-state-insights",
        params={"transition_limit": 201},
        headers=auth_headers["viewer_t1"],
    )
    assert over_budget.status_code == 422


def test_state_insights_reject_invalid_time_order(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
) -> None:
    response = test_client.get(
        "/api/v1/reception-state-insights",
        params={
            "started_from": "2026-07-02T00:00:00Z",
            "started_to": "2026-07-01T00:00:00Z",
        },
        headers=auth_headers["viewer_t1"],
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "RECEPTION_STATE_TIME_RANGE_INVALID"


def test_state_insights_require_authentication(test_client: TestClient) -> None:
    response = test_client.get("/api/v1/reception-state-insights")

    assert response.status_code == 401
