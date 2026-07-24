"""M9 R2 E2E — community summary seeding + global search (T15)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from tests.api.conftest import _run_async, seed_recording

pytestmark = pytest.mark.integration


def test_e2e_community_summary_then_global_search(test_client, auth_headers, db_session_factory):
    _run_async(seed_recording(db_session_factory, tenant_id="chang_an"))

    # Seed a LeidenJob + CommunitySummary directly via the ORM.
    from audio_graphy.models.community_summary import CommunitySummary
    from audio_graphy.models.leiden_job import LeidenJob

    async def _seed() -> int:
        async with db_session_factory() as session:
            job = LeidenJob(
                tenant_id="chang_an",
                job_type="full",
                status="succeeded",
                triggered_by="e2e",
                node_count_snapshot=3,
                edge_count_snapshot=2,
                modularity=0.45,
                levels=2,
            )
            session.add(job)
            await session.flush()
            job_id = int(job.id)
            session.add(
                CommunitySummary(
                    tenant_id="chang_an",
                    leiden_job_id=job_id,
                    level=0,
                    community_id=1,
                    title="长安社区",
                    summary="客户询问 长安CS75 价格方案",
                    member_count=3,
                    member_node_ids=json.dumps(["客户A", "长安CS75", "销售张三"]),
                    generated_at=datetime.now(UTC),
                    strategy="eager",
                )
            )
            await session.commit()
            return job_id

    _run_async(_seed())

    # Global search should find the seeded summary.
    resp = test_client.post(
        "/api/v1/search/global",
        headers=auth_headers["inspector_t1"],
        json={"query": "长安CS75", "top_k": 5, "level": 0},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] >= 1
    top = body["hits"][0]
    assert "长安" in top["title"]
