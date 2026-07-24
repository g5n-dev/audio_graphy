"""APScheduler pipeline worker — polls queued recordings and processes them.

Uses in-process ``BackgroundScheduler`` (C3 decision). The worker polls
the DB for ``status=queued`` recordings every ``pipeline_poll_seconds``
and runs the indexing pipeline with ``pipeline_concurrency`` limit.

M6 WS-2: also exposes ``run_eval_job`` — a standalone async callable
invoked by APScheduler in response to ``POST /api/v1/eval/runs``. The
job loads an ``EvalRunORM``, builds the appropriate ``EvalPipeline``
(``MockPipeline`` or ``RAGPipeline``), runs ``EvalRunner``, and persists
the result via ``EvalRunState``.

See: docs/m3-architecture.md §1.1 (C3), §4.2, docs/m3-prd.md TAG-08,
     docs/m6-architecture.md §4.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import MutableMapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.models.enums import RecordingStatus
from audio_graphy.models.recording import Recording

if TYPE_CHECKING:
    from audio_graphy.adapters.bundle import AdapterBundle
    from audio_graphy.core.pii import PIIScrubber
    from audio_graphy.storage.file_index import FileIndex
    from audio_graphy.storage.graph_networkx import NetworkXGraphStore
    from audio_graphy.storage.mysql_vector import MySQLVectorStore

logger = logging.getLogger(__name__)


class PipelineWorker:
    """Background pipeline worker that processes queued recordings.

    Args:
        session_factory: async session maker.
        bundle: AdapterBundle.
        vector_store: Global MySQLVectorStore.
        graph_stores: Dict of tenant_id -> NetworkXGraphStore.
        file_indexes: Dict of tenant_id -> FileIndex.
        poll_seconds: Polling interval.
        concurrency: Max concurrent pipeline executions.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        bundle: AdapterBundle,
        vector_store: MySQLVectorStore,
        graph_stores: MutableMapping[str, NetworkXGraphStore],
        file_indexes: dict[str, FileIndex],
        *,
        working_dir: str = "/data/working_dir",
        poll_seconds: int = 5,
        concurrency: int = 1,
        pii_scrubber: PIIScrubber | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._bundle = bundle
        self._vector_store = vector_store
        self._graph_stores = graph_stores
        self._file_indexes = file_indexes
        self._working_dir = Path(working_dir)
        self._poll_seconds = poll_seconds
        self._concurrency = concurrency
        self._pii_scrubber = pii_scrubber
        self._lock = asyncio.Lock()

    async def poll_once(self) -> int:
        """Process queued recordings once. Returns the number processed.

        Acquires a lock to enforce concurrency=1 (C5 decision).
        """
        async with self._lock:
            async with self._session_factory() as session:
                stmt = (
                    select(Recording)
                    .where(Recording.status == RecordingStatus.QUEUED.value)
                    .order_by(Recording.created_at)
                    .limit(self._concurrency)
                )
                result = await session.execute(stmt)
                recordings = list(result.scalars().all())

            if not recordings:
                return 0

            for recording in recordings:
                # Get per-tenant stores
                tenant_id = str(recording.tenant_id)
                graph_store = self._graph_stores.get(tenant_id)
                file_index = self._file_indexes.get(tenant_id)

                if graph_store is None:
                    from audio_graphy.storage.graph_networkx import NetworkXGraphStore

                    graph_store = NetworkXGraphStore(
                        self._working_dir,
                        tenant_id=tenant_id,
                    )
                    self._graph_stores[tenant_id] = graph_store

                if file_index is None:
                    from audio_graphy.storage.file_index import FileIndex

                    file_index = FileIndex(
                        self._working_dir,
                        tenant_id=tenant_id,
                    )
                    self._file_indexes[tenant_id] = file_index

                from audio_graphy.services.indexing import IndexingService

                svc = IndexingService(
                    self._session_factory,
                    self._bundle,
                    self._vector_store,
                    graph_store,
                    file_index,
                    pii_scrubber=self._pii_scrubber,
                )
                try:
                    await svc.run_pipeline(recording)
                except Exception as exc:
                    logger.error(
                        "Pipeline failed for recording %d: %s", recording.id, exc, exc_info=True
                    )

            return len(recordings)

    async def start_loop(self) -> None:
        """Start the polling loop (runs forever)."""
        logger.info(
            "Pipeline worker started (poll=%ds, concurrency=%d)",
            self._poll_seconds,
            self._concurrency,
        )
        while True:
            try:
                processed = await self.poll_once()
                if processed > 0:
                    logger.info("Pipeline worker processed %d recordings", processed)
            except Exception as exc:
                logger.error("Pipeline worker poll error: %s", exc, exc_info=True)
            await asyncio.sleep(self._poll_seconds)


def create_scheduler(
    worker: PipelineWorker,
    *,
    poll_seconds: int = 5,
) -> BackgroundScheduler:
    """Create and configure an APScheduler BackgroundScheduler.

    The scheduler runs the pipeline worker's ``poll_once`` on a fixed interval.

    Args:
        worker: The PipelineWorker instance.
        poll_seconds: Polling interval in seconds.

    Returns:
        A configured (but not started) BackgroundScheduler.
    """
    scheduler = BackgroundScheduler()

    # We need to run async code from a sync scheduler callback.
    # We use a dedicated event loop for the worker.
    _worker_loop: asyncio.AbstractEventLoop | None = None

    def _run_poll() -> None:
        nonlocal _worker_loop
        if _worker_loop is None or _worker_loop.is_closed():
            _worker_loop = asyncio.new_event_loop()
        try:
            _worker_loop.run_until_complete(worker.poll_once())
        except Exception as exc:
            logger.error("Scheduler poll error: %s", exc, exc_info=True)

    scheduler.add_job(
        _run_poll,
        "interval",
        seconds=poll_seconds,
        id="pipeline_poll",
        replace_existing=True,
    )

    return scheduler


# ============================================================
# M6 WS-2 — eval run background job
# ============================================================


async def run_eval_job(
    run_id: str,
    tenant_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    bundle: AdapterBundle | None = None,
    settings: object | None = None,
) -> None:
    """Background eval — load EvalRunORM → build pipeline → EvalRunner → persist.

    Designed to be invoked by APScheduler in response to
    ``POST /api/v1/eval/runs``. ALL exceptions are caught and persisted
    to ``eval_runs.error_message`` (status='failed') so the API never
    hangs on a pending row.

    Args:
        run_id: Target eval run ID (UUID hex).
        tenant_id: Tenant scope (must match the row's tenant_id).
        session_factory: Optional async session maker (for testing).
            When ``None``, reads from ``get_settings`` + ``create_db_engine``.
        bundle: Optional AdapterBundle (for testing). When ``None``,
            ``build_adapters(get_settings())`` is used.
        settings: Optional Settings instance. When ``None``, ``get_settings()``.
    """
    import logging as _logging
    from pathlib import Path as _Path

    from audio_graphy.eval.reporter import to_json, to_markdown
    from audio_graphy.eval.runner import EvalRunner, MockPipeline
    from audio_graphy.eval.state import EvalRunState

    log = _logging.getLogger("audio_graphy.scheduler.eval")

    # Resolve dependencies.
    if settings is None:
        from audio_graphy.config import get_settings

        settings = get_settings()
    if session_factory is None:
        from audio_graphy.db import create_db_engine, create_session_factory

        engine = create_db_engine(settings)  # type: ignore[arg-type]
        session_factory = create_session_factory(engine)
    if bundle is None:
        from audio_graphy.config import build_adapters

        bundle = build_adapters(settings)  # type: ignore[arg-type]

    state = EvalRunState(session_factory)

    # 1. Load + claim.
    try:
        run = await state.get(run_id, tenant_id)
    except Exception as exc:
        log.error("EvalRun %s load failed: %s", run_id, exc, exc_info=True)
        return
    if run is None:
        log.warning("EvalRun %s not found in tenant %s — dropping", run_id, tenant_id)
        return
    if run.status in ("completed", "failed"):
        log.info("EvalRun %s already %s — skipping", run_id, run.status)
        return

    # Transition to running.
    await state.transition_to(run_id, "running")

    try:
        # 2. Build pipeline.
        gold_path = _Path(run.gold_set_path)
        pipeline_type = run.pipeline
        pipeline: Any

        if pipeline_type == "mock":
            pipeline = MockPipeline(precision=1.0)
        elif pipeline_type == "rag":
            from audio_graphy.eval.runner import RAGPipeline
            from audio_graphy.storage.graph_networkx import NetworkXGraphStore

            # Use the tenant-scoped graph store from app state if available;
            # otherwise build a fresh one for this run.
            working_dir = _Path(str(getattr(settings, "working_dir", ".")))
            graph_store = NetworkXGraphStore(
                working_dir,
                tenant_id=tenant_id,
            )
            user_id_raw = run.config.get("user_id")
            pipeline = RAGPipeline(
                settings=settings,  # type: ignore[arg-type]
                tenant_id=tenant_id,
                user_id=int(user_id_raw) if user_id_raw is not None else None,
                bundle=bundle,
                session_factory=session_factory,
                graph_store=graph_store,
            )
        else:
            raise ValueError(f"Unknown pipeline type: {pipeline_type!r}")

        # 3. Build judge (or None).
        judge = None
        if run.judge_enabled:
            try:
                from audio_graphy.eval.judge import LLMJudge

                judge = LLMJudge(llm=bundle.strong_llm)
            except Exception as exc:
                log.warning(
                    "EvalRun %s: judge init failed (%s); proceeding without judge",
                    run_id,
                    exc,
                )
                judge = None

        # 4. Run EvalRunner.
        position_debias = bool(run.config.get("position_debias", True))
        runner = EvalRunner(
            gold_set_path=gold_path,
            pipeline=pipeline,
            judge=judge,
            settings=settings,  # type: ignore[arg-type]
            k=int(run.k_value),
            position_debias=position_debias,
        )
        eval_run = await runner.run()

        # 5. Reporter writes files.
        report_working = _Path(str(getattr(settings, "working_dir", ".")))
        report_dir = report_working / "eval_reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        md_path = report_dir / f"eval_run_{run_id}.md"
        json_path = report_dir / f"eval_run_{run_id}.json"
        to_markdown(eval_run, md_path)
        to_json(eval_run, json_path)

        # 6. Persist completion.
        await state.transition_to(
            run_id,
            "completed",
            aggregate_metrics=dict(eval_run.aggregate_metrics),
            report_markdown_path=str(md_path),
            report_json_path=str(json_path),
        )
        log.info(
            "EvalRun %s completed: metrics=%s",
            run_id,
            list(eval_run.aggregate_metrics.keys()),
        )

    except Exception as exc:
        # 7. Persist failure (truncate to fit column width).
        err_msg = repr(exc)[:8000]
        log.error("EvalRun %s failed: %s", run_id, exc, exc_info=True)
        await state.transition_to(
            run_id,
            "failed",
            error_message=err_msg,
        )
