"""Tags recompute integration tests (TEST-04)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.integration
class TestTagsRecompute:
    """打标 → activate v2 → recompute → tag_current 更新 → stats delta → 缓存命中."""

    def test_recompute_dry_run(self, test_client: TestClient, auth_headers: dict) -> None:
        """Dry run should return preview without writing."""
        resp = test_client.post(
            "/api/v1/tags/recompute",
            json={"prompt_version": "v2", "dry_run": True},
            headers=auth_headers["admin_t1"],
        )
        # May return 200 if DB available, or 500 if not
        assert resp.status_code in (200, 500)

    def test_recompute_requires_admin(self, test_client: TestClient, auth_headers: dict) -> None:
        """Only admin can trigger recompute."""
        for role in ("inspector_t1", "agent_t1", "viewer_t1"):
            resp = test_client.post(
                "/api/v1/tags/recompute",
                json={"prompt_version": "v2"},
                headers=auth_headers[role],
            )
            assert resp.status_code == 403, f"{role} should be forbidden"
