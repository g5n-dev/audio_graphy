"""Prompt ORM model — LLM prompt version management.

Stores versioned prompt templates for tag extraction. Supports version
switching, A/B diff, and rollback. The "one active version per name"
constraint is enforced at the application layer (MySQL 8 does not support
partial unique indexes).

Table: prompts
Inherits: Base (global resource, not tenant-scoped)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from audio_graphy.models.base import Base

if TYPE_CHECKING:
    from audio_graphy.models.user import User


class Prompt(Base):
    """Prompt 版本管理表 | Prompt version registry.

    Manages versioned prompt templates for LLM-based tag extraction.
    Each (name, version) pair is unique. The ``active`` flag indicates
    the currently effective version; only one active version per name
    should exist (enforced by application layer).

    Key constraints:
        - UNIQUE(name, version): prompt version uniqueness.
        - FK(created_by -> users.id) ON DELETE SET NULL.
        - INDEX(active): fast lookup of active prompts.
    """

    __tablename__ = "prompts"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    changelog: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_by_user: Mapped[User | None] = relationship("User", back_populates="created_prompts")

    __table_args__ = (
        UniqueConstraint("name", "version", name="ux_prompts_name_version"),
        CheckConstraint("active IN (TRUE, FALSE)", name="ck_prompts_active"),
        Index("ix_prompts_active", "active"),
    )
