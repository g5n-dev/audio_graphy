"""Auth API happy-path tests — login success, refresh, /me.

Covers: api/auth.py lines 43-68 (login), 89-97 (refresh), 109 (me).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import (  # noqa: F401
    SEED_USER_PASSWORD,
    _run_async,
    seed_recording,
)


@pytest.mark.integration
class TestAuthHappyPath:
    """Happy-path tests for /auth endpoints."""

    def test_login_success(self, test_client: TestClient) -> None:
        """POST /auth/login with valid credentials returns 200 + tokens."""
        resp = test_client.post(
            "/api/v1/auth/login",
            json={"email": "admin@changan.com", "password": SEED_USER_PASSWORD},
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

    def test_login_wrong_password_is_rejected(self, test_client: TestClient) -> None:
        """A known email with the wrong password must not authenticate.

        Regression guard: password verification used to be short-circuited
        whenever ``ADAPTER_MODE`` was ``mock`` — which is the default — so any
        password was accepted.
        """
        resp = test_client.post(
            "/api/v1/auth/login",
            json={"email": "admin@changan.com", "password": "definitely-not-it"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "INVALID_CREDENTIALS"

    def test_login_wrong_email(self, test_client: TestClient) -> None:
        """POST /auth/login with unknown email returns 401."""
        resp = test_client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@nowhere.com", "password": "x"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "INVALID_CREDENTIALS"

    def test_login_ambiguous_email_across_tenants_is_rejected(
        self,
        test_client: TestClient,
        db_session_factory,
    ) -> None:
        """An email present in two tenants must not authenticate into either.

        Users are unique per (tenant_id, email), so the same address can exist
        in several tenants; LoginRequest carries no tenant, so the only safe
        resolution is to refuse rather than pick whichever row comes back first.
        """
        from audio_graphy.auth.passwords import PasswordHasher
        from audio_graphy.models import User
        from audio_graphy.models.enums import UserRole

        async def _add_colliding_user() -> None:
            async with db_session_factory() as session:
                session.add(
                    User(
                        tenant_id="byd",
                        name="admin_byd_collision",
                        email="admin@changan.com",
                        role=UserRole.ADMIN.value,
                        password_hash=PasswordHasher(bcrypt_rounds=4).hash(SEED_USER_PASSWORD),
                    )
                )
                await session.commit()

        _run_async(_add_colliding_user())

        resp = test_client.post(
            "/api/v1/auth/login",
            json={"email": "admin@changan.com", "password": SEED_USER_PASSWORD},
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
            json={"email": "admin@changan.com", "password": SEED_USER_PASSWORD},
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
