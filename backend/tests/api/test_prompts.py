"""Prompts API tests."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.mark.integration
class TestPromptsAPI:
    def test_list_requires_auth(self, test_client: TestClient) -> None:
        resp = test_client.get("/api/v1/prompts")
        assert resp.status_code == 401

    def test_list_with_auth(self, test_client: TestClient, auth_headers: dict) -> None:
        resp = test_client.get("/api/v1/prompts", headers=auth_headers["admin_t1"])
        assert resp.status_code in (200, 500)

    def test_create_requires_admin(self, test_client: TestClient, auth_headers: dict) -> None:
        resp = test_client.post(
            "/api/v1/prompts",
            json={"name": "test", "version": "v0", "content": "test content"},
            headers=auth_headers["viewer_t1"],
        )
        assert resp.status_code in (403, 422, 500)


class TestPromptsAreTenantScoped:
    """`Prompt` predates TenantScopedBase and has no tenant_id of its own.

    Ownership is only reachable through ``created_by`` → ``users.tenant_id``.
    ``activate_prompt`` has always joined that way; ``list_prompts`` and
    ``get_prompt`` did not, so any authenticated caller could read any tenant's
    prompt body — the id was the only thing needed. The seeded prompts belong to
    user 1 in tenant ``chang_an``.
    """

    def test_another_tenant_cannot_read_a_prompt_body(
        self, test_client: Any, auth_headers: Any
    ) -> None:
        resp = test_client.get("/api/v1/prompts/1", headers=auth_headers["admin_t2"])

        assert resp.status_code == 404, (
            "a foreign prompt must be indistinguishable from a missing one"
        )
        assert "You are a QA inspector" not in resp.text

    def test_another_tenant_sees_an_empty_prompt_list(
        self, test_client: Any, auth_headers: Any
    ) -> None:
        resp = test_client.get("/api/v1/prompts", headers=auth_headers["admin_t2"])

        assert resp.status_code == 200
        assert resp.json()["items"] == [], "names and versions leak too, not just bodies"

    def test_the_owning_tenant_still_reads_its_own(
        self, test_client: Any, auth_headers: Any
    ) -> None:
        detail = test_client.get("/api/v1/prompts/1", headers=auth_headers["admin_t1"])
        listing = test_client.get("/api/v1/prompts", headers=auth_headers["admin_t1"])

        assert detail.status_code == 200
        assert detail.json()["content"] == "You are a QA inspector."
        assert len(listing.json()["items"]) == 2
