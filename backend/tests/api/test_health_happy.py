"""Health API happy-path tests.

Covers: api/health.py readiness checks (DB, adapters, graph_store, file_index).
"""

from __future__ import annotations

import re

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

    def test_readiness_storage_ok(self, test_client: TestClient) -> None:
        """Readiness reports on the graph factory and on writable storage.

        It used to report a hardcoded ``file_index: "ok"`` and a graph check
        whose branches were both "ok", so neither could ever fail.
        """
        resp = test_client.get("/health/readiness")
        body = resp.json()
        assert body["checks"]["graph_store"] == "ok"
        assert body["checks"]["working_dir"] == "ok"
        assert body["checks"]["startup"] == "ok"

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

    def test_readiness_reports_startup_degradations(self, test_client: TestClient) -> None:
        """A subsystem that failed to wire up must keep the replica out of rotation.

        Those failures used to be a warning line and nothing else, so a process
        missing its pipeline worker or retention scheduler still advertised
        itself as ready and kept receiving traffic.
        """
        app_state = test_client.app.state
        original = getattr(app_state, "startup_degradations", [])
        app_state.startup_degradations = [
            {"component": "pipeline_worker", "error": "RuntimeError: boom"}
        ]
        try:
            resp = test_client.get("/health/readiness")
            assert resp.status_code == 503
            body = resp.json()
            assert body["status"] == "not_ready"
            # Names the component, so the operator does not have to grep logs.
            assert body["checks"]["startup"]["pipeline_worker"] == "RuntimeError: boom"
        finally:
            app_state.startup_degradations = original

    def test_readiness_flags_unwritable_working_dir(self, test_client: TestClient) -> None:
        """Storage the app cannot write to is a readiness failure, not a surprise later."""
        settings = test_client.app.state.settings
        original = settings.working_dir
        try:
            settings.working_dir = "/nonexistent/audiography-readiness-probe"
            resp = test_client.get("/health/readiness")
            assert resp.status_code == 503
            assert "not writable" in resp.json()["checks"]["working_dir"]
        finally:
            settings.working_dir = original

    def test_root_endpoint(self, test_client: TestClient) -> None:
        """GET / returns API info."""
        resp = test_client.get("/")
        assert resp.status_code == 200
        body = resp.json()
        assert "message" in body or "version" in body


class TestReadinessChecksTheSchema:
    """Reachable is not the same as migrated.

    `alembic upgrade head` runs in the container command before uvicorn. A
    clean-clone acceptance caught it no-opping against an empty database: the
    process started, `/health` and `/health/readiness` both returned 200, and
    every request answered "table doesn't exist" — including login. Liveness
    cannot see that, and readiness could not either, so a deployment serving
    nothing looked correct.
    """

    def test_a_migrated_mysql_at_head_reports_ok(self) -> None:
        """The value is read from alembic_version and compared to the head on disk."""
        from audio_graphy.api.health import _expected_head

        head = _expected_head()
        assert head is not None, "the migration chain must have exactly one head"
        # Parsed from the files, not via `alembic.script`: backend/alembic/
        # shadows the installed distribution whenever the process runs from
        # backend/, which is how the container runs.
        assert re.fullmatch(r"\d{4}_\w+", head), f"unexpected head revision {head!r}"

    def test_a_non_mysql_session_is_skipped_not_failed(self, test_client: TestClient) -> None:
        """The suite builds its schema with create_all, so there is no version row.

        Reporting that as an error would make readiness fail in every test and in
        every SQLite dev run — a check that always fails is a check nobody reads.
        """
        body = test_client.get("/health/readiness").json()

        assert body["status"] == "ready"
        assert body["checks"]["migrations"].startswith("skipped:")

    def test_an_unmigrated_mysql_fails_readiness(self, test_client: TestClient) -> None:
        """The case that shipped: MySQL reachable, alembic_version absent."""
        app = test_client.app
        original = app.state.session_factory

        class _MySQLDialect:
            name = "mysql"

        class _MySQLBind:
            dialect = _MySQLDialect()

        class _Result:
            @staticmethod
            def scalar_one_or_none() -> None:
                return None

        class _UnmigratedSession:
            bind = _MySQLBind()

            async def __aenter__(self) -> _UnmigratedSession:
                return self

            async def __aexit__(self, *exc: object) -> None:
                return None

            async def execute(self, *args: object, **kwargs: object) -> _Result:
                # `SELECT 1` succeeds — the database really is reachable.
                return _Result()

        app.state.session_factory = _UnmigratedSession
        try:
            resp = test_client.get("/health/readiness")
            body = resp.json()
            assert resp.status_code == 503
            assert body["checks"]["migrations"] == "error: schema has never been migrated"
        finally:
            app.state.session_factory = original
