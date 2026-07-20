"""TagCurrent ORM model — current effective tag view (Layer 2).

Maintains the latest version of each (recording, tag_path) pair. Updated
by the application layer via upsert after each tag_facts insert. Statistics
and dashboard queries read this table for current tag state.

Table: tag_current
Inherits: TenantScopedBase (denormalized tenant_id for query efficiency)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from audio_graphy.models.base import TenantScopedBase

if TYPE_CHECKING:
    from audio_graphy.models.recording import Recording


class TagCurrent(TenantScopedBase):
    """当前生效标签表 | TagCurrent — latest effective tag per (recording, path).

    Acts as a materialized view of MAX(version) per (recording_id, tag_path)
    from tag_facts. The application layer upserts this table after each
    tag_facts insert. P2 may replace with a SQL VIEW.

    Key constraints:
        - FK(recording_id -> recordings.id) ON DELETE CASCADE.
        - UNIQUE(recording_id, tag_path): one current tag per path per recording.
        - CHECK(version > 0): version must be positive.
        - Denormalized tenant_id for middleware-level filtering.
    """

    __tablename__ = "tag_current"

    recording_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("recordings.id", ondelete="CASCADE"),
        nullable=False,
    )
    tag_path: Mapped[str] = mapped_column(String(255), nullable=False)
    tag_value: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)

    recording: Mapped[Recording] = relationship(back_populates="current_tags")

    __table_args__ = (
        CheckConstraint("version > 0", name="ck_tag_current_version"),
        Index(
            "ux_tag_current_recording_path",
            "recording_id",
            "tag_path",
            unique=True,
        ),
    )
