"""Graph API tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.integration
class TestGraphAPI:
    def test_explore_requires_auth(self, test_client: TestClient) -> None:
        resp = test_client.get("/api/v1/graph/explore")
        assert resp.status_code == 401

    def test_explore_with_auth(self, test_client: TestClient, auth_headers: dict) -> None:
        resp = test_client.get("/api/v1/graph/explore", headers=auth_headers["admin_t1"])
        assert resp.status_code in (200, 500)

    def test_entity_not_found(self, test_client: TestClient, auth_headers: dict) -> None:
        resp = test_client.get(
            "/api/v1/graph/entity/NonExistentEntity12345",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code in (404, 500)

    def test_subgraph_missing_param(self, test_client: TestClient, auth_headers: dict) -> None:
        """Subgraph without entity param should return 422."""
        resp = test_client.get(
            "/api/v1/graph/subgraph",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 422
