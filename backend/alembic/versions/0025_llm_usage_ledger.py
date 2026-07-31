"""Add canonical LLM usage-ledger dimensions and accounting fields.

Revision ID: 0025_llm_usage_ledger
Revises: 0024_sealed_release_budget
Create Date: 2026-07-27 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025_llm_usage_ledger"
down_revision: str | None = "0024_sealed_release_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # All new attribution fields are nullable so the deployment is compatible
    # with the current gateway Observation contract and historical rows.
    for column in (
        sa.Column("logical_request_id", sa.String(length=64), nullable=True),
        sa.Column("provider_attempt_id", sa.String(length=96), nullable=True),
        sa.Column(
            "model_tier",
            sa.String(length=16),
            server_default="other",
            nullable=False,
        ),
        sa.Column("requested_max_tokens", sa.Integer(), nullable=True),
        sa.Column("tagger_version_id", sa.BigInteger(), nullable=True),
        sa.Column("deployment_id", sa.BigInteger(), nullable=True),
        sa.Column("evaluation_run_id", sa.BigInteger(), nullable=True),
        sa.Column("optimization_run_id", sa.BigInteger(), nullable=True),
        sa.Column("optimization_trial_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "cached_prefill_tokens",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "counterfactual_saved_input_tokens",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "counterfactual_saved_output_tokens",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "counterfactual_saved_tokens",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "cost_microunits",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("price_version", sa.String(length=64), nullable=True),
        sa.Column("finish_reason", sa.String(length=64), nullable=True),
        sa.Column("provider_request_id", sa.String(length=128), nullable=True),
        sa.Column("retry_class", sa.String(length=32), nullable=True),
        sa.Column("cache_lookup_reason", sa.String(length=64), nullable=True),
        sa.Column("cache_miss_reason", sa.String(length=64), nullable=True),
        sa.Column(
            "unknown_billed",
            sa.Boolean(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    ):
        op.add_column("llm_call_logs", column)

    op.create_check_constraint(
        "ck_llm_call_logs_model_tier",
        "llm_call_logs",
        "model_tier IN ('strong', 'weak', 'other')",
    )
    op.create_check_constraint(
        "ck_llm_call_logs_requested_max_tokens",
        "llm_call_logs",
        "requested_max_tokens IS NULL OR requested_max_tokens >= 1",
    )
    op.create_check_constraint(
        "ck_llm_call_logs_usage_ledger",
        "llm_call_logs",
        "cached_prefill_tokens >= 0 "
        "AND counterfactual_saved_input_tokens >= 0 "
        "AND counterfactual_saved_output_tokens >= 0 "
        "AND counterfactual_saved_tokens >= 0 "
        "AND cost_microunits >= 0",
    )
    op.create_check_constraint(
        "ck_llm_call_logs_attempt_identity",
        "llm_call_logs",
        "event_kind != 'provider_attempt' OR provider_attempt_id IS NOT NULL",
    )
    op.create_unique_constraint(
        "ux_llm_call_logs_provider_attempt",
        "llm_call_logs",
        ["tenant_id", "provider_attempt_id"],
    )
    op.create_index(
        "ix_llm_call_logs_logical_request",
        "llm_call_logs",
        ["tenant_id", "logical_request_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_llm_call_logs_logical_request",
        table_name="llm_call_logs",
    )
    op.drop_constraint(
        "ux_llm_call_logs_provider_attempt",
        "llm_call_logs",
        type_="unique",
    )
    for constraint in (
        "ck_llm_call_logs_attempt_identity",
        "ck_llm_call_logs_usage_ledger",
        "ck_llm_call_logs_requested_max_tokens",
        "ck_llm_call_logs_model_tier",
    ):
        op.drop_constraint(constraint, "llm_call_logs", type_="check")
    for column in (
        "unknown_billed",
        "cache_miss_reason",
        "cache_lookup_reason",
        "retry_class",
        "provider_request_id",
        "finish_reason",
        "price_version",
        "cost_microunits",
        "counterfactual_saved_tokens",
        "counterfactual_saved_output_tokens",
        "counterfactual_saved_input_tokens",
        "cached_prefill_tokens",
        "optimization_trial_id",
        "optimization_run_id",
        "evaluation_run_id",
        "deployment_id",
        "tagger_version_id",
        "requested_max_tokens",
        "model_tier",
        "provider_attempt_id",
        "logical_request_id",
    ):
        op.drop_column("llm_call_logs", column)
