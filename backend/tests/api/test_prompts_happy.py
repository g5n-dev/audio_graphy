"""Prompts API happy-path tests.

Covers: api/prompts.py GET list, POST create, GET detail, POST activate.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.integration
class TestPromptsHappyPath:
    """Happy-path tests for /prompts endpoints."""

    def test_list_prompts_returns_data(self, test_client: TestClient, auth_headers: dict) -> None:
        """GET /prompts returns seeded prompt list."""
        resp = test_client.get("/api/v1/prompts", headers=auth_headers["admin_t1"])
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert len(body["items"]) >= 2  # v1 + v2 seeded

    def test_list_prompts_filter_by_name(self, test_client: TestClient, auth_headers: dict) -> None:
        """GET /prompts?name=xxx filters by name."""
        resp = test_client.get(
            "/api/v1/prompts?name=tag_prompt_v1",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        for item in body["items"]:
            assert item["name"] == "tag_prompt_v1"

    def test_list_prompts_active_only(self, test_client: TestClient, auth_headers: dict) -> None:
        """GET /prompts?active_only=true returns only active prompts."""
        resp = test_client.get(
            "/api/v1/prompts?active_only=true",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        for item in body["items"]:
            assert item["active"] is True

    def test_get_prompt_detail(self, test_client: TestClient, auth_headers: dict) -> None:
        """GET /prompts/{id} returns full prompt with content."""
        resp = test_client.get("/api/v1/prompts/1", headers=auth_headers["admin_t1"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == 1
        assert body["name"] == "tag_prompt_v1"
        assert "content" in body

    def test_get_prompt_not_found(self, test_client: TestClient, auth_headers: dict) -> None:
        """GET /prompts/{id} with nonexistent ID returns 404."""
        resp = test_client.get(
            "/api/v1/prompts/99999",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "PROMPT_NOT_FOUND"

    def test_create_prompt_success(self, test_client: TestClient, auth_headers: dict) -> None:
        """POST /prompts creates a new prompt version."""
        resp = test_client.post(
            "/api/v1/prompts",
            json={
                "name": "test_prompt_new",
                "version": "v1",
                "content": "Test prompt content",
                "changelog": "Initial version",
            },
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "test_prompt_new"
        assert body["version"] == "v1"
        assert body["active"] is False

    def test_create_prompt_with_activate(self, test_client: TestClient, auth_headers: dict) -> None:
        """Legacy create-and-activate cannot bypass canonical quality gates."""
        resp = test_client.post(
            "/api/v1/prompts",
            json={
                "name": "test_prompt_activate",
                "version": "v1",
                "content": "Content",
                "changelog": "v1",
                "activate": True,
            },
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 409
        assert "canonical" in resp.json()["detail"]

    def test_create_prompt_duplicate(self, test_client: TestClient, auth_headers: dict) -> None:
        """POST /prompts with duplicate (name, version) returns 409."""
        # tag_prompt_v1/v1 already seeded
        resp = test_client.post(
            "/api/v1/prompts",
            json={
                "name": "tag_prompt_v1",
                "version": "v1",
                "content": "Duplicate",
            },
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 409

    def test_activate_prompt(self, test_client: TestClient, auth_headers: dict) -> None:
        """Changed Prompt without a production candidate fails closed."""
        resp = test_client.post(
            "/api/v1/prompts/2/activate",
            json={"trigger_recompute": False},
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 409
        assert "candidate_tagger_version_id" in resp.json()["detail"]

    def test_activate_prompt_not_found(self, test_client: TestClient, auth_headers: dict) -> None:
        """POST /prompts/{id}/activate with nonexistent ID returns 404."""
        resp = test_client.post(
            "/api/v1/prompts/99999/activate",
            json={"trigger_recompute": False},
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 404

    def test_activate_prompt_is_tenant_scoped(
        self, test_client: TestClient, auth_headers: dict
    ) -> None:
        """A tenant cannot read or deactivate another tenant's legacy prompt."""

        resp = test_client.post(
            "/api/v1/prompts/2/activate",
            json={"trigger_recompute": False},
            headers=auth_headers["admin_t2"],
        )
        assert resp.status_code == 404
        old = test_client.get(
            "/api/v1/prompts/1",
            headers=auth_headers["admin_t1"],
        )
        candidate = test_client.get(
            "/api/v1/prompts/2",
            headers=auth_headers["admin_t1"],
        )
        assert old.status_code == 200
        assert candidate.status_code == 200
        assert old.json()["active"] is True
        assert candidate.json()["active"] is False

    def test_activate_prompt_dry_run(self, test_client: TestClient, auth_headers: dict) -> None:
        """Dry-run fails closed when legacy tags have no canonical recipe."""
        resp = test_client.post(
            "/api/v1/prompts/2/activate",
            json={"trigger_recompute": False, "dry_run": True},
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 409
        assert "canonical" in resp.json()["detail"].lower()

    def test_activate_prompt_dry_run_same_content_is_zero_call(
        self,
        test_client: TestClient,
        auth_headers: dict,
    ) -> None:
        current = test_client.get(
            "/api/v1/prompts/1",
            headers=auth_headers["admin_t1"],
        ).json()
        created = test_client.post(
            "/api/v1/prompts",
            json={
                "name": current["name"],
                "version": "same-content-v2",
                "content": current["content"] + "\r\n",
            },
            headers=auth_headers["admin_t1"],
        )
        assert created.status_code == 201

        resp = test_client.post(
            f"/api/v1/prompts/{created.json()['id']}/activate",
            json={
                "dry_run": True,
                "sample_limit": 100,
                "max_provider_tokens": 0,
                "max_provider_calls": 0,
            },
            headers=auth_headers["admin_t1"],
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["sampled_count"] == 0
        assert body["estimated_tokens"] == 0
        assert body["estimated_provider_calls"] == 0
        assert body["provider_calls"] == 0
        assert body["quality_gate_status"] == "unchanged_prompt"

    def test_activate_prompt_with_recompute(
        self, test_client: TestClient, auth_headers: dict, db_session_factory
    ) -> None:
        """Ambiguous legacy recompute is rejected before prompt activation."""
        from tests.api.conftest import _run_async, seed_recording

        _run_async(seed_recording(db_session_factory))
        before_old = test_client.get(
            "/api/v1/prompts/1",
            headers=auth_headers["admin_t1"],
        ).json()
        before_candidate = test_client.get(
            "/api/v1/prompts/2",
            headers=auth_headers["admin_t1"],
        ).json()

        resp = test_client.post(
            "/api/v1/prompts/2/activate",
            json={"trigger_recompute": True},
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 409
        assert "candidate_tagger_version_id" in resp.json()["detail"]

        after_old = test_client.get(
            "/api/v1/prompts/1",
            headers=auth_headers["admin_t1"],
        ).json()
        after_candidate = test_client.get(
            "/api/v1/prompts/2",
            headers=auth_headers["admin_t1"],
        ).json()
        assert after_old["active"] is before_old["active"] is True
        assert after_candidate["active"] is before_candidate["active"] is False
