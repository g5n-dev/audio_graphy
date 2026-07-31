"""API contract tests for the job-bound topic-cluster graph."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from tests.api.conftest import _run_async

pytestmark = pytest.mark.integration


async def _seed_topic_jobs(factory) -> None:
    from audio_graphy.models.community_summary import CommunitySummary
    from audio_graphy.models.leiden_job import LeidenJob

    async with factory() as session:
        session.add_all(
            [
                LeidenJob(
                    id=901,
                    tenant_id="chang_an",
                    job_type="full",
                    status="succeeded",
                    triggered_by="manual",
                    node_count_snapshot=4,
                    edge_count_snapshot=3,
                    modularity=0.68,
                    levels=2,
                    finished_at=datetime.now(UTC),
                ),
                LeidenJob(
                    id=902,
                    tenant_id="chang_an",
                    job_type="incremental",
                    status="running",
                    triggered_by="scheduled",
                    node_count_snapshot=5,
                    edge_count_snapshot=4,
                    levels=2,
                ),
                LeidenJob(
                    id=904,
                    tenant_id="chang_an",
                    job_type="full",
                    status="succeeded",
                    triggered_by="manual",
                    node_count_snapshot=5,
                    edge_count_snapshot=4,
                    levels=2,
                    finished_at=datetime.now(UTC),
                ),
            ]
        )
        session.add(
            CommunitySummary(
                id=903,
                tenant_id="chang_an",
                leiden_job_id=901,
                level=0,
                community_id=4,
                title="成交阻力",
                summary="预算、审批与比价形成的主题社区",
                member_count=3,
                member_node_ids=json.dumps(
                    ["价格超预算", "预算审批", "需要再比较"],
                    ensure_ascii=False,
                ),
                generated_at=datetime.now(UTC),
                strategy="eager",
            )
        )
        await session.commit()


@pytest.fixture
def topic_jobs(db_session_factory):
    _run_async(_seed_topic_jobs(db_session_factory))


def test_topic_clusters_returns_job_bound_projection(
    test_client,
    auth_headers,
    topic_jobs,
) -> None:
    response = test_client.get(
        "/api/v1/graph/topic-clusters",
        headers=auth_headers["inspector_t1"],
        params={"job_id": 901, "level": 0},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["job"]["id"] == 901
    assert payload["job"]["status"] == "succeeded"
    assert payload["clusters"][0]["title"] == "成交阻力"
    assert payload["clusters"][0]["member_node_ids"] == [
        "价格超预算",
        "预算审批",
        "需要再比较",
    ]
    assert payload["total_clusters"] == 1
    assert payload["total_members"] == 3


def test_topic_clusters_rejects_running_job(
    test_client,
    auth_headers,
    topic_jobs,
) -> None:
    response = test_client.get(
        "/api/v1/graph/topic-clusters",
        headers=auth_headers["inspector_t1"],
        params={"job_id": 902},
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "LEIDEN_JOB_NOT_SUCCEEDED"


def test_topic_clusters_reports_missing_job_bound_summaries_as_not_ready(
    test_client,
    auth_headers,
    topic_jobs,
) -> None:
    response = test_client.get(
        "/api/v1/graph/topic-clusters",
        headers=auth_headers["agent_t1"],
        params={"job_id": 904, "level": 1},
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SUMMARY_NOT_READY"
    assert response.json()["error"]["detail"]["generation_strategy"] == "lazy"


def test_topic_clusters_is_tenant_scoped_and_role_guarded(
    test_client,
    auth_headers,
    topic_jobs,
) -> None:
    cross_tenant = test_client.get(
        "/api/v1/graph/topic-clusters",
        headers=auth_headers["inspector_t2"],
        params={"job_id": 901},
    )
    agent_response = test_client.get(
        "/api/v1/graph/topic-clusters",
        headers=auth_headers["agent_t1"],
        params={"job_id": 901},
    )

    assert cross_tenant.status_code == 404
    assert agent_response.status_code == 200
    assert agent_response.json()["job"]["id"] == 901


def test_topic_clusters_searches_the_job_snapshot_and_limits_query_length(
    test_client,
    auth_headers,
    topic_jobs,
) -> None:
    matching = test_client.get(
        "/api/v1/graph/topic-clusters",
        headers=auth_headers["agent_t1"],
        params={"job_id": 901, "query": "预算审批"},
    )
    no_match = test_client.get(
        "/api/v1/graph/topic-clusters",
        headers=auth_headers["agent_t1"],
        params={"job_id": 901, "query": "售后承诺"},
    )
    too_long = test_client.get(
        "/api/v1/graph/topic-clusters",
        headers=auth_headers["agent_t1"],
        params={"job_id": 901, "query": "x" * 121},
    )

    assert matching.status_code == 200, matching.text
    assert matching.json()["total_clusters"] == 1
    assert no_match.status_code == 200, no_match.text
    assert no_match.json()["clusters"] == []
    assert too_long.status_code == 422


def test_topic_cluster_detail_keeps_job_level_and_tenant_binding(
    test_client,
    auth_headers,
    topic_jobs,
) -> None:
    response = test_client.get(
        "/api/v1/graph/topic-clusters/901/0/4",
        headers=auth_headers["inspector_t1"],
    )
    missing = test_client.get(
        "/api/v1/graph/topic-clusters/901/1/4",
        headers=auth_headers["inspector_t1"],
    )

    assert response.status_code == 200, response.text
    assert response.json()["job"]["id"] == 901
    assert response.json()["cluster"]["community_id"] == 4
    assert response.json()["cluster"]["level"] == 0
    assert missing.status_code == 404
