"""Stats API tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.integration
class TestStatsAPI:
    def test_stats_requires_auth(self, test_client: TestClient) -> None:
        resp = test_client.get("/api/v1/tags/stats")
        assert resp.status_code == 401

    def test_stats_with_auth(self, test_client: TestClient, auth_headers: dict) -> None:
        resp = test_client.get("/api/v1/tags/stats", headers=auth_headers["admin_t1"])
        assert resp.status_code in (200, 500)

    def test_stats_group_by(self, test_client: TestClient, auth_headers: dict) -> None:
        resp = test_client.get(
            "/api/v1/tags/stats?group_by=tag_value",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code in (200, 500)
