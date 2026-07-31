"""Bounded-list contracts for governance REST resources."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/tag-schemas",
        "/api/v1/tagger-versions",
        "/api/v1/tag-jobs",
        "/api/v1/tag-reviews",
        "/api/v1/tag-gold-sets",
        "/api/v1/tag-evaluations",
        "/api/v1/tag-deployments",
    ],
)
def test_governance_lists_reject_out_of_range_limits(
    path: str,
    test_client: TestClient,
    auth_headers: dict,
) -> None:
    response = test_client.get(f"{path}?limit=0", headers=auth_headers["admin_t1"])
    assert response.status_code == 422


def test_schema_list_honors_limit(
    test_client: TestClient,
    auth_headers: dict,
) -> None:
    for index in range(3):
        response = test_client.post(
            "/api/v1/tag-schemas",
            headers=auth_headers["admin_t1"],
            json={
                "key": f"bounded-schema-{index}",
                "name": f"标签体系 {index}",
                "description": "分页边界",
            },
        )
        assert response.status_code == 201, response.text

    response = test_client.get(
        "/api/v1/tag-schemas?limit=2",
        headers=auth_headers["admin_t1"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 2
    assert [item["key"] for item in response.json()["items"]] == [
        "bounded-schema-2",
        "bounded-schema-1",
    ]
