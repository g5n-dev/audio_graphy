"""API tests for T6 — Leiden admin endpoints.

Coverage:
    - POST /admin/leiden/recompute happy path (admin only).
    - GET /admin/leiden/jobs/{id}.
    - GET /admin/leiden/jobs (paginated).
    - GET /admin/leiden/status.
    - Inspector / viewer → 403.
    - Unauthenticated → 401.
    - Cross-tenant → 404 / empty list.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.api.conftest import _run_async, seed_recording

pytestmark = pytest.mark.integration


def _seed_graph_for_leiden(test_client: Any, tenant_id: str = "chang_an") -> None:
    """Seed a small graph with 3 connected nodes."""
    import networkx as nx

    from audio_graphy.core.types import _list_to_str

    graph_stores: dict[str, Any] = test_client.app.state.graph_stores
    settings = test_client.app.state.settings
    from audio_graphy.storage.graph_networkx import NetworkXGraphStore

    store = graph_stores.get(tenant_id)
    if store is None:
        store = NetworkXGraphStore(settings.working_dir, tenant_id=tenant_id)
        graph_stores[tenant_id] = store

    g: nx.MultiDiGraph = store._graph
    g.clear()
    for nid in ("客户A", "长安CS75", "销售张三"):
        g.add_node(
            nid,
            name=nid,
            type="实体",
            description="测试",
            degree=2,
            source_ids=_list_to_str(["1_1"]),
            recording_ids=_list_to_str(["1"]),
        )
    g.add_edge(
        "客户A",
        "长安CS75",
        key="询问",
        relation="询问",
        weight=1.0,
        confidence="EXTRACTED",
        confidence_score=0.9,
        source_ids=_list_to_str(["1_1"]),
    )
    g.add_edge(
        "长安CS75",
        "销售张三",
        key="推荐",
        relation="推荐",
        weight=0.8,
        confidence="INFERRED",
        confidence_score=0.7,
        source_ids=_list_to_str(["1_1"]),
    )
    store._loaded = True


@pytest.fixture
def seeded_graph_and_recording(test_client, db_session_factory):
    _run_async(seed_recording(db_session_factory, tenant_id="chang_an"))
    _seed_graph_for_leiden(test_client, tenant_id="chang_an")


# ============================================================
# POST /admin/leiden/recompute — happy path
# ============================================================


def test_recompute_happy_path(test_client, auth_headers, seeded_graph_and_recording):
    """Admin can trigger a synchronous Leiden run."""
    resp = test_client.post(
        "/api/v1/admin/leiden/recompute",
        headers=auth_headers["admin_t1"],
        json={"force_full": True, "triggered_by": "manual"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] in {"succeeded", "failed"}
    assert body["tenant_id"] == "chang_an"
    assert body["node_count_snapshot"] >= 3
    assert body["edge_count_snapshot"] >= 2


def test_recompute_forbidden_for_inspector(
    test_client, auth_headers, seeded_graph_and_recording
):
    resp = test_client.post(
        "/api/v1/admin/leiden/recompute",
        headers=auth_headers["inspector_t1"],
        json={"force_full": True},
    )
    assert resp.status_code == 403, resp.text


def test_recompute_forbidden_for_viewer(
    test_client, auth_headers, seeded_graph_and_recording
):
    resp = test_client.post(
        "/api/v1/admin/leiden/recompute",
        headers=auth_headers["viewer_t1"],
        json={"force_full": True},
    )
    assert resp.status_code == 403, resp.text


def test_recompute_unauthenticated_401(
    test_client, seeded_graph_and_recording
):
    resp = test_client.post(
        "/api/v1/admin/leiden/recompute",
        json={"force_full": True},
    )
    assert resp.status_code == 401, resp.text


# ============================================================
# GET /admin/leiden/jobs/{id} + GET /admin/leiden/jobs
# ============================================================


def test_get_job_and_list_jobs(test_client, auth_headers, seeded_graph_and_recording):
    # First create a job.
    create = test_client.post(
        "/api/v1/admin/leiden/recompute",
        headers=auth_headers["admin_t1"],
        json={"force_full": True},
    )
    assert create.status_code == 201, create.text
    job_id = create.json()["id"]

    # GET by id.
    get_resp = test_client.get(
        f"/api/v1/admin/leiden/jobs/{job_id}",
        headers=auth_headers["admin_t1"],
    )
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["id"] == job_id

    # GET list.
    list_resp = test_client.get(
        "/api/v1/admin/leiden/jobs",
        headers=auth_headers["admin_t1"],
    )
    assert list_resp.status_code == 200
    body = list_resp.json()
    assert body["total"] >= 1
    assert any(item["id"] == job_id for item in body["items"])


def test_get_job_404_when_missing(test_client, auth_headers):
    resp = test_client.get(
        "/api/v1/admin/leiden/jobs/9999999",
        headers=auth_headers["admin_t1"],
    )
    assert resp.status_code == 404, resp.text


def test_list_jobs_cross_tenant_isolation(
    test_client, auth_headers, seeded_graph_and_recording
):
    """Tenant 2 admin sees no jobs from tenant 1."""
    resp = test_client.get(
        "/api/v1/admin/leiden/jobs",
        headers=auth_headers["admin_t2"],
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


# ============================================================
# GET /admin/leiden/status
# ============================================================


def test_status_happy_path(test_client, auth_headers, seeded_graph_and_recording):
    resp = test_client.get(
        "/api/v1/admin/leiden/status",
        headers=auth_headers["admin_t1"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == "chang_an"
    assert "snapshot_exists" in body
    assert "enabled" in body


def test_status_forbidden_inspector(test_client, auth_headers):
    resp = test_client.get(
        "/api/v1/admin/leiden/status",
        headers=auth_headers["inspector_t1"],
    )
    assert resp.status_code == 403, resp.text


def test_jobs_forbidden_inspector(test_client, auth_headers):
    resp = test_client.get(
        "/api/v1/admin/leiden/jobs",
        headers=auth_headers["inspector_t1"],
    )
    assert resp.status_code == 403, resp.text
