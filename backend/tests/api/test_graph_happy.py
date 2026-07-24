"""Graph API happy-path tests.

Covers: api/graph.py explore, entity, subgraph, path.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import seed_graph


@pytest.mark.integration
class TestGraphHappyPath:
    """Happy-path tests for /graph endpoints."""

    def test_explore_returns_data(self, test_client: TestClient, auth_headers: dict) -> None:
        """GET /graph/explore returns nodes and edges (may be empty)."""
        resp = test_client.get("/api/v1/graph/explore", headers=auth_headers["admin_t1"])
        assert resp.status_code == 200
        body = resp.json()
        assert "nodes" in body
        assert "edges" in body
        assert "total_nodes" in body
        assert "total_edges" in body
        assert isinstance(body["nodes"], list)
        assert isinstance(body["edges"], list)

    def test_explore_with_seeded_graph(self, test_client: TestClient, auth_headers: dict) -> None:
        """GET /graph/explore returns seeded nodes and edges."""
        seed_graph(test_client)
        resp = test_client.get("/api/v1/graph/explore", headers=auth_headers["admin_t1"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_nodes"] == 3
        assert body["total_edges"] == 2
        labels = [n["label"] for n in body["nodes"]]
        assert "长安CS75" in labels

    def test_explore_with_node_type_filter(
        self, test_client: TestClient, auth_headers: dict
    ) -> None:
        """GET /graph/explore?node_type=产品 filters by type."""
        seed_graph(test_client)
        resp = test_client.get(
            "/api/v1/graph/explore?node_type=产品",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert all(n["type"] == "产品" for n in body["nodes"])
        assert len(body["nodes"]) == 1

    def test_explore_with_min_degree_filter(
        self, test_client: TestClient, auth_headers: dict
    ) -> None:
        """GET /graph/explore?min_degree=2 filters by degree."""
        seed_graph(test_client)
        resp = test_client.get(
            "/api/v1/graph/explore?min_degree=2",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert all(n["degree"] >= 2 for n in body["nodes"])
        assert len(body["nodes"]) == 2

    def test_explore_with_limit(self, test_client: TestClient, auth_headers: dict) -> None:
        """GET /graph/explore?limit=1 limits node count."""
        seed_graph(test_client)
        resp = test_client.get(
            "/api/v1/graph/explore?limit=1",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["nodes"]) <= 1
        visible_ids = {node["id"] for node in body["nodes"]}
        assert all(
            edge["source"] in visible_ids and edge["target"] in visible_ids
            for edge in body["edges"]
        )

    def test_explore_with_filters(self, test_client: TestClient, auth_headers: dict) -> None:
        """GET /graph/explore supports node_type and min_degree filters (no data)."""
        resp = test_client.get(
            "/api/v1/graph/explore?node_type=产品&min_degree=0&limit=50",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200

    def test_entity_detail_success(self, test_client: TestClient, auth_headers: dict) -> None:
        """GET /graph/entity/{name} returns entity detail with neighbors."""
        seed_graph(test_client)
        resp = test_client.get(
            "/api/v1/graph/entity/长安CS75",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["node"]["label"] == "长安CS75"
        assert body["node"]["type"] == "产品"
        assert len(body["neighbors"]) >= 1
        assert "relation_counts" in body

    def test_entity_not_found(self, test_client: TestClient, auth_headers: dict) -> None:
        """GET /graph/entity/{name} for nonexistent entity returns 404."""
        resp = test_client.get(
            "/api/v1/graph/entity/NonExistentEntity12345",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "ENTITY_NOT_FOUND"

    def test_subgraph_success(self, test_client: TestClient, auth_headers: dict) -> None:
        """GET /graph/subgraph returns N-hop subgraph from entity."""
        seed_graph(test_client)
        resp = test_client.get(
            "/api/v1/graph/subgraph?entity=长安CS75&max_hops=1",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_nodes"] >= 1
        assert body["total_edges"] >= 0

    def test_subgraph_missing_entity(self, test_client: TestClient, auth_headers: dict) -> None:
        """GET /graph/subgraph without entity param returns 422."""
        resp = test_client.get(
            "/api/v1/graph/subgraph",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 422

    def test_subgraph_nonexistent_entity(self, test_client: TestClient, auth_headers: dict) -> None:
        """GET /graph/subgraph for nonexistent entity returns 404."""
        resp = test_client.get(
            "/api/v1/graph/subgraph?entity=GhostEntity&max_hops=1",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 404

    def test_path_success(self, test_client: TestClient, auth_headers: dict) -> None:
        """GET /graph/path finds shortest path between two entities."""
        seed_graph(test_client)
        resp = test_client.get(
            "/api/v1/graph/path?source=客户A&target=销售张三",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["path"][0] == "客户A"
        assert body["path"][-1] == "销售张三"
        assert body["length"] >= 1
        assert len(body["edges"]) >= 1

    def test_path_missing_params(self, test_client: TestClient, auth_headers: dict) -> None:
        """GET /graph/path without source/target returns 422."""
        resp = test_client.get(
            "/api/v1/graph/path",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 422

    def test_path_nonexistent_entities(self, test_client: TestClient, auth_headers: dict) -> None:
        """GET /graph/path for nonexistent entities returns 404."""
        resp = test_client.get(
            "/api/v1/graph/path?source=GhostA&target=GhostB",
            headers=auth_headers["admin_t1"],
        )
        assert resp.status_code == 404
