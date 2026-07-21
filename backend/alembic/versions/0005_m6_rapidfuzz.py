"""M6 WS-3 — entity_aliases table for rapidfuzz Chinese entity clustering.

Creates the ``entity_aliases`` table that backs ``EntityAlias`` ORM and the
3-layer ``EntityMerger`` flow::

    Layer 1: DB alias table (this migration) — exact match on (tenant_id, alias_text)
    Layer 2: rapidfuzz fuzz.WRatio (threshold=0.85 default) against existing canonicals
    Layer 3: leave as-is (becomes a new canonical row upstream)

Backward compatibility: brand-new table; no migration risk.

Revision ID: 0005_m6_rapidfuzz
Revises: 0004_m6_eval
Create Date: 2026-07-21 14:32:11.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_m6_rapidfuzz"
down_revision: str | None = "0004_m6_eval"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create entity_aliases table."""
    op.create_table(
        "entity_aliases",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("canonical_text", sa.String(length=255), nullable=False),
        sa.Column("alias_text", sa.String(length=255), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=True),
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=False,
            server_default="manual",
        ),
        sa.Column(
            "confidence",
            sa.Float(),
            nullable=False,
            server_default="1.0",
        ),
        sa.Column(
            "created_by",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entity_aliases")),
        sa.UniqueConstraint(
            "tenant_id",
            "alias_text",
            name="uq_entity_aliases_tenant_alias",
        ),
        sa.CheckConstraint(
            "source IN ('manual', 'fuzzy_match', 'llm_inferred')",
            name="ck_entity_aliases_source",
        ),
    )
    op.create_index(
        "ix_entity_aliases_tenant_canonical",
        "entity_aliases",
        ["tenant_id", "canonical_text"],
        unique=False,
    )
    op.create_index(
        "ix_entity_aliases_tenant_alias",
        "entity_aliases",
        ["tenant_id", "alias_text"],
        unique=False,
    )
    op.create_index(
        op.f("ix_entity_aliases_tenant_id"),
        "entity_aliases",
        ["tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop entity_aliases table."""
    op.drop_index(op.f("ix_entity_aliases_tenant_id"), table_name="entity_aliases")
    op.drop_index(
        "ix_entity_aliases_tenant_alias",
        table_name="entity_aliases",
    )
    op.drop_index(
        "ix_entity_aliases_tenant_canonical",
        table_name="entity_aliases",
    )
    op.drop_table("entity_aliases")
