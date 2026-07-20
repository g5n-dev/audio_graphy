"""User ORM model — system users with RBAC roles.

Supports four roles (admin, inspector, agent, viewer) per DESIGN.md §14.1.
Email uniqueness is enforced per-tenant (same email can exist in different
tenants).

Table: users
Inherits: TenantScopedBase
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from audio_graphy.models.base import TenantScopedBase
from audio_graphy.models.enums import UserRole

if TYPE_CHECKING:
    from audio_graphy.models.audit_log import AuditLog
    from audio_graphy.models.prompt import Prompt
    from audio_graphy.models.tag_fact import TagFact


class User(TenantScopedBase):
    """用户表 | User — system user with RBAC role.

    Stores user identity and authorization role. The ``tenant_id`` logically
    references ``tenants.code`` (no physical FK). Email is unique within
    each tenant.

    Key constraints:
        - UNIQUE(tenant_id, email): email uniqueness per tenant.
        - CHECK(role IN admin/inspector/agent/viewer): RBAC role validation.
    """

    __tablename__ = "users"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=UserRole.VIEWER.value,
    )

    # ORM relationships (lazy-loaded)
    created_prompts: Mapped[list[Prompt]] = relationship("Prompt", back_populates="created_by_user")
    computed_tag_facts: Mapped[list[TagFact]] = relationship(
        "TagFact", back_populates="computed_by_user"
    )
    audit_logs: Mapped[list[AuditLog]] = relationship("AuditLog", back_populates="user")

    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="ux_users_tenant_email"),
        CheckConstraint(
            "role IN ('admin', 'inspector', 'agent', 'viewer')",
            name="ck_users_role",
        ),
    )
