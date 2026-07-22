"""M9 R2 E2E — compression admin dry-run + run + history (T15)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.api.conftest import _run_async, seed_recording

pytestmark = pytest.mark.integration


def test_e2e_compression_admin_flow(test_client, auth_headers, db_session_factory):
    """Walk dry-run → run → history in sequence."""
    rec_id = _run_async(seed_recording(db_session_factory, tenant_id="chang_an"))

    # Seed a god-node graph.
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
    g.add_node(
        "GodNode",
        name="GodNode",
        type="客户",
        description="hub",
        degree=8,
        source_ids=_list_to_str([f"{rec_id}_1"]),
        recording_ids=_list_to_str([str(rec_id)]),
    )
    for i in range(8):
        nbr = f"Leaf_{i}"
        g.add_node(
            nbr,
            name=nbr,
            type="车型",
            description="leaf",
            degree=1,
            source_ids=_list_to_str([f"{rec_id}_1"]),
            recording_ids=_list_to_str([str(rec_id)]),
        )
        g.add_edge(
            "GodNode",
            nbr,
            key="r",
            relation="r",
            weight=1.0,
            confidence="EXTRACTED",
            confidence_score=0.9,
            source_ids=_list_to_str([f"{rec_id}_1"]),
        )
    store._loaded = True

    # 1. Dry-run.
    r1 = test_client.post(
        "/api/v1/admin/compression/dry-run",
        headers=auth_headers["admin_t1"],
        json={"max_candidates": 5, "god_node_degree_threshold": 5},
    )
    assert r1.status_code == 200, r1.text
    candidates = [c["entity_id"] for c in r1.json()["candidates"]]
    assert "GodNode" in candidates

    # 2. Run.
    r2 = test_client.post(
        "/api/v1/admin/compression/run",
        headers=auth_headers["admin_t1"],
        json={"max_candidates": 1, "policy_check": True},
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert "GodNode" in body2["soft_deleted_nodes"]
    assert body2["rolled_back"] is False

    # 3. History.
    r3 = test_client.get(
        "/api/v1/admin/compression/history",
        headers=auth_headers["admin_t1"],
    )
    assert r3.status_code == 200, r3.text
    assert r3.json()["total"] >= 1
