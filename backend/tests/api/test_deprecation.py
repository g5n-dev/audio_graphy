"""Contract tests for the legacy tagging migration headers."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from audio_graphy.api.deprecation import (
    LegacyTaggingDeprecationMiddleware,
    is_legacy_tagging_path,
)


def test_legacy_path_classifier_is_narrow() -> None:
    assert is_legacy_tagging_path("/api/v1/tags")
    assert is_legacy_tagging_path("/api/v1/tags/recompute/task-1")
    assert is_legacy_tagging_path("/api/v1/prompts/7/activate")
    assert is_legacy_tagging_path("/api/v1/receptions/101/dialogue-tags/derive")

    assert not is_legacy_tagging_path("/api/v1/tag-jobs")
    assert not is_legacy_tagging_path("/api/v1/tag-schemas")
    assert not is_legacy_tagging_path("/api/v1/receptions/101/workspace")


def test_legacy_response_advertises_async_successor() -> None:
    app = FastAPI()
    app.add_middleware(LegacyTaggingDeprecationMiddleware)

    @app.get("/api/v1/tags/recompute/{task_id}")
    async def legacy(task_id: str) -> dict[str, str]:
        return {"task_id": task_id}

    response = TestClient(app).get("/api/v1/tags/recompute/task-1")

    assert response.status_code == 200
    assert response.headers["deprecation"] == "true"
    assert response.headers["sunset"] == "Fri, 31 Dec 2027 23:59:59 GMT"
    assert response.headers["link"] == ('</api/v1/tag-jobs>; rel="successor-version"')


def test_new_governance_response_has_no_deprecation_header() -> None:
    app = FastAPI()
    app.add_middleware(LegacyTaggingDeprecationMiddleware)

    @app.get("/api/v1/tag-jobs")
    async def current() -> dict[str, list[object]]:
        return {"items": []}

    response = TestClient(app).get("/api/v1/tag-jobs")

    assert response.status_code == 200
    assert "deprecation" not in response.headers
