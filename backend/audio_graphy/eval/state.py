"""EvalRunState — DB-backed state machine for async eval runs.

Wraps ``EvalRunORM`` CRUD so that the REST API and the APScheduler
worker share one writer path. All reads and writes are tenant-scoped
(``tenant_id`` column on every query).

State transitions (architecture §4.2.1)::

        POST /runs
            │
            ▼
        ┌────────┐  scheduler claim_next_pending
        │pending │─────────────────────────────►┌────────┐
        └────────┘                              │running │
                                                └────────┘
                                                    │
                                       pipeline    │   pipeline
                                       success     │   crash
                                            ▼      ▼      ▼
                                       ┌─────────┐  ┌──────┐
                                       │completed│  │failed│
                                       └─────────┘  └──────┘
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.models.eval_run import EvalRunORM

logger = logging.getLogger(__name__)

_VALID_TRANSITION_TARGETS = frozenset({"pending", "running", "completed", "failed"})


class EvalRunState:
    """DB-backed lifecycle tracker for ``EvalRunORM``.

    The single writer for the ``eval_runs`` table: REST endpoints create
    rows, the scheduler worker transitions them through running →
    completed | failed. Read paths are also tenant-scoped.

    Args:
        session_factory: async session maker bound to the DB.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # create — POST /runs
    # ------------------------------------------------------------------
    async def create(
        self,
        *,
        gold_set_path: str,
        pipeline: str,
        judge_enabled: bool,
        k: int,
        tenant_id: str,
        user_id: int | None,
        config: dict[str, Any] | None = None,
    ) -> str:
        """Insert a new EvalRun row with ``status='pending'``.

        Args:
            gold_set_path: Path to the gold set YAML file.
            pipeline: ``"mock"`` | ``"rag"``.
            judge_enabled: Whether LLM-as-judge metrics are enabled.
            k: Cutoff for ``context_precision_at_k``.
            tenant_id: Tenant scope.
            user_id: Initiating user ID (None for system).
            config: Free-form config snapshot (metadata, etc.).

        Returns:
            New run_id (UUID4 hex string).
        """
        if pipeline not in ("mock", "rag"):
            raise ValueError(f"pipeline must be 'mock' or 'rag', got {pipeline!r}")
        run_id = uuid.uuid4().hex
        # Stamp tenant + user + k + judge into config so the report can
        # render them without joining other tables.
        effective_config: dict[str, Any] = {
            "k": int(k),
            "judge_enabled": bool(judge_enabled),
            "pipeline": pipeline,
            "user_id": user_id,
        }
        if config:
            effective_config.update(config)

        async with self._session_factory() as session:
            run = EvalRunORM(
                id=run_id,
                tenant_id=tenant_id,
                gold_set_path=str(gold_set_path),
                pipeline=pipeline,
                judge_enabled=bool(judge_enabled),
                k_value=int(k),
                status="pending",
                config=effective_config,
                started_at=datetime.now(UTC),
            )
            session.add(run)
            await session.commit()
        logger.info(
            "EvalRun created: id=%s tenant=%s pipeline=%s gold=%s",
            run_id,
            tenant_id,
            pipeline,
            gold_set_path,
        )
        return run_id

    # ------------------------------------------------------------------
    # transition_to — used by scheduler worker
    # ------------------------------------------------------------------
    async def transition_to(
        self,
        run_id: str,
        status: str,
        *,
        aggregate_metrics: dict[str, float] | None = None,
        report_markdown_path: str | Path | None = None,
        report_json_path: str | Path | None = None,
        error_message: str | None = None,
    ) -> None:
        """Transition a run to ``status`` with optional updates.

        Unknown status values raise ``ValueError``. Setting
        ``status='completed'`` or ``'failed'`` stamps ``finished_at``.

        Args:
            run_id: Target run ID.
            status: Next status (must be in ``_VALID_TRANSITION_TARGETS``).
            aggregate_metrics: Mean metrics (only on completion).
            report_markdown_path: Path to the Markdown report file.
            report_json_path: Path to the JSON report file.
            error_message: Failure detail (only on failure).
        """
        if status not in _VALID_TRANSITION_TARGETS:
            raise ValueError(
                f"status must be one of {sorted(_VALID_TRANSITION_TARGETS)}, got {status!r}"
            )
        updates: dict[str, Any] = {"status": status}
        if aggregate_metrics is not None:
            updates["aggregate_metrics"] = aggregate_metrics
        if report_markdown_path is not None:
            updates["report_markdown_path"] = str(report_markdown_path)
        if report_json_path is not None:
            updates["report_json_path"] = str(report_json_path)
        if error_message is not None:
            updates["error_message"] = error_message[:8000]
        if status in ("completed", "failed"):
            updates["finished_at"] = datetime.now(UTC)

        async with self._session_factory() as session:
            await session.execute(
                update(EvalRunORM).where(EvalRunORM.id == run_id).values(**updates)
            )
            await session.commit()
        logger.info("EvalRun transitioned: id=%s status=%s", run_id, status)

    # ------------------------------------------------------------------
    # get — GET /runs/{id}
    # ------------------------------------------------------------------
    async def get(
        self,
        run_id: str,
        tenant_id: str,
    ) -> EvalRunORM | None:
        """Fetch one run by ID, scoped to ``tenant_id``.

        Returns ``None`` if the run does not exist OR exists but belongs
        to a different tenant (tenant isolation).
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(EvalRunORM).where(
                    EvalRunORM.id == run_id,
                    EvalRunORM.tenant_id == tenant_id,
                )
            )
            return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # list — GET /runs
    # ------------------------------------------------------------------
    async def list(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[EvalRunORM], int]:
        """Paginated, tenant-scoped list with optional status filter.

        Returns:
            Tuple of (runs, total_count).
        """
        conditions = [EvalRunORM.tenant_id == tenant_id]
        if status is not None:
            conditions.append(EvalRunORM.status == status)

        async with self._session_factory() as session:
            base = select(EvalRunORM).where(*conditions)
            total = (
                await session.execute(select(func.count()).select_from(base.subquery()))
            ).scalar_one()
            rows = (
                (
                    await session.execute(
                        base.order_by(EvalRunORM.started_at.desc())
                        .limit(max(1, int(limit)))
                        .offset(max(0, int(offset)))
                    )
                )
                .scalars()
                .all()
            )
            return list(rows), int(total)


__all__ = ["EvalRunState"]
