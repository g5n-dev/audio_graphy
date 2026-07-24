"""API tests for T13 — Speaker merge-pending endpoints.

Coverage:
    - GET  /speakers/merge-pending (viewer+ read).
    - POST /speakers/{speaker_id}/merge/{target_id} (inspector/admin).
    - POST /speakers/{speaker_id}/reject-merge (inspector/admin).
    - Status transitions + 409 when re-resolving.
    - Cross-tenant → 404.
    - RBAC matrix: viewer can read but not write; agent forbidden entirely.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from tests.api.conftest import _run_async, seed_recording

pytestmark = pytest.mark.integration


async def _seed_pending(
    factory: Any,
    *,
    tenant_id: str = "chang_an",
    recording_id: int = 1,
    candidate_name: str = "speaker:vp_abcdef12",
    matched_node_id: int = 1,
    fuzzy_score: float = 0.88,
    status: str = "pending",
) -> int:
    from audio_graphy.models.speaker_merge_pending import SpeakerMergePending

    async with factory() as session:
        row = SpeakerMergePending(
            tenant_id=tenant_id,
            recording_id=recording_id,
            candidate_name=candidate_name,
            matched_speaker_node_id=matched_node_id,
            fuzzy_score=fuzzy_score,
            status=status,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return int(row.id)


async def _seed_speaker_node(
    factory: Any,
    *,
    tenant_id: str = "chang_an",
    voiceprint_id: str = "a1b2c3d4e5f6g7h8",
    display_name: str = "speaker:vp_a1b2c3d4",
) -> int:
    from audio_graphy.models.speaker_node import SpeakerNode

    async with factory() as session:
        node = SpeakerNode(
            tenant_id=tenant_id,
            voiceprint_id=voiceprint_id,
            display_name=display_name,
            speaker_role="agent",
            recordings_list=[1],
            recordings_count=1,
            first_seen=datetime.now(UTC),
            total_speech_sec=30.0,
            merge_confidence=0.9,
            merge_strategy="voiceprint",
            attrs={},
        )
        session.add(node)
        await session.commit()
        await session.refresh(node)
        return int(node.id)


@pytest.fixture
def seeded_speaker_data(test_client, db_session_factory):
    _run_async(seed_recording(db_session_factory, tenant_id="chang_an"))
    node_id = _run_async(_seed_speaker_node(db_session_factory))
    pending_id = _run_async(
        _seed_pending(
            db_session_factory,
            tenant_id="chang_an",
            matched_node_id=node_id,
        )
    )
    return {"node_id": node_id, "pending_id": pending_id}


# ============================================================
# GET /speakers/merge-pending
# ============================================================


def test_list_merge_pending_viewer_can_read(test_client, auth_headers, seeded_speaker_data):
    """Viewer+ can read the queue (architecture §25.13)."""
    resp = test_client.get(
        "/api/v1/speakers/merge-pending",
        headers=auth_headers["viewer_t1"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] >= 1
    item = body["items"][0]
    assert item["status"] == "pending"
    assert item["candidate_name"] == "speaker:vp_abcdef12"


def test_list_merge_pending_status_filter(test_client, auth_headers, seeded_speaker_data):
    resp = test_client.get(
        "/api/v1/speakers/merge-pending",
        headers=auth_headers["inspector_t1"],
        params={"status": "resolved_inferred"},
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 0  # no resolved items yet


def test_list_merge_pending_cross_tenant_empty(test_client, auth_headers, seeded_speaker_data):
    """Tenant 2 viewer sees no rows from tenant 1."""
    resp = test_client.get(
        "/api/v1/speakers/merge-pending",
        headers=auth_headers["viewer_t2"],
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_list_merge_pending_unauth_401(test_client):
    resp = test_client.get("/api/v1/speakers/merge-pending")
    assert resp.status_code == 401, resp.text


# ============================================================
# POST /speakers/{speaker_id}/merge/{target_id}
# ============================================================


def test_confirm_merge_inspector_happy_path(test_client, auth_headers, seeded_speaker_data):
    pending_id = seeded_speaker_data["pending_id"]
    target_id = seeded_speaker_data["node_id"]
    resp = test_client.post(
        f"/api/v1/speakers/{pending_id}/merge/{target_id}",
        headers=auth_headers["inspector_t1"],
        json={"voiceprint_score": 0.85, "notes": "voiceprint reconfirm ok"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pending_id"] == pending_id
    assert body["status"] == "resolved_inferred"
    assert body["resolved_by"] == "human"
    assert body["voiceprint_score"] == pytest.approx(0.85)


def test_confirm_merge_forbidden_viewer(test_client, auth_headers, seeded_speaker_data):
    pending_id = seeded_speaker_data["pending_id"]
    target_id = seeded_speaker_data["node_id"]
    resp = test_client.post(
        f"/api/v1/speakers/{pending_id}/merge/{target_id}",
        headers=auth_headers["viewer_t1"],
        json={},
    )
    assert resp.status_code == 403, resp.text


def test_confirm_merge_404_pending_missing(test_client, auth_headers):
    resp = test_client.post(
        "/api/v1/speakers/999999/merge/1",
        headers=auth_headers["admin_t1"],
        json={},
    )
    assert resp.status_code == 404, resp.text


def test_confirm_merge_404_target_missing(test_client, auth_headers, seeded_speaker_data):
    pending_id = seeded_speaker_data["pending_id"]
    resp = test_client.post(
        f"/api/v1/speakers/{pending_id}/merge/999999",
        headers=auth_headers["inspector_t1"],
        json={},
    )
    assert resp.status_code == 404, resp.text


def test_confirm_merge_409_when_already_resolved(test_client, auth_headers, seeded_speaker_data):
    pending_id = seeded_speaker_data["pending_id"]
    target_id = seeded_speaker_data["node_id"]
    # First resolution.
    r1 = test_client.post(
        f"/api/v1/speakers/{pending_id}/merge/{target_id}",
        headers=auth_headers["inspector_t1"],
        json={},
    )
    assert r1.status_code == 200, r1.text
    # Second resolution attempt.
    r2 = test_client.post(
        f"/api/v1/speakers/{pending_id}/merge/{target_id}",
        headers=auth_headers["inspector_t1"],
        json={},
    )
    assert r2.status_code == 409, r2.text


# ============================================================
# POST /speakers/{speaker_id}/reject-merge
# ============================================================


def test_reject_merge_inspector_happy_path(test_client, auth_headers, seeded_speaker_data):
    pending_id = seeded_speaker_data["pending_id"]
    resp = test_client.post(
        f"/api/v1/speakers/{pending_id}/reject-merge",
        headers=auth_headers["inspector_t1"],
        json={"notes": "false positive"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "resolved_rejected"
    assert body["resolved_by"] == "human"


def test_reject_merge_admin_happy_path(test_client, auth_headers, seeded_speaker_data):
    pending_id = seeded_speaker_data["pending_id"]
    resp = test_client.post(
        f"/api/v1/speakers/{pending_id}/reject-merge",
        headers=auth_headers["admin_t1"],
        json={},
    )
    assert resp.status_code == 200, resp.text


def test_reject_merge_forbidden_viewer(test_client, auth_headers, seeded_speaker_data):
    pending_id = seeded_speaker_data["pending_id"]
    resp = test_client.post(
        f"/api/v1/speakers/{pending_id}/reject-merge",
        headers=auth_headers["viewer_t1"],
        json={},
    )
    assert resp.status_code == 403, resp.text


def test_reject_merge_404_pending_missing(test_client, auth_headers):
    resp = test_client.post(
        "/api/v1/speakers/999999/reject-merge",
        headers=auth_headers["admin_t1"],
        json={},
    )
    assert resp.status_code == 404, resp.text


def test_reject_merge_cross_tenant_404(test_client, auth_headers, seeded_speaker_data):
    """Tenant 2 admin cannot reject tenant 1 pending row."""
    pending_id = seeded_speaker_data["pending_id"]
    resp = test_client.post(
        f"/api/v1/speakers/{pending_id}/reject-merge",
        headers=auth_headers["admin_t2"],
        json={},
    )
    assert resp.status_code == 404, resp.text
