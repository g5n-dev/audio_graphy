"""Shared fixtures for M3 service / tag / scheduler unit tests.

Uses an in-memory SQLite database (via aiosqlite + StaticPool) so that
tests do NOT require a running MySQL instance. All M3 services
(IngestionService, TagFactsService, TagCurrentService, TagStatsService,
RecomputeService, IndexingService, QueryService, PipelineWorker) are
exercised through their real code paths with mock adapters.
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

# Test environment defaults (must be set before importing audio_graphy.config)
os.environ.setdefault("ADAPTER_MODE", "mock")
os.environ.setdefault("MYSQL_HOST", "127.0.0.1")
os.environ.setdefault("MYSQL_PORT", "3306")
os.environ.setdefault("MYSQL_USER", "audiography")
os.environ.setdefault("MYSQL_PASSWORD", "change-me")
os.environ.setdefault("MYSQL_DB", "audiography_test")
os.environ.setdefault("JWT_SECRET", "test-secret-32-chars-minimum-length!!")
os.environ.setdefault("WORKING_DIR", "/tmp/audiography_svc_test_working_dir")
os.environ.setdefault("DEFAULT_TENANT_ID", "default")


# SQLite does not auto-increment BIGINT columns. Register a compile hook so
# that BigInteger primary keys render as INTEGER on SQLite (which SQLite
# treats as ROWID alias → auto-increments). This registration is idempotent.
@compiles(BigInteger, "sqlite")
def _bigint_to_sqlite(element: Any, compiler: Any, **kw: Any) -> str:
    """Render BigInteger as INTEGER on SQLite for autoincrement support."""
    return "INTEGER"


@pytest_asyncio.fixture
async def db_engine() -> AsyncIterator[Any]:
    """Create an in-memory SQLite async engine with all tables.

    Uses StaticPool so the single in-memory connection is shared across
    all sessions created from the factory.
    """
    import audio_graphy.models  # noqa: F401 — register all models on Base.metadata
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


@pytest_asyncio.fixture
async def graph_store(tmp_working_dir: Path) -> Any:
    """NetworkXGraphStore instance with a temp working_dir."""
    from audio_graphy.storage.graph_networkx import NetworkXGraphStore

    return NetworkXGraphStore(tmp_working_dir, tenant_id="chang_an")


@pytest_asyncio.fixture
async def vector_store(session_factory: async_sessionmaker[AsyncSession]) -> Any:
    """MySQLVectorStore instance bound to the in-memory SQLite engine."""
    from audio_graphy.storage.mysql_vector import MySQLVectorStore

    return MySQLVectorStore(session_factory, dim=1024)


@pytest.fixture
def mock_bundle() -> Any:
    """Build a mock AdapterBundle with zero error rate for deterministic tests."""
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


# ============================================================
# Seed helpers — insert a tenant + recording for service tests
# ============================================================


@pytest_asyncio.fixture
async def seeded_recording(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> Any:
    """Insert a tenant + a single queued recording, return the Recording ORM.

    Also creates a dummy audio file so the pipeline's VAD stage can find it.
    """
    from audio_graphy.models import Prompt, Tenant, User
    from audio_graphy.models.enums import PipelineState, RecordingStatus, UserRole
    from audio_graphy.models.recording import Recording

    # Create a dummy audio file for the pipeline to process
    audio_path = tmp_path / "test_audio.wav"
    audio_path.write_bytes(b"\x00" * 500_000)

    async with session_factory() as session:
        tenant = Tenant(id=1, code="chang_an", name="长安汽车", brand="长安", region="西南")
        session.add(tenant)

        agent = User(
            id=41,
            tenant_id="chang_an",
            name="张敏",
            email="agent-41@changan.example",
            role=UserRole.AGENT.value,
            password_hash="mock",
        )
        session.add(agent)

        prompt = Prompt(
            id=1,
            name="tag_prompt_v1",
            version="v1",
            content="You are a QA inspector.",
            active=True,
            created_by=1,
        )
        session.add(prompt)

        rec = Recording(
            id=1,
            tenant_id="chang_an",
            store_id="S001",
            agent_name="张敏",
            agent_user_id=41,
            customer_hash="cust_hash_001",
            path=str(audio_path),
            status=RecordingStatus.QUEUED.value,
            pipeline_state=PipelineState.PENDING.value,
            prompt_version="v1",
        )
        session.add(rec)
        await session.commit()
        await session.refresh(rec)

    # Return a fresh detached copy by re-fetching
    async with session_factory() as session:
        from sqlalchemy import select

        result = await session.execute(select(Recording).where(Recording.id == 1))
        return result.scalar_one()
