"""FastAPI application entrypoint for AudioGraphy.

M1.2 stub: only /health is wired. Real routers land in M4-M5.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from audio_graphy.config import get_settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: startup + shutdown hooks.

    M1.2: minimal logging. Real DB pool warmup lands in M1.4.
    """
    settings = get_settings()
    logger.info(
        "AudioGraphy backend starting",
        extra={
            "adapter_mode": settings.adapter_mode,
            "mysql_host": settings.mysql_host,
            "working_dir": str(settings.working_dir),
        },
    )
    yield
    logger.info("AudioGraphy backend shutting down")


def create_app() -> FastAPI:
    """Application factory."""
    settings = get_settings()

    app = FastAPI(
        title="AudioGraphy",
        description="门店录音图谱检索与多级打标系统 · Store Recording Graph Retrieval & Multi-level Tagging",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS — frontend dev server (5173) + production nginx (3000 fallback)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        """Liveness probe — returns 200 if the process is up.

        Readiness probe (DB / adapters reachable) lands in M1.4+.
        """
        return {
            "status": "ok",
            "service": "audiography-backend",
            "version": "0.1.0",
            "adapter_mode": settings.adapter_mode,
        }

    @app.get("/", tags=["meta"])
    async def root() -> dict[str, str]:
        """Root redirect to /docs."""
        return {"message": "AudioGraphy API", "docs": "/docs"}

    return app


app = create_app()
