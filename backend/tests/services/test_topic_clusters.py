"""Topic-cluster projection tests.

The projection must never mix community rows from different Leiden runs. A
requested run is usable only after it has reached ``succeeded``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from audio_graphy.errors import ConflictError, TaskNotFoundError
from audio_graphy.models.community_summary import CommunitySummary
from audio_graphy.models.leiden_job import LeidenJob
from audio_graphy.services.topic_clusters import TopicClusterService


async def _seed_job(
    session_factory,
    *,
    job_id: int,
    tenant_id: str = "chang_an",
    status: str = "succeeded",
    finished_offset_minutes: int = 0,
) -> None:
    async with session_factory() as session:
        session.add(
            LeidenJob(
                id=job_id,
                tenant_id=tenant_id,
                job_type="full",
                status=status,
                triggered_by="manual",
                node_count_snapshot=8,
                edge_count_snapshot=7,
                modularity=0.72,
                levels=2,
                finished_at=datetime.now(UTC) + timedelta(minutes=finished_offset_minutes),
            )
        )
        await session.commit()


async def _seed_summary(
    session_factory,
    *,
    row_id: int,
    job_id: int,
    title: str,
    members: list[str],
    tenant_id: str = "chang_an",
    level: int = 0,
    community_id: int = 1,
) -> None:
    async with session_factory() as session:
        session.add(
            CommunitySummary(
                id=row_id,
                tenant_id=tenant_id,
                leiden_job_id=job_id,
                level=level,
                community_id=community_id,
                title=title,
                summary=f"{title}的业务主题摘要",
                member_count=len(members),
                member_node_ids=json.dumps(members, ensure_ascii=False),
                generated_at=datetime.now(UTC),
                strategy="eager",
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_projection_is_bound_to_the_requested_successful_job(
    session_factory,
) -> None:
    await _seed_job(session_factory, job_id=701)
    await _seed_job(session_factory, job_id=702, finished_offset_minutes=1)
    await _seed_summary(
        session_factory,
        row_id=801,
        job_id=701,
        title="预算与价格",
        members=["预算敏感", "价格异议"],
    )
    await _seed_summary(
        session_factory,
        row_id=802,
        job_id=702,
        title="服务与保障",
        members=["售后保障", "保养政策"],
    )

    async with session_factory() as session:
        result = await TopicClusterService(session).get_snapshot(
            tenant_id="chang_an",
            job_id=701,
            level=0,
        )

    assert result.job.id == 701
    assert result.job.status == "succeeded"
    assert [cluster.title for cluster in result.clusters] == ["预算与价格"]
    assert result.total_members == 2


@pytest.mark.asyncio
async def test_projection_defaults_to_latest_successful_job(
    session_factory,
) -> None:
    await _seed_job(session_factory, job_id=711)
    await _seed_job(session_factory, job_id=712, status="failed", finished_offset_minutes=2)
    await _seed_job(session_factory, job_id=713, finished_offset_minutes=1)
    await _seed_summary(
        session_factory,
        row_id=811,
        job_id=711,
        title="较早任务",
        members=["A"],
    )
    await _seed_summary(
        session_factory,
        row_id=813,
        job_id=713,
        title="最新成功任务",
        members=["B", "C"],
    )

    async with session_factory() as session:
        result = await TopicClusterService(session).get_snapshot(
            tenant_id="chang_an",
            job_id=None,
            level=0,
        )

    assert result.job.id == 713
    assert [cluster.title for cluster in result.clusters] == ["最新成功任务"]
    assert [job.id for job in result.available_jobs] == [713, 711]


@pytest.mark.asyncio
async def test_projection_filters_the_complete_job_snapshot_server_side(
    session_factory,
) -> None:
    await _seed_job(session_factory, job_id=714)
    await _seed_summary(
        session_factory,
        row_id=814,
        job_id=714,
        title="价格协商",
        members=["预算审批", "优惠空间"],
        community_id=1,
    )
    await _seed_summary(
        session_factory,
        row_id=815,
        job_id=714,
        title="服务保障",
        members=["保养政策", "售后承诺"],
        community_id=2,
    )

    async with session_factory() as session:
        by_title = await TopicClusterService(session).get_snapshot(
            tenant_id="chang_an",
            job_id=714,
            level=0,
            query="价格",
        )
        by_member = await TopicClusterService(session).get_snapshot(
            tenant_id="chang_an",
            job_id=714,
            level=0,
            query="售后承诺",
        )
        no_match = await TopicClusterService(session).get_snapshot(
            tenant_id="chang_an",
            job_id=714,
            level=0,
            query="不存在的标签",
        )

    assert [cluster.title for cluster in by_title.clusters] == ["价格协商"]
    assert [cluster.title for cluster in by_member.clusters] == ["服务保障"]
    assert no_match.clusters == []
    assert no_match.total_clusters == 0
    assert no_match.total_members == 0


@pytest.mark.asyncio
async def test_projection_reports_summary_not_ready_instead_of_empty_graph(
    session_factory,
) -> None:
    await _seed_job(session_factory, job_id=715)

    async with session_factory() as session:
        with pytest.raises(ConflictError) as exc_info:
            await TopicClusterService(session).get_snapshot(
                tenant_id="chang_an",
                job_id=715,
                level=1,
            )

    assert exc_info.value.code == "SUMMARY_NOT_READY"
    assert exc_info.value.detail == {
        "tenant_id": "chang_an",
        "job_id": 715,
        "level": 1,
        "generation_strategy": "lazy",
    }


@pytest.mark.asyncio
async def test_projection_allows_a_truly_empty_successful_partition(
    session_factory,
) -> None:
    await _seed_job(session_factory, job_id=716)
    async with session_factory() as session:
        job = await session.get(LeidenJob, 716)
        assert job is not None
        job.node_count_snapshot = 0
        job.edge_count_snapshot = 0
        await session.commit()

    async with session_factory() as session:
        result = await TopicClusterService(session).get_snapshot(
            tenant_id="chang_an",
            job_id=716,
            level=0,
        )

    assert result.clusters == []
    assert result.total_clusters == 0
    assert result.total_members == 0


@pytest.mark.asyncio
async def test_projection_rejects_non_successful_and_cross_tenant_jobs(
    session_factory,
) -> None:
    await _seed_job(session_factory, job_id=721, status="running")
    await _seed_job(session_factory, job_id=722, tenant_id="byd")

    async with session_factory() as session:
        service = TopicClusterService(session)
        with pytest.raises(ConflictError):
            await service.get_snapshot(
                tenant_id="chang_an",
                job_id=721,
                level=0,
            )
        with pytest.raises(TaskNotFoundError):
            await service.get_snapshot(
                tenant_id="chang_an",
                job_id=722,
                level=0,
            )


@pytest.mark.asyncio
async def test_projection_tolerates_malformed_members_without_leaking_rows(
    session_factory,
) -> None:
    await _seed_job(session_factory, job_id=731)
    async with session_factory() as session:
        session.add(
            CommunitySummary(
                id=831,
                tenant_id="chang_an",
                leiden_job_id=731,
                level=0,
                community_id=1,
                title="异常旧摘要",
                summary="成员字段来自旧版本",
                member_count=9,
                member_node_ids="{not-json",
                generated_at=datetime.now(UTC),
                strategy="eager",
            )
        )
        await session.commit()

    async with session_factory() as session:
        result = await TopicClusterService(session).get_snapshot(
            tenant_id="chang_an",
            job_id=731,
            level=0,
        )

    assert result.clusters[0].member_node_ids == []
    assert result.total_members == 0


@pytest.mark.asyncio
async def test_detail_is_bound_to_exact_job_level_and_community(
    session_factory,
) -> None:
    await _seed_job(session_factory, job_id=741)
    await _seed_summary(
        session_factory,
        row_id=841,
        job_id=741,
        title="服务体验",
        members=["导购专业度", "售后保障"],
        level=1,
        community_id=8,
    )

    async with session_factory() as session:
        service = TopicClusterService(session)
        detail = await service.get_detail(
            tenant_id="chang_an",
            job_id=741,
            level=1,
            community_id=8,
        )
        with pytest.raises(TaskNotFoundError):
            await service.get_detail(
                tenant_id="chang_an",
                job_id=741,
                level=0,
                community_id=8,
            )

    assert detail.job.id == 741
    assert detail.cluster.title == "服务体验"
