"""M9 R2 E2E — bi-temporal API end-to-end (T15).

Walks the full flow:
    1. Seed a recording.
    2. Seed a bi-temporal edge into the tenant's graph.
    3. Insert an EdgeEvent row.
    4. GET /recordings/{id}/edges?at=<now>  →  edge present.
    5. GET /recordings/{id}/edges/{edge_key}/history  →  event present.
    6. GET /recordings/{id}/edges/range?from=...&to=...  →  edge present.
    7. DELETE flag scenario not applicable (L9 router-gated).

The test exercises the full FastAPI stack including auth + tenant scoping.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.api.conftest import _run_async, seed_recording

pytestmark = pytest.mark.integration


def test_e2e_bitemporal_full_flow(test_client, auth_headers, db_session_factory):
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
    past = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    g.add_node(
        "客户E2E",
        name="客户E2E",
        type="客户",
        description="E2E",
        degree=1,
        source_ids=_list_to_str([f"{rec_id}_1"]),
        recording_ids=_list_to_str([str(rec_id)]),
    )
    g.add_node(
        "长安E2",
        name="长安E2",
        type="车型",
        description="E2E",
        degree=1,
        source_ids=_list_to_str([f"{rec_id}_1"]),
        recording_ids=_list_to_list([str(rec_id)]),
    )
    g.add_edge(
        "客户E2E",
        "长安E2",
        key="咨询",
        relation="咨询",
        weight=1.0,
        confidence="EXTRACTED",
        confidence_score=0.95,
        source_ids=_list_to_str([f"{rec_id}_1"]),
        valid_at=past,
        created_at=past,
    )
    store._loaded = True

    # Seed EdgeEvent.
    from audio_graphy.models.edge_event import EdgeEvent

    async def _seed_event() -> None:
        async with db_session_factory() as session:
            session.add(
                EdgeEvent(
                    tenant_id="chang_an",
                    event_type="insert",
                    edge_key="客户E2E|咨询|长安E2",
                    source="客户E2E",
                    target="长安E2",
                    relation="咨询",
                    valid_at=datetime.now(UTC) - timedelta(days=2),
                    actor="e2e",
                    payload="{}",
                )
            )
            await session.commit()

    _run_async(_seed_event())

    # 4. Time-travel query.
    r1 = test_client.get(
        f"/api/v1/recordings/{rec_id}/edges",
        headers=auth_headers["inspector_t1"],
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["total"] >= 1

    # 5. History.
    r2 = test_client.get(
        f"/api/v1/recordings/{rec_id}/edges/客户E2E|咨询|长安E2/history",
        headers=auth_headers["inspector_t1"],
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["total"] >= 1

    # 6. Range query.
    now = datetime.now(UTC)
    r3 = test_client.get(
        f"/api/v1/recordings/{rec_id}/edges/range",
        headers=auth_headers["inspector_t1"],
        params={
            "from": (now - timedelta(days=1)).isoformat(),
            "to": (now + timedelta(days=1)).isoformat(),
        },
    )
    assert r3.status_code == 200, r3.text
    assert r3.json()["total"] == 1


def _list_to_list(items: list[str]) -> str:
    from audio_graphy.core.types import _list_to_str

    return _list_to_str(items)
