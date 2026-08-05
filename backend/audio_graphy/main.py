"""FastAPI application entrypoint for AudioGraphy M3.

Registers all routers, middleware, exception handlers, and lifespan hooks
(DB engine + adapter bundle + stores + scheduler initialization).

See: docs/m3-architecture.md §1.2, docs/m3-prd.md API-09.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from audio_graphy.api.auth import router as auth_router
from audio_graphy.api.deprecation import LegacyTaggingDeprecationMiddleware
from audio_graphy.api.dsar import router as dsar_router
from audio_graphy.api.eval import router as eval_router
from audio_graphy.api.graph import router as graph_router
from audio_graphy.api.health import router as health_router
from audio_graphy.api.integration_admin import router as integration_admin_router
from audio_graphy.api.open import router as open_router
from audio_graphy.api.prompt_lab import router as prompt_lab_router
from audio_graphy.api.prompts import router as prompts_router
from audio_graphy.api.query import router as query_router
from audio_graphy.api.reception_pipeline import router as reception_pipeline_router
from audio_graphy.api.reception_state_insights import (
    router as reception_state_insights_router,
)
from audio_graphy.api.reception_tags import router as reception_tags_router
from audio_graphy.api.receptions import router as receptions_router
from audio_graphy.api.recordings import router as recordings_router
from audio_graphy.api.segments import router as segments_router
from audio_graphy.api.speakers import recordings_router as recording_speakers_router
from audio_graphy.api.speakers import router as speakers_router
from audio_graphy.api.stats import router as stats_router
from audio_graphy.api.tag_governance import router as tag_governance_router
from audio_graphy.api.tag_insights import router as tag_insights_router
from audio_graphy.api.tags import router as tags_router
from audio_graphy.auth.jwt_utils import JWTManager
from audio_graphy.auth.middleware import (
    AuthMiddleware,
    RequestBodyLimitMiddleware,
    RequestIdMiddleware,
)
from audio_graphy.config import Settings, get_settings
from audio_graphy.errors import register_exception_handlers

if TYPE_CHECKING:
    from audio_graphy.core.crypto import AudioCrypto

logger = logging.getLogger(__name__)

# Bounded so a stuck voiceprint service cannot hold up a deploy; anything
# still running past this is cancelled and its recording stays unlinked,
# which the backfill job can pick up later.
_SPEAKER_LINK_DRAIN_SEC = 30

API_VERSION = "v1"
API_PREFIX = f"/api/{API_VERSION}"


def _record_degradation(app: FastAPI, component: str, exc: BaseException) -> None:
    """Record that an optional subsystem failed to wire up.

    Several lifespan steps are allowed to fail without stopping the process —
    losing the retention scheduler should not take the API down. But logging a
    warning and moving on means the process reports itself healthy while
    silently missing a subsystem. Readiness reads this list, so orchestration
    stops routing traffic to a replica that came up crippled.
    """
    degradations: list[dict[str, str]] = getattr(app.state, "startup_degradations", [])
    degradations.append({"component": component, "error": f"{type(exc).__name__}: {exc}"})
    app.state.startup_degradations = degradations


def _build_audio_crypto(settings: Any) -> AudioCrypto:
    """Construct and eagerly validate the at-rest audio encryption service."""
    from audio_graphy.core.crypto import AudioCrypto

    audio_crypto = AudioCrypto(
        Path(str(settings.master_key_path)),
        dev_mode=settings.log_level.upper() == "DEBUG",
        chunk_size_bytes=settings.audio_crypto_chunk_size_bytes,
        max_plaintext_bytes=settings.max_recording_audio_bytes,
    )
    audio_crypto.validate_master_key()
    return audio_crypto


def _build_graph_store_factory(
    graph_stores: MutableMapping[str, Any],
    working_dir: object,
    *,
    max_entries: int = 64,
) -> Callable[[str], Awaitable[Any]]:
    """Build a cache-reusing factory that cold-loads tenant GraphML."""
    from audio_graphy.storage.graph_networkx import NetworkXGraphStore
    from audio_graphy.storage.tenant_graph_cache import TenantGraphStoreCache

    raw_mapping: MutableMapping[str, Any] | None
    if isinstance(graph_stores, TenantGraphStoreCache):
        cache = graph_stores
        raw_mapping = None
    else:
        cache = TenantGraphStoreCache[Any](max_entries=max_entries)
        for tenant_id, store in graph_stores.items():
            cache[tenant_id] = store
        raw_mapping = graph_stores

    raw_mapping_guard = threading.Lock()

    def _sync_raw_mapping() -> None:
        if raw_mapping is None:
            return
        with raw_mapping_guard:
            raw_mapping.clear()
            raw_mapping.update(cache.snapshot_items())

    _sync_raw_mapping()

    async def _factory(tenant_id: str) -> NetworkXGraphStore:
        def _load() -> NetworkXGraphStore:
            with cache.load_guard(tenant_id):
                store = cache.get(tenant_id)
                created = False
                if store is None:
                    created = True
                    store = NetworkXGraphStore(
                        Path(str(working_dir)),
                        tenant_id=tenant_id,
                    )
                assert store is not None

                try:
                    if not bool(getattr(store, "_loaded", False)):
                        store._sync_load()
                        store._loaded = True
                        store.invalidate_path_projection()
                except Exception:
                    if created:
                        cache.discard_if_same(tenant_id, store)
                    _sync_raw_mapping()
                    raise
                if created:
                    cache[tenant_id] = store
                _sync_raw_mapping()
                return store

        return await asyncio.to_thread(_load)

    return _factory


def _configure_audio_assembler(app: FastAPI, settings: Settings) -> None:
    """Best-effort physical assembler wiring; logical receptions remain usable."""
    try:
        from audio_graphy.core.audio_assembler import AudioAssembler

        audio_root = Path(settings.working_dir)
        audio_root.mkdir(parents=True, exist_ok=True)
        app.state.audio_assembler = AudioAssembler(
            audio_root,
            max_sources=settings.audio_assembly_max_sources,
            max_total_bytes=settings.audio_assembly_max_total_bytes,
            max_estimated_pcm_bytes=settings.audio_assembly_max_estimated_pcm_bytes,
            max_temporary_bytes=settings.audio_assembly_max_temporary_bytes,
            ffprobe_timeout_sec=settings.audio_assembly_ffprobe_timeout_sec,
            ffmpeg_timeout_sec=settings.audio_assembly_ffmpeg_timeout_sec,
            max_concurrent_processes=settings.audio_assembly_max_processes,
        )
    except Exception as exc:
        logger.warning(
            "AudioAssembler init failed; physical reception merge disabled: %s",
            exc,
        )
        app.state.audio_assembler = None


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


async def _run_erasure_outbox_reconciler(
    processor: Any,
    *,
    interval_seconds: float = 30.0,
) -> None:
    """Continuously drain durable privacy erasures without blocking startup."""
    while True:
        try:
            report = await processor.drain_pending(limit=100)
            if report["selected"]:
                logger.info("Erasure outbox reconciliation: %s", report)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Erasure outbox reconciliation failed", exc_info=True)
        await asyncio.sleep(interval_seconds)


async def _run_reception_audio_reconciler(
    service: Any,
    *,
    interval_seconds: float = 5.0,
    batch_limit: int = 4,
) -> None:
    """Recover expired audio work and dispatch committed queued operations."""
    while True:
        try:
            recovered = await service.reconcile_stale()
            artifacts = await service.reconcile_artifacts(limit=100)
            operation_ids = await service.pending_operation_ids(limit=batch_limit)
            if operation_ids:
                await asyncio.gather(
                    *(service.run_operation(operation_id) for operation_id in operation_ids)
                )
            if recovered or artifacts or operation_ids:
                logger.info(
                    "Reception audio reconciliation: recovered=%d artifacts=%d dispatched=%d",
                    recovered,
                    artifacts,
                    len(operation_ids),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Reception audio reconciliation failed", exc_info=True)
        await asyncio.sleep(interval_seconds)


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
    _configure_audio_assembler(app, settings)

    logger.info(
        "AudioGraphy M3 backend starting",
        extra={
            "adapter_mode": settings.adapter_mode,
            "mysql_host": settings.mysql_host,
            "working_dir": str(settings.working_dir),
        },
    )

    # Validate encryption before opening the database or starting workers.
    try:
        audio_crypto = _build_audio_crypto(settings)
    except Exception as exc:
        logger.critical("Audio encryption key validation failed: %s", exc)
        raise RuntimeError("audio encryption key is unavailable or invalid") from exc
    app.state.audio_crypto = audio_crypto

    # DB engine + session factory (with graceful fallback for test/no-DB environments)
    from audio_graphy.db import create_db_engine, create_session_factory

    session_factory = None
    try:
        engine = create_db_engine(settings)
        session_factory = create_session_factory(engine)
        app.state.engine = engine
        app.state.session_factory = session_factory
    except Exception as exc:
        if not settings.allow_degraded_startup:
            logger.critical("DB engine creation failed: %s", exc, exc_info=True)
            raise
        logger.error(
            "DB engine creation failed; ALLOW_DEGRADED_STARTUP is set so the app "
            "will serve without a database: %s",
            exc,
            exc_info=True,
        )
        _record_degradation(app, "database", exc)
        app.state.engine = None
        app.state.session_factory = None

    # Adapter bundle
    from audio_graphy.config import build_adapters

    bundle = build_adapters(settings)
    llm_runtime: Any | None = None
    if session_factory is not None:
        from audio_graphy.services.llm_runtime import build_llm_runtime

        llm_runtime = await build_llm_runtime(settings, session_factory, bundle)
        bundle = llm_runtime.bundle
        app.state.llm_runtime = llm_runtime
        app.state.llm_cache = llm_runtime.cache
        app.state.llm_cache_store = llm_runtime.store
    else:
        # Even in diagnostic/no-DB mode all calls still pass through the
        # centralized retry/concurrency/recipe boundary; result caching is off.
        from dataclasses import replace

        from audio_graphy.services.llm_gateway import LLMGateway

        bundle = replace(
            bundle,
            strong_llm=LLMGateway(
                bundle.strong_llm,
                model_tier="strong",
                max_concurrency=getattr(settings, "llm_strong_concurrency", 4),
            ),
            weak_llm=LLMGateway(
                bundle.weak_llm,
                model_tier="weak",
                max_concurrency=getattr(settings, "llm_weak_concurrency", 8),
            ),
        )
        app.state.llm_runtime = None
        app.state.llm_cache = None
        app.state.llm_cache_store = None
    app.state.adapter_bundle = bundle

    # Global vector store
    from audio_graphy.storage.mysql_vector import MySQLVectorStore

    vector_store: MySQLVectorStore | None = None
    if session_factory is not None:
        vector_store = MySQLVectorStore(
            session_factory,
            dim=settings.embedding_dim,
            cache_ttl_seconds=settings.vector_index_cache_ttl_seconds,
            cache_max_entries=settings.vector_index_cache_max_entries,
            cache_max_bytes=settings.vector_index_cache_max_bytes,
            load_batch_rows=settings.vector_index_load_batch_rows,
            load_max_rows=settings.vector_index_load_max_rows,
            load_max_source_bytes=settings.vector_index_load_max_source_bytes,
            load_max_memory_bytes=settings.vector_index_load_max_memory_bytes,
        )
    app.state.vector_store = vector_store

    # Per-tenant store caches
    from audio_graphy.storage.file_index import FileIndex
    from audio_graphy.storage.graph_networkx import NetworkXGraphStore
    from audio_graphy.storage.tenant_graph_cache import TenantGraphStoreCache

    graph_stores = TenantGraphStoreCache[NetworkXGraphStore](
        max_entries=settings.graph_store_cache_max_entries,
    )
    file_indexes: dict[str, FileIndex] = {}
    app.state.graph_stores = graph_stores
    app.state.file_indexes = file_indexes
    graph_store_factory = _build_graph_store_factory(
        graph_stores,
        settings.working_dir,
        max_entries=settings.graph_store_cache_max_entries,
    )
    app.state.graph_store_factory = graph_store_factory

    def file_index_factory(tenant_id: str) -> FileIndex:
        index = file_indexes.get(tenant_id)
        if index is None:
            index = FileIndex(
                Path(settings.working_dir),
                tenant_id=tenant_id,
            )
            file_indexes[tenant_id] = index
        return index

    erasure_reconciler_task: asyncio.Task[None] | None = None
    app.state.erasure_outbox_processor = None
    if session_factory is not None:
        try:
            from audio_graphy.services.erasure_outbox import (
                ErasureOutboxProcessor,
                remove_recording_graph_refs,
            )

            def _graph_cleanup(store: Any, recording_id: int, tenant_id: str) -> None:
                retention = getattr(app.state, "retention_enforcer", None)
                if retention is not None:
                    retention._remove_graph_refs(
                        store,
                        recording_id,
                        tenant_id=tenant_id,
                    )
                    return
                remove_recording_graph_refs(store, recording_id, tenant_id)

            erasure_processor = ErasureOutboxProcessor(
                session_factory,
                working_dir=Path(settings.working_dir),
                graph_store_factory=graph_store_factory,
                file_index_factory=file_index_factory,
                graph_cleanup=_graph_cleanup,
                llm_cache=getattr(app.state, "llm_cache", None),
                worker_id="lifespan-reconciler",
            )
            app.state.erasure_outbox_processor = erasure_processor
            erasure_reconciler_task = asyncio.create_task(
                _run_erasure_outbox_reconciler(erasure_processor),
                name="erasure-outbox-reconciler",
            )
        except Exception as exc:
            logger.error("Erasure outbox reconciler wiring failed: %s", exc, exc_info=True)
            _record_degradation(app, "erasure_outbox_reconciler", exc)

    # Stateless PII scrubber is created before the worker so raw ASR text is
    # redacted before any persistent or derived store can observe it.
    from audio_graphy.core.pii import PIIScrubber

    pii_scrubber = PIIScrubber()
    app.state.pii_scrubber = pii_scrubber

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
        if session_factory is None or vector_store is None:
            raise RuntimeError("database is unavailable; pipeline worker is disabled")
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
            pii_scrubber=pii_scrubber,
            enable_adaptive_gleaning=settings.enable_adaptive_gleaning,
            settings=settings,
            audio_crypto=audio_crypto,
        )
        # Run the worker as an async background task
        worker_task = asyncio.create_task(worker.start_loop())
        app.state.pipeline_worker = worker
    except Exception as exc:
        logger.error("Failed to start pipeline worker: %s", exc, exc_info=True)
        _record_degradation(app, "pipeline_worker", exc)

    integration_callback_task: asyncio.Task[None] | None = None
    integration_callback_worker = None
    try:
        import socket as _socket

        from audio_graphy.services.integration import (
            IntegrationCallbackWorker,
            load_signing_root,
        )

        if session_factory is None:
            raise RuntimeError("session factory unavailable")
        integration_callback_worker = IntegrationCallbackWorker(
            session_factory,
            signing_root=load_signing_root(str(settings.master_key_path), settings.jwt_secret),
            allow_private_targets=settings.integration_allow_private_callback_urls,
            worker_id=f"{_socket.gethostname()}:{os.getpid()}",
            request_timeout_sec=settings.integration_callback_timeout_sec,
        )
        integration_callback_task = asyncio.create_task(integration_callback_worker.run_forever())
    except Exception as exc:
        logger.error("Failed to start integration callback worker: %s", exc, exc_info=True)
        _record_degradation(app, "integration_callback_worker", exc)

    reception_audio_reconciler_task: asyncio.Task[None] | None = None
    app.state.reception_audio_operation_service = None
    if session_factory is not None:
        try:
            from audio_graphy.services.reception_audio_operations import (
                ReceptionAudioOperationService,
            )
            from audio_graphy.services.receptions import ReceptionService

            reception_service = ReceptionService(
                session_factory,
                audio_root=Path(settings.working_dir),
                audio_assembler=getattr(app.state, "audio_assembler", None),
                audio_crypto=audio_crypto,
                embed_adapter=getattr(bundle, "embed", None),
            )
            reception_audio_operation_service = ReceptionAudioOperationService(
                session_factory,
                reception_service,
            )
            app.state.reception_audio_operation_service = reception_audio_operation_service
            reception_audio_reconciler_task = asyncio.create_task(
                _run_reception_audio_reconciler(
                    reception_audio_operation_service,
                    interval_seconds=max(1.0, float(settings.pipeline_poll_seconds)),
                    batch_limit=max(1, int(settings.pipeline_concurrency)),
                ),
                name="reception-audio-reconciler",
            )
        except Exception as exc:
            logger.error("Reception audio reconciler wiring failed: %s", exc, exc_info=True)
            _record_degradation(app, "reception_audio_reconciler", exc)

    # AuditWriter (async-batched; lifespan-managed).
    from audio_graphy.core.audit import AuditWriter

    audit_writer: AuditWriter | None = None
    if session_factory is not None:
        try:
            audit_writer = AuditWriter(session_factory)
            await audit_writer.start()
            app.state.audit_writer = audit_writer
        except Exception as exc:
            logger.error("AuditWriter init failed: %s", exc, exc_info=True)
            _record_degradation(app, "audit_writer", exc)
            app.state.audit_writer = None
    else:
        app.state.audit_writer = None

    # RetentionEnforcer — registered with APScheduler (daily 03:00 cron).
    if audit_writer is not None and session_factory is not None:
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.cron import CronTrigger

            from audio_graphy.core.retention import RetentionEnforcer

            retention_enforcer = RetentionEnforcer(
                session_factory,
                audio_crypto,
                audit_writer,
                graph_store_factory,
                working_dir=Path(settings.working_dir),
                file_index_factory=file_index_factory,
                llm_cache=getattr(app.state, "llm_cache", None),
            )
            app.state.retention_enforcer = retention_enforcer

            async def _run_retention_job() -> None:
                try:
                    report = await retention_enforcer.run_sweep()
                    assert audit_writer is not None
                    await audit_writer.flush()
                    logger.info(
                        "Retention sweep: scanned=%d deleted=%d errors=%d duration=%.2fs",
                        report.total_scanned,
                        report.deleted,
                        len(report.errors),
                        report.duration_sec,
                    )
                except Exception as exc:
                    logger.error(
                        "Retention sweep failed: %s",
                        exc,
                        exc_info=True,
                    )

            # Run on the application's event loop so cached FileIndex objects
            # and their asyncio locks are never crossed between loops.
            retention_scheduler = AsyncIOScheduler(
                event_loop=asyncio.get_running_loop(),
            )
            retention_scheduler.add_job(
                _run_retention_job,
                trigger=CronTrigger(hour=3, minute=0, timezone="Asia/Shanghai"),
                id="retention_daily",
                coalesce=True,
                max_instances=1,
                replace_existing=True,
            )
            retention_scheduler.start()
            app.state.retention_scheduler = retention_scheduler
        except Exception as exc:
            logger.error("RetentionEnforcer wiring failed: %s", exc, exc_info=True)
            _record_degradation(app, "retention_enforcer", exc)

    yield

    # Shutdown
    # Deferred speaker links first, and by waiting rather than cancelling:
    # each one is a short sequence of separate commits, so killing it
    # mid-flight leaves a recording half-linked, which nothing retries.
    speaker_link_tasks = getattr(app.state, "speaker_link_tasks", None)
    if speaker_link_tasks:
        pending = list(speaker_link_tasks)
        logger.info("Waiting for %d deferred speaker link(s)", len(pending))
        with contextlib.suppress(Exception):
            await asyncio.wait(pending, timeout=_SPEAKER_LINK_DRAIN_SEC)
        still_running = [task for task in pending if not task.done()]
        for task in still_running:
            task.cancel()
        if still_running:
            logger.warning(
                "%d speaker link(s) did not finish within %ds and were cancelled",
                len(still_running),
                _SPEAKER_LINK_DRAIN_SEC,
            )
    if worker_task is not None:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
    if erasure_reconciler_task is not None:
        erasure_reconciler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await erasure_reconciler_task
    if reception_audio_reconciler_task is not None:
        reception_audio_reconciler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reception_audio_reconciler_task
    if integration_callback_task is not None:
        if integration_callback_worker is not None:
            integration_callback_worker.stop()
        integration_callback_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await integration_callback_task

    # M6: shut down AuditWriter (flushes remaining queue) + retention scheduler.
    audit_writer = getattr(app.state, "audit_writer", None)
    if audit_writer is not None:
        with contextlib.suppress(Exception):
            await audit_writer.aclose()
    retention_scheduler = getattr(app.state, "retention_scheduler", None)
    if retention_scheduler is not None:
        with contextlib.suppress(Exception):
            retention_scheduler.shutdown(wait=False)

    if llm_runtime is not None:
        with contextlib.suppress(Exception):
            await llm_runtime.aclose()

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

    # M8: close the streaming connection pool (real ASR mode only).
    streaming_pool = getattr(app.state, "streaming_pool", None)
    if streaming_pool is not None:
        with contextlib.suppress(Exception):
            await streaming_pool.close_all()

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
    app.state.settings = settings
    _configure_audio_assembler(app, settings)

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
    app.add_middleware(
        AuthMiddleware,
        jwt_manager=jwt_manager,
        playback_secret=settings.jwt_secret,
    )
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_body_bytes=settings.max_request_body_bytes,
    )
    app.add_middleware(LegacyTaggingDeprecationMiddleware)

    # Exception handlers
    register_exception_handlers(app)

    # Register routers with /api/v1 prefix
    # Health router is registered WITHOUT prefix (health/readiness has no prefix)
    app.include_router(health_router)
    app.include_router(auth_router, prefix=API_PREFIX)
    app.include_router(recordings_router, prefix=API_PREFIX)
    app.include_router(open_router, prefix=API_PREFIX)
    app.include_router(integration_admin_router, prefix=API_PREFIX)
    app.include_router(receptions_router, prefix=API_PREFIX)
    app.include_router(reception_pipeline_router, prefix=API_PREFIX)
    app.include_router(reception_state_insights_router, prefix=API_PREFIX)
    app.include_router(reception_tags_router, prefix=API_PREFIX)
    app.include_router(segments_router, prefix=API_PREFIX)
    app.include_router(query_router, prefix=API_PREFIX)
    app.include_router(graph_router, prefix=API_PREFIX)
    app.include_router(tags_router, prefix=API_PREFIX)
    app.include_router(tag_insights_router, prefix=API_PREFIX)
    app.include_router(tag_governance_router, prefix=API_PREFIX)
    app.include_router(prompt_lab_router, prefix=API_PREFIX)
    app.include_router(prompts_router, prefix=API_PREFIX)
    app.include_router(stats_router, prefix=API_PREFIX)
    app.include_router(dsar_router, prefix=API_PREFIX)
    app.include_router(eval_router, prefix=API_PREFIX)
    app.include_router(speakers_router, prefix=API_PREFIX)
    app.include_router(recording_speakers_router, prefix=API_PREFIX)

    # M8 Phase 4 — WebSocket /ws/stream router. Only mounted when
    # ``enable_streaming=True`` (default False per PRD §17.11). When False,
    # /ws/stream returns 404 and M1-M7 tests have zero regression.
    if getattr(settings, "enable_streaming", False):
        try:
            from audio_graphy.adapters.bundle import build_streaming_adapters
            from audio_graphy.api.ws_stream import router as ws_stream_router

            # Build the per-app streaming bundle (lazy; empty when disabled).
            streaming_bundle = build_streaming_adapters(settings)
            app.state.streaming_bundle = streaming_bundle
            app.state.streaming_pool = streaming_bundle.pool
            # Track active sessions for diagnostics / graceful shutdown.
            app.state.stream_sessions = {}

            app.include_router(ws_stream_router)  # NO API_PREFIX — path is /ws/stream
            logger.info(
                "M8 streaming ENABLED (vad=%s asr=%s)",
                settings.adapter_streaming_vad_mode,
                settings.adapter_streaming_asr_mode,
            )
        except Exception as exc:
            # ENABLE_STREAMING is on, so the operator asked for these routes.
            # Swallowing this produced a 404 indistinguishable from the flag
            # being off — the hardest possible thing to diagnose from outside.
            logger.critical("M8 streaming router registration failed: %s", exc, exc_info=True)
            raise

    # M9 R2 — Advanced Graph routers (L9 master flag).
    # When ``enable_advanced_graph=False`` (the default), every R2 path
    # below returns 404 and M1-M8 surfaces are unchanged.
    if getattr(settings, "enable_advanced_graph", False):
        try:
            from audio_graphy.api.bi_temporal import router as bi_temporal_router
            from audio_graphy.api.compression_admin import (
                router as compression_admin_router,
            )
            from audio_graphy.api.leiden_admin import (
                router as leiden_admin_router,
            )
            from audio_graphy.api.search import router as search_router

            app.include_router(bi_temporal_router, prefix=API_PREFIX)
            app.include_router(leiden_admin_router, prefix=API_PREFIX)
            app.include_router(search_router, prefix=API_PREFIX)
            app.include_router(compression_admin_router, prefix=API_PREFIX)
            logger.info("M9 advanced graph ENABLED (R2 routers registered)")
        except Exception as exc:
            # Same reasoning as the streaming block above: a flag that is on
            # must either work or stop the process.
            logger.critical("M9 R2 router registration failed: %s", exc, exc_info=True)
            raise

        # M9 R2 T10 — weekly Sunday 03:00 compression cron.
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger

            # Reuse the retention scheduler if present; else create a fresh one.
            comp_scheduler = getattr(app.state, "retention_scheduler", None)
            if comp_scheduler is None:
                comp_scheduler = BackgroundScheduler(daemon=True)
                app.state.compression_scheduler = comp_scheduler

            def _compression_cron() -> None:
                """Bridge: invoke the async compression sweep from a sync job."""
                import asyncio as _asyncio

                from audio_graphy.core.retention import (
                    run_weekly_compression_sweep,
                )

                # The cold-loading factory, not a lookup in the resident cache:
                # a tenant whose graph has not been touched since start-up is
                # exactly the one most likely to need compressing.
                gs_factory = getattr(app.state, "graph_store_factory", None)
                if gs_factory is None:
                    logger.error("Compression cron: no graph store factory on app.state")
                    return

                try:
                    loop = _asyncio.new_event_loop()
                    try:
                        loop.run_until_complete(
                            run_weekly_compression_sweep(
                                session_factory=app.state.session_factory,
                                graph_store_factory=gs_factory,
                                settings=settings,
                            )
                        )
                    finally:
                        loop.close()
                except Exception as exc:
                    logger.error("Weekly compression cron failed: %s", exc, exc_info=True)

            comp_scheduler.add_job(
                _compression_cron,
                trigger=CronTrigger(day_of_week="sun", hour=3, minute=0, timezone="Asia/Shanghai"),
                id="compression_weekly",
                coalesce=True,
                max_instances=1,
                replace_existing=True,
            )
            if not comp_scheduler.running:
                comp_scheduler.start()
            logger.info("M9 weekly compression cron scheduled (Sun 03:00 CST)")
        except Exception as exc:
            logger.warning("M9 compression cron registration failed: %s", exc)

    # Root redirect
    @app.get("/", tags=["meta"])
    async def root() -> dict[str, str]:
        """Root endpoint."""
        return {"message": "AudioGraphy API", "docs": "/docs", "version": "0.3.0"}

    return app


app = create_app()
