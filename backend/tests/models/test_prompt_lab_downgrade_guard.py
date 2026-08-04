"""0033's downgrade must refuse before it destroys the constraint it cannot restore.

The downgrade narrows two CHECKs that 0033 widened: ``tag_extraction_jobs.job_type``
loses ``prompt_compile`` and ``tagger_versions.origin`` loses ``prompt_lab``. MySQL
autocommits DDL and cannot alter a CHECK in place, so each narrowing is a DROP CHECK
followed by an ADD CONSTRAINT. When a live row violates the narrowed list the ADD
fails with errno 3819 -- but the DROP has already committed. The table is left with no
job-type CHECK at all, ``alembic_version`` still reads ``0033_prompt_lab``, and a
second attempt fails on errno 3821 (the constraint it wants to drop is gone). The
operator hand-repairs the schema.

These tests pin the preflight that makes that unreachable: the count runs as the first
statement of ``downgrade()``, so a refusal leaves the schema byte-identical.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
# The venv binary, not the system one: backend/alembic/ shadows the installed
# alembic distribution for anything resolved off this working directory.
ALEMBIC_BIN = str(Path(sys.executable).parent / "alembic")

_PREVIOUS_REVISION = "0032_voiceprint_duration_idx"
_JOB_TYPE_CHECK = "ck_tag_extraction_jobs_type"
_ORIGIN_CHECK = "ck_tagger_versions_origin"


def _run_alembic(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [ALEMBIC_BIN, "-c", str(ALEMBIC_INI), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(BACKEND_DIR),
    )


def _check_clause(engine: Engine, table_name: str, constraint_name: str) -> str | None:
    """Return the live CHECK expression, or None when the constraint is absent."""

    with engine.connect() as conn:
        database = conn.execute(text("SELECT DATABASE()")).scalar_one()
        row = conn.execute(
            text(
                """
                SELECT cc.CHECK_CLAUSE
                FROM information_schema.CHECK_CONSTRAINTS AS cc
                JOIN information_schema.TABLE_CONSTRAINTS AS tc
                  ON tc.CONSTRAINT_SCHEMA = cc.CONSTRAINT_SCHEMA
                 AND tc.CONSTRAINT_NAME = cc.CONSTRAINT_NAME
                WHERE cc.CONSTRAINT_SCHEMA = :database
                  AND tc.TABLE_NAME = :table_name
                  AND cc.CONSTRAINT_NAME = :constraint_name
                """
            ),
            {
                "database": database,
                "table_name": table_name,
                "constraint_name": constraint_name,
            },
        ).scalar()
    return None if row is None else str(row)


@pytest.fixture
def migrated_database(
    request: pytest.FixtureRequest, mysql_container: Any
) -> Iterator[tuple[Engine, dict[str, str]]]:
    """Build a throwaway database at head and hand back an engine plus alembic env."""

    parsed = urlparse(mysql_container.get_connection_url())
    credentials = f"{parsed.username}:{parsed.password}@{parsed.hostname}:{parsed.port}"
    database = f"alembic_0033_guard_{abs(hash(request.node.name)) % 100000}_{os.getpid()}"

    admin_engine = create_engine(f"mysql+pymysql://{credentials}/")
    with admin_engine.begin() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS `{database}`"))
        conn.execute(text(f"CREATE DATABASE `{database}`"))

    env = os.environ.copy()
    env["MYSQL_HOST"] = str(parsed.hostname)
    env["MYSQL_PORT"] = str(parsed.port)
    env["MYSQL_USER"] = str(parsed.username)
    env["MYSQL_PASSWORD"] = str(parsed.password)
    env["MYSQL_DB"] = database

    engine = create_engine(f"mysql+pymysql://{credentials}/{database}")
    try:
        upgraded = _run_alembic("upgrade", "head", env=env)
        assert upgraded.returncode == 0, (
            f"alembic upgrade head failed:\nstdout: {upgraded.stdout}\nstderr: {upgraded.stderr}"
        )
        yield engine, env
    finally:
        engine.dispose()
        with admin_engine.begin() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS `{database}`"))
        admin_engine.dispose()


def _seed_tagger_version(engine: Engine, *, tenant_id: str, origin: str) -> None:
    """Insert one tagger version with the requested origin, plus its schema parents."""

    with engine.begin() as conn:
        schema_id = int(
            conn.execute(
                text(
                    """
                    INSERT INTO tag_schemas
                        (tenant_id, `key`, name, status, created_by)
                    VALUES
                        (:tenant_id, 'guard-schema', 'Guard schema', 'draft', 1)
                    """
                ),
                {"tenant_id": tenant_id},
            ).lastrowid
        )
        schema_version_id = int(
            conn.execute(
                text(
                    """
                    INSERT INTO tag_schema_versions
                        (tenant_id, schema_id, version, definitions, checksum,
                         status, created_by)
                    VALUES
                        (:tenant_id, :schema_id, '1', '[]', :checksum, 'draft', 1)
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "schema_id": schema_id,
                    "checksum": "g" * 64,
                },
            ).lastrowid
        )
        conn.execute(
            text(
                """
                INSERT INTO tagger_versions
                    (tenant_id, schema_version_id, version, engine, prompt_content,
                     rule_bundle, model_version, thresholds, config_checksum, status,
                     origin, created_by)
                VALUES
                    (:tenant_id, :schema_version_id, '1', 'hybrid', 'compiled prompt',
                     :rule_bundle, 'test-model', :thresholds, :checksum, 'draft',
                     :origin, 1)
                """
            ),
            {
                "tenant_id": tenant_id,
                "schema_version_id": schema_version_id,
                "rule_bundle": '{"rules":[]}',
                "thresholds": '{"default":0.7}',
                "checksum": "h" * 64,
                "origin": origin,
            },
        )


@pytest.mark.integration
def test_0033_downgrade_refuses_a_live_prompt_compile_job_before_touching_the_schema(
    migrated_database: tuple[Engine, dict[str, str]],
) -> None:
    """A queued ``prompt_compile`` job blocks the downgrade with the CHECK still intact.

    This is the job type ``PromptLabService`` enqueues, so any tenant that has used the
    prompt lab has such a row. Without the preflight the DROP CHECK commits and the
    table permanently loses its job-type constraint.
    """

    engine, env = migrated_database
    widened = _check_clause(engine, "tag_extraction_jobs", _JOB_TYPE_CHECK)
    assert widened is not None and "prompt_compile" in widened

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO tag_extraction_jobs
                    (tenant_id, job_type, status, scope, idempotency_key,
                     failed_subset, created_by)
                VALUES
                    ('guard-tenant', 'prompt_compile', 'queued', '{}',
                     'prompt-compile-guard', '[]', 1)
                """
            )
        )

    blocked = _run_alembic("downgrade", _PREVIOUS_REVISION, env=env)
    assert blocked.returncode != 0, "the downgrade must refuse while the job row exists"
    output = f"{blocked.stdout}\n{blocked.stderr}"
    assert "incompatible 0033 data" in output
    assert "tag_extraction_jobs.job_type=['prompt_compile'] (1 row(s))" in output

    # The whole point: refusing costs nothing, because nothing ran. Before the guard
    # this read None -- the constraint had already been dropped and could not come back.
    assert _check_clause(engine, "tag_extraction_jobs", _JOB_TYPE_CHECK) == widened

    with engine.connect() as conn:
        surviving = conn.execute(
            text("SELECT COUNT(*) FROM tag_extraction_jobs WHERE job_type = 'prompt_compile'")
        ).scalar_one()
    assert surviving == 1

    # And the refusal is recoverable, which is what the half-rolled-back schema was
    # not: resolve the row and the same command goes through. On the unguarded
    # migration this second run died on errno 3821 -- the first attempt had already
    # dropped the constraint this one wants to drop.
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM tag_extraction_jobs WHERE job_type = 'prompt_compile'"))

    resolved = _run_alembic("downgrade", _PREVIOUS_REVISION, env=env)
    assert resolved.returncode == 0, (
        "downgrade must succeed once resolved:\n"
        f"stdout: {resolved.stdout}\nstderr: {resolved.stderr}"
    )

    narrowed = _check_clause(engine, "tag_extraction_jobs", _JOB_TYPE_CHECK)
    assert narrowed is not None and "prompt_compile" not in narrowed
    with engine.connect() as conn:
        tables = {str(row[0]) for row in conn.execute(text("SHOW TABLES"))}
    assert "tag_prompt_artifacts" not in tables


@pytest.mark.integration
def test_0033_downgrade_refuses_a_prompt_lab_tagger_origin(
    migrated_database: tuple[Engine, dict[str, str]],
) -> None:
    """The origin CHECK narrows in the same downgrade and needs the same preflight.

    It is narrowed after the trigger rebuild, so a violation here used to leave the
    schema even further along: job-type CHECK replaced, trigger replaced, origin CHECK
    dropped and never re-added.
    """

    engine, env = migrated_database
    widened = _check_clause(engine, "tagger_versions", _ORIGIN_CHECK)
    assert widened is not None and "prompt_lab" in widened

    _seed_tagger_version(engine, tenant_id="guard-origin-tenant", origin="prompt_lab")

    blocked = _run_alembic("downgrade", _PREVIOUS_REVISION, env=env)
    assert blocked.returncode != 0, "the downgrade must refuse while the tagger row exists"
    output = f"{blocked.stdout}\n{blocked.stderr}"
    assert "incompatible 0033 data" in output
    assert "tagger_versions.origin=['prompt_lab'] (1 row(s))" in output

    assert _check_clause(engine, "tagger_versions", _ORIGIN_CHECK) == widened
    # Nothing downstream of the refusal ran either.
    assert _check_clause(engine, "tag_extraction_jobs", _JOB_TYPE_CHECK) is not None
    with engine.connect() as conn:
        columns = {
            str(row["Field"])
            for row in conn.execute(text("SHOW COLUMNS FROM tagger_versions")).mappings()
        }
    assert "prompt_artifact_id" in columns
