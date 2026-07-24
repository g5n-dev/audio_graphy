"""Reception-state insight service tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from audio_graphy.errors import ValidationError


async def _exercise_filtered_service(factory: Any) -> Any:
    from audio_graphy.models import DialogueStateTransition, Reception
    from audio_graphy.services.reception_state_insights import (
        ReceptionStateInsightService,
    )

    started_at = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
    async with factory() as session:
        included = Reception(
            tenant_id="chang_an",
            external_session_id="state-service-included",
            scenario="gold",
            store_id="SERVICE",
            agent_name="agent_ca",
            agent_user_id=3,
            status="ready",
            merge_mode="logical",
            started_at=started_at,
            ended_at=started_at + timedelta(minutes=10),
            version=1,
        )
        excluded = Reception(
            tenant_id="chang_an",
            external_session_id="state-service-excluded",
            scenario="gold",
            store_id="SERVICE",
            agent_name="agent_other",
            agent_user_id=2,
            status="ready",
            merge_mode="logical",
            started_at=started_at,
            ended_at=started_at + timedelta(minutes=10),
            version=1,
        )
        session.add_all([included, excluded])
        await session.flush()
        session.add_all(
            [
                DialogueStateTransition(
                    tenant_id="chang_an",
                    reception_id=reception.id,
                    dialogue_unit_id=None,
                    sequence_no=0,
                    from_state="问候",
                    to_state="探索",
                    trigger="提问",
                    confidence=confidence,
                    evidence_refs=[],
                    algorithm_version="state-v1",
                )
                for reception, confidence in ((included, 0.9), (excluded, 0.1))
            ]
        )
        await session.commit()

    service = ReceptionStateInsightService(factory)
    return await service.analyze(
        tenant_id="chang_an",
        forced_agent_user_id=3,
        store_ids=["SERVICE"],
        agent_names=[],
        scenarios=["gold"],
        started_from=started_at - timedelta(minutes=1),
        started_to=started_at + timedelta(minutes=1),
        reception_ids=[],
        transition_limit=100,
    )


async def test_service_applies_stable_agent_identity_and_filters(
    session_factory: Any,
) -> None:
    result = await _exercise_filtered_service(session_factory)

    assert result.total_receptions == 1
    assert result.total_transitions == 1
    assert result.transitions[0].average_confidence == 0.9
    assert result.stages[0].reception_count == 1


async def test_service_uses_a_half_open_time_window(
    session_factory: Any,
) -> None:
    await _exercise_filtered_service(session_factory)
    from audio_graphy.services.reception_state_insights import (
        ReceptionStateInsightService,
    )

    started_at = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
    result = await ReceptionStateInsightService(session_factory).analyze(
        tenant_id="chang_an",
        forced_agent_user_id=3,
        store_ids=["SERVICE"],
        agent_names=[],
        scenarios=["gold"],
        started_from=started_at - timedelta(minutes=1),
        started_to=started_at,
        reception_ids=[],
        transition_limit=100,
    )

    assert result.total_receptions == 0
    assert result.total_transitions == 0


async def test_service_rejects_an_empty_time_window(
    session_factory: Any,
) -> None:
    from audio_graphy.services.reception_state_insights import (
        ReceptionStateInsightService,
    )

    boundary = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
    with pytest.raises(ValidationError, match="time filters are inconsistent"):
        await ReceptionStateInsightService(session_factory).analyze(
            tenant_id="chang_an",
            forced_agent_user_id=None,
            store_ids=[],
            agent_names=[],
            scenarios=[],
            started_from=boundary,
            started_to=boundary,
            reception_ids=[],
            transition_limit=100,
        )


def test_state_insight_schema_rejects_impossible_aggregate_totals() -> None:
    from audio_graphy.schemas.reception_state_insights import (
        ReceptionStateInsightsResponse,
        StateTransitionInsight,
    )

    with pytest.raises(
        ValueError,
        match="visible transition counts cannot exceed total_transitions",
    ):
        ReceptionStateInsightsResponse(
            stages=[],
            transitions=[
                StateTransitionInsight(
                    from_state="问候",
                    to_state="探索",
                    count=2,
                    average_confidence=0.9,
                    evidence_count=1,
                )
            ],
            total_receptions=2,
            total_transitions=1,
            returned_stages=0,
            returned_transitions=1,
            transition_limit=100,
            truncated=False,
            generated_at=datetime.now(UTC),
        )
