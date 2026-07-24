"""Schema and conservative backfill contracts for recording ownership."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace

from sqlalchemy import create_engine, text

import alembic
from audio_graphy.models import Recording

BACKEND_DIR = Path(__file__).resolve().parents[2]
BACKFILL_MIGRATION = (
    BACKEND_DIR / "alembic" / "versions" / "0019_backfill_recording_agent_identity.py"
)


def _load_backfill_migration(monkeypatch) -> ModuleType:
    # The repository's ``alembic/`` migration package shadows the installed
    # Alembic distribution during direct module loading in tests.
    monkeypatch.setattr(
        alembic,
        "op",
        SimpleNamespace(get_bind=lambda: None),
        raising=False,
    )
    spec = importlib.util.spec_from_file_location(
        "recording_agent_identity_backfill",
        BACKFILL_MIGRATION,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_recording_agent_identity_is_nullable_fk_with_agent_queue_index() -> None:
    column = Recording.__table__.c.agent_user_id
    assert column.nullable is True
    foreign_keys = list(column.foreign_keys)
    assert len(foreign_keys) == 1
    assert foreign_keys[0].target_fullname == "users.id"
    assert foreign_keys[0].ondelete == "SET NULL"

    indexes = {
        index.name: tuple(item.name for item in index.columns)
        for index in Recording.__table__.indexes
    }
    assert indexes["ix_recordings_tenant_agent_recorded_id"] == (
        "tenant_id",
        "agent_user_id",
        "recorded_at",
        "id",
    )


def test_backfill_sets_only_unique_tenant_local_agent_matches(
    monkeypatch,
) -> None:
    migration = _load_backfill_migration(monkeypatch)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY,
                    tenant_id VARCHAR(64) NOT NULL,
                    name VARCHAR(255) NOT NULL,
                    role VARCHAR(32) NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE recordings (
                    id INTEGER PRIMARY KEY,
                    tenant_id VARCHAR(64) NOT NULL,
                    agent_name VARCHAR(255),
                    agent_user_id INTEGER
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO users (id, tenant_id, name, role) VALUES
                    (10, 'tenant-a', 'unique', 'agent'),
                    (11, 'tenant-a', 'duplicate', 'agent'),
                    (12, 'tenant-a', 'duplicate', 'agent'),
                    (13, 'tenant-b', 'unique', 'agent'),
                    (14, 'tenant-a', 'not-an-agent', 'viewer')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO recordings (id, tenant_id, agent_name, agent_user_id) VALUES
                    (1, 'tenant-a', 'unique', NULL),
                    (2, 'tenant-a', 'duplicate', NULL),
                    (3, 'tenant-a', 'not-an-agent', NULL),
                    (4, 'tenant-b', 'unique', NULL),
                    (5, 'tenant-a', NULL, NULL),
                    (6, 'tenant-a', 'unique', 99)
                """
            )
        )
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)

        migration.upgrade()

        rows = connection.execute(
            text("SELECT id, agent_user_id FROM recordings ORDER BY id")
        ).all()
    engine.dispose()

    assert rows == [
        (1, 10),
        (2, None),
        (3, None),
        (4, 13),
        (5, None),
        (6, 99),
    ]
