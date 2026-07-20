"""Shared fixtures for tag-layer unit tests (facts / current / stats / recompute).

Uses an in-memory SQLite database (via aiosqlite + StaticPool) so that
tag tests do NOT require a running MySQL instance.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import BigInteger
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

# Ensure backend/ is importable
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Test environment defaults
os.environ.setdefault("ADAPTER_MODE", "mock")
os.environ.setdefault("MYSQL_HOST", "127.0.0.1")
os.environ.setdefault("MYSQL_PORT", "3306")
os.environ.setdefault("MYSQL_USER", "audiography")
os.environ.setdefault("MYSQL_PASSWORD", "change-me")
os.environ.setdefault("MYSQL_DB", "audiography_test")
os.environ.setdefault("JWT_SECRET", "test-secret-32-chars-minimum-length!!")
os.environ.setdefault("WORKING_DIR", "/tmp/audiography_tag_test_working_dir")
os.environ.setdefault("DEFAULT_TENANT_ID", "default")


# SQLite BigInteger autoincrement fix (see services/conftest.py for details).
@compiles(BigInteger, "sqlite")
def _bigint_to_sqlite(element: Any, compiler: Any, **kw: Any) -> str:
    """Render BigInteger as INTEGER on SQLite for autoincrement support."""
    return "INTEGER"


@pytest_asyncio.fixture
async def db_engine() -> AsyncIterator[Any]:
    """Create an in-memory SQLite async engine with all tables."""
    import audio_graphy.models  # noqa: F401
    from audio_graphy.models.base import Base

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        future=True,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(db_engine: Any) -> async_sessionmaker[AsyncSession]:
    """Async session maker bound to the in-memory SQLite engine."""
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def tmp_working_dir(tmp_path: Path) -> Path:
    """Temporary working_dir directory."""
    wd = tmp_path / "working_dir"
    wd.mkdir(parents=True, exist_ok=True)
    return wd


@pytest_asyncio.fixture
async def file_index(tmp_working_dir: Path) -> Any:
    """FileIndex instance with a temp working_dir."""
    from audio_graphy.storage.file_index import FileIndex

    return FileIndex(tmp_working_dir, tenant_id="chang_an")


@pytest.fixture
def mock_bundle() -> Any:
    """Build a mock AdapterBundle with zero error rate."""
    from audio_graphy.adapters.bundle import AdapterBundle
    from audio_graphy.adapters.mock_asr import MockASRAdapter
    from audio_graphy.adapters.mock_embed import MockEmbedAdapter
    from audio_graphy.adapters.mock_llm import MockLLMAdapter
    from audio_graphy.adapters.mock_vad import MockVADAdapter

    return AdapterBundle(
        vad=MockVADAdapter(),
        asr=MockASRAdapter(flaky=False),
        strong_llm=MockLLMAdapter(model="test-strong", error_rate=0.0),
        weak_llm=MockLLMAdapter(model="test-weak", error_rate=0.0),
        embed=MockEmbedAdapter(dim=1024),
    )


@pytest_asyncio.fixture
async def seeded_recording(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> Any:
    """Insert a tenant + a single queued recording, return the Recording ORM."""
    from audio_graphy.models import Tenant
    from audio_graphy.models.enums import PipelineState, RecordingStatus
    from audio_graphy.models.recording import Recording

    audio_path = tmp_path / "test_audio.wav"
    audio_path.write_bytes(b"\x00" * 500_000)

    async with session_factory() as session:
        tenant = Tenant(id=1, code="chang_an", name="长安汽车", brand="长安", region="西南")
        session.add(tenant)
        rec = Recording(
            id=1,
            tenant_id="chang_an",
            store_id="S001",
            agent_name="张敏",
            customer_hash="cust_hash_001",
            path=str(audio_path),
            status=RecordingStatus.QUEUED.value,
            pipeline_state=PipelineState.PENDING.value,
            prompt_version="v1",
        )
        session.add(rec)
        await session.commit()
        await session.refresh(rec)

    from sqlalchemy import select

    async with session_factory() as session:
        result = await session.execute(select(Recording).where(Recording.id == 1))
        return result.scalar_one()
