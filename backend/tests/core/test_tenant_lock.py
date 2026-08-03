"""tenant_advisory_lock — mutual exclusion and, above all, release.

MySQL scopes GET_LOCK to a *connection*, and SQLAlchemy hands connections
back to a pool. A lock that is not released before the session closes rides
the connection back into the pool: the next borrower inherits it and every
other worker blocks until that connection happens to be recycled. These
tests pin the release path for the ways it can go wrong.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.core.tenant_lock import tenant_advisory_lock

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _is_free(
    session_factory: async_sessionmaker[AsyncSession],
    name: str,
) -> int:
    async with session_factory() as session:
        return int(
            (await session.execute(text("SELECT IS_FREE_LOCK(:n)"), {"n": name})).scalar()
            or 0
        )


class TestTenantAdvisoryLock:
    async def test_acquires_and_releases(
        self,
        async_session_factory: Any,
    ) -> None:
        async with tenant_advisory_lock(
            async_session_factory, purpose="test", tenant_id="t-basic"
        ) as acquired:
            assert acquired is True
        assert await _is_free(async_session_factory, "ag:test:t-basic") == 1

    async def test_excludes_a_second_holder(
        self,
        async_session_factory: Any,
    ) -> None:
        # Deliberately nested, not combined: the outer lock must already be
        # held when the inner one is attempted, which is the whole assertion.
        async with tenant_advisory_lock(  # noqa: SIM117
            async_session_factory, purpose="test", tenant_id="t-excl"
        ):
            # A second attempt must time out rather than double-enter.
            async with tenant_advisory_lock(
                async_session_factory,
                purpose="test",
                tenant_id="t-excl",
                timeout_sec=1,
            ) as second:
                assert second is False

    async def test_different_tenants_do_not_block_each_other(
        self,
        async_session_factory: Any,
    ) -> None:
        async with tenant_advisory_lock(  # noqa: SIM117
            async_session_factory, purpose="test", tenant_id="t-a"
        ):
            async with tenant_advisory_lock(
                async_session_factory,
                purpose="test",
                tenant_id="t-b",
                timeout_sec=1,
            ) as other:
                assert other is True

    async def test_releases_when_the_body_raises(
        self,
        async_session_factory: Any,
    ) -> None:
        with pytest.raises(RuntimeError):
            async with tenant_advisory_lock(
                async_session_factory, purpose="test", tenant_id="t-raise"
            ):
                raise RuntimeError("boom")
        assert await _is_free(async_session_factory, "ag:test:t-raise") == 1

    async def test_releases_when_the_task_is_cancelled(
        self,
        async_session_factory: Any,
    ) -> None:
        """The case that used to leak silently.

        On cancellation the release statement itself cannot run, so the guard
        has to drop the connection instead — otherwise the pool keeps handing
        out a connection that still owns the lock.
        """

        async def holder() -> None:
            async with tenant_advisory_lock(
                async_session_factory, purpose="test", tenant_id="t-cancel"
            ):
                await asyncio.sleep(30)

        task = asyncio.create_task(holder())
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await _is_free(async_session_factory, "ag:test:t-cancel") == 1

    async def test_lock_name_is_truncated_to_mysql_limit(
        self,
        async_session_factory: Any,
    ) -> None:
        """MySQL rejects names over 64 bytes; a long tenant id must still work."""
        async with tenant_advisory_lock(
            async_session_factory,
            purpose="test",
            tenant_id="t" * 200,
        ) as acquired:
            assert acquired is True
