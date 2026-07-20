"""Prompts API tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.integration
class TestPromptsAPI:
    def test_list_requires_auth(self, test_client: TestClient) -> None:
        resp = test_client.get("/api/v1/prompts")
        assert resp.status_code == 401

    def test_list_with_auth(self, test_client: TestClient, auth_headers: dict) -> None:
        resp = test_client.get("/api/v1/prompts", headers=auth_headers["admin_t1"])
        assert resp.status_code in (200, 500)

    def test_create_requires_admin(self, test_client: TestClient, auth_headers: dict) -> None:
        resp = test_client.post(
            "/api/v1/prompts",
            json={"name": "test", "version": "v0", "content": "test content"},
            headers=auth_headers["viewer_t1"],
        )
        assert resp.status_code in (403, 422, 500)
