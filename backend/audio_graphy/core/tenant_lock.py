"""Cross-process advisory locking, scoped to one tenant.

Speaker linking reads a snapshot of a tenant's speakers, decides, then
inserts — across several transactions, over rows that do not exist yet.
There is nothing to lock pessimistically, so concurrent workers would each
conclude "no match" and create their own duplicate SpeakerNode. A MySQL
advisory lock is the narrowest tool that closes that window.

Degradation is deliberate: if the lock cannot be taken, callers proceed
unserialized. A duplicate speaker node is reviewable and mergeable; a
dropped link is silent data loss.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

# MySQL caps lock names at 64 bytes.
_MAX_LOCK_NAME = 64


@asynccontextmanager
async def tenant_advisory_lock(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    purpose: str,
    tenant_id: str,
    timeout_sec: int = 30,
) -> AsyncIterator[bool]:
    """Hold a named per-tenant lock for the duration of the block.

    Yields whether the lock was actually acquired, so callers can log or
    weaken their guarantees knowingly rather than assuming exclusivity.

    The lock lives on its own connection: MySQL releases ``GET_LOCK`` when
    that connection closes, so an abandoned worker cannot wedge the tenant.
    """
    lock_name = f"ag:{purpose}:{tenant_id}"[:_MAX_LOCK_NAME]
    session: AsyncSession | None = None
    acquired = False
    try:
        session = session_factory()
        await session.__aenter__()
        result = await session.execute(
            text("SELECT GET_LOCK(:name, :timeout)"),
            {"name": lock_name, "timeout": timeout_sec},
        )
        acquired = result.scalar() == 1
        if not acquired:
            logger.warning(
                "Could not acquire %s lock for tenant %s within %ds; proceeding unserialized",
                purpose,
                tenant_id,
                timeout_sec,
            )
    except Exception as exc:
        logger.warning(
            "Advisory lock %s unavailable for tenant %s (%s); proceeding unserialized",
            purpose,
            tenant_id,
            exc,
        )
    try:
        yield acquired
    finally:
        if session is not None:
            released = not acquired
            if acquired:
                try:
                    await session.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name})
                    released = True
                except BaseException:
                    # BaseException, not Exception: on task cancellation the
                    # release never happens either, and that is exactly the
                    # case that must not silently leak the lock.
                    logger.warning(
                        "Failed to release %s lock for tenant %s; dropping the "
                        "connection so the lock cannot outlive it",
                        purpose,
                        tenant_id,
                    )
            if not released:
                # A pooled connection is handed back still owning the lock —
                # MySQL scopes GET_LOCK to the connection, so the next borrower
                # inherits it and every other worker blocks until this
                # connection happens to be recycled. Invalidating closes it,
                # which is what makes MySQL drop the lock.
                with suppress(Exception):
                    await session.invalidate()
            with suppress(Exception):
                await session.__aexit__(None, None, None)


__all__ = ["tenant_advisory_lock"]
