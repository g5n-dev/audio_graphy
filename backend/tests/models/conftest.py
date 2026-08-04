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
from sqlalchemy.schema import AddConstraint

# Import all models to ensure they register on Base.metadata
import audio_graphy.models  # noqa: F401
from audio_graphy.models.base import Base
from tests.dbreset import drop_every_table, ensure_database, suite_database

# MySQL connection parameters for the existing docker-compose container.
# NOTE: The higher-level tests/conftest.py sets MYSQL_PORT=3306 via setdefault,
# but the docker-compose MySQL maps host port 3307→container 3306. We must
# override to 3307 when using the existing docker-compose MySQL (not testcontainers).
# We use a separate env var MODEL_TEST_MYSQL_PORT to avoid conflict.
MYSQL_HOST = os.environ.get("MODEL_TEST_MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = os.environ.get("MODEL_TEST_MYSQL_PORT", "3307")
MYSQL_USER = os.environ.get("MODEL_TEST_MYSQL_USER", "audiography")
MYSQL_PASSWORD = os.environ.get("MODEL_TEST_MYSQL_PASSWORD", "change-me")
MYSQL_DB = suite_database("models")


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
        # Probed HERE, not only in db_engine: the alembic roundtrip, the 0030
        # consistency migration and the 0033 downgrade guard all build their own
        # engines from this fixture's URL and never touch db_engine — patching
        # only db_engine left them as 11 raw OperationalError failures on a host
        # without MySQL. One probe at the fixture every consumer shares covers
        # them all (and AUDIOGRAPHY_REQUIRE_MYSQL=1 still turns it into an error).
        ensure_database(
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            name=MYSQL_DB,
        )

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
    """Create a SQLAlchemy engine and build the schema from the current models."""

    # Probed here rather than at import time: a pytest.skip during conftest import
    # is fatal, not a skip, so it took down every test in the package including the
    # ones needing no database.
    ensure_database(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        name=MYSQL_DB,
    )
    url: str = mysql_container.get_connection_url()

    engine = create_engine(
        url,
        echo=False,
        poolclass=QueuePool,
        pool_size=5,
        pool_reset_on_return="rollback",
    )

    # Introspect and drop rather than metadata.drop_all: a schema left behind by
    # older models would otherwise wedge the fixture that exists to reset it.
    drop_every_table(engine)

    # Created per table so a single failure is reported with its table name instead
    # of aborting the whole schema.
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

    # ``Table.create()`` intentionally skips use_alter foreign keys, so add them once
    # every table exists. This is what makes the fixture equivalent to create_all().
    for table in Base.metadata.tables.values():
        for constraint in table.foreign_key_constraints:
            if not constraint.use_alter:
                continue
            try:
                with engine.connect() as conn:
                    conn.execute(AddConstraint(constraint))
                    conn.commit()
            except OperationalError as e:
                failed_tables.append(f"{table.name}.{constraint.name}: {e}")

    if failed_tables:
        # Fail loudly. A half-built schema used to be tolerated here, which is how the
        # test database silently drifted from the models: every later run inherited the
        # gap, and the downstream errors pointed anywhere but at the missing table.
        raise RuntimeError(
            "the test schema could not be built from the current models:\n"
            + "\n".join(failed_tables)
        )

    yield engine

    drop_every_table(engine)
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
