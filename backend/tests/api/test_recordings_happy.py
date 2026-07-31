"""Recordings API happy-path tests.

Covers: api/recordings.py lines 45-50 (create), 86-118 (list),
131-149 (detail), 177-183 (status), 202-209 (reindex).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import _run_async, seed_recording


@pytest.mark.integration
class TestRecordingsHappyPath:
    """Happy-path + error tests for /recordings endpoints."""

    def test_list_recordings_empty(self, test_client: TestClient, auth_headers: dict) -> None:
        """GET /recordings returns empty list when no recordings exist."""
        resp = test_client.get("/api/v1/recordings", headers=auth_headers["admin_t1"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["total"] == 0

    def test_list_with_seeded_recording(
        self, test_client: TestClient, auth_headers: dict, db_session_factory
    ) -> None:
        """GET /recordings returns seeded recordings."""
        rec_id = _run_async(seed_recording(db_session_factory))

        resp = test_client.get("/api/v1/recordings", headers=auth_headers["admin_t1"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 1
        assert any(r["id"] == rec_id for r in body["items"])

    def test_list_with_pagination(self, test_client: TestClient, auth_headers: dict) -> None:
        """GET /recordings supports page/page_size params."""
        resp = test_client.get(
            "/api/v1/recordings?page=1&page_size=5",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["page"] == 1
        assert body["page_size"] == 5

    def test_list_filter_by_store(self, test_client: TestClient, auth_headers: dict) -> None:
        """GET /recordings supports store_id filter."""
        resp = test_client.get(
            "/api/v1/recordings?store_id=S999",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_list_filter_exposes_only_declared_recording_statuses(
        self, test_client: TestClient, auth_headers: dict
    ) -> None:
        silence = test_client.get(
            "/api/v1/recordings?status=ready_no_speech",
            headers=auth_headers["admin_t1"],
        )
        invalid = test_client.get(
            "/api/v1/recordings?status=not-a-recording-status",
            headers=auth_headers["admin_t1"],
        )

        assert silence.status_code == 200
        assert invalid.status_code == 422

    def test_get_recording_detail_success(
        self, test_client: TestClient, auth_headers: dict, db_session_factory
    ) -> None:
        """GET /recordings/{id} returns full detail for existing recording."""
        rec_id = _run_async(seed_recording(db_session_factory))

        resp = test_client.get(
            f"/api/v1/recordings/{rec_id}",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == rec_id
        assert body["tenant_id"] == "chang_an"
        assert body["store_id"] == "S001"
        assert "segments_count" in body
        assert "chunks_count" in body
        assert "current_tags" in body

    def test_get_recording_status_success(
        self, test_client: TestClient, auth_headers: dict, db_session_factory
    ) -> None:
        """GET /recordings/{id}/status returns lightweight status."""
        rec_id = _run_async(seed_recording(db_session_factory))

        resp = test_client.get(
            f"/api/v1/recordings/{rec_id}/status",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == rec_id
        assert "status" in body
        assert "pipeline_state" in body

    def test_reindex_recording(
        self, test_client: TestClient, auth_headers: dict, db_session_factory
    ) -> None:
        """POST reindex returns a queryable, idempotent processing operation."""
        rec_id = _run_async(seed_recording(db_session_factory))

        resp = test_client.post(
            f"/api/v1/recordings/{rec_id}/reindex",
            headers={
                **auth_headers["admin_t1"],
                "Idempotency-Key": "recordings-happy-reindex",
            },
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["id"] == rec_id
        assert body["status"] == "queued"
        assert body["operation_id"] > 0
        assert body["generation"] == 1
        assert body["operation_state"] == "queued"

        replay = test_client.post(
            f"/api/v1/recordings/{rec_id}/reindex",
            headers={
                **auth_headers["admin_t1"],
                "Idempotency-Key": "recordings-happy-reindex",
            },
        )
        assert replay.status_code == 202
        assert replay.json()["operation_id"] == body["operation_id"]

        operation = test_client.get(
            f"/api/v1/recordings/{rec_id}/processing-runs/{body['operation_id']}",
            headers=auth_headers["admin_t1"],
        )
        assert operation.status_code == 200
        assert operation.json()["state"] == "queued"

    def test_reindex_nonexistent(self, test_client: TestClient, auth_headers: dict) -> None:
        """POST /recordings/{id}/reindex with nonexistent ID returns 404."""
        resp = test_client.post(
            "/api/v1/recordings/99999/reindex",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 404

    def test_cross_tenant_get_returns_404(
        self, test_client: TestClient, auth_headers: dict, db_session_factory
    ) -> None:
        """GET /recordings/{id} from different tenant returns 404."""
        rec_id = _run_async(seed_recording(db_session_factory, tenant_id="chang_an"))

        resp = test_client.get(
            f"/api/v1/recordings/{rec_id}",
            headers=auth_headers["admin_t2"],
        )
        assert resp.status_code == 404
