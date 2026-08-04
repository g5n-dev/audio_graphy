"""Conftest for API integration tests.

Sets up a TestClient with an in-memory SQLite async database (no MySQL needed).
Uses mock adapter mode and a temporary working directory.

Seed data: 2 tenants × 4 users (admin/inspector/agent/viewer per tenant),
plus a second Chang'an inspector for independent blind reviews.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

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
os.environ.setdefault("WORKING_DIR", "/tmp/audiography_api_test_working_dir")
os.environ.setdefault("DEFAULT_TENANT_ID", "default")

# Shared plaintext for every seeded user. Password verification has no bypass,
# so tests that exercise POST /auth/login must send this exact value.
SEED_USER_PASSWORD = "seed-user-password"
# M9 R2: enable the advanced graph endpoints for API integration tests.
# The L9 regression test explicitly skips when this is True.
# NOTE: tests/core/test_config_m9.py asserts the *default* value is False.
# To avoid env bleed, we only set this inside the api_settings fixture below
# (per-test), not as a process-wide env var.


@pytest.fixture
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def api_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fresh settings with temp working dir."""
    from audio_graphy.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("WORKING_DIR", str(tmp_path / "working_dir"))
    monkeypatch.setenv("ADAPTER_MODE", "mock")
    # M9 R2: enable the advanced graph endpoints for API integration tests.
    # Set per-test so the default-False contract documented in
    # tests/core/test_config_m9.py is unaffected.
    monkeypatch.setenv("ENABLE_ADVANCED_GRAPH", "true")
    (tmp_path / "working_dir").mkdir(parents=True, exist_ok=True)
    return get_settings()


@pytest.fixture
def jwt_manager(api_settings):
    """JWT manager for test token generation."""
    from audio_graphy.auth.jwt_utils import JWTManager

    return JWTManager(
        secret=api_settings.jwt_secret,
        algorithm=api_settings.jwt_algorithm,
        exp_hours=api_settings.jwt_exp_hours,
        refresh_exp_hours=api_settings.jwt_refresh_exp_hours,
    )


def _make_token(jwt_manager, user_id: int, tenant_id: str, role: str) -> str:
    """Helper to create a JWT access token."""
    return jwt_manager.create_access_token(user_id, tenant_id, role)


@pytest.fixture
def test_client(api_settings):
    """FastAPI TestClient with in-memory SQLite + mock adapters.

    Uses a synchronous SQLite engine with SQLAlchemy's sync layer wrapped
    in async. The key insight: we use StaticPool with a shared connection
    that persists across the TestClient's event loop.

    An alternative approach: we initialize the DB schema + seed data
    synchronously before TestClient starts, then use the same connection
    via StaticPool for all async operations.
    """
    from pathlib import Path as PathLib

    # Create a file-based SQLite DB for cross-thread/cross-loop compatibility
    db_path = PathLib(api_settings.working_dir) / "test_api.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Initialize schema + seed data synchronously
    # Build sync engine to create tables and seed
    from sqlalchemy import create_engine as sync_create_engine

    from audio_graphy.auth.passwords import PasswordHasher
    from audio_graphy.models import (
        Base,
        Prompt,
        Tenant,
        User,
    )
    from audio_graphy.models.enums import UserRole

    # Every seeded user shares one password. Hash it once at the lowest cost
    # factor bcrypt allows — the digest embeds its own rounds, so verification
    # works regardless of the hasher the application is configured with.
    seeded_password_hash = PasswordHasher(bcrypt_rounds=4).hash(SEED_USER_PASSWORD)

    sync_engine = sync_create_engine(
        f"sqlite:///{db_path}",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(sync_engine)

    # Seed data synchronously
    from sqlalchemy.orm import Session as SyncSession

    with SyncSession(sync_engine) as session:
        # Check if already seeded
        existing = session.query(Tenant).first()
        if existing is None:
            # Tenants (explicit IDs for JWT user_id mapping)
            t1 = Tenant(id=1, code="chang_an", name="长安汽车", brand="长安", region="西南")
            t2 = Tenant(id=2, code="byd", name="比亚迪", brand="BYD", region="华南")
            session.add_all([t1, t2])

            # Users — 4 roles × 2 tenants (IDs 1-8 match JWT user_id), plus
            # an independent Chang'an inspector for multi-reviewer workflows.
            users = [
                User(
                    id=1,
                    tenant_id="chang_an",
                    name="admin_ca",
                    email="admin@changan.example.com",
                    role=UserRole.ADMIN.value,
                    password_hash=seeded_password_hash,
                ),
                User(
                    id=2,
                    tenant_id="chang_an",
                    name="inspector_ca",
                    email="inspector@changan.example.com",
                    role=UserRole.INSPECTOR.value,
                    password_hash=seeded_password_hash,
                ),
                User(
                    id=3,
                    tenant_id="chang_an",
                    name="agent_ca",
                    email="agent@changan.example.com",
                    role=UserRole.AGENT.value,
                    password_hash=seeded_password_hash,
                ),
                User(
                    id=4,
                    tenant_id="chang_an",
                    name="viewer_ca",
                    email="viewer@changan.example.com",
                    role=UserRole.VIEWER.value,
                    password_hash=seeded_password_hash,
                ),
                User(
                    id=5,
                    tenant_id="byd",
                    name="admin_byd",
                    email="admin@byd.example.com",
                    role=UserRole.ADMIN.value,
                    password_hash=seeded_password_hash,
                ),
                User(
                    id=6,
                    tenant_id="byd",
                    name="inspector_byd",
                    email="inspector@byd.example.com",
                    role=UserRole.INSPECTOR.value,
                    password_hash=seeded_password_hash,
                ),
                User(
                    id=7,
                    tenant_id="byd",
                    name="agent_byd",
                    email="agent@byd.example.com",
                    role=UserRole.AGENT.value,
                    password_hash=seeded_password_hash,
                ),
                User(
                    id=8,
                    tenant_id="byd",
                    name="viewer_byd",
                    email="viewer@byd.example.com",
                    role=UserRole.VIEWER.value,
                    password_hash=seeded_password_hash,
                ),
                User(
                    id=10,
                    tenant_id="chang_an",
                    name="inspector2_ca",
                    email="inspector2@changan.example.com",
                    role=UserRole.INSPECTOR.value,
                    password_hash=seeded_password_hash,
                ),
            ]
            session.add_all(users)

            # Prompts
            p1 = Prompt(
                id=1,
                name="tag_prompt_v1",
                version="v1",
                content="You are a QA inspector.",
                active=True,
                created_by=1,
            )
            p2 = Prompt(
                id=2,
                name="tag_prompt_v1",
                version="v2",
                content="You are a better QA inspector.",
                active=False,
                created_by=1,
            )
            session.add_all([p1, p2])

            session.commit()

    sync_engine.dispose()

    # Now create the async engine pointing to the same file-based DB
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
    from sqlalchemy.pool import StaticPool

    async_engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    session_factory = async_sessionmaker(
        async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    # Build FastAPI app
    from fastapi.testclient import TestClient

    from audio_graphy.main import create_app

    app = create_app()

    # Set up app state manually (bypass lifespan)
    app.state.settings = api_settings
    app.state.version = "0.3.0"
    app.state.engine = async_engine
    app.state.session_factory = session_factory

    from audio_graphy.config import build_adapters

    bundle = build_adapters(api_settings)
    app.state.adapter_bundle = bundle

    # Create vector store with the real session factory
    from audio_graphy.storage.mysql_vector import MySQLVectorStore

    vector_store = MySQLVectorStore(session_factory, dim=api_settings.embedding_dim)
    app.state.vector_store = vector_store

    app.state.graph_stores = {}
    app.state.file_indexes = {}

    # Readiness checks that a factory exists, so provide one. Deliberately not
    # the production factory: that one mirrors the mapping into its own cache
    # and writes it back, which would discard the stub stores several tests
    # inject directly into `graph_stores`.
    async def _test_graph_store_factory(tenant_id: str) -> Any:
        return app.state.graph_stores.get(tenant_id)

    app.state.graph_store_factory = _test_graph_store_factory

    from audio_graphy.auth.jwt_utils import JWTManager

    jwt_mgr = JWTManager(
        secret=api_settings.jwt_secret,
        algorithm=api_settings.jwt_algorithm,
        exp_hours=api_settings.jwt_exp_hours,
        refresh_exp_hours=api_settings.jwt_refresh_exp_hours,
    )
    app.state.jwt_manager = jwt_mgr

    # TestClient without lifespan context manager to avoid MySQL connection.
    # We manually set up app.state, so lifespan is not needed.
    client = TestClient(app, raise_server_exceptions=False)
    yield client

    # Cleanup: dispose the async engine
    import contextlib

    with contextlib.suppress(Exception):
        asyncio.run(async_engine.dispose())


@pytest.fixture
def auth_headers(jwt_manager):
    """Pre-generated auth headers for each role × tenant.

    Returns a dict like:
        {"admin_t1": {"Authorization": "Bearer <token>"}, ...}
    """
    tokens: dict[str, dict[str, str]] = {}
    # Tenant 1: chang_an
    for role, uid in [("admin", 1), ("inspector", 2), ("agent", 3), ("viewer", 4)]:
        token = _make_token(jwt_manager, uid, "chang_an", role)
        tokens[f"{role}_t1"] = {"Authorization": f"Bearer {token}"}
    second_inspector = _make_token(jwt_manager, 10, "chang_an", "inspector")
    tokens["inspector2_t1"] = {"Authorization": f"Bearer {second_inspector}"}
    # Tenant 2: byd
    for role, uid in [("admin", 5), ("inspector", 6), ("agent", 7), ("viewer", 8)]:
        token = _make_token(jwt_manager, uid, "byd", role)
        tokens[f"{role}_t2"] = {"Authorization": f"Bearer {token}"}
    return tokens


@pytest.fixture
def db_session_factory(test_client):
    """Provide the async session factory from the test app for seeding data."""
    return test_client.app.state.session_factory


def _run_async(coro):
    """Run an async coroutine from a sync test function."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def seed_recording(
    factory,
    tenant_id: str = "chang_an",
    store_id: str = "S001",
    agent_name: str = "agent_ca",
    status: str = "indexed",
    pipeline_state: str = "done",
    recording_id: int | None = None,
    agent_user_id: int | None = None,
) -> int:
    """Seed a recording into the test DB and return its ID."""
    from datetime import UTC, datetime

    from audio_graphy.models import Recording
    from audio_graphy.services.agent_identity import resolve_unique_agent_user_id

    async with factory() as session:
        if agent_user_id is None:
            agent_user_id = await resolve_unique_agent_user_id(
                session,
                tenant_id=tenant_id,
                agent_name=agent_name,
            )
        rec = Recording(
            tenant_id=tenant_id,
            store_id=store_id,
            agent_name=agent_name,
            agent_user_id=agent_user_id,
            customer_hash="cust_hash_001",
            path=f"/tmp/test_{store_id}_{agent_name}.wav",
            status=status,
            pipeline_state=pipeline_state,
            recorded_at=datetime.now(UTC),
            indexed_at=datetime.now(UTC) if status == "indexed" else None,
            prompt_version="tag_prompt_v1/v1",
        )
        if recording_id is not None:
            rec.id = recording_id
        session.add(rec)
        await session.commit()
        await session.refresh(rec)
        return rec.id


async def seed_tag(
    factory,
    recording_id: int,
    tenant_id: str,
    tag_path: str = "quality.greeting",
    tag_value: str = "pass",
    version: int = 1,
    prompt_version: str = "tag_prompt_v1/v1",
) -> int:
    """Seed a tag_fact + tag_current for a recording."""
    from datetime import UTC, datetime

    from audio_graphy.models import TagCurrent, TagFact, TagStat

    async with factory() as session:
        fact = TagFact(
            recording_id=recording_id,
            tenant_id=tenant_id,
            tag_path=tag_path,
            tag_value=tag_value,
            version=version,
            prompt_version=prompt_version,
            model_version="mock-model",
            input_hash="abc123",
            confidence=0.95,
            source="llm",
            computed_by=1,
            computed_at=datetime.now(UTC),
        )
        session.add(fact)
        await session.flush()

        current = TagCurrent(
            recording_id=recording_id,
            tenant_id=tenant_id,
            tag_path=tag_path,
            tag_value=tag_value,
            version=version,
            prompt_version=prompt_version,
        )
        session.add(current)

        # Also seed a tag_stat
        stat = TagStat(
            tenant_id=tenant_id,
            store_id="S001",
            agent_name="agent_ca",
            tag_path=tag_path,
            tag_value=tag_value,
            tag_count=1,
        )
        session.add(stat)

        await session.commit()
        return fact.id


async def seed_segment(
    factory,
    recording_id: int,
    tenant_id: str,
    idx: int = 0,
    start_sec: float = 0.0,
    end_sec: float = 5.0,
    transcript: str = "Hello, welcome to our store.",
) -> int:
    """Seed a segment for a recording."""
    from audio_graphy.models import Segment

    async with factory() as session:
        seg = Segment(
            recording_id=recording_id,
            tenant_id=tenant_id,
            idx=idx,
            start_sec=start_sec,
            end_sec=end_sec,
            transcript=transcript,
            speaker="agent",
            vad_conf=0.95,
        )
        session.add(seg)
        await session.commit()
        await session.refresh(seg)
        return seg.id


def seed_graph(test_client, tenant_id: str = "chang_an") -> None:
    """Populate the in-memory NetworkX graph store with test entities and edges.

    Creates 3 nodes (长安CS75, 客户A, 销售) and 2 edges connecting them.
    Must be called *after* test_client fixture is initialized.
    """
    import networkx as nx

    from audio_graphy.core.types import _list_to_str

    # Get or create the graph store for this tenant
    graph_stores: dict = test_client.app.state.graph_stores
    store = graph_stores.get(tenant_id)
    if store is None:
        settings = test_client.app.state.settings
        from audio_graphy.storage.graph_networkx import NetworkXGraphStore

        store = NetworkXGraphStore(settings.working_dir, tenant_id=tenant_id)
        graph_stores[tenant_id] = store

    g: nx.MultiDiGraph = store._graph
    g.clear()

    # Add nodes
    g.add_node(
        "长安CS75",
        name="长安CS75",
        type="产品",
        description="长安CS75 SUV",
        degree=2,
        source_ids=_list_to_str(["seg_1"]),
        recording_ids=_list_to_str(["1"]),
    )
    g.add_node(
        "客户A",
        name="客户A",
        type="客户",
        description="来电客户",
        degree=2,
        source_ids=_list_to_str(["seg_1"]),
        recording_ids=_list_to_str(["1"]),
    )
    g.add_node(
        "销售张三",
        name="销售张三",
        type="员工",
        description="门店销售",
        degree=1,
        source_ids=_list_to_str(["seg_2"]),
        recording_ids=_list_to_str(["2"]),
    )

    # Add edges
    g.add_edge(
        "客户A",
        "长安CS75",
        key="询问",
        relation="询问",
        weight=1.0,
        confidence="EXTRACTED",
        confidence_score=0.95,
        source_ids=_list_to_str(["seg_1"]),
    )
    g.add_edge(
        "长安CS75",
        "销售张三",
        key="推荐",
        relation="推荐",
        weight=0.8,
        confidence="INFERRED",
        confidence_score=0.72,
        source_ids=_list_to_str(["seg_2"]),
    )

    store._loaded = True
