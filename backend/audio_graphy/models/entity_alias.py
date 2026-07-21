"""EntityAlias ORM — tenant-scoped alias → canonical entity name map (M6 WS-3).

Stores manual and fuzzy-matched aliases so the entity extractor can normalise
near-duplicate Chinese entity names (``CS75 Plus`` ↔ ``CS75PLUS`` ↔
``长安 CS75 Plus``) into a single canonical node in the knowledge graph.

Design (per docs/m6-architecture.md §5, docs/m6-prd.md §6.2):

| Decision        | Choice                                            |
|-----------------|---------------------------------------------------|
| Storage         | DB-backed (tenant-scoped, hot-editable)           |
| Schema          | (tenant_id, alias_text) UNIQUE                    |
| Source          | ``manual`` (admin) / ``fuzzy_match`` (rapidfuzz)  |
|                 | / ``llm_inferred`` (M7+)                          |
| Confidence      | 1.0 for manual; WRatio score [0, 1] for fuzzy     |
| Hot-reload      | Re-queried on every extraction run (no restart)   |

Table: ``entity_aliases``. Inherits ``tenant_id`` from ``TenantScopedBase``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from audio_graphy.models.base import TenantScopedBase

if TYPE_CHECKING:
    from audio_graphy.models.user import User

_VALID_SOURCES = ("manual", "fuzzy_match", "llm_inferred")


class EntityAlias(TenantScopedBase):
    """EntityAlias — maps a textual alias to its canonical form.

    Table: ``entity_aliases``. Inherits ``created_at`` / ``updated_at`` /
    ``tenant_id`` from ``TenantScopedBase``.

    Attributes:
        canonical_text: Normalised canonical entity name (e.g. ``"CS75 Plus"``).
        alias_text: Raw alias text (e.g. ``"cs75plus"``). Unique per tenant.
        entity_type: Optional domain type filter (``"车型"`` / ``"客户"``).
            When set, the alias only matches entities of the same type.
        source: ``"manual"`` (admin-curated) / ``"fuzzy_match"`` (auto-found
            by rapidfuzz during extraction) / ``"llm_inferred"`` (M7+).
        confidence: ``1.0`` for manual; rapidfuzz WRatio score for fuzzy.
        created_by: Optional admin user ID who curated this alias.
        note: Optional free-form annotation.
    """

    __tablename__ = "entity_aliases"

    canonical_text: Mapped[str] = mapped_column(String(255), nullable=False)
    alias_text: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    confidence: Mapped[float] = mapped_column(default=1.0)
    created_by: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    user: Mapped[User | None] = relationship()

    __table_args__ = (
        UniqueConstraint("tenant_id", "alias_text", name="uq_entity_aliases_tenant_alias"),
        Index(
            "ix_entity_aliases_tenant_canonical",
            "tenant_id",
            "canonical_text",
        ),
        Index(
            "ix_entity_aliases_tenant_alias",
            "tenant_id",
            "alias_text",
        ),
        CheckConstraint(
            f"source IN {_VALID_SOURCES}",
            name="ck_entity_aliases_source",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<EntityAlias id={self.id} "
            f"alias={self.alias_text!r} -> canonical={self.canonical_text!r} "
            f"source={self.source!r}>"
        )


__all__ = ["EntityAlias"]
