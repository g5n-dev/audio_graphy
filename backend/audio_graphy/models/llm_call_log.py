"""LLMCallLog ORM model — LLM call instrumentation log.

Records logical LLM requests and each provider attempt with token counts,
latency, outcome, and cache status.
This is the truth source for "re-tagging cost" analysis (DESIGN.md §15.4).

Column rename notes (MySQL reserved word / clarity):
    - ``at`` -> ``logged_at`` (SQL context ambiguity)
    - ``latency`` -> ``latency_ms`` (explicit unit)

Table: llm_call_logs
Inherits: TenantScopedBase
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from audio_graphy.models.base import TenantScopedBase


class LLMCallLog(TenantScopedBase):
    """LLM 调用日志表 | LLMCallLog — LLM API call instrumentation.

    Each row records either one logical request or one provider attempt.
    ``event_kind`` makes those layers distinguishable while ``attempt`` and
    ``error_type`` retain retry diagnostics without adding metric labels.

    Column rename notes:
        - ``at`` -> ``logged_at``: avoid SQL context ambiguity.
        - ``latency`` -> ``latency_ms``: explicit unit (milliseconds).

    Key constraints:
        - CHECK(tokens_in >= 0 AND tokens_out >= 0): non-negative tokens.
        - CHECK(latency_ms >= 0): non-negative latency.
        - INDEX(tenant_id, model): per-model statistics.
        - INDEX(logged_at): time-range queries.
        - INDEX(prompt_hash): cache analysis.
    """

    __tablename__ = "llm_call_logs"

    model: Mapped[str] = mapped_column(String(64), nullable=False)
    logical_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_attempt_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    model_tier: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="other",
        server_default="other",
    )
    requested_max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tagger_version_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    deployment_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    evaluation_run_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    optimization_run_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    optimization_trial_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    event_kind: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="logical_request",
        server_default="logical_request",
    )
    outcome: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="success",
        server_default="success",
    )
    attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    purpose: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="legacy",
        server_default="legacy",
    )
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_prefill_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    counterfactual_saved_input_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    counterfactual_saved_output_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    counterfactual_saved_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    cost_microunits: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
    )
    price_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    retry_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cache_source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="provider",
        server_default="provider",
    )
    provider_called: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("1"),
    )
    cache_lookup_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cache_miss_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    unknown_billed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("0"),
    )
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    logged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "tokens_in >= 0 AND tokens_out >= 0",
            name="ck_llm_call_logs_tokens",
        ),
        CheckConstraint("latency_ms >= 0", name="ck_llm_call_logs_latency"),
        CheckConstraint(
            "event_kind IN ('logical_request', 'provider_attempt')",
            name="ck_llm_call_logs_event_kind",
        ),
        CheckConstraint(
            "event_kind != 'provider_attempt' OR provider_attempt_id IS NOT NULL",
            name="ck_llm_call_logs_attempt_identity",
        ),
        CheckConstraint(
            "outcome IN ('success', 'error', 'cancelled')",
            name="ck_llm_call_logs_outcome",
        ),
        CheckConstraint(
            "attempt IS NULL OR attempt >= 1",
            name="ck_llm_call_logs_attempt",
        ),
        CheckConstraint(
            "model_tier IN ('strong', 'weak', 'other')",
            name="ck_llm_call_logs_model_tier",
        ),
        CheckConstraint(
            "requested_max_tokens IS NULL OR requested_max_tokens >= 1",
            name="ck_llm_call_logs_requested_max_tokens",
        ),
        CheckConstraint(
            "cached_prefill_tokens >= 0 "
            "AND counterfactual_saved_input_tokens >= 0 "
            "AND counterfactual_saved_output_tokens >= 0 "
            "AND counterfactual_saved_tokens >= 0 "
            "AND cost_microunits >= 0",
            name="ck_llm_call_logs_usage_ledger",
        ),
        UniqueConstraint(
            "tenant_id",
            "provider_attempt_id",
            name="ux_llm_call_logs_provider_attempt",
        ),
        Index("ix_llm_call_logs_tenant_model", "tenant_id", "model"),
        Index("ix_llm_call_logs_logged_at", "logged_at"),
        Index("ix_llm_call_logs_prompt_hash", "prompt_hash"),
        Index(
            "ix_llm_call_logs_logical_request",
            "tenant_id",
            "logical_request_id",
        ),
    )
