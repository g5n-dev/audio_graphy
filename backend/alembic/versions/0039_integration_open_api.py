"""Open API for external systems: api_keys, integration_uploads, integration_callbacks.

Three tables backing the machine-to-machine surface (``/api/v1/open``):

* ``api_keys`` — SHA-256 of the credential only; the plaintext is shown once at
  creation and the webhook signing secret is derived from the master key plus
  the row id, so neither travels with a database backup.
* ``integration_uploads`` — one row per external upload, unique on the caller's
  ``external_ref`` per tenant; that reference is the idempotency key and the
  correlation id echoed in every callback.
* ``integration_callbacks`` — the delivery outbox. Deliberately the same
  five-state machine as ``erasure_outbox``: rows are inserted in the SAME
  transaction as the recording's terminal status transition so completion and
  notification cannot diverge across a crash.

Revision ID: 0039_integration_open_api
Revises: 0038_backfill_attach_cos
Create Date: 2026-08-05 12:00:00.000000
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0039_integration_open_api"
down_revision: str | None = "0038_backfill_attach_cos"
branch_labels: str | None = None
depends_on: str | None = None


def _base_columns() -> list[sa.Column[Any]]:
    return [
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "api_keys",
        *_base_columns(),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_api_keys"),
    )
    op.create_index("ix_api_keys_tenant_id", "api_keys", ["tenant_id"])
    # Globally unique: the hash is the sole lookup key on every request — the
    # caller never presents a tenant out of band.
    op.create_index("ux_api_keys_hash", "api_keys", ["key_hash"], unique=True)
    op.create_index("ux_api_keys_tenant_name", "api_keys", ["tenant_id", "name"], unique=True)

    op.create_table(
        "integration_uploads",
        *_base_columns(),
        sa.Column("external_ref", sa.String(length=128), nullable=False),
        sa.Column("recording_id", sa.BigInteger(), nullable=False),
        sa.Column("api_key_id", sa.BigInteger(), nullable=False),
        sa.Column("callback_url", sa.String(length=512), nullable=True),
        sa.ForeignKeyConstraint(
            ["recording_id"],
            ["recordings.id"],
            name="fk_integration_uploads_recording",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            ["api_keys.id"],
            name="fk_integration_uploads_api_key",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_integration_uploads"),
    )
    op.create_index("ix_integration_uploads_tenant_id", "integration_uploads", ["tenant_id"])
    op.create_index(
        "ux_integration_uploads_ref",
        "integration_uploads",
        ["tenant_id", "external_ref"],
        unique=True,
    )
    op.create_index(
        "ix_integration_uploads_recording", "integration_uploads", ["recording_id"]
    )

    op.create_table(
        "integration_callbacks",
        *_base_columns(),
        sa.Column("upload_id", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("callback_url", sa.String(length=512), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'succeeded', 'failed', 'dead_letter')",
            name="ck_integration_callbacks_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_integration_callbacks_attempts"),
        sa.ForeignKeyConstraint(
            ["upload_id"],
            ["integration_uploads.id"],
            name="fk_integration_callbacks_upload",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_integration_callbacks"),
    )
    op.create_index("ix_integration_callbacks_tenant_id", "integration_callbacks", ["tenant_id"])
    op.create_index(
        "ux_integration_callbacks_event", "integration_callbacks", ["event_id"], unique=True
    )
    op.create_index(
        "ix_integration_callbacks_claim",
        "integration_callbacks",
        ["status", "available_at"],
    )


def downgrade() -> None:
    op.drop_table("integration_callbacks")
    op.drop_table("integration_uploads")
    op.drop_table("api_keys")
