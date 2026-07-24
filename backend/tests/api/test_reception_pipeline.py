"""API contracts for the resumable reception automation state machine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import _run_async, seed_recording


async def _seed_ready_for_automation(factory: Any) -> int:
    from sqlalchemy import select

    from audio_graphy.models import (
        Reception,
        ReceptionRecording,
        Recording,
        Segment,
    )

    recording_id = await seed_recording(factory)
    base = datetime(2026, 7, 23, 9, 0, tzinfo=UTC)
    async with factory() as session:
        recording = (
            await session.execute(select(Recording).where(Recording.id == recording_id))
        ).scalar_one()
        recording.recorded_at = base
        reception = Reception(
            tenant_id="chang_an",
            scenario="automotive",
            store_id="S001",
            agent_name="agent_ca",
            agent_user_id=3,
            customer_hash="customer-a",
            status="confirmed",
            merge_mode="logical",
            merge_confidence=1,
            started_at=base,
            ended_at=base + timedelta(seconds=60),
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
                timeline_start_sec=0,
                timeline_end_sec=60,
                source_start_sec=0,
                source_end_sec=60,
                gap_before_sec=0,
                decision_source="auto",
                merge_confidence=1,
                merge_reasons={},
            )
        )
        session.add_all(
            [
                Segment(
                    tenant_id="chang_an",
                    recording_id=recording_id,
                    idx=0,
                    start_sec=0,
                    end_sec=30,
                    transcript="客户想看 SUV。",
                    text_scrubbed="客户想看 SUV。",
                    speaker="agent_ca",
                    vad_conf=0.99,
                ),
                Segment(
                    tenant_id="chang_an",
                    recording_id=recording_id,
                    idx=1,
                    start_sec=30,
                    end_sec=60,
                    transcript="价格太高，先安排试驾。",
                    text_scrubbed="价格太高，先安排试驾。",
                    speaker="customer",
                    vad_conf=0.99,
                ),
            ]
        )
        await session.commit()
        return reception.id


@pytest.mark.integration
def test_run_endpoint_completes_and_get_exposes_checkpoint(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    reception_id = _run_async(_seed_ready_for_automation(db_session_factory))

    response = test_client.post(
        f"/api/v1/receptions/{reception_id}/automation/run",
        json={},
        headers=auth_headers["inspector_t1"],
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["stage"] == "ready"
    assert payload["attempt_count"] == 1
    assert payload["checkpoints"]["segmentation"]["status"] == "completed"
    assert payload["checkpoints"]["tagging"]["status"] == "completed"

    read_response = test_client.get(
        f"/api/v1/receptions/{reception_id}/automation",
        headers=auth_headers["agent_t1"],
    )
    assert read_response.status_code == 200
    assert read_response.json()["id"] == payload["id"]


@pytest.mark.integration
def test_pipeline_write_is_role_and_tenant_scoped(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    reception_id = _run_async(_seed_ready_for_automation(db_session_factory))

    viewer = test_client.post(
        f"/api/v1/receptions/{reception_id}/automation/run",
        json={},
        headers=auth_headers["viewer_t1"],
    )
    other_tenant = test_client.post(
        f"/api/v1/receptions/{reception_id}/automation/run",
        json={},
        headers=auth_headers["inspector_t2"],
    )

    assert viewer.status_code == 403
    assert other_tenant.status_code == 404
