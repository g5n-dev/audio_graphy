"""Tags API happy-path tests.

Covers: api/tags.py GET tags, POST tags (auto/manual), recompute.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import _run_async, seed_recording, seed_tag


@pytest.mark.integration
class TestTagsHappyPath:
    """Happy-path tests for tags endpoints."""

    def test_get_tags_empty(
        self, test_client: TestClient, auth_headers: dict, db_session_factory
    ) -> None:
        """GET /recordings/{id}/tags returns empty list when no tags."""
        rec_id = _run_async(seed_recording(db_session_factory))

        resp = test_client.get(
            f"/api/v1/recordings/{rec_id}/tags",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["recording_id"] == rec_id
        assert body["view"] == "current"
        assert body["tags"] == []

    def test_get_tags_with_data(
        self, test_client: TestClient, auth_headers: dict, db_session_factory
    ) -> None:
        """GET /recordings/{id}/tags returns seeded tags."""
        rec_id = _run_async(seed_recording(db_session_factory))
        _run_async(seed_tag(db_session_factory, rec_id, "chang_an"))

        resp = test_client.get(
            f"/api/v1/recordings/{rec_id}/tags",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["tags"]) >= 1
        tag = body["tags"][0]
        assert tag["tag_path"] == "quality.greeting"
        assert tag["tag_value"] == "pass"

    def test_get_tags_history_view(
        self, test_client: TestClient, auth_headers: dict, db_session_factory
    ) -> None:
        """GET /recordings/{id}/tags?view=history returns facts."""
        rec_id = _run_async(seed_recording(db_session_factory))
        _run_async(seed_tag(db_session_factory, rec_id, "chang_an"))

        resp = test_client.get(
            f"/api/v1/recordings/{rec_id}/tags?view=history",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["view"] == "history"
        assert len(body["tags"]) >= 1

    def test_get_tags_facts_view(
        self, test_client: TestClient, auth_headers: dict, db_session_factory
    ) -> None:
        """GET /recordings/{id}/tags?view=facts returns full fact records."""
        rec_id = _run_async(seed_recording(db_session_factory))
        _run_async(seed_tag(db_session_factory, rec_id, "chang_an"))

        resp = test_client.get(
            f"/api/v1/recordings/{rec_id}/tags?view=facts",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["view"] == "facts"
        assert len(body["tags"]) >= 1
        fact_tag = body["tags"][0]
        assert "model_version" in fact_tag
        assert "input_hash" in fact_tag

    def test_get_tags_with_tag_path_filter(
        self, test_client: TestClient, auth_headers: dict, db_session_factory
    ) -> None:
        """GET /recordings/{id}/tags?view=history&tag_path=quality filters."""
        rec_id = _run_async(seed_recording(db_session_factory))
        _run_async(seed_tag(db_session_factory, rec_id, "chang_an", tag_path="quality.greeting"))

        resp = test_client.get(
            f"/api/v1/recordings/{rec_id}/tags?view=history&tag_path=quality",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200

    def test_get_tags_cross_tenant_404(
        self, test_client: TestClient, auth_headers: dict, db_session_factory
    ) -> None:
        """GET tags from different tenant returns 404."""
        rec_id = _run_async(seed_recording(db_session_factory, tenant_id="chang_an"))

        resp = test_client.get(
            f"/api/v1/recordings/{rec_id}/tags",
            headers=auth_headers["admin_t2"],
        )
        assert resp.status_code == 404

    def test_get_tags_not_found(self, test_client: TestClient, auth_headers: dict) -> None:
        """GET tags for nonexistent recording returns 404."""
        resp = test_client.get(
            "/api/v1/recordings/99999/tags",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 404

    def test_post_manual_tag_success(
        self, test_client: TestClient, auth_headers: dict, db_session_factory
    ) -> None:
        """POST /recordings/{id}/tags with mode=manual on indexed recording."""
        rec_id = _run_async(seed_recording(db_session_factory, status="indexed"))

        resp = test_client.post(
            f"/api/v1/recordings/{rec_id}/tags",
            json={
                "mode": "manual",
                "tag_path": "quality.greeting",
                "tag_value": "fail",
            },
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["recording_id"] == rec_id
        assert body["tag_path"] == "quality.greeting"
        assert body["tag_value"] == "fail"
        assert body["source"] == "manual"

    def test_post_auto_tag_success(
        self, test_client: TestClient, auth_headers: dict, db_session_factory
    ) -> None:
        """POST /recordings/{id}/tags with mode=auto triggers LLM tagging."""
        rec_id = _run_async(seed_recording(db_session_factory, status="indexed"))

        resp = test_client.post(
            f"/api/v1/recordings/{rec_id}/tags",
            json={
                "mode": "auto",
                "tag_paths": ["quality.greeting"],
            },
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["recording_id"] == rec_id
        assert body["tagged"] >= 1
        assert len(body["results"]) >= 1

    def test_post_tags_not_found(self, test_client: TestClient, auth_headers: dict) -> None:
        """POST tags on nonexistent recording returns 404."""
        resp = test_client.post(
            "/api/v1/recordings/99999/tags",
            json={"mode": "manual", "tag_path": "test", "tag_value": "x"},
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 404

    def test_post_tags_not_indexed(
        self, test_client: TestClient, auth_headers: dict, db_session_factory
    ) -> None:
        """POST tags on non-indexed recording returns 409."""
        rec_id = _run_async(seed_recording(db_session_factory, status="queued"))

        resp = test_client.post(
            f"/api/v1/recordings/{rec_id}/tags",
            json={"mode": "manual", "tag_path": "test", "tag_value": "x"},
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 409

    def test_recompute_dry_run(
        self, test_client: TestClient, auth_headers: dict, db_session_factory
    ) -> None:
        """POST /tags/recompute with dry_run=true returns preview."""
        _run_async(seed_recording(db_session_factory))

        resp = test_client.post(
            "/api/v1/tags/recompute",
            json={"prompt_version": "v2", "dry_run": True},
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["dry_run"] is True
        assert "affected_count" in body
        assert "changed_count" in body

    def test_recompute_execute(
        self, test_client: TestClient, auth_headers: dict, db_session_factory
    ) -> None:
        """POST /tags/recompute with dry_run=false creates and executes task."""
        _run_async(seed_recording(db_session_factory))

        resp = test_client.post(
            "/api/v1/tags/recompute",
            json={"prompt_version": "v2", "dry_run": False},
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["dry_run"] is False
        assert "task_id" in body
        assert "status" in body

    def test_get_recompute_task_success(
        self, test_client: TestClient, auth_headers: dict, db_session_factory
    ) -> None:
        """GET /tags/recompute/{task_id} returns task status after recompute."""
        _run_async(seed_recording(db_session_factory))

        # First create a recompute task
        create_resp = test_client.post(
            "/api/v1/tags/recompute",
            json={"prompt_version": "v2", "dry_run": False},
            headers=auth_headers["admin_t1"],
        )
        task_id = create_resp.json()["task_id"]

        # Then query its status
        resp = test_client.get(
            f"/api/v1/tags/recompute/{task_id}",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] == task_id
        assert "status" in body
        assert "total" in body

    def test_get_recompute_task_not_found(
        self, test_client: TestClient, auth_headers: dict
    ) -> None:
        """GET /tags/recompute/{task_id} with nonexistent task returns 404."""
        resp = test_client.get(
            "/api/v1/tags/recompute/nonexistent-task-id",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 404
