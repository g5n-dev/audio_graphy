"""Alembic environment for AudioGraphy.

Reads DB URL from `audio_graphy.config.Settings` (env-driven) rather than
hardcoded in alembic.ini. Supports both online (real DB) and offline
(SQL script generation) modes.
"""

from __future__ import annotations

import logging
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# Ensure backend/ is on sys.path so audio_graphy.config imports cleanly
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Import settings + Base metadata (M1.4 will populate Base.metadata with models)
from audio_graphy.config import get_settings
from audio_graphy.models.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

logger = logging.getLogger("alembic.env")

# Pull DB URL from settings (env-driven)
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.mysql_dsn_sync)

# Target metadata — all ORM models register themselves on import.
# M1.4 will add explicit imports of all model modules here.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL scripts without a DB connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live DB."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
