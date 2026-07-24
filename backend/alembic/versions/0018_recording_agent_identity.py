"""Add a stable, nullable recording agent authorization identity.

Revision ID: 0018_recording_agent_identity
Revises: 0017_reception_agent_backfill
Create Date: 2026-07-24 14:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018_recording_agent_identity"
down_revision: str | None = "0017_reception_agent_backfill"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Expand recordings without assigning ambiguous historical ownership."""

    op.add_column(
        "recordings",
        sa.Column("agent_user_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_recordings_agent_user_id_users",
        "recordings",
        "users",
        ["agent_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_recordings_tenant_agent_recorded_id",
        "recordings",
        ["tenant_id", "agent_user_id", "recorded_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the stable recording authorization key."""

    op.drop_index(
        "ix_recordings_tenant_agent_recorded_id",
        table_name="recordings",
    )
    op.drop_constraint(
        "fk_recordings_agent_user_id_users",
        "recordings",
        type_="foreignkey",
    )
    op.drop_column("recordings", "agent_user_id")
