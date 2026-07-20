"""Async database engine and session factory.

Provides ``create_db_engine`` and ``create_session_factory`` for use in
the FastAPI lifespan. The session factory is stored on ``app.state.session_factory``
and consumed by the ``get_db`` dependency.

See: docs/m3-architecture.md §7.3.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

if TYPE_CHECKING:
    from audio_graphy.config import Settings

logger = logging.getLogger(__name__)


def create_db_engine(settings: Settings) -> AsyncEngine:
    """Create an async SQLAlchemy engine from application settings.

    Args:
        settings: Application settings (reads ``mysql_dsn_async``).

    Returns:
        An ``AsyncEngine`` instance with pool pre-ping enabled.
    """
    engine = create_async_engine(
        settings.mysql_dsn_async,
        echo=False,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        pool_recycle=3600,
    )
    logger.info("Created async DB engine for %s", settings.mysql_host)
    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create an async session factory bound to the given engine.

    Args:
        engine: An ``AsyncEngine`` instance.

    Returns:
        An ``async_sessionmaker`` producing ``AsyncSession`` instances.
    """
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
