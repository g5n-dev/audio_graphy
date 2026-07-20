"""Tests for audio_graphy.main FastAPI app."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from audio_graphy.main import create_app


@pytest.fixture
def client(fresh_settings) -> TestClient:
    """Test client wired to fresh settings (no lifespan to avoid DB timeout)."""
    app = create_app()

    # Initialize app state manually
    from audio_graphy.auth.jwt_utils import JWTManager
    from audio_graphy.config import build_adapters

    app.state.settings = fresh_settings
    app.state.version = "0.3.0"
    app.state.engine = None
    app.state.session_factory = None
    app.state.adapter_bundle = build_adapters(fresh_settings)
    app.state.vector_store = None
    app.state.graph_stores = {}
    app.state.file_indexes = {}

    jwt_manager = JWTManager(
        secret=fresh_settings.jwt_secret,
        algorithm=fresh_settings.jwt_algorithm,
        exp_hours=fresh_settings.jwt_exp_hours,
        refresh_exp_hours=fresh_settings.jwt_refresh_exp_hours,
    )
    app.state.jwt_manager = jwt_manager

    return TestClient(app, raise_server_exceptions=False)


class TestHealthEndpoint:
    """GET /health — liveness probe."""

    @pytest.mark.unit
    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200

    @pytest.mark.unit
    def test_returns_status_ok(self, client: TestClient) -> None:
        resp = client.get("/health")
        body = resp.json()
        assert body["status"] == "ok"
        assert body["service"] == "audiography-backend"

    @pytest.mark.unit
    def test_returns_adapter_mode(self, client: TestClient) -> None:
        resp = client.get("/health")
        body = resp.json()
        # Health endpoint now returns service name; adapter_mode check moved to readiness
        assert body["service"] == "audiography-backend"

    @pytest.mark.unit
    def test_returns_version(self, client: TestClient) -> None:
        resp = client.get("/")
        body = resp.json()
        assert "version" in body


class TestRootEndpoint:
    """GET / — root redirect."""

    @pytest.mark.unit
    def test_returns_200_with_docs_link(self, client: TestClient) -> None:
        resp = client.get("/")
        assert resp.status_code == 200
        body = resp.json()
        assert body["docs"] == "/docs"
