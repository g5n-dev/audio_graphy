"""Segments API tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.integration
class TestSegmentsAPI:
    def test_segments_requires_auth(self, test_client: TestClient) -> None:
        resp = test_client.get("/api/v1/recordings/1/segments")
        assert resp.status_code == 401

    def test_segments_nonexistent_recording(
        self, test_client: TestClient, auth_headers: dict
    ) -> None:
        resp = test_client.get(
            "/api/v1/recordings/99999/segments",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code in (404, 500)
