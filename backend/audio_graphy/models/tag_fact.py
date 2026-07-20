"""TagFact ORM model — append-only tag versioning truth source (Layer 1).

Each row records a tag judgment with its complete "recipe" (prompt_version,
model_version, input_hash). Rows are insert-only — never updated or deleted.
The application layer enforces append-only semantics.

Table: tag_facts
Inherits: TenantScopedBase (denormalized tenant_id for query efficiency)
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from audio_graphy.models.base import TenantScopedBase
from audio_graphy.models.enums import TagSource

if TYPE_CHECKING:
    from audio_graphy.models.recording import Recording
    from audio_graphy.models.user import User


class TagFact(TenantScopedBase):
    """标签事实表 | TagFact — append-only tag versioning truth source.

    Every tag judgment is recorded here with full provenance: which prompt
    version, which model version, and which input hash produced it. This
    enables reproducibility and audit trails.

    Append-only semantics: rows are INSERT-only (updated_at exists but is
    never modified). The application layer must enforce this constraint.

    Key constraints:
        - FK(recording_id -> recordings.id) ON DELETE CASCADE.
        - FK(computed_by -> users.id) ON DELETE SET NULL.
        - UNIQUE(recording_id, tag_path, version): version uniqueness.
        - CHECK(source IN llm/manual): tag source validation.
        - CHECK(version > 0): version must be positive.
        - Denormalized tenant_id for middleware-level filtering.
    """

    __tablename__ = "tag_facts"

    recording_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("recordings.id", ondelete="CASCADE"),
        nullable=False,
    )
    tag_path: Mapped[str] = mapped_column(String(255), nullable=False)
    tag_value: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=TagSource.LLM.value,
    )
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    computed_by: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    recording: Mapped[Recording] = relationship(back_populates="tag_facts")
    computed_by_user: Mapped[User | None] = relationship(
        "User", back_populates="computed_tag_facts"
    )

    __table_args__ = (
        CheckConstraint(
            "source IN ('llm', 'manual')",
            name="ck_tag_facts_source",
        ),
        CheckConstraint("version > 0", name="ck_tag_facts_version"),
        Index(
            "ux_tag_facts_recording_path_version",
            "recording_id",
            "tag_path",
            "version",
            unique=True,
        ),
        Index("ix_tag_facts_recording_path", "recording_id", "tag_path"),
        Index("ix_tag_facts_prompt_version", "prompt_version"),
    )
