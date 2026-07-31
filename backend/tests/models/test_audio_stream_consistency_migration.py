"""Real-MySQL coverage for the 0030 audio/stream consistency migration."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from sqlalchemy import Engine, create_engine, text

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
ALEMBIC_BIN = str(Path(sys.executable).parent / "alembic")


def _run_alembic(
    *args: str,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [ALEMBIC_BIN, "-c", str(ALEMBIC_INI), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(BACKEND_DIR),
        check=False,
    )


@pytest.fixture
def migration_database(mysql_container: Any) -> Iterator[tuple[Engine, dict[str, str]]]:
    parsed = urlparse(mysql_container.get_connection_url())
    database = f"alembic_0030_audio_{os.getpid()}"
    server_url = (
        f"mysql+pymysql://{parsed.username}:{parsed.password}"
        f"@{parsed.hostname}:{parsed.port}/"
    )
    admin_engine = create_engine(server_url)
    with admin_engine.begin() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS `{database}`"))
        conn.execute(text(f"CREATE DATABASE `{database}`"))

    env = os.environ.copy()
    env.update(
        {
            "MYSQL_HOST": str(parsed.hostname),
            "MYSQL_PORT": str(parsed.port),
            "MYSQL_USER": str(parsed.username),
            "MYSQL_PASSWORD": str(parsed.password),
            "MYSQL_DB": database,
        }
    )
    database_url = (
        f"mysql+pymysql://{parsed.username}:{parsed.password}"
        f"@{parsed.hostname}:{parsed.port}/{database}"
    )
    engine = create_engine(database_url)
    try:
        yield engine, env
    finally:
        engine.dispose()
        with admin_engine.begin() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS `{database}`"))
        admin_engine.dispose()


def _seed_0029_reception_and_stream(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO tenants (code, name)
                VALUES ('migration-tenant', 'Migration tenant')
                """
            )
        )
        recording_result = conn.execute(
            text(
                """
                INSERT INTO recordings
                    (tenant_id, store_id, path, status, pipeline_state)
                VALUES
                    ('migration-tenant', 'store-1', '/tmp/source.wav',
                     'queued', 'pending')
                """
            )
        )
        recording_id = int(recording_result.lastrowid)
        reception_result = conn.execute(
            text(
                """
                INSERT INTO receptions
                    (tenant_id, scenario, store_id, status, merge_mode,
                     started_at, ended_at, version)
                VALUES
                    ('migration-tenant', 'gold', 'store-1', 'confirmed',
                     'logical', '2026-07-29 08:00:00',
                     '2026-07-29 08:00:10', 3)
                """
            )
        )
        reception_id = int(reception_result.lastrowid)
        conn.execute(
            text(
                """
                INSERT INTO reception_recordings
                    (tenant_id, reception_id, recording_id, sequence_no,
                     timeline_start_sec, timeline_end_sec,
                     source_start_sec, source_end_sec, gap_before_sec,
                     decision_source, merge_reasons)
                VALUES
                    ('migration-tenant', :reception_id, :recording_id, 0,
                     5.000, 7.222, 1.234, 3.456, 0.125,
                     'manual', '{}')
                """
            ),
            {"reception_id": reception_id, "recording_id": recording_id},
        )
        conn.execute(
            text(
                """
                INSERT INTO streaming_sessions
                    (tenant_id, session_id, recording_id, started_at, ended_at,
                     seg_confirmed_count, seg_realtime_count, bytes_in,
                     error_count, end_reason, consent_token_hash)
                VALUES
                    ('migration-tenant', 'legacy-session', :recording_id,
                     '2026-07-29 08:00:00', '2026-07-29 08:00:10',
                     2, 3, 4096, 0, 'normal', :consent_hash)
                """
            ),
            {"recording_id": recording_id, "consent_hash": "c" * 64},
        )
        speaker_result = conn.execute(
            text(
                """
                INSERT INTO speaker_nodes
                    (tenant_id, voiceprint_id, display_name, speaker_role,
                     recordings_list, recordings_count, total_speech_sec,
                     merge_confidence, merge_strategy, attrs)
                VALUES
                    ('migration-tenant', 'voiceprint-1', 'Speaker 1', 'unknown',
                     '[]', 0, 0, 0, 'single_recording', '{}')
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO speaker_merge_pending
                    (tenant_id, recording_id, candidate_name,
                     matched_speaker_node_id, fuzzy_score, status,
                     resolved_by)
                VALUES
                    ('migration-tenant', :recording_id, 'Candidate',
                     :speaker_id, 0.9000, 'resolved_rejected', 'human')
                """
            ),
            {
                "recording_id": recording_id,
                "speaker_id": int(speaker_result.lastrowid),
            },
        )


@pytest.mark.integration
def test_0030_upgrades_0029_data_and_matches_runtime_contract(
    migration_database: tuple[Engine, dict[str, str]],
) -> None:
    engine, env = migration_database
    result = _run_alembic("upgrade", "0029_audio_consistency_runs", env=env)
    assert result.returncode == 0, result.stderr
    _seed_0029_reception_and_stream(engine)

    result = _run_alembic(
        "upgrade",
        "0030_audio_stream_consistency",
        env=env,
    )
    assert result.returncode == 0, result.stderr

    expected_tables = {
        "reception_timeline_revisions",
        "reception_audio_operations",
        "reception_audio_artifacts",
        "streaming_ws_tickets",
        "streaming_pcm_frames",
        "streaming_segment_receipts",
        "erasure_outbox",
    }
    expected_columns = {
        "receptions": {"active_timeline_revision_id"},
        "reception_recordings": {
            "timeline_revision_id",
            "source_start_ms",
            "source_end_ms",
            "timeline_start_ms",
            "timeline_end_ms",
            "gap_before_ms",
        },
        "dialogue_units": {"stage_confidence", "timeline_revision_id"},
        "dialogue_state_transitions": {"timeline_revision_id"},
        "dialogue_tag_assignments": {"timeline_revision_id"},
        "streaming_sessions": {
            "epoch",
            "status",
            "generation",
            "pipeline_run_id",
            "ack_seq_high_watermark",
            "durable_segment_high_watermark",
            "lease_expires_at",
            "lease_token",
        },
        "speaker_merge_pending": {
            "observation_state",
            "state_version",
            "candidate_speaker_id",
            "candidate_voiceprint_id",
            "candidate_vector_encrypted",
            "candidate_encryption_meta",
            "candidate_speech_sec",
            "candidate_first_seen",
            "candidate_role_hint",
            "generation",
        },
    }
    with engine.connect() as conn:
        tables = {str(row[0]) for row in conn.execute(text("SHOW TABLES"))}
        columns = {
            table: {
                str(row["Field"])
                for row in conn.execute(text(f"SHOW COLUMNS FROM `{table}`")).mappings()
            }
            for table in expected_columns
        }
        mapping = conn.execute(
            text(
                """
                SELECT source_start_ms, source_end_ms, timeline_start_ms,
                       timeline_end_ms, gap_before_ms
                FROM reception_recordings
                """
            )
        ).mappings().one()
        stream = conn.execute(
            text(
                """
                SELECT epoch, status, generation, ack_seq_high_watermark,
                       durable_segment_high_watermark
                FROM streaming_sessions
                WHERE session_id = 'legacy-session'
                """
            )
        ).mappings().one()
        speaker = conn.execute(
            text(
                """
                SELECT observation_state, state_version, generation
                FROM speaker_merge_pending
                """
            )
        ).mappings().one()
        stream_index = list(
            conn.execute(
                text(
                    """
                    SHOW INDEX FROM streaming_sessions
                    WHERE Key_name = 'ux_streaming_sessions_tenant_session_epoch'
                    """
                )
            ).mappings()
        )
        erasure_indexes = {
            str(row["Key_name"]): (
                int(row["Non_unique"]),
                str(row["Column_name"]),
                int(row["Seq_in_index"]),
            )
            for row in conn.execute(text("SHOW INDEX FROM erasure_outbox")).mappings()
            if str(row["Key_name"]) != "PRIMARY"
        }

    assert expected_tables <= tables
    for table, required in expected_columns.items():
        assert required <= columns[table]
    assert dict(mapping) == {
        "source_start_ms": 1234,
        "source_end_ms": 3456,
        "timeline_start_ms": 5000,
        "timeline_end_ms": 7222,
        "gap_before_ms": 125,
    }
    assert dict(stream) == {
        "epoch": 1,
        "status": "CLOSED",
        "generation": 0,
        "ack_seq_high_watermark": -1,
        "durable_segment_high_watermark": 0,
    }
    assert dict(speaker) == {
        "observation_state": "REJECTED",
        "state_version": 1,
        "generation": 0,
    }
    assert [
        str(row["Column_name"])
        for row in sorted(stream_index, key=lambda row: row["Seq_in_index"])
    ] == ["tenant_id", "session_id", "epoch"]
    assert {int(row["Non_unique"]) for row in stream_index} == {0}
    assert {
        name for name, _value in erasure_indexes.items()
    } >= {
        "ux_erasure_outbox_subject",
        "ix_erasure_outbox_claim",
        "ix_erasure_outbox_tenant_id",
    }


@pytest.mark.integration
def test_0030_downgrade_blocks_duplicate_legacy_session_ids_before_mutation(
    migration_database: tuple[Engine, dict[str, str]],
) -> None:
    engine, env = migration_database
    result = _run_alembic("upgrade", "head", env=env)
    assert result.returncode == 0, result.stderr

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO tenants (code, name)
                VALUES ('downgrade-tenant', 'Downgrade tenant')
                """
            )
        )
        recording_result = conn.execute(
            text(
                """
                INSERT INTO recordings
                    (tenant_id, store_id, path, status, pipeline_state)
                VALUES
                    ('downgrade-tenant', 'store-1', '/tmp/source.wav',
                     'queued', 'pending')
                """
            )
        )
        recording_id = int(recording_result.lastrowid)
        for epoch in (1, 2):
            conn.execute(
                text(
                    """
                    INSERT INTO streaming_sessions
                        (tenant_id, session_id, epoch, status, generation,
                         ack_seq_high_watermark,
                         durable_segment_high_watermark, recording_id,
                         started_at, seg_confirmed_count, seg_realtime_count,
                         bytes_in, error_count, consent_token_hash)
                    VALUES
                        ('downgrade-tenant', 'reconnected-session', :epoch,
                         'INCOMPLETE', 1, -1, 0, :recording_id,
                         CURRENT_TIMESTAMP, 0, 0, 0, 0, :consent_hash)
                    """
                ),
                {
                    "epoch": epoch,
                    "recording_id": recording_id,
                    "consent_hash": "d" * 64,
                },
            )

    blocked = _run_alembic("downgrade", "0029_audio_consistency_runs", env=env)
    assert blocked.returncode != 0
    assert "duplicate session_id" in f"{blocked.stdout}\n{blocked.stderr}"

    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        tables = {str(row[0]) for row in conn.execute(text("SHOW TABLES"))}
        columns = {
            str(row["Field"])
            for row in conn.execute(text("SHOW COLUMNS FROM streaming_sessions")).mappings()
        }
    assert version == "0030_audio_stream_consistency"
    assert "streaming_segment_receipts" in tables
    assert "erasure_outbox" in tables
    assert {"epoch", "lease_token"} <= columns

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                DELETE FROM streaming_sessions
                WHERE session_id = 'reconnected-session' AND epoch = 2
                """
            )
        )
    result = _run_alembic("downgrade", "0029_audio_consistency_runs", env=env)
    assert result.returncode == 0, result.stderr
    with engine.connect() as conn:
        indexes = list(
            conn.execute(
                text(
                    """
                    SHOW INDEX FROM streaming_sessions
                    WHERE Key_name = 'ux_streaming_sessions_session_id'
                    """
                )
            ).mappings()
        )
        downgraded_tables = {
            str(row[0]) for row in conn.execute(text("SHOW TABLES"))
        }
    assert [str(row["Column_name"]) for row in indexes] == ["session_id"]
    assert {int(row["Non_unique"]) for row in indexes} == {0}
    assert "erasure_outbox" not in downgraded_tables


@pytest.mark.integration
def test_0030_rejects_invalid_legacy_geometry_before_mysql_ddl(
    migration_database: tuple[Engine, dict[str, str]],
) -> None:
    engine, env = migration_database
    result = _run_alembic("upgrade", "0029_audio_consistency_runs", env=env)
    assert result.returncode == 0, result.stderr
    _seed_0029_reception_and_stream(engine)
    with engine.begin() as conn:
        invalid_mapping_id = int(
            conn.execute(
                text(
                    """
                    SELECT id
                    FROM reception_recordings
                    LIMIT 1
                    """
                )
            ).scalar_one()
        )
        conn.execute(
            text(
                """
                UPDATE reception_recordings
                SET gap_before_sec = -0.250
                WHERE id = :mapping_id
                """
            ),
            {"mapping_id": invalid_mapping_id},
        )

    blocked = _run_alembic("upgrade", "0030_audio_stream_consistency", env=env)
    assert blocked.returncode != 0
    output = f"{blocked.stdout}\n{blocked.stderr}"
    assert "invalid reception timeline rows" in output
    assert f"mapping ids: {invalid_mapping_id}" in output

    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        tables = {str(row[0]) for row in conn.execute(text("SHOW TABLES"))}
        recording_columns = {
            str(row["Field"])
            for row in conn.execute(
                text("SHOW COLUMNS FROM reception_recordings")
            ).mappings()
        }
    assert version == "0029_audio_consistency_runs"
    assert "reception_timeline_revisions" not in tables
    assert "source_start_ms" not in recording_columns


@pytest.mark.integration
def test_0031_roundtrips_ready_no_speech_without_false_indexed_state(
    migration_database: tuple[Engine, dict[str, str]],
) -> None:
    engine, env = migration_database
    result = _run_alembic("upgrade", "0030_audio_stream_consistency", env=env)
    assert result.returncode == 0, result.stderr

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO tenants (code, name)
                VALUES ('silence-tenant', 'Silence tenant')
                """
            )
        )
        recording_id = int(
            conn.execute(
                text(
                    """
                    INSERT INTO recordings
                        (tenant_id, store_id, path, status, pipeline_state)
                    VALUES
                        ('silence-tenant', 'store-1', '/tmp/silence.wav',
                         'indexed', 'done')
                    """
                )
            ).lastrowid
        )
        run_id = int(
            conn.execute(
                text(
                    """
                    INSERT INTO recording_pipeline_runs
                        (tenant_id, recording_id, generation, idempotency_key,
                         source_fingerprint, config_fingerprint, state,
                         required_projections, completed_projections)
                    VALUES
                        ('silence-tenant', :recording_id, 1, 'silence-run',
                         :fingerprint, :fingerprint, 'ready_no_speech',
                         '["file_index"]', '["file_index"]')
                    """
                ),
                {"recording_id": recording_id, "fingerprint": "a" * 64},
            ).lastrowid
        )
        conn.execute(
            text(
                """
                UPDATE recordings
                SET active_pipeline_run_id = :run_id,
                    indexed_at = CURRENT_TIMESTAMP
                WHERE id = :recording_id
                """
            ),
            {"recording_id": recording_id, "run_id": run_id},
        )

    result = _run_alembic("upgrade", "head", env=env)
    assert result.returncode == 0, result.stderr
    with engine.connect() as conn:
        migrated = conn.execute(
            text(
                """
                SELECT status, pipeline_state, indexed_at
                FROM recordings
                WHERE id = :recording_id
                """
            ),
            {"recording_id": recording_id},
        ).mappings().one()
        create_sql = str(
            conn.execute(text("SHOW CREATE TABLE recordings")).one()[1]
        )
    assert dict(migrated) == {
        "status": "ready_no_speech",
        "pipeline_state": "done",
        "indexed_at": None,
    }
    assert "ready_no_speech" in create_sql

    result = _run_alembic("downgrade", "0030_audio_stream_consistency", env=env)
    assert result.returncode == 0, result.stderr
    with engine.connect() as conn:
        recording = conn.execute(
            text(
                """
                SELECT status, pipeline_state, indexed_at
                FROM recordings
                WHERE id = :recording_id
                """
            ),
            {"recording_id": recording_id},
        ).mappings().one()
        create_sql = str(
            conn.execute(text("SHOW CREATE TABLE recordings")).one()[1]
        )

    assert dict(recording) == {
        "status": "queued",
        "pipeline_state": "pending",
        "indexed_at": None,
    }
    assert "ready_no_speech" not in create_sql
