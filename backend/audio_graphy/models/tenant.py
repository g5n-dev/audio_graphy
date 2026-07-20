"""Tenant ORM model — multi-tenant root table.

Each tenant represents a brand/enterprise. The ``code`` column serves as
the business identifier logically referenced by ``tenant_id`` in all
TenantScopedBase tables (no physical FK to avoid DDL bloat).

Table: tenants
Inherits: Base (no tenant_id — self is the tenant)
"""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from audio_graphy.models.base import Base


class Tenant(Base):
    """租户表 | Tenant — multi-tenant root entity.

    Stores tenant metadata (name, brand, region). The ``code`` column
    is the business identifier referenced by ``TenantScopedBase.tenant_id``
    across all business tables.

    Key constraints:
        - UNIQUE(code): tenant business identifier must be unique.
    """

    __tablename__ = "tenants"

    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    brand: Mapped[str | None] = mapped_column(String(255), nullable=True)
    region: Mapped[str | None] = mapped_column(String(255), nullable=True)
