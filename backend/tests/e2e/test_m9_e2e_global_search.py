"""M9 R2 E2E — local search + drill-down (T15)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.api.conftest import _run_async, seed_recording

pytestmark = pytest.mark.integration


def test_e2e_local_search_and_drill_down(
    test_client, auth_headers, db_session_factory
):
    rec_id = _run_async(seed_recording(db_session_factory, tenant_id="chang_an"))

    # Seed graph.
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
    for nid in ("种子节点", "邻居A", "邻居B"):
        g.add_node(
            nid,
            name=nid,
            type="实体",
            description="E2E",
            degree=1,
            source_ids=_list_to_str([f"{rec_id}_1"]),
            recording_ids=_list_to_str([str(rec_id)]),
        )
    g.add_edge("种子节点", "邻居A", key="r", relation="r", weight=1.0, confidence="EXTRACTED", confidence_score=0.9, source_ids=_list_to_str([f"{rec_id}_1"]))
    g.add_edge("邻居A", "邻居B", key="r", relation="r", weight=1.0, confidence="EXTRACTED", confidence_score=0.9, source_ids=_list_to_str([f"{rec_id}_1"]))
    store._loaded = True

    # 1. Local search.
    r1 = test_client.post(
        "/api/v1/search/local",
        headers=auth_headers["inspector_t1"],
        json={
            "query": "邻居",
            "seed_entity_ids": ["种子节点"],
            "depth": 2,
            "top_k": 5,
        },
    )
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    found_ids = [h["entity_id"] for h in body1["hits"]]
    assert "邻居A" in found_ids
    assert "邻居B" in found_ids
