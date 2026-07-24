"""Tests for audio_graphy.main FastAPI app."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from audio_graphy.main import _build_audio_crypto, create_app


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


def test_audio_crypto_startup_validation_fails_closed_without_key(tmp_path) -> None:
    settings = SimpleNamespace(
        master_key_path=str(tmp_path / "missing.key"),
        log_level="INFO",
        audio_crypto_chunk_size_bytes=4096,
        max_recording_audio_bytes=1024 * 1024,
    )

    with pytest.raises(FileNotFoundError, match="Master key not found"):
        _build_audio_crypto(settings)


def test_audio_crypto_debug_startup_generates_and_validates_key(tmp_path) -> None:
    key_path = tmp_path / "dev.key"
    settings = SimpleNamespace(
        master_key_path=str(key_path),
        log_level="DEBUG",
        audio_crypto_chunk_size_bytes=4096,
        max_recording_audio_bytes=1024 * 1024,
    )

    crypto = _build_audio_crypto(settings)

    assert key_path.exists()
    crypto.validate_master_key()


@pytest.mark.asyncio
async def test_graph_store_factory_cold_loads_and_reuses_tenant_store(
    tmp_path,
) -> None:
    """The lifespan factory must not skip a tenant merely because cache is cold."""
    from audio_graphy.core.types import _list_to_str
    from audio_graphy.main import _build_graph_store_factory
    from audio_graphy.storage.graph_networkx import NetworkXGraphStore

    seeded = NetworkXGraphStore(tmp_path, tenant_id="tenant-a")
    await seeded.load()
    seeded.graph.add_node("persisted", recording_ids=_list_to_str(["7"]))
    seeded.invalidate_path_projection()
    await seeded.save()

    stores = {}
    factory = _build_graph_store_factory(stores, tmp_path)
    first = await factory("tenant-a")
    second = await factory("tenant-a")

    assert first is second
    assert first.graph.has_node("persisted")
    assert stores["tenant-a"] is first
