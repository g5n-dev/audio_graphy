"""Declarative base for all AudioGraphy ORM models.

Design decisions:
- `Metadata` from DESIGN.md §6.1: all tables carry `tenant_id`, `created_at`, `updated_at`.
- Use `BigInteger` for all PKs (vector tables expect 10^5-10^6 rows).
- Naming convention follows SQLAlchemy recommended pattern for FK constraints.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, String
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column


class Base(DeclarativeBase):
    """Declarative base with common columns all tables inherit."""

    @declared_attr.directive
    def __tablename__(cls) -> str:  # noqa: N805
        # CamelCase → snake_case
        name = cls.__name__
        result: list[str] = []
        for i, ch in enumerate(name):
            if ch.isupper() and i > 0:
                result.append("_")
            result.append(ch.lower())
        return "".join(result)

    # Common columns — overridden by tables that don't need them
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    def __repr__(self) -> str:
        pk = getattr(self, "id", None)
        return f"<{self.__class__.__name__} id={pk}>"


class TenantScopedBase(Base):
    """Base for tables that carry a tenant_id for multi-tenant isolation.

    Per DESIGN.md §14.2, all business tables carry tenant_id and the auth
    middleware enforces row-level filtering.
    """

    @declared_attr.directive
    def tenant_id(cls) -> Mapped[str]:  # noqa: N805
        return mapped_column(String(64), nullable=False, index=True)

    def to_dict(self) -> dict[str, Any]:
        """Convenience dict serializer for logging / API responses."""
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}
