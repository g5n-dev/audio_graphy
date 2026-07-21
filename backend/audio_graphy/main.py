"""FastAPI application entrypoint for AudioGraphy M3.

Registers all routers, middleware, exception handlers, and lifespan hooks
(DB engine + adapter bundle + stores + scheduler initialization).

See: docs/m3-architecture.md §1.2, docs/m3-prd.md API-09.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from audio_graphy.api.auth import router as auth_router
from audio_graphy.api.dsar import router as dsar_router
from audio_graphy.api.eval import router as eval_router
from audio_graphy.api.graph import router as graph_router
from audio_graphy.api.health import router as health_router
from audio_graphy.api.prompts import router as prompts_router
from audio_graphy.api.query import router as query_router
from audio_graphy.api.recordings import router as recordings_router
from audio_graphy.api.segments import router as segments_router
from audio_graphy.api.stats import router as stats_router
from audio_graphy.api.tags import router as tags_router
from audio_graphy.auth.jwt_utils import JWTManager
from audio_graphy.auth.middleware import AuthMiddleware, RequestIdMiddleware
from audio_graphy.config import get_settings
from audio_graphy.errors import register_exception_handlers

logger = logging.getLogger(__name__)

API_VERSION = "v1"
API_PREFIX = f"/api/{API_VERSION}"


def _run_retention_sweep_wrapper(
    enforcer: object,
    session_factory: object,
) -> object:
    """Build a sync-callable wrapper that APScheduler can invoke.

    APScheduler's BackgroundScheduler runs jobs in a thread, so we need a
    bridge to call the async ``run_sweep()``. The wrapper creates a fresh
    event loop, runs the sweep, and tears the loop down. Errors are logged.
    """
    import asyncio

    def _run() -> None:
        async def _go() -> None:
            try:
                report = await enforcer.run_sweep()  # type: ignore[attr-defined]
                logger.info(
                    "Retention sweep: scanned=%d deleted=%d errors=%d duration=%.2fs",
                    report.total_scanned,
                    report.deleted,
                    len(report.errors),
                    report.duration_sec,
                )
                # Flush any audit records queued by the sweep.
                audit = getattr(_go, "_audit", None)
                if audit is not None:
                    await audit.flush()
            except Exception as exc:
                logger.error("Retention sweep failed: %s", exc, exc_info=True)

        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_go())
            finally:
                loop.close()
        except Exception as exc:
            logger.error("Retention sweep event loop failed: %s", exc)

    return _run


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: startup + shutdown hooks.

    Initializes:
        - settings (cached singleton)
        - async DB engine + session factory
        - adapter bundle
        - global MySQLVectorStore
        - per-tenant stores caches (graph_stores, file_indexes)
        - JWT manager
        - APScheduler pipeline worker
    """
    settings = get_settings()
    app.state.settings = settings
    app.state.version = "0.3.0"

    logger.info(
        "AudioGraphy M3 backend starting",
        extra={
            "adapter_mode": settings.adapter_mode,
            "mysql_host": settings.mysql_host,
            "working_dir": str(settings.working_dir),
        },
    )

    # DB engine + session factory (with graceful fallback for test/no-DB environments)
    from audio_graphy.db import create_db_engine, create_session_factory

    try:
        engine = create_db_engine(settings)
        session_factory = create_session_factory(engine)
        app.state.engine = engine
        app.state.session_factory = session_factory
    except Exception as exc:
        logger.warning("DB engine creation failed (continuing without DB): %s", exc)
        app.state.engine = None
        app.state.session_factory = None

    # Adapter bundle
    from audio_graphy.config import build_adapters

    bundle = build_adapters(settings)
    app.state.adapter_bundle = bundle

    # Global vector store
    from audio_graphy.storage.mysql_vector import MySQLVectorStore

    vector_store = MySQLVectorStore(session_factory, dim=settings.embedding_dim)
    app.state.vector_store = vector_store

    # Per-tenant store caches
    from audio_graphy.storage.file_index import FileIndex
    from audio_graphy.storage.graph_networkx import NetworkXGraphStore

    graph_stores: dict[str, NetworkXGraphStore] = {}
    file_indexes: dict[str, FileIndex] = {}
    app.state.graph_stores = graph_stores
    app.state.file_indexes = file_indexes

    # JWT manager
    jwt_manager = JWTManager(
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        exp_hours=settings.jwt_exp_hours,
        refresh_exp_hours=settings.jwt_refresh_exp_hours,
    )
    app.state.jwt_manager = jwt_manager

    # APScheduler pipeline worker
    worker_task: asyncio.Task[None] | None = None
    try:
        from audio_graphy.scheduler import PipelineWorker

        worker = PipelineWorker(
            session_factory,
            bundle,
            vector_store,
            graph_stores,
            file_indexes,
            working_dir=str(settings.working_dir),
            poll_seconds=settings.pipeline_poll_seconds,
            concurrency=settings.pipeline_concurrency,
        )
        # Run the worker as an async background task
        worker_task = asyncio.create_task(worker.start_loop())
        app.state.pipeline_worker = worker
    except Exception as exc:
        logger.warning("Failed to start pipeline worker: %s", exc)

    # ---- M6 PIPL §14.3 wiring ------------------------------------------
    # AudioCrypto (master key path from settings; dev_mode in DEBUG).
    from pathlib import Path

    from audio_graphy.core.crypto import AudioCrypto
    from audio_graphy.core.pii import PIIScrubber

    master_key_path_setting = getattr(settings, "master_key_path", None)
    if master_key_path_setting is None:
        # Default location (see docs/deployment.md).
        master_key_path_setting = Path("/run/secrets/audiography_master.key")
    master_key_path = Path(str(master_key_path_setting))
    dev_mode = settings.log_level.upper() == "DEBUG"
    audio_crypto: AudioCrypto | None
    # ASYNC240: Path.exists() is blocking; wrap with anyio for true async.
    # In startup lifespan the event loop is ours, so a synchronous probe
    # of a tiny filesystem entry is acceptable. Disable the rule locally.
    key_present = master_key_path.exists()  # noqa: ASYNC240
    if key_present or dev_mode:
        try:
            audio_crypto = AudioCrypto(master_key_path, dev_mode=dev_mode)
            # Touch lazy loader so any key-format errors surface at startup.
            _ = audio_crypto.encrypt_file
            app.state.audio_crypto = audio_crypto
        except Exception as exc:
            logger.warning(
                "AudioCrypto init failed (%s); encryption disabled", exc
            )
            audio_crypto = None
            app.state.audio_crypto = None
    else:
        logger.info(
            "Master key not present at %s — audio encryption disabled. "
            "Set AUDIOGRAPHY_MASTER_KEY_PATH to enable (PIPL §14.3).",
            master_key_path,
        )
        audio_crypto = None
        app.state.audio_crypto = None

    # PIIScrubber (stateless; cheap to instantiate).
    pii_scrubber = PIIScrubber()
    app.state.pii_scrubber = pii_scrubber

    # AuditWriter (async-batched; lifespan-managed).
    from audio_graphy.core.audit import AuditWriter

    audit_writer: AuditWriter | None = None
    if session_factory is not None:
        try:
            audit_writer = AuditWriter(session_factory)
            await audit_writer.start()
            app.state.audit_writer = audit_writer
        except Exception as exc:
            logger.warning("AuditWriter init failed: %s", exc)
            app.state.audit_writer = None
    else:
        app.state.audit_writer = None

    # RetentionEnforcer — registered with APScheduler (daily 03:00 cron).
    if audit_writer is not None and audio_crypto is not None and session_factory is not None:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger

            from audio_graphy.core.retention import RetentionEnforcer

            def _gs_factory(tenant_id: str) -> object | None:
                return graph_stores.get(tenant_id)

            retention_enforcer = RetentionEnforcer(
                session_factory,
                audio_crypto,
                audit_writer,
                _gs_factory,  # type: ignore[arg-type]
            )
            app.state.retention_enforcer = retention_enforcer

            retention_scheduler = BackgroundScheduler(daemon=True)
            retention_scheduler.add_job(
                _run_retention_sweep_wrapper(retention_enforcer, session_factory),
                trigger=CronTrigger(hour=3, minute=0, timezone="Asia/Shanghai"),
                id="retention_daily",
                coalesce=True,
                max_instances=1,
                replace_existing=True,
            )
            retention_scheduler.start()
            app.state.retention_scheduler = retention_scheduler
        except Exception as exc:
            logger.warning("RetentionEnforcer wiring failed: %s", exc)

    yield

    # Shutdown
    if worker_task is not None:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task

    # M6: shut down AuditWriter (flushes remaining queue) + retention scheduler.
    audit_writer = getattr(app.state, "audit_writer", None)
    if audit_writer is not None:
        with contextlib.suppress(Exception):
            await audit_writer.aclose()
    retention_scheduler = getattr(app.state, "retention_scheduler", None)
    if retention_scheduler is not None:
        with contextlib.suppress(Exception):
            retention_scheduler.shutdown(wait=False)

    # Close real adapter httpx clients (mock adapters have no aclose()).
    # Real adapters (Silero/LLM/BGE) own httpx.AsyncClient pools; failing to
    # close them triggers an event-loop warning on shutdown.
    for adapter in (bundle.vad, bundle.asr, bundle.strong_llm, bundle.weak_llm, bundle.embed):
        aclose = getattr(adapter, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()

    if app.state.engine is not None:
        await app.state.engine.dispose()
    logger.info("AudioGraphy M3 backend shutting down")


def create_app() -> FastAPI:
    """Application factory."""
    settings = get_settings()

    app = FastAPI(
        title="AudioGraphy",
        description=(
            "门店录音图谱检索与多级打标系统 | Store Recording Graph Retrieval & Multi-level Tagging"
        ),
        version="0.3.0",
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # M6 Q3: Prometheus metrics middleware + /metrics router.
    # Registered before other middleware so it sees ALL responses including
    # CORS preflight and auth failures.
    from audio_graphy.api.metrics import register_metrics

    register_metrics(app)

    # Request ID middleware (must be first to set request_id)
    app.add_middleware(RequestIdMiddleware)

    # Auth middleware
    jwt_manager = JWTManager(
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        exp_hours=settings.jwt_exp_hours,
        refresh_exp_hours=settings.jwt_refresh_exp_hours,
    )
    app.add_middleware(AuthMiddleware, jwt_manager=jwt_manager)

    # Exception handlers
    register_exception_handlers(app)

    # Register routers with /api/v1 prefix
    # Health router is registered WITHOUT prefix (health/readiness has no prefix)
    app.include_router(health_router)
    app.include_router(auth_router, prefix=API_PREFIX)
    app.include_router(recordings_router, prefix=API_PREFIX)
    app.include_router(segments_router, prefix=API_PREFIX)
    app.include_router(query_router, prefix=API_PREFIX)
    app.include_router(graph_router, prefix=API_PREFIX)
    app.include_router(tags_router, prefix=API_PREFIX)
    app.include_router(prompts_router, prefix=API_PREFIX)
    app.include_router(stats_router, prefix=API_PREFIX)
    app.include_router(dsar_router, prefix=API_PREFIX)
    app.include_router(eval_router, prefix=API_PREFIX)

    # Root redirect
    @app.get("/", tags=["meta"])
    async def root() -> dict[str, str]:
        """Root endpoint."""
        return {"message": "AudioGraphy API", "docs": "/docs", "version": "0.3.0"}

    return app


app = create_app()
