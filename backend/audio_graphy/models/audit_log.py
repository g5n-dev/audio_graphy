"""AuditLog ORM model — sensitive operation audit trail.

Records sensitive operations (decrypt, delete, export, etc.) with before/after
state snapshots. Column names are renamed from MySQL reserved words:
``before`` -> ``before_value``, ``after`` -> ``after_value``, ``at`` ->
``occurred_at``.

Table: audit_logs
Inherits: TenantScopedBase
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from audio_graphy.models.base import TenantScopedBase

if TYPE_CHECKING:
    from audio_graphy.models.user import User


class AuditLog(TenantScopedBase):
    """审计日志表 | AuditLog — sensitive operation audit trail.

    Each row records a sensitive operation with its actor, action type,
    target entity, and before/after state snapshots (as JSON).

    Column rename notes (MySQL reserved word avoidance):
        - ``before`` -> ``before_value``
        - ``after`` -> ``after_value`` (MySQL reserved word in trigger syntax)
        - ``at`` -> ``occurred_at`` (SQL context ambiguity)

    Key constraints:
        - FK(user_id -> users.id) ON DELETE SET NULL.
        - INDEX(tenant_id, user_id): audit lookup by user within tenant.
        - INDEX(occurred_at): time-range audit queries.
        - JSON(before_value, after_value): state snapshots.
    """

    __tablename__ = "audit_logs"

    user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(255), nullable=False)
    before_value: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after_value: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    user: Mapped[User | None] = relationship(back_populates="audit_logs")

    __table_args__ = (
        Index("ix_audit_logs_tenant_user", "tenant_id", "user_id"),
        Index("ix_audit_logs_occurred_at", "occurred_at"),
    )
