"""Schema reset helpers that survive a database drifting away from the models.

``Base.metadata.drop_all`` drops what the models *say* should exist. That works until
the database disagrees -- a table removed from the models, or a ``use_alter`` foreign
key that was never created because an earlier run died midway. From then on drop_all
raises while trying to drop something absent, so the fixture that was supposed to
restore a clean slate is the very thing that cannot run. The database stays broken
until someone clears it by hand.

Dropping what the database actually contains has no such failure mode.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

_DISABLE_FK = "SET FOREIGN_KEY_CHECKS = 0"
_ENABLE_FK = "SET FOREIGN_KEY_CHECKS = 1"


def suite_database(suite: str) -> str:
    """Name the database a test package owns exclusively.

    The MySQL-backed packages build the schema at different fixture scopes: models
    once per session, core and storage once per test. Pointed at one database they
    quietly sabotage each other -- a function-scoped teardown drops the tables the
    session-scoped fixture is still relying on, and the failure surfaces far from its
    cause. A database per package removes the shared resource entirely.
    """

    base = os.environ.get("MODEL_TEST_MYSQL_DB", "audiography_test")
    return f"{base}_{suite}"


def ensure_database(*, host: str, port: str, user: str, password: str, name: str) -> None:
    """Create the database if it does not exist yet, or skip the test that asked.

    Call this from the engine fixture, never at conftest import time. A
    ``pytest.skip`` raised while a conftest is being imported is not a skip --
    pytest treats a failed conftest import as fatal and aborts the whole run, so
    an unreachable MySQL took down the 99 test files that need no database along
    with the ones that do. From inside a fixture it does what it says, and only
    to the tests that actually need a connection.

    ``AUDIOGRAPHY_REQUIRE_MYSQL=1`` restores the hard failure, and CI sets it. A
    suite that silently skips its integration tests because the service failed to
    start is worse than one that refuses to run: the pipeline stays green while
    covering a fraction of what it claims.
    """

    server_url = f"mysql+pymysql://{user}:{password}@{host}:{port}/"
    engine = create_engine(server_url, echo=False)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"CREATE DATABASE IF NOT EXISTS `{name}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            )
    except OperationalError as exc:
        if os.environ.get("AUDIOGRAPHY_REQUIRE_MYSQL") == "1":
            raise
        pytest.skip(
            f"MySQL is unreachable at {host}:{port} ({exc.orig}); "
            "start it with `docker compose up -d mysql`, or set "
            "AUDIOGRAPHY_REQUIRE_MYSQL=1 to make this an error"
        )
    finally:
        engine.dispose()


def _drop_statements(table_names: list[str]) -> list[str]:
    return [f"DROP TABLE IF EXISTS `{name}`" for name in table_names]


def drop_every_table(engine: Engine) -> None:
    """Drop every table present in the connected schema."""

    with engine.begin() as conn:
        names = inspect(conn).get_table_names()
        if not names:
            return
        conn.execute(text(_DISABLE_FK))
        for statement in _drop_statements(names):
            conn.execute(text(statement))
        conn.execute(text(_ENABLE_FK))


async def drop_every_table_async(engine: AsyncEngine) -> None:
    """Async counterpart of :func:`drop_every_table`."""

    async with engine.begin() as conn:
        names = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
        if not names:
            return
        await conn.execute(text(_DISABLE_FK))
        for statement in _drop_statements(names):
            await conn.execute(text(statement))
        await conn.execute(text(_ENABLE_FK))
