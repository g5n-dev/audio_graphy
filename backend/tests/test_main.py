"""Tests for audio_graphy.main FastAPI app."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from audio_graphy.main import create_app


@pytest.fixture
def client(fresh_settings) -> TestClient:
    """Test client wired to fresh settings."""
    app = create_app()
    return TestClient(app)


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
        # Default in conftest is "mock"
        assert body["adapter_mode"] == "mock"

    @pytest.mark.unit
    def test_returns_version(self, client: TestClient) -> None:
        resp = client.get("/health")
        body = resp.json()
        assert body["version"] == "0.1.0"


class TestRootEndpoint:
    """GET / — root redirect."""

    @pytest.mark.unit
    def test_returns_200_with_docs_link(self, client: TestClient) -> None:
        resp = client.get("/")
        assert resp.status_code == 200
        body = resp.json()
        assert body["docs"] == "/docs"
