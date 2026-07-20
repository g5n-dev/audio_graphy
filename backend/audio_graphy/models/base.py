"""Declarative base for all AudioGraphy ORM models.

Design decisions:
- All tables inherit from ``Base`` (global) or ``TenantScopedBase`` (multi-tenant).
- ``BigInteger`` primary keys for scalability (vector tables expect 10^5-10^6 rows).
- Naming convention ensures consistent constraint names (ux_/ix_/ck_/fk_/pk_).
- ``to_dict()`` provides shallow serialization for logging / API responses.

See: docs/DESIGN.md §6.1, docs/m1.4-prd.md §5, docs/m1.4-architecture.md §1.3.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, MetaData, String
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

# Naming convention for auto-generated constraint names.
# Explicit names in model definitions always take precedence.
# See: https://docs.sqlalchemy.org/en/20/core/constraints.html#constraint-naming-conventions
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "ux_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


def _utcnow() -> datetime:
    """Return current UTC time (replaces deprecated ``datetime.utcnow``)."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base with common columns all tables inherit.

    Provides:
        - ``id``: BigInteger auto-increment primary key.
        - ``created_at``: timezone-aware timestamp (UTC).
        - ``updated_at``: timezone-aware timestamp (UTC), auto-updated on flush.
        - ``to_dict()``: shallow dict serialization for logging / API responses.

    Subclasses **must** declare an explicit ``__tablename__`` (the auto-generated
    CamelCase→snake_case conversion produces singular names, but DESIGN.md uses
    plural table names).
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    @declared_attr.directive
    def __tablename__(cls) -> str:  # noqa: N805
        """Auto-generate table name from class name (CamelCase -> snake_case).

        Subclasses should override with an explicit ``__tablename__``.
        """
        name = cls.__name__
        result: list[str] = []
        for i, ch in enumerate(name):
            if ch.isupper() and i > 0:
                result.append("_")
            result.append(ch.lower())
        return "".join(result)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )

    def to_dict(self) -> dict[str, Any]:
        """Shallow dict serialization of all column values.

        Returns:
            A dict mapping column names to their Python values.
        """
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}

    def __repr__(self) -> str:
        pk = getattr(self, "id", None)
        return f"<{self.__class__.__name__} id={pk}>"


class TenantScopedBase(Base):
    """Base for tables that carry a tenant_id for multi-tenant isolation.

    Per DESIGN.md §14.2, all business tables carry tenant_id and the auth
    middleware enforces row-level filtering via ``WHERE tenant_id = ?``.

    The ``tenant_id`` column logically references ``tenants.code`` (String(64))
    but no physical foreign key is created to avoid DDL bloat.
    """

    __abstract__ = True

    @declared_attr.directive
    def tenant_id(cls) -> Mapped[str]:  # noqa: N805
        return mapped_column(String(64), nullable=False, index=True)
