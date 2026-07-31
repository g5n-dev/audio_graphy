"""Schemas for the job-bound topic-cluster graph."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TopicClusterJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    job_type: str
    modularity: float | None = None
    finished_at: datetime | None = None


class TopicClusterResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    community_id: int
    level: int
    title: str
    summary: str
    member_count: int
    member_node_ids: list[str] = Field(default_factory=list)


class TopicClustersResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    job: TopicClusterJobResponse
    available_jobs: list[TopicClusterJobResponse] = Field(default_factory=list)
    level: int
    clusters: list[TopicClusterResponse] = Field(default_factory=list)
    total_clusters: int
    total_members: int
    generated_at: datetime | None = None


class TopicClusterDetailResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    job: TopicClusterJobResponse
    cluster: TopicClusterResponse
    related_clusters: list[TopicClusterResponse] = Field(default_factory=list)


__all__ = [
    "TopicClusterDetailResponse",
    "TopicClusterJobResponse",
    "TopicClusterResponse",
    "TopicClustersResponse",
]
