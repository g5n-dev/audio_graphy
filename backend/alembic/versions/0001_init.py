"""Initial schema — all 13 core tables for AudioGraphy M1.4.

Creates the full schema defined in docs/DESIGN.md §12.2 and docs/m1.4-prd.md §5.
Tables are created in FK dependency order; downgrade drops in reverse.

Tables (creation order):
    1. tenants          — multi-tenant root
    2. users            — RBAC users (TSB)
    3. recordings       — recording pipeline master (TSB)
    4. segments         — VAD-split audio segments (TSB, FK→recordings)
    5. chunks           — text chunks for extraction (TSB, FK→recordings)
    6. tag_facts        — append-only tag versioning (TSB, FK→recordings+users)
    7. tag_current      — current effective tags (TSB, FK→recordings)
    8. tag_stats        — tag statistics aggregation (TSB)
    9. prompts          — prompt version management (Base, FK→users)
    10. vectors_entity  — entity embeddings (TSB)
    11. vectors_chunk   — chunk embeddings (TSB, FK→chunks)
    12. audit_logs      — sensitive operation audit (TSB, FK→users)
    13. llm_call_logs   — LLM call instrumentation (TSB)

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
    """Create all 13 core tables with constraints and indexes."""
    # ── 1. tenants ──────────────────────────────────────────────
    op.create_table(
        "tenants",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("brand", sa.String(255), nullable=True),
        sa.Column("region", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("code", name="ux_tenants_code"),
    )

    # ── 2. users ────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column(
            "role", sa.String(32), nullable=False, server_default="viewer"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("tenant_id", "email", name="ux_users_tenant_email"),
        sa.CheckConstraint(
            "role IN ('admin', 'inspector', 'agent', 'viewer')",
            name="ck_users_role",
        ),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])

    # ── 3. recordings ───────────────────────────────────────────
    op.create_table(
        "recordings",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("store_id", sa.String(64), nullable=False),
        sa.Column("agent_name", sa.String(255), nullable=True),
        sa.Column("customer_hash", sa.String(64), nullable=True),
        sa.Column("path", sa.String(512), nullable=False),
        sa.Column(
            "status", sa.String(32), nullable=False, server_default="queued"
        ),
        sa.Column(
            "pipeline_state",
            sa.String(32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("prompt_version", sa.String(64), nullable=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'indexed', 'failed', 'archived')",
            name="ck_recordings_status",
        ),
        sa.CheckConstraint(
            "pipeline_state IN ('pending', 'vad', 'asr', 'chunking', "
            "'embedding', 'extraction', 'graph_merge', 'tagging', 'done', 'error')",
            name="ck_recordings_pipeline_state",
        ),
    )
    op.create_index("ix_recordings_tenant_id", "recordings", ["tenant_id"])
    op.create_index(
        "ix_recordings_tenant_store", "recordings", ["tenant_id", "store_id"]
    )
    op.create_index(
        "ix_recordings_tenant_status", "recordings", ["tenant_id", "status"]
    )
    op.create_index("ix_recordings_recorded_at", "recordings", ["recorded_at"])
    op.create_index(
        "ix_recordings_prompt_version", "recordings", ["prompt_version"]
    )

    # ── 4. segments ─────────────────────────────────────────────
    op.create_table(
        "segments",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("recording_id", sa.BigInteger, nullable=False),
        sa.Column("idx", sa.Integer, nullable=False),
        sa.Column("start_sec", sa.Float, nullable=False),
        sa.Column("end_sec", sa.Float, nullable=False),
        sa.Column("transcript", sa.Text, nullable=True),
        sa.Column("speaker", sa.String(64), nullable=True),
        sa.Column("vad_conf", sa.Float, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["recording_id"],
            ["recordings.id"],
            ondelete="CASCADE",
            name="fk_segments_recording_id",
        ),
        sa.UniqueConstraint(
            "recording_id", "idx", name="ux_segments_recording_idx"
        ),
        sa.CheckConstraint("end_sec > start_sec", name="ck_segments_time_order"),
    )
    op.create_index("ix_segments_tenant_id", "segments", ["tenant_id"])
    op.create_index("ix_segments_recording_id", "segments", ["recording_id"])

    # ── 5. chunks ───────────────────────────────────────────────
    op.create_table(
        "chunks",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("recording_id", sa.BigInteger, nullable=False),
        sa.Column("segment_ids", sa.JSON, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("token_n", sa.Integer, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["recording_id"],
            ["recordings.id"],
            ondelete="CASCADE",
            name="fk_chunks_recording_id",
        ),
        sa.UniqueConstraint(
            "tenant_id", "content_hash", name="ux_chunks_content_hash"
        ),
        sa.CheckConstraint("token_n > 0", name="ck_chunks_token_n"),
    )
    op.create_index("ix_chunks_tenant_id", "chunks", ["tenant_id"])
    op.create_index("ix_chunks_recording_id", "chunks", ["recording_id"])

    # ── 6. tag_facts ────────────────────────────────────────────
    op.create_table(
        "tag_facts",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("recording_id", sa.BigInteger, nullable=False),
        sa.Column("tag_path", sa.String(255), nullable=False),
        sa.Column("tag_value", sa.String(255), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("model_version", sa.String(64), nullable=False),
        sa.Column(
            "source", sa.String(16), nullable=False, server_default="llm"
        ),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("computed_by", sa.BigInteger, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["recording_id"],
            ["recordings.id"],
            ondelete="CASCADE",
            name="fk_tag_facts_recording_id",
        ),
        sa.ForeignKeyConstraint(
            ["computed_by"],
            ["users.id"],
            ondelete="SET NULL",
            name="fk_tag_facts_computed_by",
        ),
        sa.UniqueConstraint(
            "recording_id",
            "tag_path",
            "version",
            name="ux_tag_facts_recording_path_version",
        ),
        sa.CheckConstraint(
            "source IN ('llm', 'manual')", name="ck_tag_facts_source"
        ),
        sa.CheckConstraint("version > 0", name="ck_tag_facts_version"),
    )
    op.create_index("ix_tag_facts_tenant_id", "tag_facts", ["tenant_id"])
    op.create_index(
        "ix_tag_facts_recording_path",
        "tag_facts",
        ["recording_id", "tag_path"],
    )
    op.create_index(
        "ix_tag_facts_prompt_version", "tag_facts", ["prompt_version"]
    )

    # ── 7. tag_current ──────────────────────────────────────────
    op.create_table(
        "tag_current",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("recording_id", sa.BigInteger, nullable=False),
        sa.Column("tag_path", sa.String(255), nullable=False),
        sa.Column("tag_value", sa.String(255), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["recording_id"],
            ["recordings.id"],
            ondelete="CASCADE",
            name="fk_tag_current_recording_id",
        ),
        sa.UniqueConstraint(
            "recording_id", "tag_path", name="ux_tag_current_recording_path"
        ),
        sa.CheckConstraint("version > 0", name="ck_tag_current_version"),
    )
    op.create_index("ix_tag_current_tenant_id", "tag_current", ["tenant_id"])

    # ── 8. tag_stats ────────────────────────────────────────────
    op.create_table(
        "tag_stats",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("store_id", sa.String(64), nullable=False),
        sa.Column("agent_name", sa.String(100), nullable=True),
        sa.Column("tag_path", sa.String(100), nullable=False),
        sa.Column("tag_value", sa.String(100), nullable=False),
        sa.Column(
            "tag_count", sa.Integer, nullable=False, server_default="0"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "store_id",
            "agent_name",
            "tag_path",
            "tag_value",
            name="ux_tag_stats_dim",
        ),
        sa.CheckConstraint("tag_count >= 0", name="ck_tag_stats_count"),
    )
    op.create_index("ix_tag_stats_tenant_id", "tag_stats", ["tenant_id"])
    op.create_index(
        "ix_tag_stats_tenant_store", "tag_stats", ["tenant_id", "store_id"]
    )

    # ── 9. prompts ──────────────────────────────────────────────
    op.create_table(
        "prompts",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("changelog", sa.Text, nullable=True),
        sa.Column(
            "active", sa.Boolean, nullable=False, server_default=sa.text("0")
        ),
        sa.Column("created_by", sa.BigInteger, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            ondelete="SET NULL",
            name="fk_prompts_created_by",
        ),
        sa.UniqueConstraint("name", "version", name="ux_prompts_name_version"),
        sa.CheckConstraint("active IN (TRUE, FALSE)", name="ck_prompts_active"),
    )
    op.create_index("ix_prompts_active", "prompts", ["active"])

    # ── 10. vectors_entity ──────────────────────────────────────
    op.create_table(
        "vectors_entity",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(255), nullable=False),
        sa.Column("embedding", sa.LargeBinary, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_vectors_entity_tenant_id", "vectors_entity", ["tenant_id"]
    )
    op.create_index(
        "ix_vectors_entity_entity_id",
        "vectors_entity",
        ["tenant_id", "entity_id"],
    )

    # ── 11. vectors_chunk ───────────────────────────────────────
    op.create_table(
        "vectors_chunk",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("chunk_id", sa.BigInteger, nullable=False),
        sa.Column("embedding", sa.LargeBinary, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["chunks.id"],
            ondelete="CASCADE",
            name="fk_vectors_chunk_chunk_id",
        ),
        sa.UniqueConstraint(
            "tenant_id", "chunk_id", name="ux_vectors_chunk_chunk_id"
        ),
    )
    op.create_index(
        "ix_vectors_chunk_tenant_id", "vectors_chunk", ["tenant_id"]
    )
    op.create_index("ix_vectors_chunk_chunk_id", "vectors_chunk", ["chunk_id"])

    # ── 12. audit_logs ──────────────────────────────────────────
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.BigInteger, nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target", sa.String(255), nullable=False),
        sa.Column("before_value", sa.JSON, nullable=True),
        sa.Column("after_value", sa.JSON, nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="SET NULL",
            name="fk_audit_logs_user_id",
        ),
    )
    op.create_index("ix_audit_logs_tenant_id", "audit_logs", ["tenant_id"])
    op.create_index(
        "ix_audit_logs_tenant_user", "audit_logs", ["tenant_id", "user_id"]
    )
    op.create_index(
        "ix_audit_logs_occurred_at", "audit_logs", ["occurred_at"]
    )

    # ── 13. llm_call_logs ───────────────────────────────────────
    op.create_table(
        "llm_call_logs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("prompt_hash", sa.String(64), nullable=False),
        sa.Column(
            "tokens_in", sa.Integer, nullable=False, server_default="0"
        ),
        sa.Column(
            "tokens_out", sa.Integer, nullable=False, server_default="0"
        ),
        sa.Column(
            "cached", sa.Boolean, nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "latency_ms", sa.Integer, nullable=False, server_default="0"
        ),
        sa.Column("logged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "tokens_in >= 0 AND tokens_out >= 0",
            name="ck_llm_call_logs_tokens",
        ),
        sa.CheckConstraint("latency_ms >= 0", name="ck_llm_call_logs_latency"),
    )
    op.create_index(
        "ix_llm_call_logs_tenant_id", "llm_call_logs", ["tenant_id"]
    )
    op.create_index(
        "ix_llm_call_logs_tenant_model",
        "llm_call_logs",
        ["tenant_id", "model"],
    )
    op.create_index(
        "ix_llm_call_logs_logged_at", "llm_call_logs", ["logged_at"]
    )
    op.create_index(
        "ix_llm_call_logs_prompt_hash", "llm_call_logs", ["prompt_hash"]
    )


def downgrade() -> None:
    """Drop all 13 tables in reverse FK dependency order."""
    op.drop_table("llm_call_logs")
    op.drop_table("audit_logs")
    op.drop_table("vectors_chunk")
    op.drop_table("vectors_entity")
    op.drop_table("prompts")
    op.drop_table("tag_stats")
    op.drop_table("tag_current")
    op.drop_table("tag_facts")
    op.drop_table("chunks")
    op.drop_table("segments")
    op.drop_table("recordings")
    op.drop_table("users")
    op.drop_table("tenants")
