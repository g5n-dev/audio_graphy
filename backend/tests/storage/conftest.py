"""Shared fixtures for storage layer tests.

Provides async MySQL sessions (via the docker-compose MySQL at port 3307),
temporary working_dir, and pre-built store instances.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Import all models to register on Base.metadata
import audio_graphy.models  # noqa: F401
from audio_graphy.models.base import Base

# MySQL connection — uses docker-compose MySQL (host port 3307 → container 3306)
MYSQL_HOST = os.environ.get("MODEL_TEST_MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = os.environ.get("MODEL_TEST_MYSQL_PORT", "3307")
MYSQL_USER = os.environ.get("MODEL_TEST_MYSQL_USER", "audiography")
MYSQL_PASSWORD = os.environ.get("MODEL_TEST_MYSQL_PASSWORD", "change-me")
MYSQL_DB = os.environ.get("MODEL_TEST_MYSQL_DB", "audiography_test")

ASYNC_DSN = (
    f"mysql+aiomysql://{MYSQL_USER}:{MYSQL_PASSWORD}"
    f"@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DB}?charset=utf8mb4"
)


@pytest_asyncio.fixture
async def async_engine() -> AsyncIterator[Any]:
    """Create an async SQLAlchemy engine and initialise all tables (function-scoped)."""
    engine = create_async_engine(ASYNC_DSN, echo=False, pool_size=5)

    # Drop all existing tables, then create fresh
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def async_session_factory(async_engine: Any) -> async_sessionmaker[AsyncSession]:
    """Return an async_sessionmaker bound to the test engine."""
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def async_db_session(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Provide an async session with automatic cleanup."""
    session = async_session_factory()
    yield session
    await session.close()
    # Truncate all tables for clean state
    async with async_session_factory() as cleanup_session:
        await cleanup_session.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in Base.metadata.sorted_tables:
            with contextlib.suppress(Exception):
                await cleanup_session.execute(text(f"TRUNCATE TABLE `{table.name}`"))
        await cleanup_session.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        await cleanup_session.commit()


@pytest.fixture
def tmp_working_dir(tmp_path: Any) -> Any:
    """Temporary working_dir directory."""
    return tmp_path / "working_dir"


@pytest_asyncio.fixture
async def file_index(tmp_working_dir: Any) -> Any:
    """FileIndex instance with a temporary working_dir."""
    from audio_graphy.storage.file_index import FileIndex

    return FileIndex(tmp_working_dir, tenant_id="default")


@pytest_asyncio.fixture
async def graph_store(tmp_working_dir: Any) -> Any:
    """NetworkXGraphStore instance with a temporary working_dir."""
    from audio_graphy.storage.graph_networkx import NetworkXGraphStore

    return NetworkXGraphStore(tmp_working_dir, tenant_id="default")


@pytest_asyncio.fixture
async def vector_store(async_session_factory: async_sessionmaker[AsyncSession]) -> Any:
    """MySQLVectorStore instance with the test DB session factory."""
    from audio_graphy.storage.mysql_vector import MySQLVectorStore

    return MySQLVectorStore(async_session_factory, dim=1024)
