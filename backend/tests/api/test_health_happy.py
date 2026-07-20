"""Health API happy-path tests.

Covers: api/health.py readiness checks (DB, adapters, graph_store, file_index).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.integration
class TestHealthHappyPath:
    """Happy-path tests for /health endpoints."""

    def test_liveness_ok(self, test_client: TestClient) -> None:
        """GET /health returns 200 with status=ok."""
        resp = test_client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["service"] == "audiography-backend"

    def test_readiness_checks(self, test_client: TestClient) -> None:
        """GET /health/readiness returns checks dict with DB + adapter status."""
        resp = test_client.get("/health/readiness")
        assert resp.status_code in (200, 503)
        body = resp.json()
        assert "status" in body
        assert "checks" in body
        assert "version" in body
        checks = body["checks"]
        assert "database" in checks
        assert "adapters" in checks
        # Adapter checks should have sub-components
        adapters = checks["adapters"]
        assert "vad" in adapters
        assert "asr" in adapters
        assert "strong_llm" in adapters

    def test_readiness_db_ok(self, test_client: TestClient) -> None:
        """Readiness check should show DB as ok (SQLite test DB is available)."""
        resp = test_client.get("/health/readiness")
        body = resp.json()
        # With SQLite test DB, database check should pass
        assert body["checks"]["database"] == "ok"

    def test_readiness_adapters_ok(self, test_client: TestClient) -> None:
        """Readiness check should show all adapters as ok (mock mode)."""
        resp = test_client.get("/health/readiness")
        body = resp.json()
        adapters = body["checks"]["adapters"]
        for adapter_name in ("vad", "asr", "strong_llm", "weak_llm", "embed"):
            assert adapters[adapter_name] == "ok", f"Adapter {adapter_name} not ok"

    def test_readiness_graph_store_ok(self, test_client: TestClient) -> None:
        """Readiness check should show graph_store and file_index as ok."""
        resp = test_client.get("/health/readiness")
        body = resp.json()
        assert body["checks"]["graph_store"] == "ok"
        assert body["checks"]["file_index"] == "ok"

    def test_readiness_all_ok_returns_200(self, test_client: TestClient) -> None:
        """When all checks pass, readiness returns 200 with status=ready."""
        resp = test_client.get("/health/readiness")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ready"

    def test_readiness_db_error_returns_503(self, test_client: TestClient) -> None:
        """When session_factory is missing, readiness returns 503."""
        # Temporarily remove session_factory to trigger error path
        original_factory = test_client.app.state.session_factory
        test_client.app.state.session_factory = None
        try:
            resp = test_client.get("/health/readiness")
            assert resp.status_code == 503
            body = resp.json()
            assert body["status"] == "not_ready"
            assert "error" in body["checks"]["database"]
        finally:
            test_client.app.state.session_factory = original_factory

    def test_readiness_adapter_bundle_missing(self, test_client: TestClient) -> None:
        """When adapter bundle is missing, readiness returns 503."""
        original_bundle = test_client.app.state.adapter_bundle
        test_client.app.state.adapter_bundle = None
        try:
            resp = test_client.get("/health/readiness")
            assert resp.status_code == 503
            body = resp.json()
            assert body["status"] == "not_ready"
        finally:
            test_client.app.state.adapter_bundle = original_bundle

    def test_root_endpoint(self, test_client: TestClient) -> None:
        """GET / returns API info."""
        resp = test_client.get("/")
        assert resp.status_code == 200
        body = resp.json()
        assert "message" in body or "version" in body
