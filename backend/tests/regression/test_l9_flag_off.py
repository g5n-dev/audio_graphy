"""L9 contract verification — explicit test that ``enable_advanced_graph=False``
causes every M9 R2 endpoint to return 404.

This test deliberately overrides the default ``api_settings`` fixture to
force the flag off, then probes each R2 path.
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture
def api_settings_off(tmp_path, monkeypatch):
    """Force enable_advanced_graph=False regardless of test-wide env."""
    from audio_graphy.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("WORKING_DIR", str(tmp_path / "working_dir"))
    monkeypatch.setenv("ADAPTER_MODE", "mock")
    monkeypatch.setenv("ENABLE_ADVANCED_GRAPH", "false")
    (tmp_path / "working_dir").mkdir(parents=True, exist_ok=True)
    s = get_settings()
    assert s.enable_advanced_graph is False, "fixture precondition"
    return s


@pytest.fixture
def l9_client(api_settings_off) -> Any:
    """Build a TestClient with the advanced-graph flag forced off."""
    from fastapi.testclient import TestClient

    from audio_graphy.main import create_app

    app = create_app()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def l9_auth_headers(api_settings_off) -> dict[str, str]:
    """Build a valid admin JWT so requests reach the router layer."""
    from audio_graphy.auth.jwt_utils import JWTManager

    jm = JWTManager(
        secret=api_settings_off.jwt_secret,
        algorithm=api_settings_off.jwt_algorithm,
        exp_hours=1,
        refresh_exp_hours=24,
    )
    token = jm.create_access_token(user_id=1, tenant_id="chang_an", role="admin")
    return {"Authorization": f"Bearer {token}"}


def test_l9_disabled_returns_404_for_all_r2_paths(l9_client, l9_auth_headers) -> None:
    """Every R2 endpoint must 404 when the master flag is False."""
    paths = [
        ("GET", "/api/v1/recordings/1/edges"),
        ("GET", "/api/v1/recordings/1/edges/range"),
        ("GET", "/api/v1/recordings/1/edges/abc/history"),
        ("GET", "/api/v1/admin/leiden/jobs"),
        ("GET", "/api/v1/admin/leiden/status"),
        ("POST", "/api/v1/admin/leiden/recompute"),
        ("POST", "/api/v1/search/global"),
        ("POST", "/api/v1/search/local"),
        ("POST", "/api/v1/search/communities/1/drill-down"),
        ("POST", "/api/v1/admin/compression/dry-run"),
        ("POST", "/api/v1/admin/compression/run"),
        ("GET", "/api/v1/admin/compression/history"),
    ]
    for method, path in paths:
        if method == "GET":
            resp = l9_client.get(path, headers=l9_auth_headers)
        else:
            resp = l9_client.post(path, json={}, headers=l9_auth_headers)
        assert resp.status_code == 404, (
            f"L9 violation: {method} {path} returned {resp.status_code} "
            "(expected 404 because enable_advanced_graph=False)"
        )
