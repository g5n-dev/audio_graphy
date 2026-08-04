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

import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

# MySQL caps lock names at 64 bytes.
_MAX_LOCK_NAME = 64


def _lock_name(purpose: str, tenant_id: str) -> str:
    """Build a lock name that stays inside MySQL's 64-byte cap without colliding.

    Truncating ``ag:{purpose}:{tenant_id}`` is what this used to do, and it means
    two tenant codes sharing a long enough prefix serialize against each other:
    with ``purpose='speaker_link'`` only 48 bytes of tenant id survive. Tenant
    codes are unvalidated operator input and the column holds 64 characters, so
    the input that produces this is accepted everywhere else. The symptom is
    latency coupling rather than a wrong answer -- B acquires once A releases --
    which is exactly why it would be diagnosed late.

    Short names are left readable so a ``SHOW PROCESSLIST`` still says which
    tenant is holding what; only over-long ones fall back to a digest.
    """

    name = f"ag:{purpose}:{tenant_id}"
    if len(name.encode("utf-8")) <= _MAX_LOCK_NAME:
        return name
    digest = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:32]
    hashed = f"ag:{purpose}:h:{digest}"
    # A purpose long enough to overflow even the digest form is a bug in the
    # caller, not operator input; truncating here keeps the name legal.
    return hashed[:_MAX_LOCK_NAME]


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
    lock_name = _lock_name(purpose, tenant_id)
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
