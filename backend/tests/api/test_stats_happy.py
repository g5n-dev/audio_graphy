"""Stats API happy-path tests.

Covers: api/stats.py lines 37-56.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import _run_async, seed_recording, seed_tag


@pytest.mark.integration
class TestStatsHappyPath:
    """Happy-path tests for /tags/stats."""

    def test_stats_returns_data(self, test_client: TestClient, auth_headers: dict) -> None:
        """GET /tags/stats returns stats items."""
        resp = test_client.get("/api/v1/tags/stats", headers=auth_headers["admin_t1"])
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert "dimensions" in body
        assert "total_records" in body
        assert body["dimensions"] == ["tag_path"]

    def test_stats_group_by_tag_value(self, test_client: TestClient, auth_headers: dict) -> None:
        """GET /tags/stats?group_by=tag_value groups by tag value."""
        resp = test_client.get(
            "/api/v1/tags/stats?group_by=tag_value",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["dimensions"] == ["tag_value"]

    def test_stats_group_by_store(self, test_client: TestClient, auth_headers: dict) -> None:
        """GET /tags/stats?group_by=store_id groups by store."""
        resp = test_client.get(
            "/api/v1/tags/stats?group_by=store_id",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["dimensions"] == ["store_id"]

    def test_stats_with_seeded_tags(
        self, test_client: TestClient, auth_headers: dict, db_session_factory
    ) -> None:
        """GET /tags/stats returns seeded tag stats."""
        rec_id = _run_async(seed_recording(db_session_factory))
        _run_async(
            seed_tag(
                db_session_factory,
                rec_id,
                "chang_an",
                tag_path="quality.greeting",
                tag_value="pass",
            )
        )

        resp = test_client.get("/api/v1/tags/stats", headers=auth_headers["admin_t1"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_records"] >= 1

    def test_stats_filter_by_tag_path(self, test_client: TestClient, auth_headers: dict) -> None:
        """GET /tags/stats?tag_path=quality filters by tag path prefix."""
        resp = test_client.get(
            "/api/v1/tags/stats?tag_path=quality",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200
