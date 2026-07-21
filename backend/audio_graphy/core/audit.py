"""AuditWriter — async-batched writer for ``audit_logs``.

Fire-and-forget API: callers (services, DSAR endpoints, retention cron)
append records to an in-memory queue, and a background task flushes them
in batches to the DB every 5 seconds or 50 rows (whichever first).

Design (per docs/m6-architecture.md §3.4):

| Decision        | Choice                         |
|-----------------|--------------------------------|
| Failure mode    | Swallow + WARNING (no rollback of caller's business op) |
| Batch size      | 50 rows / 5 seconds            |
| Queue           | ``asyncio.Queue`` unbounded    |
| Sync fallback   | Direct insert if queue closed  |

Lifecycle:
    writer = AuditWriter(session_factory)
    await writer.start()      # at app startup (main.py lifespan)
    await writer.record(...)  # called throughout app lifetime
    await writer.aclose()     # at app shutdown — drains remaining queue

The writer never raises from ``record()``; failures surface only as
log entries. This is intentional: PIPL §14.3 mandates audit, but
business-critical operations (e.g. recording upload) should not roll
back just because the audit sink is briefly unavailable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.models.audit_log import AuditLog

logger = logging.getLogger(__name__)

_DEFAULT_BATCH_SIZE = 50
_DEFAULT_INTERVAL_SEC = 5.0


@dataclass(frozen=True, slots=True)
class _PendingAudit:
    """One record awaiting batch flush."""

    tenant_id: str
    user_id: int | None
    action: str
    target: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    occurred_at: datetime


class AuditWriter:
    """Async-batched audit log writer (5s flush or 50 rows).

    Args:
        session_factory: async session maker bound to the DB.
        flush_batch_size: Max rows per batch flush (default 50).
        flush_interval_sec: Max seconds between flushes (default 5.0).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        flush_batch_size: int = _DEFAULT_BATCH_SIZE,
        flush_interval_sec: float = _DEFAULT_INTERVAL_SEC,
    ) -> None:
        self._session_factory = session_factory
        self._batch_size = flush_batch_size
        self._interval = flush_interval_sec
        self._queue: asyncio.Queue[_PendingAudit] = asyncio.Queue()
        self._flusher_task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        """Start the background flusher. Call once at app startup."""
        if self._flusher_task is None or self._flusher_task.done():
            self._flusher_task = asyncio.create_task(self._flush_loop())

    async def aclose(self) -> None:
        """Flush remaining records + cancel flusher. Call at app shutdown."""
        self._closed = True
        await self._flush_remaining()
        if self._flusher_task is not None:
            self._flusher_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flusher_task
            self._flusher_task = None

    async def flush(self) -> int:
        """Force-flush the current queue. Returns rows flushed."""
        batch: list[_PendingAudit] = []
        while not self._queue.empty():
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if batch:
            await self._write_batch(batch)
        return len(batch)

    async def record(
        self,
        *,
        tenant_id: str,
        user_id: int | None,
        action: str,
        target: str,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
    ) -> None:
        """Append an audit record. Fire-and-forget — never raises.

        Args:
            tenant_id: Tenant scope.
            user_id: Acting user ID (None for system / cron).
            action: Action code (e.g. ``"recording.uploaded"``,
                ``"dsar.export"``, ``"retention_delete"``).
            target: ``"<entity_type>:<entity_id>"``.
            before: Pre-operation state snapshot.
            after: Post-operation state snapshot.
        """
        if self._closed:
            # Post-shutdown fallback: direct write so we never drop audit.
            await self._write_batch(
                [
                    _PendingAudit(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        action=action,
                        target=target,
                        before=before,
                        after=after,
                        occurred_at=datetime.now(UTC),
                    )
                ]
            )
            return

        try:
            self._queue.put_nowait(
                _PendingAudit(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    action=action,
                    target=target,
                    before=before,
                    after=after,
                    occurred_at=datetime.now(UTC),
                )
            )
            # Fast flush when queue crosses batch threshold.
            if self._queue.qsize() >= self._batch_size:
                # Trigger an immediate flush on the loop (non-blocking).
                # Task reference is intentionally discarded; the background
                # flusher loop also drains the queue, so even if this fire-and
                # -forget task is GC'd before running, the data is not lost.
                asyncio.create_task(self.flush())  # noqa: RUF006
        except Exception as exc:
            logger.error("Audit record enqueue failed: %s", exc, exc_info=True)

    # ------------------------------------------------------------------
    # Internal flush loop
    # ------------------------------------------------------------------
    async def _flush_loop(self) -> None:
        """Background task: batch insert every interval or batch_size."""
        while not self._closed:
            try:
                await asyncio.sleep(self._interval)
                await self.flush()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Audit flush loop error: %s", exc, exc_info=True)

    async def _flush_remaining(self) -> None:
        """Drain queue at shutdown."""
        try:
            await self.flush()
        except Exception as exc:
            logger.error("Audit shutdown flush failed: %s", exc, exc_info=True)

    async def _write_batch(self, batch: list[_PendingAudit]) -> None:
        """Insert a batch. Errors are logged but never raised."""
        if not batch:
            return
        try:
            async with self._session_factory() as session:
                for item in batch:
                    session.add(
                        AuditLog(
                            tenant_id=item.tenant_id,
                            user_id=item.user_id,
                            action=item.action,
                            target=item.target,
                            before_value=item.before,
                            after_value=item.after,
                            occurred_at=item.occurred_at,
                        )
                    )
                await session.commit()
        except Exception as exc:
            logger.error(
                "Audit batch write failed (%d records dropped): %s",
                len(batch),
                exc,
                exc_info=True,
            )


__all__ = ["AuditWriter"]
