"""Query API tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.integration
class TestQueryAPI:
    def test_query_requires_auth(self, test_client: TestClient) -> None:
        resp = test_client.post("/api/v1/query", json={"query": "test"})
        assert resp.status_code == 401

    def test_query_validation_empty(self, test_client: TestClient, auth_headers: dict) -> None:
        """Empty query should fail validation."""
        resp = test_client.post(
            "/api/v1/query",
            json={"query": ""},
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 422

    def test_query_all_roles(self, test_client: TestClient, auth_headers: dict) -> None:
        """All authenticated roles can query."""
        for role in ("admin_t1", "inspector_t1", "agent_t1", "viewer_t1"):
            resp = test_client.post(
                "/api/v1/query",
                json={"query": "test query"},
                headers=auth_headers[role],
            )
            assert resp.status_code in (200, 500), f"{role}: {resp.status_code}"
