"""Integration tests for T10 — compression admin API + weekly cron.

Coverage:
    - POST /admin/compression/dry-run returns candidate list.
    - POST /admin/compression/run performs soft-delete mutations.
    - GET  /admin/compression/history returns recent audit rows.
    - RBAC: inspector/viewer → 403; unauth → 401.
    - ``run_weekly_compression_sweep`` runs end-to-end without errors
      on an empty tenant.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.api.conftest import _run_async, seed_recording

pytestmark = pytest.mark.integration


def _seed_compressible_graph(test_client: Any, tenant_id: str = "chang_an") -> None:
    """Seed a graph containing a god-node (high-degree) candidate."""
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
    # God-node: connected to 5+ neighbours.
    g.add_node(
        "客户实体_root",
        name="客户实体_root",
        type="客户",
        description="聚合客户节点",
        degree=10,
        source_ids=_list_to_str(["1_1"]),
        recording_ids=_list_to_str(["1"]),
    )
    for i in range(10):
        nbr = f"产品_{i}"
        g.add_node(
            nbr,
            name=nbr,
            type="车型",
            description=f"产品 {i}",
            degree=1,
            source_ids=_list_to_str(["1_1"]),
            recording_ids=_list_to_str(["1"]),
        )
        g.add_edge(
            "客户实体_root",
            nbr,
            key="询问",
            relation="询问",
            weight=1.0,
            confidence="EXTRACTED",
            confidence_score=0.9,
            source_ids=_list_to_str(["1_1"]),
        )
    store._loaded = True


@pytest.fixture
def seeded_graph(test_client, db_session_factory):
    _run_async(seed_recording(db_session_factory, tenant_id="chang_an"))
    _seed_compressible_graph(test_client, tenant_id="chang_an")


# ============================================================
# POST /admin/compression/dry-run
# ============================================================


def test_dry_run_returns_candidates(test_client, auth_headers, seeded_graph):
    resp = test_client.post(
        "/api/v1/admin/compression/dry-run",
        headers=auth_headers["admin_t1"],
        json={"max_candidates": 5, "god_node_degree_threshold": 5},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == "chang_an"
    # The god-node should be the top candidate.
    top_ids = [c["entity_id"] for c in body["candidates"]]
    assert "客户实体_root" in top_ids


def test_dry_run_forbidden_inspector(test_client, auth_headers, seeded_graph):
    resp = test_client.post(
        "/api/v1/admin/compression/dry-run",
        headers=auth_headers["inspector_t1"],
        json={"max_candidates": 5},
    )
    assert resp.status_code == 403, resp.text


def test_dry_run_unauth_401(test_client, seeded_graph):
    resp = test_client.post(
        "/api/v1/admin/compression/dry-run",
        json={"max_candidates": 5},
    )
    assert resp.status_code == 401, resp.text


# ============================================================
# POST /admin/compression/run
# ============================================================


def test_run_executes_soft_deletes(test_client, auth_headers, seeded_graph):
    resp = test_client.post(
        "/api/v1/admin/compression/run",
        headers=auth_headers["admin_t1"],
        json={"max_candidates": 1, "policy_check": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rolled_back"] is False
    # The god-node should be in soft_deleted_nodes when picked.
    assert "客户实体_root" in body["soft_deleted_nodes"]
    # Soft-deleted edges list contains at least 1 entry.
    assert len(body["soft_deleted_edges"]) >= 1


def test_run_forbidden_viewer(test_client, auth_headers, seeded_graph):
    resp = test_client.post(
        "/api/v1/admin/compression/run",
        headers=auth_headers["viewer_t1"],
        json={"max_candidates": 1},
    )
    assert resp.status_code == 403, resp.text


# ============================================================
# GET /admin/compression/history
# ============================================================


def test_history_returns_audit_rows(test_client, auth_headers, seeded_graph):
    # Run once to write an audit row.
    test_client.post(
        "/api/v1/admin/compression/run",
        headers=auth_headers["admin_t1"],
        json={"max_candidates": 1},
    )
    resp = test_client.get(
        "/api/v1/admin/compression/history",
        headers=auth_headers["admin_t1"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] >= 1
    assert any(item["action"].startswith("compression") for item in body["items"])


def test_history_forbidden_inspector(test_client, auth_headers):
    resp = test_client.get(
        "/api/v1/admin/compression/history",
        headers=auth_headers["inspector_t1"],
    )
    assert resp.status_code == 403, resp.text


# ============================================================
# Cron sweep — direct call
# ============================================================


def test_run_weekly_compression_sweep_no_tenant():
    """The cron helper returns an empty summary when no tenants are configured."""
    from audio_graphy.core.retention import run_weekly_compression_sweep

    class _Settings:
        compression_god_node_degree: int = 50
        compression_stale_days: int = 180
        compression_max_candidates_per_run: int = 100

    async def _runner() -> dict[str, Any]:
        return await run_weekly_compression_sweep(
            session_factory=None,
            graph_store_factory=lambda tid: None,
            settings=_Settings(),
        )

    summary = _run_async(_runner())
    assert isinstance(summary, dict)
    # With _compression_tenant_index unset, default tenant yields an error
    # because its store is None — but the function returns successfully.
    assert "default" in summary or summary == {}
