"""Recording ORM model — recording pipeline master table.

Tracks each recording's ingestion status, pipeline progress, and tag version.
This is the central entity that segments, chunks, and tags reference.

Table: recordings
Inherits: TenantScopedBase
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from audio_graphy.models.base import TenantScopedBase
from audio_graphy.models.enums import PipelineState, RecordingStatus

if TYPE_CHECKING:
    from audio_graphy.models.chunk import Chunk
    from audio_graphy.models.segment import Segment
    from audio_graphy.models.tag_current import TagCurrent
    from audio_graphy.models.tag_fact import TagFact


class Recording(TenantScopedBase):
    """录音表 | Recording — pipeline master entity.

    Tracks each recording through the ingestion pipeline (VAD -> ASR ->
    chunking -> embedding -> extraction -> graph_merge -> tagging -> done).
    The ``status`` field tracks high-level lifecycle, while ``pipeline_state``
    tracks the current processing stage.

    Key constraints:
        - CHECK(status): valid recording lifecycle status.
        - CHECK(pipeline_state): valid pipeline stage.
        - INDEX(tenant_id, store_id): filter by store within tenant.
        - INDEX(tenant_id, status): pipeline worker queue lookup.
        - INDEX(recorded_at): time-range queries.
        - INDEX(prompt_version): re-computation by prompt version.
    """

    __tablename__ = "recordings"

    store_id: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Stable authorization identity. ``agent_name`` is a historical display
    # snapshot and must not be used as an ownership key.
    agent_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    customer_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=RecordingStatus.QUEUED.value,
    )
    pipeline_state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=PipelineState.PENDING.value,
    )
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # M6 PIPL §14.3 — audio envelope encryption at rest.
    # When audio_encrypted_path is NULL, the recording reverts to plaintext
    # behaviour (read `path` directly, e.g. legacy M5 rows).
    audio_encrypted_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    audio_encryption_meta: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # ORM relationships (lazy-loaded, cascade delete for children)
    segments: Mapped[list[Segment]] = relationship(
        back_populates="recording", cascade="all, delete-orphan"
    )
    chunks: Mapped[list[Chunk]] = relationship(
        back_populates="recording", cascade="all, delete-orphan"
    )
    tag_facts: Mapped[list[TagFact]] = relationship(
        back_populates="recording", cascade="all, delete-orphan"
    )
    current_tags: Mapped[list[TagCurrent]] = relationship(
        back_populates="recording", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'processing', 'indexed', 'failed', 'archived')",
            name="ck_recordings_status",
        ),
        CheckConstraint(
            "pipeline_state IN ('pending', 'vad', 'asr', 'chunking', "
            "'embedding', 'extraction', 'graph_merge', 'tagging', 'done', 'error')",
            name="ck_recordings_pipeline_state",
        ),
        Index("ix_recordings_tenant_store", "tenant_id", "store_id"),
        Index("ix_recordings_tenant_status", "tenant_id", "status"),
        Index(
            "ix_recordings_tenant_store_status_recorded_id",
            "tenant_id",
            "store_id",
            "status",
            "recorded_at",
            "id",
        ),
        Index(
            "ix_recordings_tenant_agent_recorded_id",
            "tenant_id",
            "agent_user_id",
            "recorded_at",
            "id",
        ),
        Index("ix_recordings_recorded_at", "recorded_at"),
        Index("ix_recordings_prompt_version", "prompt_version"),
    )
