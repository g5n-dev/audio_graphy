"""Add encrypted, tenant-scoped persistent LLM cache tables.

Revision ID: 0021_llm_cache
Revises: 0020_tag_governance
Create Date: 2026-07-25 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "0021_llm_cache"
down_revision: str | None = "0020_tag_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _base_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
    ]


def upgrade() -> None:
    op.add_column(
        "llm_call_logs",
        sa.Column(
            "event_kind",
            sa.String(length=32),
            server_default="logical_request",
            nullable=False,
        ),
    )
    op.add_column(
        "llm_call_logs",
        sa.Column(
            "outcome",
            sa.String(length=16),
            server_default="success",
            nullable=False,
        ),
    )
    op.add_column(
        "llm_call_logs",
        sa.Column("attempt", sa.Integer(), nullable=True),
    )
    op.add_column(
        "llm_call_logs",
        sa.Column("error_type", sa.String(length=128), nullable=True),
    )
    op.create_check_constraint(
        "ck_llm_call_logs_event_kind",
        "llm_call_logs",
        "event_kind IN ('logical_request', 'provider_attempt')",
    )
    op.create_check_constraint(
        "ck_llm_call_logs_outcome",
        "llm_call_logs",
        "outcome IN ('success', 'error', 'cancelled')",
    )
    op.create_check_constraint(
        "ck_llm_call_logs_attempt",
        "llm_call_logs",
        "attempt IS NULL OR attempt >= 1",
    )
    op.add_column(
        "llm_call_logs",
        sa.Column(
            "purpose",
            sa.String(length=64),
            server_default="legacy",
            nullable=False,
        ),
    )
    op.add_column(
        "llm_call_logs",
        sa.Column(
            "cache_source",
            sa.String(length=32),
            server_default="provider",
            nullable=False,
        ),
    )
    op.add_column(
        "llm_call_logs",
        sa.Column(
            "provider_called",
            sa.Boolean(),
            server_default=sa.text("1"),
            nullable=False,
        ),
    )

    op.create_table(
        "llm_cache_entries",
        *_base_columns(),
        sa.Column("namespace", sa.String(length=64), nullable=False),
        sa.Column("recipe_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("model_epoch", sa.String(length=128), nullable=False),
        sa.Column(
            "payload_encrypted",
            sa.LargeBinary().with_variant(mysql.MEDIUMBLOB(), "mysql"),
            nullable=True,
        ),
        sa.Column("encryption_meta", sa.JSON(), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column(
            "payload_size_bytes",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "has_provenance",
            sa.Boolean(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_accessed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("hit_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("semantic_scope_hash", sa.String(length=64), nullable=True),
        sa.Column("semantic_guard_hash", sa.String(length=64), nullable=True),
        sa.Column("semantic_embedding", sa.LargeBinary(), nullable=True),
        sa.Column("semantic_dim", sa.Integer(), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'ready')",
            name="ck_llm_cache_entries_status",
        ),
        sa.CheckConstraint(
            "payload_size_bytes >= 0 AND hit_count >= 0",
            name="ck_llm_cache_entries_counters",
        ),
        sa.CheckConstraint(
            "semantic_dim IS NULL OR semantic_dim > 0",
            name="ck_llm_cache_entries_semantic_dim",
        ),
    )
    op.create_index(
        "ix_llm_cache_entries_tenant_id",
        "llm_cache_entries",
        ["tenant_id"],
    )
    op.create_index(
        "ux_llm_cache_entries_identity",
        "llm_cache_entries",
        ["tenant_id", "namespace", "recipe_sha256"],
        unique=True,
    )
    op.create_index(
        "ix_llm_cache_entries_expiry",
        "llm_cache_entries",
        ["expires_at"],
    )
    op.create_index(
        "ix_llm_cache_entries_tenant_access",
        "llm_cache_entries",
        ["tenant_id", "last_accessed_at"],
    )
    op.create_index(
        "ix_llm_cache_entries_lease",
        "llm_cache_entries",
        ["status", "lease_expires_at"],
    )
    op.create_index(
        "ix_llm_cache_entries_semantic",
        "llm_cache_entries",
        [
            "tenant_id",
            "namespace",
            "semantic_scope_hash",
            "semantic_guard_hash",
            "language",
            "last_accessed_at",
        ],
    )

    op.create_table(
        "llm_cache_refs",
        *_base_columns(),
        sa.Column(
            "cache_entry_id",
            sa.BigInteger(),
            sa.ForeignKey("llm_cache_entries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
    )
    op.create_index(
        "ix_llm_cache_refs_tenant_id",
        "llm_cache_refs",
        ["tenant_id"],
    )
    op.create_index(
        "ux_llm_cache_refs_entry_source",
        "llm_cache_refs",
        ["cache_entry_id", "source_type", "source_id"],
        unique=True,
    )
    op.create_index(
        "ix_llm_cache_refs_tenant_source",
        "llm_cache_refs",
        ["tenant_id", "source_type", "source_id"],
    )

    op.create_table(
        "llm_cache_source_guards",
        *_base_columns(),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column(
            "state",
            sa.String(length=16),
            server_default="active",
            nullable=False,
        ),
        sa.Column("erased_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('active', 'erased')",
            name="ck_llm_cache_source_guards_state",
        ),
    )
    op.create_index(
        "ix_llm_cache_source_guards_tenant_id",
        "llm_cache_source_guards",
        ["tenant_id"],
    )
    op.create_index(
        "ux_llm_cache_source_guards_identity",
        "llm_cache_source_guards",
        ["tenant_id", "source_type", "source_id"],
        unique=True,
    )
    op.create_index(
        "ix_llm_cache_source_guards_erased",
        "llm_cache_source_guards",
        ["state", "erased_at"],
    )

    op.create_table(
        "llm_cache_purges",
        *_base_columns(),
        sa.Column("namespace", sa.String(length=64), nullable=False),
        sa.Column("recipe_sha256", sa.String(length=64), nullable=False),
    )
    op.create_index(
        "ix_llm_cache_purges_tenant_id",
        "llm_cache_purges",
        ["tenant_id"],
    )
    op.create_index(
        "ux_llm_cache_purges_identity",
        "llm_cache_purges",
        ["tenant_id", "namespace", "recipe_sha256"],
        unique=True,
    )
    op.create_index(
        "ix_llm_cache_purges_created",
        "llm_cache_purges",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_table("llm_cache_purges")
    op.drop_table("llm_cache_source_guards")
    op.drop_table("llm_cache_refs")
    op.drop_table("llm_cache_entries")
    op.drop_column("llm_call_logs", "provider_called")
    op.drop_column("llm_call_logs", "cache_source")
    op.drop_column("llm_call_logs", "purpose")
    op.drop_constraint(
        "ck_llm_call_logs_attempt",
        "llm_call_logs",
        type_="check",
    )
    op.drop_constraint(
        "ck_llm_call_logs_outcome",
        "llm_call_logs",
        type_="check",
    )
    op.drop_constraint(
        "ck_llm_call_logs_event_kind",
        "llm_call_logs",
        type_="check",
    )
    op.drop_column("llm_call_logs", "error_type")
    op.drop_column("llm_call_logs", "attempt")
    op.drop_column("llm_call_logs", "outcome")
    op.drop_column("llm_call_logs", "event_kind")
