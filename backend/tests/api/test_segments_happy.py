"""Segments API happy-path tests.

Covers: api/segments.py lines 41-94.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import _run_async, seed_recording, seed_segment


@pytest.mark.integration
class TestSegmentsHappyPath:
    """Happy-path tests for /recordings/{id}/segments."""

    def test_get_segments_empty(
        self, test_client: TestClient, auth_headers: dict, db_session_factory
    ) -> None:
        """GET segments for recording with no segments returns empty list."""
        rec_id = _run_async(seed_recording(db_session_factory))

        resp = test_client.get(
            f"/api/v1/recordings/{rec_id}/segments",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["recording_id"] == rec_id
        assert body["items"] == []
        assert body["total"] == 0

    def test_get_segments_with_data(
        self, test_client: TestClient, auth_headers: dict, db_session_factory
    ) -> None:
        """GET segments returns seeded segments."""
        rec_id = _run_async(seed_recording(db_session_factory))
        _run_async(seed_segment(db_session_factory, rec_id, "chang_an", idx=0, transcript="Hello"))
        _run_async(
            seed_segment(
                db_session_factory, rec_id, "chang_an", idx=1, transcript="How can I help?"
            )
        )

        resp = test_client.get(
            f"/api/v1/recordings/{rec_id}/segments",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert len(body["items"]) == 2
        assert body["items"][0]["transcript"] == "Hello"

    def test_get_segments_paginated(
        self, test_client: TestClient, auth_headers: dict, db_session_factory
    ) -> None:
        """GET segments supports pagination."""
        rec_id = _run_async(seed_recording(db_session_factory))

        resp = test_client.get(
            f"/api/v1/recordings/{rec_id}/segments?page=1&page_size=10",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["page"] == 1
        assert body["page_size"] == 10

    def test_get_segments_cross_tenant_404(
        self, test_client: TestClient, auth_headers: dict, db_session_factory
    ) -> None:
        """GET segments from another tenant returns 404."""
        rec_id = _run_async(seed_recording(db_session_factory, tenant_id="chang_an"))

        resp = test_client.get(
            f"/api/v1/recordings/{rec_id}/segments",
            headers=auth_headers["admin_t2"],
        )
        assert resp.status_code == 404
