"""M9 R2 E2E — Leiden admin API end-to-end (T15)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.api.conftest import _run_async, seed_recording

pytestmark = pytest.mark.integration


def test_e2e_leiden_admin_full_flow(test_client, auth_headers, db_session_factory):
    rec_id = _run_async(seed_recording(db_session_factory, tenant_id="chang_an"))
    # Seed a graph with 3 connected nodes.
    import networkx as nx

    from audio_graphy.core.types import _list_to_str

    graph_stores: dict[str, Any] = test_client.app.state.graph_stores
    settings = test_client.app.state.settings
    from audio_graphy.storage.graph_networkx import NetworkXGraphStore

    store = graph_stores.get("chang_an")
    if store is None:
        store = NetworkXGraphStore(settings.working_dir, tenant_id="chang_an")
        graph_stores["chang_an"] = store

    g: nx.MultiDiGraph = store._graph
    g.clear()
    for nid in ("节点A", "节点B", "节点C"):
        g.add_node(
            nid,
            name=nid,
            type="实体",
            description="E2E",
            degree=2,
            source_ids=_list_to_str([f"{rec_id}_1"]),
            recording_ids=_list_to_list([str(rec_id)]),
        )
    g.add_edge("节点A", "节点B", key="r", relation="r", weight=1.0, confidence="EXTRACTED", confidence_score=0.9, source_ids=_list_to_str([f"{rec_id}_1"]))
    g.add_edge("节点B", "节点C", key="r", relation="r", weight=1.0, confidence="EXTRACTED", confidence_score=0.9, source_ids=_list_to_str([f"{rec_id}_1"]))
    store._loaded = True

    # 1. Recompute.
    r1 = test_client.post(
        "/api/v1/admin/leiden/recompute",
        headers=auth_headers["admin_t1"],
        json={"force_full": True, "triggered_by": "e2e"},
    )
    assert r1.status_code == 201, r1.text
    job_id = r1.json()["id"]

    # 2. GET by id.
    r2 = test_client.get(
        f"/api/v1/admin/leiden/jobs/{job_id}",
        headers=auth_headers["admin_t1"],
    )
    assert r2.status_code == 200
    assert r2.json()["id"] == job_id

    # 3. List.
    r3 = test_client.get(
        "/api/v1/admin/leiden/jobs",
        headers=auth_headers["admin_t1"],
    )
    assert r3.status_code == 200
    assert r3.json()["total"] >= 1

    # 4. Status.
    r4 = test_client.get(
        "/api/v1/admin/leiden/status",
        headers=auth_headers["admin_t1"],
    )
    assert r4.status_code == 200
    assert r4.json()["last_job"] is not None


def _list_to_list(items: list[str]) -> str:
    from audio_graphy.core.types import _list_to_str

    return _list_to_str(items)
