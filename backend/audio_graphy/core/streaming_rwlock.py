"""StreamingRWLock — asyncio read-write lock (M8 lightweight version).

M8 Phase 4 (WS-2 / T7). Per architecture §9.1.5, this is the *simplified*
RWLock that the PRD P0 ships — multiple concurrent readers, single writer,
no snapshot versioning. The full snapshot lock is P2-1.

Guarantees:
    - Multiple readers can hold the lock simultaneously.
    - A single writer excludes all readers and other writers.
    - Writer-preference: if a writer is waiting, new readers block (avoids
      writer starvation under heavy read load).
    - Reentrant reads are NOT supported (acquire_read twice on the same
      task without release_read will deadlock).

Typical use (DeltaGraphUpdater + StreamingRetriever):

    async with rwlock.write_lock():
        ... # mutate the NetworkX graph

    async with rwlock.read_lock():
        ... # query the graph

The lock is per-tenant (DeltaGraphUpdater holds one per tenant).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class StreamingRWLock:
    """Asyncio read-write lock with writer preference.

    Implementation note: built on ``asyncio.Condition`` so that readers and
    writers can wait/notify through a single primitive. ``_readers`` tracks
    the number of active readers; ``_writer_active`` is the writer flag;
    ``_writers_waiting`` blocks new readers when a writer is queued.
    """

    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self._readers: int = 0
        self._writer_active: bool = False
        self._writers_waiting: int = 0

    @property
    def reader_count(self) -> int:
        """Current active reader count (diagnostics)."""
        return self._readers

    @property
    def writer_active(self) -> bool:
        """True if a writer currently holds the lock."""
        return self._writer_active

    async def acquire_read(self) -> None:
        """Acquire a read lock. Blocks if a writer is active or waiting."""
        async with self._cond:
            while self._writer_active or self._writers_waiting > 0:
                await self._cond.wait()
            self._readers += 1

    async def release_read(self) -> None:
        """Release a read lock. Notifies waiting writers if no more readers."""
        async with self._cond:
            self._readers -= 1
            if self._readers == 0:
                self._cond.notify_all()

    async def acquire_write(self) -> None:
        """Acquire the write lock. Blocks until no readers or writers active."""
        async with self._cond:
            self._writers_waiting += 1
            try:
                while self._writer_active or self._readers > 0:
                    await self._cond.wait()
                self._writer_active = True
            finally:
                self._writers_waiting -= 1

    async def release_write(self) -> None:
        """Release the write lock. Notifies all waiters."""
        async with self._cond:
            self._writer_active = False
            self._cond.notify_all()

    @asynccontextmanager
    async def read_lock(self) -> AsyncIterator[None]:
        """Async context manager for read-lock acquisition."""
        await self.acquire_read()
        try:
            yield
        finally:
            await self.release_read()

    @asynccontextmanager
    async def write_lock(self) -> AsyncIterator[None]:
        """Async context manager for write-lock acquisition."""
        await self.acquire_write()
        try:
            yield
        finally:
            await self.release_write()
