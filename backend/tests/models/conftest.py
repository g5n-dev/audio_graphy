"""Pytest fixtures for model integration tests.

Uses the existing docker-compose MySQL 8 container (audiography_test database)
for database isolation. Each test gets a clean database state via TRUNCATE
after test completion.

Falls back to testcontainers if the TESTCONTAINERS env var is set.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool

# Import all models to ensure they register on Base.metadata
import audio_graphy.models  # noqa: F401
from audio_graphy.models.base import Base

# MySQL connection parameters for the existing docker-compose container.
# NOTE: The higher-level tests/conftest.py sets MYSQL_PORT=3306 via setdefault,
# but the docker-compose MySQL maps host port 3307→container 3306. We must
# override to 3307 when using the existing docker-compose MySQL (not testcontainers).
# We use a separate env var MODEL_TEST_MYSQL_PORT to avoid conflict.
MYSQL_HOST = os.environ.get("MODEL_TEST_MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = os.environ.get("MODEL_TEST_MYSQL_PORT", "3307")
MYSQL_USER = os.environ.get("MODEL_TEST_MYSQL_USER", "audiography")
MYSQL_PASSWORD = os.environ.get("MODEL_TEST_MYSQL_PASSWORD", "change-me")
MYSQL_DB = os.environ.get("MODEL_TEST_MYSQL_DB", "audiography_test")


@pytest.fixture(scope="session")
def mysql_container() -> Iterator[Any]:
    """Provide MySQL container — uses existing docker-compose MySQL or testcontainers.

    Set TESTCONTAINERS=1 to force testcontainers (requires Docker socket mount).
    By default, uses the existing docker-compose MySQL at 127.0.0.1:3307.
    """
    if os.environ.get("TESTCONTAINERS", "") == "1":
        from testcontainers.mysql import MySqlContainer

        container = MySqlContainer("mysql:8.0")
        container.start()
        yield container
        container.stop()
    else:
        # Use existing docker-compose MySQL — return a dummy object
        # with get_connection_url() for backward compatibility
        class _ExistingMySQL:
            def get_connection_url(self) -> str:
                return (
                    f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}"
                    f"@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DB}"
                )

        yield _ExistingMySQL()


@pytest.fixture(scope="session")
def db_engine(mysql_container: Any) -> Iterator[Engine]:
    """Create a SQLAlchemy engine and initialize all 13 tables.

    Handles the known source bug where ux_tag_stats_dim index exceeds
    MySQL 8's 3072-byte key limit. Tables are created individually so
    that one index failure does not block all tests.
    """
    url: str = mysql_container.get_connection_url()

    # Use QueuePool with pool_reset_on_return to ensure connections are
    # properly reset after a failed operation (e.g., tag_stats index creation).
    engine = create_engine(
        url,
        echo=False,
        poolclass=QueuePool,
        pool_size=5,
        pool_reset_on_return="rollback",
    )

    # Clean slate: drop all existing tables first
    Base.metadata.drop_all(engine)

    # Create tables individually so one failure doesn't block everything.
    # The ux_tag_stats_dim index on tag_stats is too long for MySQL 8 utf8mb4
    # (3572 bytes > 3072 byte limit) — this is a known source code bug.
    failed_tables: list[str] = []
    for table in Base.metadata.sorted_tables:
        try:
            # Use a fresh connection for each table creation to avoid
            # connection pool contamination from failed DDL operations.
            with engine.connect() as conn:
                table.create(conn, checkfirst=True)
                conn.commit()
        except OperationalError as e:
            failed_tables.append(f"{table.name}: {e}")

    if failed_tables:
        # Log table/index creation failures but don't fail — tests that depend
        # on the missing index will fail individually with clear assertions.
        print(
            "\nWARNING: Some table/index creation failures (source code bugs):\n"
            + "\n".join(failed_tables)
            + "\n"
        )

    yield engine

    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def db_session(db_engine: Engine) -> Iterator[Session]:
    """Provide a SQLAlchemy session with automatic cleanup after each test."""
    session = Session(db_engine)
    yield session
    session.close()
    # Truncate all tables to ensure clean state for next test
    with db_engine.connect() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in Base.metadata.sorted_tables:
            with contextlib.suppress(Exception):
                conn.execute(text(f"TRUNCATE TABLE `{table.name}`"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        conn.commit()
