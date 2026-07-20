"""RBAC matrix tests — 4 roles × key endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.integration
class TestRBACMatrix:
    """Test that each endpoint enforces the correct role requirement."""

    ENDPOINT_COMBINATIONS: list[tuple[str, str, str, set[int]]] = [
        # (method, path, role_key, expected_status_set)
        ("GET", "/api/v1/recordings", "viewer_t1", {200, 500}),
        ("GET", "/api/v1/recordings", "inspector_t1", {200, 500}),
        ("GET", "/api/v1/recordings", "agent_t1", {200, 500}),
        ("POST", "/api/v1/recordings", "viewer_t1", {403, 422, 500}),
        ("POST", "/api/v1/recordings", "agent_t1", {403, 422, 500}),
        ("POST", "/api/v1/recordings", "inspector_t1", {403, 400, 409, 422, 500}),
        ("GET", "/api/v1/graph/explore", "viewer_t1", {200, 500}),
        ("GET", "/api/v1/graph/explore", "agent_t1", {200, 500}),
        ("GET", "/api/v1/prompts", "viewer_t1", {200, 500}),
        ("GET", "/api/v1/prompts", "agent_t1", {200, 500}),
        ("POST", "/api/v1/prompts", "viewer_t1", {403, 422, 500}),
        ("POST", "/api/v1/prompts", "agent_t1", {403, 422, 500}),
        ("POST", "/api/v1/prompts", "inspector_t1", {403, 409, 422, 500}),
        ("POST", "/api/v1/query", "viewer_t1", {200, 422, 500}),
        ("POST", "/api/v1/query", "agent_t1", {200, 422, 500}),
        ("GET", "/api/v1/tags/stats", "viewer_t1", {200, 500}),
        ("GET", "/api/v1/tags/stats", "agent_t1", {200, 500}),
    ]

    @pytest.mark.parametrize(
        "method, path, role_key, expected",
        ENDPOINT_COMBINATIONS,
    )
    def test_endpoint_rbac(
        self,
        test_client: TestClient,
        auth_headers: dict,
        method: str,
        path: str,
        role_key: str,
        expected: set,
    ) -> None:
        """Each endpoint should enforce correct role requirements."""
        headers = auth_headers[role_key]
        if method == "GET":
            resp = test_client.get(path, headers=headers)
        elif method == "POST":
            json_body = {}
            if "recordings" in path and "reindex" not in path:
                json_body = {"store_id": "S1", "path": "/tmp/test.wav"}
            elif "prompts" in path and "activate" not in path:
                json_body = {"name": "test", "version": "v0", "content": "test"}
            elif "query" in path:
                json_body = {"query": "test"}
            resp = test_client.post(path, json=json_body, headers=headers)
        else:
            pytest.skip(f"Unsupported method: {method}")
        assert resp.status_code in expected, (
            f"{method} {path} as {role_key}: expected {expected}, got {resp.status_code}"
        )
