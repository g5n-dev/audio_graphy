"""M8 WS-1/2 — streaming_sessions table (P0-11).

Creates ``streaming_sessions`` for per-WebSocket-connection session state
(M8 architecture §14.1.1). Each row tracks one ``/ws/stream`` session's
lifecycle metrics + audit fields (consent_token_hash, end_reason, etc.).

Deviation from architecture §14.1.2:
    The architecture doc also specifies ``ALTER TABLE edges`` adding
    ``confidence_tag`` / ``streaming_origin`` / ``source_session_id`` columns.
    AudioGraphy does NOT have an ``edges`` SQL table — graph edges live in
    the NetworkX ``MultiDiGraph`` (in-memory + GraphML on disk). Therefore
    this migration skips the edges ALTER. The three new metadata fields are
    carried on the in-memory ``GraphEdge`` dataclass (M8 T7 / DeltaGraphUpdater)
    and persisted in GraphML attributes alongside each edge. No data is lost.

Revision ID: 0009_m8_streaming_init
Revises: 0008_m7_indexes
Create Date: 2026-07-22 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_m8_streaming_init"
down_revision: str | None = "0008_m7_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create streaming_sessions table."""
    op.create_table(
        "streaming_sessions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column(
            "recording_id",
            sa.BigInteger(),
            sa.ForeignKey("recordings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_chunk_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "seg_confirmed_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "seg_realtime_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("bytes_in", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("end_reason", sa.String(length=32), nullable=True),
        sa.Column("consent_token_hash", sa.String(length=64), nullable=False),
        sa.Column("stats", sa.JSON(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_streaming_sessions")),
        sa.UniqueConstraint(
            "session_id", name="ux_streaming_sessions_session_id",
        ),
        sa.CheckConstraint(
            "end_reason IS NULL OR end_reason IN "
            "('normal', 'client_disconnect', 'server_shutdown', "
            "'error', 'backpressure', 'timeout')",
            name="ck_streaming_sessions_end_reason",
        ),
    )
    op.create_index(
        "ix_streaming_sessions_tenant_started",
        "streaming_sessions",
        ["tenant_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_streaming_sessions_recording",
        "streaming_sessions",
        ["recording_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_streaming_sessions_tenant_id"),
        "streaming_sessions",
        ["tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop streaming_sessions table.

    Do NOT explicitly drop indexes first: MySQL 8 refuses to drop
    ``ix_streaming_sessions_recording`` because the FK
    ``streaming_sessions.recording_id -> recordings.id`` depends on it
    (same bug class as M7 ``ix_vp_speaker``). ``drop_table`` cascades
    FK + index cleanup automatically.
    """
    op.drop_table("streaming_sessions")
