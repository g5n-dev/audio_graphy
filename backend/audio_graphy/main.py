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

    yield

    # Shutdown
    if worker_task is not None:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task

    await engine.dispose()
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

    # Root redirect
    @app.get("/", tags=["meta"])
    async def root() -> dict[str, str]:
        """Root endpoint."""
        return {"message": "AudioGraphy API", "docs": "/docs", "version": "0.3.0"}

    return app


app = create_app()
