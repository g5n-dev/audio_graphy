"""The open (machine-to-machine) surface: keys, upload, status, and its walls.

The auth middleware passes ``/api/v1/open/`` through untouched, so the ONLY
thing standing between the internet and these routes is the ``require_api_key``
dependency — the last test in this file pins that every route carries it.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit


def _mint_key(test_client: TestClient, auth_headers: dict, name: str = "crm-sync") -> dict:
    response = test_client.post(
        "/api/v1/integration/api-keys",
        headers=auth_headers["admin_t1"],
        json={"name": name},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _upload(
    test_client: TestClient,
    api_key: str,
    *,
    external_ref: str,
    payload: bytes = b"RIFF-ish bytes standing in for audio",
    callback_url: str | None = None,
):
    data = {"external_ref": external_ref, "store_id": "store-9"}
    if callback_url is not None:
        data["callback_url"] = callback_url
    return test_client.post(
        "/api/v1/open/recordings",
        headers={"Authorization": f"Bearer {api_key}"},
        data=data,
        files={"audio": ("a.wav", io.BytesIO(payload), "audio/wav")},
    )


class TestApiKeyLifecycle:
    def test_minting_shows_the_secret_material_exactly_once(
        self, test_client: TestClient, auth_headers: dict
    ) -> None:
        minted = _mint_key(test_client, auth_headers)
        assert minted["api_key"].startswith("agk_")
        assert len(minted["webhook_secret"]) == 64

        listed = test_client.get("/api/v1/integration/api-keys", headers=auth_headers["admin_t1"])
        assert listed.status_code == 200
        body = listed.json()
        assert body["total"] == 1
        # The list must never re-surface either secret, in any field.
        assert "agk_" not in listed.text
        assert minted["webhook_secret"] not in listed.text

    def test_minting_is_admin_only(self, test_client: TestClient, auth_headers: dict) -> None:
        response = test_client.post(
            "/api/v1/integration/api-keys",
            headers=auth_headers["inspector_t1"],
            json={"name": "nope"},
        )
        assert response.status_code == 403

    def test_a_revoked_key_stops_authenticating(
        self, test_client: TestClient, auth_headers: dict
    ) -> None:
        minted = _mint_key(test_client, auth_headers)
        ok = test_client.get(
            "/api/v1/open/recordings/whatever/status",
            headers={"Authorization": f"Bearer {minted['api_key']}"},
        )
        assert ok.status_code == 404  # authenticated; the ref just doesn't exist

        revoked = test_client.post(
            f"/api/v1/integration/api-keys/{minted['key']['id']}/revoke",
            headers=auth_headers["admin_t1"],
        )
        assert revoked.status_code == 200
        after = test_client.get(
            "/api/v1/open/recordings/whatever/status",
            headers={"Authorization": f"Bearer {minted['api_key']}"},
        )
        assert after.status_code == 401


class TestUploadAndStatus:
    def test_upload_registers_and_status_follows_by_external_ref(
        self, test_client: TestClient, auth_headers: dict
    ) -> None:
        minted = _mint_key(test_client, auth_headers)
        created = _upload(test_client, minted["api_key"], external_ref="crm-42")
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["external_ref"] == "crm-42"
        assert body["replayed"] is False
        recording_id = body["recording_id"]

        status = test_client.get(
            "/api/v1/open/recordings/crm-42/status",
            headers={"Authorization": f"Bearer {minted['api_key']}"},
        )
        assert status.status_code == 200, status.text
        payload = status.json()
        assert payload["recording_id"] == recording_id
        assert payload["terminal"] is False
        # The status answer is ids and states only — never content. This is
        # the PIPL boundary: the open surface is an event channel, not an
        # export path.
        assert "transcript" not in status.text
        assert "path" not in payload

    def test_replaying_the_same_reference_is_idempotent(
        self, test_client: TestClient, auth_headers: dict
    ) -> None:
        minted = _mint_key(test_client, auth_headers)
        first = _upload(test_client, minted["api_key"], external_ref="crm-77")
        again = _upload(test_client, minted["api_key"], external_ref="crm-77")
        assert first.status_code == 201
        assert again.status_code == 201
        assert again.json()["replayed"] is True
        assert again.json()["recording_id"] == first.json()["recording_id"]

    def test_the_reference_namespace_is_per_tenant(
        self, test_client: TestClient, auth_headers: dict
    ) -> None:
        minted_t1 = _mint_key(test_client, auth_headers)
        _upload(test_client, minted_t1["api_key"], external_ref="shared-ref")

        response = test_client.post(
            "/api/v1/integration/api-keys",
            headers=auth_headers["admin_t2"],
            json={"name": "other-tenant"},
        )
        assert response.status_code == 201
        cross = test_client.get(
            "/api/v1/open/recordings/shared-ref/status",
            headers={"Authorization": f"Bearer {response.json()['api_key']}"},
        )
        assert cross.status_code == 404

    def test_an_empty_upload_is_refused(self, test_client: TestClient, auth_headers: dict) -> None:
        minted = _mint_key(test_client, auth_headers)
        response = _upload(test_client, minted["api_key"], external_ref="crm-empty", payload=b"")
        assert response.status_code == 422

    def test_a_metadata_service_callback_target_is_refused(
        self, test_client: TestClient, auth_headers: dict
    ) -> None:
        """The delivery worker POSTs wherever this field says — that is an
        SSRF primitive unless non-public targets are refused up front."""

        minted = _mint_key(test_client, auth_headers)
        response = _upload(
            test_client,
            minted["api_key"],
            external_ref="crm-ssrf",
            callback_url="http://169.254.169.254/latest/meta-data/",
        )
        assert response.status_code == 422
        assert "non-public" in response.text


class TestTheWallHasNoGaps:
    def test_requests_without_a_key_are_rejected(self, test_client: TestClient) -> None:
        assert test_client.get("/api/v1/open/recordings/x/status").status_code == 401
        assert (
            test_client.post(
                "/api/v1/open/recordings", data={"external_ref": "x", "store_id": "s"}
            ).status_code
            == 401
        )

    def test_a_wrong_key_is_rejected(self, test_client: TestClient) -> None:
        response = test_client.get(
            "/api/v1/open/recordings/x/status",
            headers={"Authorization": "Bearer agk_" + "0" * 40},
        )
        assert response.status_code == 401

    def test_every_open_route_requires_the_api_key_dependency(self) -> None:
        """The middleware passes /api/v1/open/ through on prefix; this is the
        assertion that makes that pass-through safe. A route added without
        ``require_api_key`` would be reachable by anyone on the internet."""

        from audio_graphy.api.open import require_api_key, router

        for route in router.routes:
            dependencies = getattr(route, "dependant", None)
            names = set()
            stack = [dependencies] if dependencies else []
            while stack:
                node = stack.pop()
                if node.call is not None:
                    names.add(node.call)
                stack.extend(node.dependencies)
            assert require_api_key in names, (
                f"route {getattr(route, 'path', route)} ships without require_api_key"
            )
