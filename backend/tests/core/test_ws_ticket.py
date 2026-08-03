"""WebSocket ticket issuance and one-time redemption contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import audio_graphy.models  # noqa: F401
from audio_graphy.core.stream_session import hash_consent_token
from audio_graphy.core.ws_ticket import WSTicketError, consume_ws_ticket, issue_ws_ticket
from audio_graphy.models.base import Base
from audio_graphy.models.recording import Recording
from audio_graphy.models.streaming_ws_ticket import StreamingWSTicket


@pytest_asyncio.fixture
async def ticket_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add(
            Recording(
                id=1,
                tenant_id="t1",
                store_id="s1",
                agent_user_id=7,
                path="/tmp/ticket.wav",
                status="queued",
            )
        )
        await db.commit()
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_ticket_is_bound_to_tenant_recording_and_consumed_once(
    ticket_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with ticket_factory() as db:
        issued = await issue_ws_ticket(
            db,
            tenant_id="t1",
            recording_id=1,
            user_id=7,
            role="agent",
            consent_token_hash=hash_consent_token("consent"),
            ttl_sec=60,
            now=now,
        )

    consumed = await consume_ws_ticket(ticket_factory, issued.token, now=now)
    assert consumed.tenant_id == "t1"
    assert consumed.recording_id == 1
    assert consumed.user_id == 7
    assert consumed.consent_token_hash == hash_consent_token("consent")

    with pytest.raises(WSTicketError, match="already consumed"):
        await consume_ws_ticket(ticket_factory, issued.token, now=now)


@pytest.mark.asyncio
async def test_agent_cannot_issue_ticket_for_another_agents_recording(
    ticket_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with ticket_factory() as db:
        with pytest.raises(WSTicketError, match="another agent"):
            await issue_ws_ticket(
                db,
                tenant_id="t1",
                recording_id=1,
                user_id=8,
                role="agent",
                consent_token_hash=hash_consent_token("consent"),
                ttl_sec=60,
            )


@pytest.mark.asyncio
async def test_expired_ticket_is_rejected(
    ticket_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with ticket_factory() as db:
        issued = await issue_ws_ticket(
            db,
            tenant_id="t1",
            recording_id=1,
            user_id=7,
            role="agent",
            consent_token_hash=hash_consent_token("consent"),
            ttl_sec=1,
            now=now,
        )

    with pytest.raises(WSTicketError, match="expired"):
        await consume_ws_ticket(
            ticket_factory,
            issued.token,
            now=now + timedelta(seconds=2),
        )
    async with ticket_factory() as db:
        state = (
            await db.execute(
                select(StreamingWSTicket.state).where(StreamingWSTicket.token_hash.is_not(None))
            )
        ).scalar_one()
    assert state == "EXPIRED"
