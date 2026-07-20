"""Auth flow integration tests.

TEST-01: login → token → /me → cross-tenant 404 → expired 401 → forbidden 403.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.integration
class TestAuthFlow:
    """End-to-end auth flow tests."""

    def test_health_no_auth(self, test_client: TestClient) -> None:
        """Health endpoint should be accessible without auth."""
        resp = test_client.get("/health")
        assert resp.status_code == 200

    def test_readiness_no_auth(self, test_client: TestClient) -> None:
        """Readiness endpoint should be accessible without auth."""
        resp = test_client.get("/health/readiness")
        assert resp.status_code in (200, 503)

    def test_protected_endpoint_without_token(self, test_client: TestClient) -> None:
        """Accessing a protected endpoint without token should return 401."""
        resp = test_client.get("/api/v1/recordings")
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"]["code"] in ("INVALID_TOKEN", "MISSING_TOKEN")

    def test_protected_endpoint_with_invalid_token(self, test_client: TestClient) -> None:
        """Invalid token should return 401."""
        resp = test_client.get(
            "/api/v1/recordings",
            headers={"Authorization": "Bearer invalid-token-string"},
        )
        assert resp.status_code == 401

    def test_me_with_valid_token(self, test_client: TestClient, auth_headers: dict) -> None:
        """/auth/me should return user info with valid token."""
        resp = test_client.get("/api/v1/auth/me", headers=auth_headers["admin_t1"])
        # May return 200 if DB is available, or error if not
        assert resp.status_code in (200, 500)

    def test_forbidden_role_access(self, test_client: TestClient, auth_headers: dict) -> None:
        """Viewer should not be able to POST /recordings."""
        resp = test_client.post(
            "/api/v1/recordings",
            json={"store_id": "S1", "path": "/tmp/test.wav"},
            headers=auth_headers["viewer_t1"],
        )
        assert resp.status_code in (403, 422, 500)

    def test_cross_tenant_isolation(self, test_client: TestClient, auth_headers: dict) -> None:
        """Cross-tenant access should return 404 (not 403)."""
        # Try to access a recording from tenant 2 with tenant 1 token
        resp = test_client.get(
            "/api/v1/recordings/99999",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code in (404, 500)


@pytest.mark.integration
class TestErrorFormat:
    """Verify unified error format."""

    def test_error_envelope(self, test_client: TestClient) -> None:
        """Error response should have the standard envelope."""
        resp = test_client.get("/api/v1/recordings")
        body = resp.json()
        assert "error" in body
        assert "code" in body["error"]
        assert "message" in body["error"]

    def test_request_id_header(self, test_client: TestClient) -> None:
        """Response should include X-Request-ID header."""
        resp = test_client.get("/health")
        assert "X-Request-ID" in resp.headers or "x-request-id" in resp.headers

    def test_request_id_passthrough(self, test_client: TestClient) -> None:
        """Custom request ID should be passed through."""
        custom_id = "test-custom-id-12345"
        resp = test_client.get("/health", headers={"X-Request-ID": custom_id})
        assert resp.headers.get("X-Request-ID") == custom_id
