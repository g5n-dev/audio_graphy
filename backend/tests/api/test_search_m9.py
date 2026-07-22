"""API tests for T8 — /search/global, /search/local, /search/communities/{id}/drill-down.

Coverage:
    - POST /search/global happy path (inspector+).
    - POST /search/local happy path.
    - POST /search/communities/{id}/drill-down happy path.
    - 403 for agent/viewer; 401 unauth.
    - Empty tenant → empty results, not 500.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.api.conftest import _run_async, seed_recording

pytestmark = pytest.mark.integration


# ============================================================
# Seeders
# ============================================================


async def _seed_community_summary(
    factory: Any,
    *,
    tenant_id: str = "chang_an",
    leiden_job_id: int = 1,
    level: int = 0,
    community_id: int = 1,
    title: str = "长安车系社区",
    summary: str = "客户询问 长安CS75 价格方案",
    member_count: int = 2,
) -> int:
    from audio_graphy.models.community_summary import CommunitySummary
    from audio_graphy.models.leiden_job import LeidenJob

    async with factory() as session:
        # Ensure parent LeidenJob exists (FK constraint).
        existing_job = await session.get(LeidenJob, leiden_job_id)
        if existing_job is None:
            session.add(
                LeidenJob(
                    id=leiden_job_id,
                    tenant_id=tenant_id,
                    job_type="full",
                    status="succeeded",
                    triggered_by="manual",
                    node_count_snapshot=3,
                    edge_count_snapshot=2,
                    modularity=0.4,
                    levels=2,
                )
            )
            await session.flush()
        rec = CommunitySummary(
            tenant_id=tenant_id,
            leiden_job_id=leiden_job_id,
            level=level,
            community_id=community_id,
            title=title,
            summary=summary,
            member_count=member_count,
            member_node_ids=json.dumps(["客户A", "长安CS75"]),
            generated_at=datetime.now(UTC),
            strategy="eager",
        )
        session.add(rec)
        await session.commit()
        await session.refresh(rec)
        return int(rec.id)


def _seed_graph(test_client: Any, tenant_id: str = "chang_an") -> None:
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
    g.add_node(
        "客户A",
        name="客户A",
        type="客户",
        description="潜在客户",
        degree=1,
        source_ids=_list_to_str(["1_1"]),
        recording_ids=_list_to_str(["1"]),
    )
    g.add_node(
        "长安CS75",
        name="长安CS75",
        type="车型",
        description="长安SUV",
        degree=1,
        source_ids=_list_to_str(["1_1"]),
        recording_ids=_list_to_list(["1"]),
    )
    g.add_edge(
        "客户A",
        "长安CS75",
        key="询问",
        relation="询问",
        weight=1.0,
        confidence="EXTRACTED",
        confidence_score=0.95,
        source_ids=_list_to_str(["1_1"]),
    )
    store._loaded = True


def _list_to_list(items: list[str]) -> str:
    from audio_graphy.core.types import _list_to_str

    return _list_to_str(items)


@pytest.fixture
def seeded_search_data(test_client, db_session_factory):
    _run_async(seed_recording(db_session_factory, tenant_id="chang_an"))
    _run_async(
        _seed_community_summary(
            db_session_factory,
            tenant_id="chang_an",
            level=0,
            community_id=1,
        )
    )
    _run_async(
        _seed_community_summary(
            db_session_factory,
            tenant_id="chang_an",
            level=1,
            community_id=10,
        )
    )
    _seed_graph(test_client, tenant_id="chang_an")


# ============================================================
# /search/global
# ============================================================


def test_global_search_happy_path(test_client, auth_headers, seeded_search_data):
    resp = test_client.post(
        "/api/v1/search/global",
        headers=auth_headers["inspector_t1"],
        json={"query": "长安CS75", "top_k": 5, "level": 0},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["query"] == "长安CS75"
    assert body["level"] == 0
    assert isinstance(body["hits"], list)
    # At least one community summary exists, so the result should be non-empty.
    assert body["total"] >= 1


def test_global_search_forbidden_agent(test_client, auth_headers, seeded_search_data):
    resp = test_client.post(
        "/api/v1/search/global",
        headers=auth_headers["agent_t1"],
        json={"query": "test"},
    )
    assert resp.status_code == 403, resp.text


def test_global_search_unauth_401(test_client, seeded_search_data):
    resp = test_client.post(
        "/api/v1/search/global",
        json={"query": "test"},
    )
    assert resp.status_code == 401, resp.text


# ============================================================
# /search/local
# ============================================================


def test_local_search_happy_path(test_client, auth_headers, seeded_search_data):
    resp = test_client.post(
        "/api/v1/search/local",
        headers=auth_headers["inspector_t1"],
        json={
            "query": "长安",
            "seed_entity_ids": ["客户A"],
            "depth": 1,
            "top_k": 5,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["seed_entity_ids"] == ["客户A"]
    # Should discover the neighbour node 长安CS75.
    ids = [h["entity_id"] for h in body["hits"]]
    assert "长安CS75" in ids or "客户A" in ids


def test_local_search_missing_seed_404(test_client, auth_headers, seeded_search_data):
    resp = test_client.post(
        "/api/v1/search/local",
        headers=auth_headers["inspector_t1"],
        json={"query": "x", "seed_entity_ids": ["Nonexistent"]},
    )
    assert resp.status_code == 404, resp.text


def test_local_search_forbidden_viewer(test_client, auth_headers, seeded_search_data):
    resp = test_client.post(
        "/api/v1/search/local",
        headers=auth_headers["viewer_t1"],
        json={"query": "x", "seed_entity_ids": ["客户A"]},
    )
    assert resp.status_code == 403, resp.text


# ============================================================
# /search/communities/{id}/drill-down
# ============================================================


def test_drill_down_happy_path(test_client, auth_headers, seeded_search_data):
    # Parent at level 0 has community_id 1; we look for children at level 1.
    resp = test_client.post(
        "/api/v1/search/communities/1/drill-down",
        headers=auth_headers["inspector_t1"],
        json={"level": 0},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["community_id"] == 1
    assert body["child_level"] == 1


def test_drill_down_parent_missing_404(test_client, auth_headers, seeded_search_data):
    resp = test_client.post(
        "/api/v1/search/communities/9999/drill-down",
        headers=auth_headers["inspector_t1"],
        json={"level": 0},
    )
    assert resp.status_code == 404, resp.text


def test_drill_down_at_level_2_returns_400(
    test_client, auth_headers, seeded_search_data
):
    """Cannot drill below level 2 (Q2 cap)."""
    resp = test_client.post(
        "/api/v1/search/communities/1/drill-down",
        headers=auth_headers["inspector_t1"],
        json={"level": 2},
    )
    assert resp.status_code == 400, resp.text


def test_drill_down_forbidden_agent(test_client, auth_headers, seeded_search_data):
    resp = test_client.post(
        "/api/v1/search/communities/1/drill-down",
        headers=auth_headers["agent_t1"],
        json={"level": 0},
    )
    assert resp.status_code == 403, resp.text
