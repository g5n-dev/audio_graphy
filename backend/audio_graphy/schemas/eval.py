"""Eval REST API schemas (M6 WS-2).

Pydantic models for the 4 endpoints in ``api/eval.py``:
    - POST /api/v1/eval/runs
    - GET  /api/v1/eval/runs/{run_id}
    - GET  /api/v1/eval/runs/{run_id}/report
    - GET  /api/v1/eval/runs

See: docs/m6-architecture.md §4.3.1, docs/m6-prd.md §5.2-5.3.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class EvalRunCreate(BaseModel):
    """Body for ``POST /api/v1/eval/runs``.

    Attributes:
        gold_set_path: Path to the gold set YAML file.
        pipeline: ``"mock"`` (echoes gold; smoke testing) or ``"rag"``
            (real QueryService + extraction).
        judge_enabled: When ``False``, LLM-judge metrics are skipped.
        k: Cutoff for ``context_precision_at_k``.
        position_debias: When ``True``, judge metrics run twice (original
            + reversed retrieved context) and the mean is reported.
        metadata: Optional free-form metadata stamped into ``config``.
    """

    gold_set_path: str = Field(..., min_length=1, description="Path to gold set YAML")
    pipeline: str = Field("mock", pattern="^(mock|rag)$")
    judge_enabled: bool = Field(True, description="Enable LLM-as-judge metrics")
    k: int = Field(5, ge=1, le=50)
    position_debias: bool = Field(True, description="Run judge twice (original + reversed context)")
    metadata: dict[str, str] = Field(default_factory=dict)


class EvalRunCreateResponse(BaseModel):
    """Response for ``POST /api/v1/eval/runs`` (202 Accepted)."""

    run_id: str
    status: str = "pending"
    poll_interval_seconds: int = 5


class EvalRunOut(BaseModel):
    """One EvalRun row (used in GET responses)."""

    id: str
    tenant_id: str
    gold_set_path: str
    pipeline: str
    judge_enabled: bool
    k_value: int
    status: str
    config: dict[str, Any]
    aggregate_metrics: dict[str, Any] | None = None
    report_markdown_path: str | None = None
    report_json_path: str | None = None
    error_message: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


class EvalRunListResponse(BaseModel):
    """Paginated list response for ``GET /api/v1/eval/runs``."""

    items: list[EvalRunOut]
    total: int
    limit: int
    offset: int


__all__ = [
    "EvalRunCreate",
    "EvalRunCreateResponse",
    "EvalRunListResponse",
    "EvalRunOut",
]
