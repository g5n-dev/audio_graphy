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
import hashlib
import logging
import uuid
from collections.abc import MutableMapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.models.enums import RecordingStatus
from audio_graphy.models.pipeline import (
    DEFAULT_REQUIRED_PROJECTIONS,
    PIPELINE_RUN_CLAIMABLE_STATES,
    RecordingPipelineRun,
)
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
        enable_adaptive_gleaning: Opt-in quality-gated entity gleaning mode.
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
        enable_adaptive_gleaning: bool = False,
        worker_id: str | None = None,
        lease_seconds: int = 120,
        settings: Any = None,
        audio_crypto: Any = None,
    ) -> None:
        if lease_seconds < 10:
            raise ValueError("lease_seconds must be at least 10")
        self._session_factory = session_factory
        self._bundle = bundle
        self._vector_store = vector_store
        self._graph_stores = graph_stores
        self._file_indexes = file_indexes
        self._working_dir = Path(working_dir)
        self._poll_seconds = poll_seconds
        self._concurrency = concurrency
        self._pii_scrubber = pii_scrubber
        self._enable_adaptive_gleaning = enable_adaptive_gleaning
        # Forwarded to IndexingService: speaker linking needs the voiceprint
        # settings and the key that encrypts vectors at rest.
        self._settings = settings
        self._audio_crypto = audio_crypto
        self._worker_id = worker_id or f"pipeline-{uuid.uuid4().hex}"
        self._lease_seconds = lease_seconds
        self._lock = asyncio.Lock()

    async def poll_once(self) -> int:
        """Process queued recordings once. Returns the number processed.

        The in-process lock bounds one worker instance; the database CAS lease
        is the authority across processes/hosts.
        """
        async with self._lock:
            await self._ensure_legacy_queued_runs()
            claimed = await self._claim_runs()

            if not claimed:
                return 0

            for recording, pipeline_run in claimed:
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
                    enable_adaptive_gleaning=self._enable_adaptive_gleaning,
                    settings=self._settings,
                    audio_crypto=self._audio_crypto,
                )
                try:
                    await svc.run_pipeline(
                        recording,
                        pipeline_run_id=pipeline_run.id,
                        lease_owner=self._worker_id,
                        lease_seconds=self._lease_seconds,
                    )
                except Exception as exc:
                    logger.error(
                        "Pipeline failed for recording %d: %s", recording.id, exc, exc_info=True
                    )

            return len(claimed)

    async def _ensure_legacy_queued_runs(self) -> None:
        """Create deterministic generation-1 runs for pre-0029 queued rows."""
        try:
            async with self._session_factory() as session, session.begin():
                candidates = list(
                    (
                        await session.execute(
                            select(Recording)
                            .where(Recording.status == RecordingStatus.QUEUED.value)
                            .order_by(Recording.created_at)
                            .limit(self._concurrency)
                            .with_for_update(skip_locked=True)
                        )
                    ).scalars()
                )
                for recording in candidates:
                    existing = (
                        await session.execute(
                            select(func.count(RecordingPipelineRun.id)).where(
                                RecordingPipelineRun.recording_id == recording.id,
                                RecordingPipelineRun.tenant_id == recording.tenant_id,
                                RecordingPipelineRun.state.in_(
                                    tuple(PIPELINE_RUN_CLAIMABLE_STATES)
                                ),
                            )
                        )
                    ).scalar_one()
                    if existing:
                        continue
                    latest = (
                        await session.execute(
                            select(func.max(RecordingPipelineRun.generation)).where(
                                RecordingPipelineRun.recording_id == recording.id
                            )
                        )
                    ).scalar_one_or_none()
                    generation = int(latest or 0) + 1
                    source_fingerprint = (
                        recording.audio_sha256
                        or hashlib.sha256(
                            f"{recording.path}:{recording.source_revision}".encode()
                        ).hexdigest()
                    )
                    session.add(
                        RecordingPipelineRun(
                            tenant_id=str(recording.tenant_id),
                            recording_id=recording.id,
                            generation=generation,
                            idempotency_key=(
                                f"pipeline:{recording.tenant_id}:{recording.id}:{generation}"
                            ),
                            source_fingerprint=source_fingerprint,
                            config_fingerprint=hashlib.sha256(
                                (
                                    f"recording-generation-v1:{recording.prompt_version or ''}"
                                ).encode()
                            ).hexdigest(),
                            state="queued",
                            required_projections=list(DEFAULT_REQUIRED_PROJECTIONS),
                            completed_projections=[],
                        )
                    )
        except IntegrityError:
            # Another worker won the deterministic unique generation/key.
            logger.debug("Concurrent worker created the legacy pipeline run first")

    async def _claim_runs(
        self,
    ) -> list[tuple[Recording, RecordingPipelineRun]]:
        """CAS-claim runnable generations and attach a renewable lease."""
        now = datetime.now(UTC)
        lease_expires_at = now + timedelta(seconds=self._lease_seconds)
        eligible = or_(
            RecordingPipelineRun.state == "queued",
            and_(
                RecordingPipelineRun.state.in_(tuple(PIPELINE_RUN_CLAIMABLE_STATES - {"queued"})),
                or_(
                    RecordingPipelineRun.lease_expires_at.is_(None),
                    RecordingPipelineRun.lease_expires_at <= now,
                ),
            ),
        )
        async with self._session_factory() as session, session.begin():
            candidate_ids = list(
                (
                    await session.execute(
                        select(RecordingPipelineRun.id)
                        .where(eligible)
                        .order_by(RecordingPipelineRun.created_at)
                        .limit(self._concurrency)
                    )
                ).scalars()
            )
            claimed_ids: list[int] = []
            for run_id in candidate_ids:
                result = await session.execute(
                    update(RecordingPipelineRun)
                    .where(
                        RecordingPipelineRun.id == run_id,
                        eligible,
                    )
                    .values(
                        state="claimed",
                        lease_owner=self._worker_id,
                        lease_expires_at=lease_expires_at,
                        attempt_count=RecordingPipelineRun.attempt_count + 1,
                        started_at=func.coalesce(
                            RecordingPipelineRun.started_at,
                            now,
                        ),
                    )
                )
                if getattr(result, "rowcount", 0) == 1:
                    claimed_ids.append(int(run_id))

            claimed: list[tuple[Recording, RecordingPipelineRun]] = []
            for run_id in claimed_ids:
                run = await session.get(RecordingPipelineRun, run_id)
                if run is None:
                    continue
                recording = await session.get(Recording, run.recording_id)
                if recording is None:
                    run.state = "failed_terminal"
                    run.error_code = "RECORDING_MISSING"
                    run.finished_at = now
                    continue
                claimed.append((recording, run))
            return claimed

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
            the raw ``build_adapters(get_settings())`` result is wrapped by
            ``build_llm_runtime`` before any production LLM call.
        settings: Optional Settings instance. When ``None``, ``get_settings()``.
    """
    log = logging.getLogger("audio_graphy.scheduler.eval")
    owned_engine: Any | None = None
    runtime: Any | None = None

    if settings is None:
        from audio_graphy.config import get_settings

        settings = get_settings()
    try:
        if session_factory is None:
            from audio_graphy.db import create_db_engine, create_session_factory

            owned_engine = create_db_engine(settings)  # type: ignore[arg-type]
            session_factory = create_session_factory(owned_engine)

        if bundle is None:
            from audio_graphy.config import build_adapters
            from audio_graphy.services.llm_runtime import build_llm_runtime

            raw_bundle = build_adapters(settings)  # type: ignore[arg-type]
            runtime = await build_llm_runtime(
                settings,  # type: ignore[arg-type]
                session_factory,
                raw_bundle,
            )
            bundle = runtime.bundle

        await _execute_eval_job(
            run_id,
            tenant_id,
            session_factory=session_factory,
            bundle=bundle,
            settings=settings,
            log=log,
        )
    finally:
        if runtime is not None:
            await _close_eval_resource(runtime, "aclose", "LLM runtime", log)
        if owned_engine is not None:
            await _close_eval_resource(owned_engine, "dispose", "database engine", log)


async def _close_eval_resource(
    resource: Any,
    method_name: str,
    label: str,
    log: logging.Logger,
) -> None:
    """Close an owned eval resource without masking the job outcome."""
    close = getattr(resource, method_name, None)
    if not callable(close):
        return
    try:
        await close()
    except Exception:
        log.warning("Eval %s cleanup failed", label, exc_info=True)


async def _execute_eval_job(
    run_id: str,
    tenant_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    bundle: AdapterBundle,
    settings: object,
    log: logging.Logger,
) -> None:
    """Execute one eval using already-resolved dependencies."""
    from pathlib import Path as _Path

    from audio_graphy.eval.reporter import to_json, to_markdown
    from audio_graphy.eval.runner import EvalRunner, MockPipeline
    from audio_graphy.eval.state import EvalRunState

    state = EvalRunState(session_factory)

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

    await state.transition_to(run_id, "running")

    try:
        gold_path = _Path(run.gold_set_path)
        pipeline_type = run.pipeline
        pipeline: Any

        if pipeline_type == "mock":
            pipeline = MockPipeline(precision=1.0)
        elif pipeline_type == "rag":
            from audio_graphy.eval.runner import RAGPipeline
            from audio_graphy.storage.graph_networkx import NetworkXGraphStore

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

        report_working = _Path(str(getattr(settings, "working_dir", ".")))
        report_dir = report_working / "eval_reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        md_path = report_dir / f"eval_run_{run_id}.md"
        json_path = report_dir / f"eval_run_{run_id}.json"
        to_markdown(eval_run, md_path)
        to_json(eval_run, json_path)

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
        err_msg = repr(exc)[:8000]
        log.error("EvalRun %s failed: %s", run_id, exc, exc_info=True)
        await state.transition_to(
            run_id,
            "failed",
            error_message=err_msg,
        )
