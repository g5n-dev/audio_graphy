"""LLMCallLog ORM model — LLM call instrumentation log.

Records every LLM API call with token counts, latency, and cache hit status.
This is the truth source for "re-tagging cost" analysis (DESIGN.md §15.4).

Column rename notes (MySQL reserved word / clarity):
    - ``at`` -> ``logged_at`` (SQL context ambiguity)
    - ``latency`` -> ``latency_ms`` (explicit unit)

Table: llm_call_logs
Inherits: TenantScopedBase
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from audio_graphy.models.base import TenantScopedBase


class LLMCallLog(TenantScopedBase):
    """LLM 调用日志表 | LLMCallLog — LLM API call instrumentation.

    Each row records a single LLM API call: model used, prompt hash (for
    cache key correlation), token counts, response latency, and whether
    the response was served from cache.

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
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    logged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "tokens_in >= 0 AND tokens_out >= 0",
            name="ck_llm_call_logs_tokens",
        ),
        CheckConstraint("latency_ms >= 0", name="ck_llm_call_logs_latency"),
        Index("ix_llm_call_logs_tenant_model", "tenant_id", "model"),
        Index("ix_llm_call_logs_logged_at", "logged_at"),
        Index("ix_llm_call_logs_prompt_hash", "prompt_hash"),
    )
