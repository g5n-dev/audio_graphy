"""Unit tests for AuditWriter — async-batched audit log writer (PIPL §14.3).

Uses an in-memory SQLite engine + real AuditLog ORM so the full insert
path is exercised. The writer's batch + interval flush logic is driven
through its public API (start/aclose/flush/record).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import audio_graphy.models  # noqa: F401 — register models on Base.metadata
from audio_graphy.core.audit import AuditWriter
from audio_graphy.models.audit_log import AuditLog
from audio_graphy.models.base import Base


@pytest_asyncio.fixture
async def audit_engine() -> AsyncIterator[Any]:
    """In-memory SQLite engine with all tables created."""
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
async def audit_factory(audit_engine: Any) -> async_sessionmaker[AsyncSession]:
    """Async session factory bound to the in-memory engine."""
    return async_sessionmaker(audit_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def short_interval_writer(audit_factory: async_sessionmaker[AsyncSession]) -> AuditWriter:
    """AuditWriter with a tiny interval so the flush loop ticks promptly in tests."""
    return AuditWriter(audit_factory, flush_batch_size=50, flush_interval_sec=0.05)


# --------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_record_written(
    short_interval_writer: AuditWriter,
    audit_factory: async_sessionmaker[AsyncSession],
) -> None:
    """record() → queue → flush → row exists in audit_logs."""
    await short_interval_writer.start()
    try:
        await short_interval_writer.record(
            tenant_id="chang_an",
            user_id=1,
            action="recording.uploaded",
            target="recording:1",
            after={"path": "/tmp/x.wav"},
        )
        # Force a synchronous flush rather than waiting on the loop.
        flushed = await short_interval_writer.flush()
        assert flushed == 1
    finally:
        await short_interval_writer.aclose()

    async with audit_factory() as session:
        rows = list((await session.execute(select(AuditLog))).scalars().all())
    assert len(rows) == 1
    row = rows[0]
    assert row.tenant_id == "chang_an"
    assert row.action == "recording.uploaded"
    assert row.target == "recording:1"
    assert row.user_id == 1
    assert row.after_value == {"path": "/tmp/x.wav"}


@pytest.mark.asyncio
async def test_batch_flush_at_50(
    audit_factory: async_sessionmaker[AsyncSession],
) -> None:
    """50 records enqueued trigger a near-immediate flush."""
    writer = AuditWriter(audit_factory, flush_batch_size=50, flush_interval_sec=10.0)
    await writer.start()
    try:
        for i in range(50):
            await writer.record(
                tenant_id="default",
                user_id=None,
                action="retention_delete",
                target=f"recording:{i}",
            )
        # Allow the queue-threshold-driven flush task to run.
        for _ in range(10):
            await writer.flush()
    finally:
        await writer.aclose()

    async with audit_factory() as session:
        rows = list((await session.execute(select(AuditLog))).scalars().all())
    assert len(rows) == 50


@pytest.mark.asyncio
async def test_flush_on_interval(audit_factory: async_sessionmaker[AsyncSession]) -> None:
    """Records are auto-flushed by the background loop after flush_interval_sec."""
    import asyncio

    writer = AuditWriter(audit_factory, flush_batch_size=100, flush_interval_sec=0.05)
    await writer.start()
    try:
        await writer.record(
            tenant_id="default",
            user_id=None,
            action="retention_delete",
            target="recording:1",
        )
        # Wait for the loop to tick.
        await asyncio.sleep(0.2)
    finally:
        await writer.aclose()

    async with audit_factory() as session:
        rows = list((await session.execute(select(AuditLog))).scalars().all())
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_aclose_drains_remaining(
    audit_factory: async_sessionmaker[AsyncSession],
) -> None:
    """aclose() flushes anything still in the queue."""
    writer = AuditWriter(audit_factory, flush_batch_size=1000, flush_interval_sec=10.0)
    await writer.start()
    for i in range(5):
        await writer.record(
            tenant_id="default",
            user_id=None,
            action="retention_delete",
            target=f"recording:{i}",
        )
    # Without manual flush() or waiting on the loop, queue still holds 5 items.
    await writer.aclose()

    async with audit_factory() as session:
        rows = list((await session.execute(select(AuditLog))).scalars().all())
    assert len(rows) == 5


@pytest.mark.asyncio
async def test_record_after_close_writes_directly(
    audit_factory: async_sessionmaker[AsyncSession],
) -> None:
    """After aclose(), record() still persists synchronously (post-shutdown fallback)."""
    writer = AuditWriter(audit_factory, flush_batch_size=10, flush_interval_sec=10.0)
    await writer.start()
    await writer.aclose()

    await writer.record(
        tenant_id="default",
        user_id=42,
        action="dsar.export",
        target="recording:1",
    )

    async with audit_factory() as session:
        rows = list((await session.execute(select(AuditLog))).scalars().all())
    assert len(rows) == 1
    assert rows[0].action == "dsar.export"


@pytest.mark.asyncio
async def test_exception_in_writer_does_not_propagate(
    audit_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A DB failure surfaces as a WARNING log but does NOT raise to caller."""
    # Build a writer whose factory always raises.
    class _BadFactory:
        def __call__(self) -> Any:
            raise RuntimeError("simulated DB down")

    bad_writer = AuditWriter(  # type: ignore[arg-type]
        _BadFactory(),  # type: ignore[arg-type]
        flush_batch_size=5,
        flush_interval_sec=0.05,
    )
    await bad_writer.start()
    try:
        # Must NOT raise.
        await bad_writer.record(
            tenant_id="default",
            user_id=None,
            action="recording.uploaded",
            target="recording:1",
        )
        # Trigger a flush attempt; must swallow + log.
        await bad_writer.flush()
    finally:
        await bad_writer.aclose()

    # Confirm at least one error-level log mentions the failure.
    messages = [r.message for r in caplog.records]
    assert any("Audit batch write failed" in m for m in messages)
