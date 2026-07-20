"""Tags API tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.integration
class TestTagsAPI:
    def test_get_tags_requires_auth(self, test_client: TestClient) -> None:
        resp = test_client.get("/api/v1/recordings/1/tags")
        assert resp.status_code == 401

    def test_get_tags_nonexistent(self, test_client: TestClient, auth_headers: dict) -> None:
        resp = test_client.get(
            "/api/v1/recordings/99999/tags",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code in (404, 500)

    def test_post_tags_requires_write(self, test_client: TestClient, auth_headers: dict) -> None:
        """Viewer/agent cannot POST tags."""
        resp = test_client.post(
            "/api/v1/recordings/1/tags",
            json={"mode": "manual", "tag_path": "test", "tag_value": "pass"},
            headers=auth_headers["viewer_t1"],
        )
        assert resp.status_code in (403, 404, 500)

    def test_recompute_requires_admin(self, test_client: TestClient, auth_headers: dict) -> None:
        """Only admin can trigger recompute."""
        resp = test_client.post(
            "/api/v1/tags/recompute",
            json={"prompt_version": "v2"},
            headers=auth_headers["inspector_t1"],
        )
        assert resp.status_code == 403
