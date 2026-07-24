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
    # M6 WS-3
    "entity_aliases",
    # M6 WS-2
    "eval_runs",
    "recompute_tasks",
    # M7 WS-2
    "speaker_nodes",
    "speaker_links",
    "vectors_voiceprint",
    "vectors_audio",
    # M8
    "streaming_sessions",
    # M9
    "edge_events",
    "community_summaries",
    "leiden_jobs",
    "speaker_merge_pending",
    # Reception/dialogue workspace
    "receptions",
    "reception_recordings",
    "dialogue_units",
    "dialogue_state_transitions",
    "dialogue_tag_assignments",
    "provenance_events",
    "reception_automation_runs",
}


def _run_alembic(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
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
        """Verify alembic upgrade head creates every registered table."""
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
            f"alembic upgrade head failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
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
            voiceprint_index_rows = (
                conn.execute(
                    text(
                        "SHOW INDEX FROM vectors_voiceprint "
                        "WHERE Key_name = 'ix_vp_tenant_speaker_created'"
                    )
                )
                .mappings()
                .all()
            )

        assert EXPECTED_TABLES.issubset(tables), f"Missing tables: {EXPECTED_TABLES - tables}"
        assert "_alembic_sentinel" not in tables
        assert [
            row["Column_name"]
            for row in sorted(voiceprint_index_rows, key=lambda row: row["Seq_in_index"])
        ] == ["tenant_id", "speaker_entity_id", "created_at"]

        # Run alembic downgrade base
        result = _run_alembic("downgrade", "base", env=env)
        assert result.returncode == 0, (
            f"alembic downgrade base failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
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
