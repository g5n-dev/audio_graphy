"""Add stable reception agent identity and queue/discovery indexes.

Revision ID: 0016_reception_agent_identity
Revises: 0015_reception_automation
Create Date: 2026-07-23 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_reception_agent_identity"
down_revision: str | None = "0015_reception_automation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Expand the schema with a nullable, server-owned authorization key."""

    op.add_column(
        "receptions",
        sa.Column("agent_user_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_receptions_agent_user_id_users",
        "receptions",
        "users",
        ["agent_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_receptions_tenant_started_id",
        "receptions",
        ["tenant_id", "started_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_receptions_tenant_agent_started_id",
        "receptions",
        ["tenant_id", "agent_user_id", "started_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_recordings_tenant_store_status_recorded_id",
        "recordings",
        ["tenant_id", "store_id", "status", "recorded_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the stable authorization key and supporting indexes."""

    op.drop_index(
        "ix_recordings_tenant_store_status_recorded_id",
        table_name="recordings",
    )
    op.drop_index(
        "ix_receptions_tenant_agent_started_id",
        table_name="receptions",
    )
    op.drop_index(
        "ix_receptions_tenant_started_id",
        table_name="receptions",
    )
    op.drop_constraint(
        "fk_receptions_agent_user_id_users",
        "receptions",
        type_="foreignkey",
    )
    op.drop_column("receptions", "agent_user_id")
