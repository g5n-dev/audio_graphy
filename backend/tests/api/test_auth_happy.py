"""Auth API happy-path tests — login success, refresh, /me.

Covers: api/auth.py lines 43-68 (login), 89-97 (refresh), 109 (me).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import seed_recording  # noqa: F401


@pytest.mark.integration
class TestAuthHappyPath:
    """Happy-path tests for /auth endpoints."""

    def test_login_success(self, test_client: TestClient) -> None:
        """POST /auth/login with valid credentials returns 200 + tokens."""
        resp = test_client.post(
            "/api/v1/auth/login",
            json={"email": "admin@changan.com", "password": "anything"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "access_token" in body
        assert "refresh_token" in body
        assert body["token_type"] == "bearer"
        assert body["expires_in"] > 0
        assert body["user"]["email"] == "admin@changan.com"
        assert body["user"]["role"] == "admin"
        assert body["user"]["tenant_id"] == "chang_an"

    def test_login_wrong_email(self, test_client: TestClient) -> None:
        """POST /auth/login with unknown email returns 401."""
        resp = test_client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@nowhere.com", "password": "x"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "INVALID_CREDENTIALS"

    def test_login_invalid_email_format(self, test_client: TestClient) -> None:
        """POST /auth/login with malformed email returns 422."""
        resp = test_client.post(
            "/api/v1/auth/login",
            json={"email": "not-an-email", "password": "x"},
        )
        assert resp.status_code == 422

    def test_refresh_success(self, test_client: TestClient, auth_headers: dict) -> None:
        """POST /auth/refresh with valid refresh token returns new access token."""
        # First login to get a refresh token
        login_resp = test_client.post(
            "/api/v1/auth/login",
            json={"email": "admin@changan.com", "password": "x"},
        )
        refresh_token = login_resp.json()["refresh_token"]

        resp = test_client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh_token},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "access_token" in body
        assert body["token_type"] == "bearer"

    def test_refresh_invalid_token(self, test_client: TestClient) -> None:
        """POST /auth/refresh with invalid token returns 401."""
        resp = test_client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": "invalid-token-string"},
        )
        assert resp.status_code == 401

    def test_me_success(self, test_client: TestClient, auth_headers: dict) -> None:
        """GET /auth/me returns user info for authenticated user."""
        resp = test_client.get("/api/v1/auth/me", headers=auth_headers["admin_t1"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == 1
        assert body["name"] == "admin_ca"
        assert body["email"] == "admin@changan.com"
        assert body["role"] == "admin"
        assert body["tenant_id"] == "chang_an"

    def test_me_agent_role(self, test_client: TestClient, auth_headers: dict) -> None:
        """GET /auth/me for agent role returns agent user info."""
        resp = test_client.get("/api/v1/auth/me", headers=auth_headers["agent_t1"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["role"] == "agent"
        assert body["name"] == "agent_ca"
