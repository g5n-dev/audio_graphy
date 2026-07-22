"""T3 — DeltaGraphUpdater M9 bi-temporal hook tests.

Verifies:
  1. When ``enable_advanced_graph=False`` the updater behaves identically
     to M8 (zero-regression): no bi-temporal fields, no edge_events buffer.
  2. When ``enable_advanced_graph=True`` every newly written edge carries
     the 4 M9 bi-temporal timestamps + the ``m9_edge_events`` buffer is
     populated with one EdgeEvent row per edge.
  3. ``DeltaUpdateReport.m9_edge_events`` is None on the skipped_by_hash
     short-circuit path (regression safety).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

import audio_graphy.models  # noqa: F401
from audio_graphy.adapters.bundle import AdapterBundle
from audio_graphy.adapters.protocols import EdgeConfidence
from audio_graphy.core.chunker import ChunkRecord
from audio_graphy.core.delta_graph_updater import DeltaGraphUpdater
from audio_graphy.core.extractor import ExtractedEntity, ExtractedRelation
from audio_graphy.core.streaming_rwlock import StreamingRWLock
from audio_graphy.models.base import Base


# ============================================================
# Fakes — minimal stand-ins to avoid touching real LLM / merger / store
# ============================================================


class _FakeGraphStore:
    """Captures upserts for assertions; matches NetworkXGraphStore protocol."""

    def __init__(self) -> None:
        self.nodes: list[Any] = []
        self.edges: list[Any] = []

    async def upsert_node(self, node: Any) -> None:
        self.nodes.append(node)

    async def upsert_edge(self, edge: Any) -> None:
        self.edges.append(edge)


class _FakeExtractorShim:
    """Replaces DeltaGraphUpdater._extractor to return canned results."""

    def __init__(self, extraction: Any) -> None:
        self._extraction = extraction

    async def extract_from_chunk(
        self, *, chunk_id: int, chunk_text: str, recording_id: int
    ) -> Any:
        return self._extraction


class _FakeMerger:
    async def merge(
        self, pairs: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        # identity merge — canonical names equal the originals
        return pairs


class _FakeLinker:
    pass


# ============================================================
# Fixtures
# ============================================================


@pytest_asyncio.fixture
async def dgu_engine() -> AsyncIterator[Any]:
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
async def dgu_factory(
    dgu_engine: Any,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        dgu_engine, class_=AsyncSession, expire_on_commit=False
    )


def _make_extraction() -> Any:
    """Two entities + one EXTRACTED relation between them."""
    entities = [
        ExtractedEntity(
            name="客户A",
            type="客户",
            description="d1",
            chunk_id=1,
            recording_id=42,
        ),
        ExtractedEntity(
            name="车型X",
            type="车型",
            description="d2",
            chunk_id=1,
            recording_id=42,
        ),
    ]
    relations = [
        ExtractedRelation(
            source_name="客户A",
            target_name="车型X",
            relation="推荐",
            description="",
            weight=1.0,
            confidence="EXTRACTED",
            chunk_id=1,
            recording_id=42,
        )
    ]

    class _Ext:
        pass

    ext = _Ext()
    ext.entities = entities
    ext.relations = relations
    return ext


def _make_chunk() -> ChunkRecord:
    return ChunkRecord(
        segment_ids=[1],
        text="客户A 推荐车型X。",
        token_n=20,
        content_hash="deterministic_hash_for_t3_test",
    )


def _build_updater(
    factory: async_sessionmaker[AsyncSession],
    *,
    enable_advanced_graph: bool,
    graph_store: _FakeGraphStore,
) -> DeltaGraphUpdater:
    """Construct an updater whose extractor returns the canned extraction."""
    bundle = AdapterBundle.__new__(AdapterBundle)  # bypass __init__ (no LLM)

    updater = DeltaGraphUpdater(
        bundle=bundle,
        session_factory=factory,
        prompt_template="tpl",
        merger_factory=lambda session, tenant: _FakeMerger(),
        linker_factory=lambda **kw: _FakeLinker(),
        file_index=None,
        graph_store_factory=lambda tenant: graph_store,
        rwlock=StreamingRWLock(),
        session_id="test_session",
        enable_advanced_graph=enable_advanced_graph,
    )
    # Replace the extractor with a shim that returns canned data.
    updater._extractor = _FakeExtractorShim(_make_extraction())  # type: ignore[assignment]
    return updater


# ============================================================
# Tests
# ============================================================


@pytest.mark.asyncio
async def test_flag_false_zero_regression(
    dgu_factory: async_sessionmaker[AsyncSession],
) -> None:
    """M1-M8 path: edges have None bi-temporal fields, no events buffer."""
    gs = _FakeGraphStore()
    updater = _build_updater(
        dgu_factory, enable_advanced_graph=False, graph_store=gs
    )

    report = await updater.update(
        chunk=_make_chunk(),
        recording_id=42,
        tenant_id="t1",
    )

    assert report.skipped_by_hash is False
    assert report.new_edges == 1
    assert report.m9_edge_events is None
    assert len(gs.edges) == 1
    e = gs.edges[0]
    assert e.valid_at is None
    assert e.invalid_at is None
    assert e.created_at is None
    assert e.expired_at is None
    assert e.superseded_by is None


@pytest.mark.asyncio
async def test_flag_true_populates_bitemporal_and_events(
    dgu_factory: async_sessionmaker[AsyncSession],
) -> None:
    """M9 path: 4 timestamps + supersede pointer + EdgeEvent buffer."""
    gs = _FakeGraphStore()
    updater = _build_updater(
        dgu_factory, enable_advanced_graph=True, graph_store=gs
    )

    report = await updater.update(
        chunk=_make_chunk(),
        recording_id=42,
        tenant_id="t1",
    )

    assert report.new_edges == 1
    assert report.m9_edge_events is not None
    assert len(report.m9_edge_events) == 1

    event = report.m9_edge_events[0]
    assert event.tenant_id == "t1"
    assert event.event_type == "insert"
    assert event.actor == "system"
    assert event.edge_key == "客户A|推荐|车型X"

    e = gs.edges[0]
    assert e.valid_at is not None
    assert e.invalid_at is None  # open interval
    assert e.created_at is not None
    assert e.expired_at is None  # not soft-deleted
    assert e.superseded_by is None  # fresh, not superseded


@pytest.mark.asyncio
async def test_skipped_by_hash_returns_none_events(
    dgu_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When content_hash dedup fires, m9_edge_events must be None (regression)."""
    gs = _FakeGraphStore()
    updater = _build_updater(
        dgu_factory, enable_advanced_graph=True, graph_store=gs
    )

    # First call seeds the chunk row.
    await updater.update(
        chunk=_make_chunk(),
        recording_id=42,
        tenant_id="t1",
    )

    # Second call with the same content_hash hits the dedup short-circuit.
    report = await updater.update(
        chunk=_make_chunk(),
        recording_id=42,
        tenant_id="t1",
    )

    assert report.skipped_by_hash is True
    assert report.m9_edge_events is None
    assert report.new_edges == 0
