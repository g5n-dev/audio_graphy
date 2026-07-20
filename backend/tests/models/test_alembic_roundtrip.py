"""Integration test for alembic migration roundtrip (upgrade head -> downgrade base).

Uses subprocess to invoke the alembic CLI, avoiding the local backend/alembic/
directory shadowing the installed alembic Python package.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from sqlalchemy import create_engine, text

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
PYTHON_BIN = sys.executable

# Use the venv's alembic binary (not the system one) to avoid local
# backend/alembic/ directory shadowing the installed alembic package.
ALEMBIC_BIN = str(Path(PYTHON_BIN).parent / "alembic")

EXPECTED_TABLES = {
    "tenants",
    "users",
    "recordings",
    "segments",
    "chunks",
    "tag_facts",
    "tag_current",
    "tag_stats",
    "prompts",
    "vectors_entity",
    "vectors_chunk",
    "audit_logs",
    "llm_call_logs",
}


def _run_alembic(
    *args: str, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run alembic CLI command via subprocess using the installed binary."""
    cmd = [ALEMBIC_BIN, "-c", str(ALEMBIC_INI), *list(args)]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(BACKEND_DIR),
    )
    return result


@pytest.mark.integration
class TestAlembicRoundtrip:
    """Test alembic upgrade head and downgrade base roundtrip."""

    def test_alembic_upgrade_creates_all_tables(
        self, mysql_container: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify alembic upgrade head creates all 13 tables."""
        url: str = mysql_container.get_connection_url()
        parsed = urlparse(url)

        # Create a fresh database for alembic testing
        server_url = (
            f"mysql+pymysql://{parsed.username}:{parsed.password}@{parsed.hostname}:{parsed.port}/"
        )
        admin_engine = create_engine(server_url)
        with admin_engine.connect() as conn:
            conn.execute(text("DROP DATABASE IF EXISTS alembic_rt_test"))
            conn.execute(text("CREATE DATABASE alembic_rt_test"))
            conn.commit()
        admin_engine.dispose()

        # Set env vars for alembic subprocess
        env = os.environ.copy()
        env["MYSQL_HOST"] = str(parsed.hostname)
        env["MYSQL_PORT"] = str(parsed.port)
        env["MYSQL_USER"] = str(parsed.username)
        env["MYSQL_PASSWORD"] = str(parsed.password)
        env["MYSQL_DB"] = "alembic_rt_test"

        # Clear settings cache in this process too (for any later use)
        from audio_graphy.config import get_settings

        get_settings.cache_clear()

        # Run alembic upgrade head
        result = _run_alembic("upgrade", "head", env=env)
        assert result.returncode == 0, (
            f"alembic upgrade head failed:\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )

        # Verify all 13 tables exist
        test_url = (
            f"mysql+pymysql://{parsed.username}:{parsed.password}"
            f"@{parsed.hostname}:{parsed.port}/alembic_rt_test"
        )
        test_engine = create_engine(test_url)
        with test_engine.connect() as conn:
            result_set = conn.execute(text("SHOW TABLES"))
            tables = {row[0] for row in result_set}

        assert EXPECTED_TABLES.issubset(tables), (
            f"Missing tables: {EXPECTED_TABLES - tables}"
        )
        assert "_alembic_sentinel" not in tables

        # Run alembic downgrade base
        result = _run_alembic("downgrade", "base", env=env)
        assert result.returncode == 0, (
            f"alembic downgrade base failed:\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )

        # Verify all 13 tables are dropped
        with test_engine.connect() as conn:
            result_set = conn.execute(text("SHOW TABLES"))
            tables = {row[0] for row in result_set}

        assert not EXPECTED_TABLES.intersection(tables), (
            f"Tables not dropped: {EXPECTED_TABLES.intersection(tables)}"
        )

        test_engine.dispose()

        # Cleanup
        get_settings.cache_clear()
