"""Initial schema — empty placeholder.

M1.3 will replace this with the full CREATE TABLE statements for all 16 tables
defined in docs/DESIGN.md §6.1.

Revision ID: 0001_init
Revises:
Create Date: 2026-07-20 09:40:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_init"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """M1.2 placeholder — no tables yet.

    M1.3 will populate this with the full schema. For now we just create
    a sentinel table so `alembic upgrade head` succeeds and the version
    table is initialized.
    """
    op.create_table(
        "_alembic_sentinel",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("note", sa.String(255), nullable=False),
        comment="Sentinel table created in M1.2 — dropped in M1.3 when real schema lands",
    )
    op.bulk_insert(
        sa.table(
            "_alembic_sentinel",
            sa.column("note", sa.String),
        ),
        [{"note": "M1.2 initial migration — schema lands in M1.3"}],
    )


def downgrade() -> None:
    op.drop_table("_alembic_sentinel")
