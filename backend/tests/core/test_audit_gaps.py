"""Coverage gap-fill tests for AuditWriter.

Targets the uncovered branches:
- queue empty after qsize check (race / get_nowait QueueEmpty catch)
- record() enqueue failure (queue.put_nowait raises)
- _flush_loop generic exception path (sleep interrupted)
- _flush_remaining flush failure path
- start() called after task already done (restart branch)
- record() with None before/after args
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import audio_graphy.models  # noqa: F401
from audio_graphy.core.audit import AuditWriter
from audio_graphy.models.audit_log import AuditLog
from audio_graphy.models.base import Base


@pytest_asyncio.fixture
async def ag_engine() -> AsyncIterator[Any]:
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
async def ag_factory(ag_engine: Any) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(ag_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.mark.asyncio
async def test_flush_with_empty_queue_returns_zero(ag_factory: Any) -> None:
    """flush() on an empty queue returns 0 and doesn't raise."""
    writer = AuditWriter(ag_factory, flush_batch_size=10, flush_interval_sec=1.0)
    flushed = await writer.flush()
    assert flushed == 0


@pytest.mark.asyncio
async def test_record_enqueue_exception_swallowed(
    ag_factory: Any, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If queue.put_nowait raises, the exception is logged but not re-raised."""

    def _raise_putnowait(_item: Any) -> None:
        raise RuntimeError("simulated queue corruption")

    writer = AuditWriter(ag_factory, flush_batch_size=10, flush_interval_sec=1.0)
    # Monkeypatch the bound queue method.
    monkeypatch.setattr(writer._queue, "put_nowait", _raise_putnowait)

    with caplog.at_level("ERROR"):
        # Must not raise.
        await writer.record(
            tenant_id="default",
            user_id=None,
            action="recording.uploaded",
            target="recording:1",
        )

    assert any("Audit record enqueue failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_flush_loop_swallows_generic_exception(
    ag_factory: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A generic exception inside _flush_loop is logged but doesn't crash the loop."""
    writer = AuditWriter(ag_factory, flush_batch_size=1000, flush_interval_sec=0.05)
    await writer.start()

    # Replace flush() with one that raises, then yields control so the loop ticks.
    call_count = 0

    async def _boom_flush() -> int:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated flush failure")
        return 0

    writer.flush = _boom_flush  # type: ignore[method-assign]

    with caplog.at_level("ERROR"):
        await asyncio.sleep(0.2)

    await writer.aclose()
    assert any("Audit flush loop error" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_flush_remaining_failure_logged(
    ag_factory: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Exception inside _flush_remaining (aclose shutdown) is logged."""
    writer = AuditWriter(ag_factory, flush_batch_size=1000, flush_interval_sec=10.0)
    await writer.start()

    async def _boom_flush() -> int:
        raise RuntimeError("simulated shutdown flush failure")

    writer.flush = _boom_flush  # type: ignore[method-assign]

    with caplog.at_level("ERROR"):
        await writer.aclose()

    assert any("Audit shutdown flush failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_start_restarts_after_done(ag_factory: Any) -> None:
    """start() can re-launch the flusher if a previous task is done."""
    writer = AuditWriter(ag_factory, flush_batch_size=10, flush_interval_sec=0.05)

    # First start.
    await writer.start()
    task1 = writer._flusher_task
    assert task1 is not None

    # Cancel + await it so .done() is True.
    task1.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task1
    assert task1.done()

    # start() again should create a new task.
    await writer.start()
    task2 = writer._flusher_task
    assert task2 is not None
    assert task2 is not task1

    await writer.aclose()


@pytest.mark.asyncio
async def test_record_with_none_before_after(ag_factory: Any) -> None:
    """record() accepts None for before/after and persists NULL columns."""
    writer = AuditWriter(ag_factory, flush_batch_size=10, flush_interval_sec=1.0)
    await writer.start()
    try:
        await writer.record(
            tenant_id="default",
            user_id=None,
            action="manual.test",
            target="recording:1",
            before=None,
            after=None,
        )
        await writer.flush()
    finally:
        await writer.aclose()

    async with ag_factory() as session:
        rows = list((await session.execute(select(AuditLog))).scalars().all())
    assert len(rows) == 1
    row = rows[0]
    assert row.before_value is None
    assert row.after_value is None


@pytest.mark.asyncio
async def test_record_after_close_writes_via_write_batch(
    ag_factory: Any,
) -> None:
    """After aclose(), record() routes through _write_batch (direct insert)."""
    writer = AuditWriter(ag_factory, flush_batch_size=10, flush_interval_sec=10.0)
    await writer.start()
    await writer.aclose()
    # Now closed → record() takes the sync-direct path.
    await writer.record(
        tenant_id="default",
        user_id=1,
        action="post.close",
        target="recording:1",
        before={"k": "v"},
        after={"k": "v2"},
    )
    async with ag_factory() as session:
        rows = list((await session.execute(select(AuditLog))).scalars().all())
    assert len(rows) == 1
    assert rows[0].action == "post.close"
    assert rows[0].before_value == {"k": "v"}
