"""Job-bound topic-cluster projections for the global graph workspace.

Community summaries are immutable projections of a specific successful Leiden
run.  This service resolves the run first and applies its id to every summary
query so rows from different partitions can never be mixed accidentally.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from audio_graphy.errors import ConflictError, TaskNotFoundError
from audio_graphy.models.community_summary import CommunitySummary
from audio_graphy.models.leiden_job import LeidenJob


@dataclass(frozen=True, slots=True)
class TopicClusterJobRecord:
    """Minimal successful Leiden-run metadata exposed to graph clients."""

    id: int
    status: str
    job_type: str
    modularity: float | None
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class TopicClusterRecord:
    """One persisted community summary in a job-bound projection."""

    community_id: int
    level: int
    title: str
    summary: str
    member_count: int
    member_node_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TopicClusterSnapshot:
    """Bounded cluster graph for one Leiden job and hierarchy level."""

    job: TopicClusterJobRecord
    available_jobs: list[TopicClusterJobRecord]
    level: int
    clusters: list[TopicClusterRecord]
    total_clusters: int
    total_members: int
    generated_at: datetime | None


@dataclass(frozen=True, slots=True)
class TopicClusterDetail:
    """Exact cluster lookup plus overlapping clusters from the same job."""

    job: TopicClusterJobRecord
    cluster: TopicClusterRecord
    related_clusters: list[TopicClusterRecord]


def _job_record(job: LeidenJob) -> TopicClusterJobRecord:
    return TopicClusterJobRecord(
        id=int(job.id),
        status=str(job.status),
        job_type=str(job.job_type),
        modularity=float(job.modularity) if job.modularity is not None else None,
        finished_at=job.finished_at,
    )


def _member_ids(raw: str | None) -> list[str]:
    """Parse legacy JSON safely and return stable, unique string ids."""
    try:
        values = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(values, list):
        return []

    members: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        members.append(normalized)
    return members


def _cluster_record(row: CommunitySummary) -> TopicClusterRecord:
    members = _member_ids(row.member_node_ids)
    return TopicClusterRecord(
        community_id=int(row.community_id),
        level=int(row.level),
        title=str(row.title),
        summary=str(row.summary),
        # The parsed member list is authoritative for the graph projection.
        # ``member_count`` remains persisted metadata and may come from an
        # older writer, so never use it to fabricate missing members.
        member_count=len(members),
        member_node_ids=members,
    )


def _matches_query(cluster: TopicClusterRecord, query: str) -> bool:
    """Match a user query against the complete persisted cluster payload."""
    needle = query.casefold()
    return any(
        needle in value.casefold()
        for value in (
            cluster.title,
            cluster.summary,
            *cluster.member_node_ids,
        )
    )


class TopicClusterService:
    """Read-only projection over successful, tenant-scoped Leiden jobs."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _resolve_job(
        self,
        *,
        tenant_id: str,
        job_id: int | None,
    ) -> LeidenJob:
        if job_id is None:
            stmt = (
                select(LeidenJob)
                .where(
                    LeidenJob.tenant_id == tenant_id,
                    LeidenJob.status == "succeeded",
                )
                .order_by(LeidenJob.finished_at.desc(), LeidenJob.id.desc())
                .limit(1)
            )
            job = (await self._session.execute(stmt)).scalar_one_or_none()
            if job is None:
                raise TaskNotFoundError(
                    message="No successful Leiden job is available",
                    detail={"tenant_id": tenant_id, "required_status": "succeeded"},
                    code="LEIDEN_JOB_NOT_FOUND",
                )
            return job

        stmt = select(LeidenJob).where(
            LeidenJob.id == job_id,
            LeidenJob.tenant_id == tenant_id,
        )
        job = (await self._session.execute(stmt)).scalar_one_or_none()
        if job is None:
            raise TaskNotFoundError(
                message="Leiden job not found",
                detail={"tenant_id": tenant_id, "job_id": job_id},
                code="LEIDEN_JOB_NOT_FOUND",
            )
        if str(job.status) != "succeeded":
            raise ConflictError(
                message="Topic clusters require a succeeded Leiden job",
                detail={
                    "tenant_id": tenant_id,
                    "job_id": job_id,
                    "status": str(job.status),
                    "required_status": "succeeded",
                },
                code="LEIDEN_JOB_NOT_SUCCEEDED",
            )
        return job

    async def _available_jobs(
        self,
        *,
        tenant_id: str,
        limit: int = 12,
    ) -> list[TopicClusterJobRecord]:
        stmt = (
            select(LeidenJob)
            .where(
                LeidenJob.tenant_id == tenant_id,
                LeidenJob.status == "succeeded",
            )
            .order_by(LeidenJob.finished_at.desc(), LeidenJob.id.desc())
            .limit(limit)
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        return [_job_record(row) for row in rows]

    async def get_snapshot(
        self,
        *,
        tenant_id: str,
        job_id: int | None,
        level: int,
        query: str | None = None,
    ) -> TopicClusterSnapshot:
        """Return clusters from exactly one successful Leiden run."""
        job = await self._resolve_job(tenant_id=tenant_id, job_id=job_id)
        stmt = (
            select(CommunitySummary)
            .where(
                CommunitySummary.tenant_id == tenant_id,
                CommunitySummary.leiden_job_id == int(job.id),
                CommunitySummary.level == level,
            )
            .order_by(
                CommunitySummary.member_count.desc(),
                CommunitySummary.community_id.asc(),
            )
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        clusters = [_cluster_record(row) for row in rows]

        # A successful non-empty partition with no persisted rows is not an
        # empty graph: level 1 is generated lazily, while level 0 and the leaf
        # level are eager.  Reconstructing it from the current graph could mix
        # a newer graph state with this exact Leiden snapshot, so surface an
        # explicit retryable state until the job-bound summary writer finishes.
        if not rows and int(job.node_count_snapshot) > 0:
            raise ConflictError(
                message="Topic-cluster summaries are not ready for this job level",
                detail={
                    "tenant_id": tenant_id,
                    "job_id": int(job.id),
                    "level": level,
                    "generation_strategy": "lazy" if level == 1 else "eager",
                },
                code="SUMMARY_NOT_READY",
            )

        normalized_query = (query or "").strip()
        if normalized_query:
            clusters = [
                cluster for cluster in clusters if _matches_query(cluster, normalized_query)
            ]

        members = {member for cluster in clusters for member in cluster.member_node_ids}
        generated_at = max(
            (row.generated_at for row in rows),
            default=job.finished_at,
        )
        return TopicClusterSnapshot(
            job=_job_record(job),
            available_jobs=await self._available_jobs(tenant_id=tenant_id),
            level=level,
            clusters=clusters,
            total_clusters=len(clusters),
            total_members=len(members),
            generated_at=generated_at,
        )

    async def get_detail(
        self,
        *,
        tenant_id: str,
        job_id: int,
        level: int,
        community_id: int,
    ) -> TopicClusterDetail:
        """Return an exact cluster; related rows remain in the same job."""
        job = await self._resolve_job(tenant_id=tenant_id, job_id=job_id)
        stmt = select(CommunitySummary).where(
            CommunitySummary.tenant_id == tenant_id,
            CommunitySummary.leiden_job_id == int(job.id),
            CommunitySummary.level == level,
            CommunitySummary.community_id == community_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise TaskNotFoundError(
                message="Topic cluster not found",
                detail={
                    "tenant_id": tenant_id,
                    "job_id": job_id,
                    "level": level,
                    "community_id": community_id,
                },
                code="TOPIC_CLUSTER_NOT_FOUND",
            )

        cluster = _cluster_record(row)
        member_set = set(cluster.member_node_ids)
        related_stmt = select(CommunitySummary).where(
            CommunitySummary.tenant_id == tenant_id,
            CommunitySummary.leiden_job_id == int(job.id),
            CommunitySummary.id != row.id,
        )
        candidates = list((await self._session.execute(related_stmt)).scalars().all())
        related = [
            candidate
            for candidate in (_cluster_record(item) for item in candidates)
            if member_set.intersection(candidate.member_node_ids)
        ]
        related.sort(
            key=lambda item: (
                -len(member_set.intersection(item.member_node_ids)),
                item.level,
                item.community_id,
            )
        )
        return TopicClusterDetail(
            job=_job_record(job),
            cluster=cluster,
            related_clusters=related[:8],
        )


__all__ = [
    "TopicClusterDetail",
    "TopicClusterJobRecord",
    "TopicClusterRecord",
    "TopicClusterService",
    "TopicClusterSnapshot",
]
