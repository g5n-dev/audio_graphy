"""Issue and atomically consume short-lived WebSocket tickets."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.models.recording import Recording
from audio_graphy.models.streaming_ws_ticket import StreamingWSTicket


class WSTicketError(ValueError):
    """Ticket is invalid, expired, consumed, or outside its tenant scope."""


@dataclass(frozen=True, slots=True)
class IssuedWSTicket:
    token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ConsumedWSTicket:
    tenant_id: str
    recording_id: int
    user_id: int
    role: str
    consent_token_hash: str


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def issue_ws_ticket(
    db: AsyncSession,
    *,
    tenant_id: str,
    recording_id: int,
    user_id: int,
    role: str,
    consent_token_hash: str,
    ttl_sec: int,
    now: datetime | None = None,
) -> IssuedWSTicket:
    """Validate the tenant-owned recording and persist a one-time ticket."""
    if ttl_sec < 1 or ttl_sec > 300:
        raise ValueError("ttl_sec must be between 1 and 300")
    recording = (
        await db.execute(
            select(Recording.id, Recording.agent_user_id).where(
                Recording.id == recording_id,
                Recording.tenant_id == tenant_id,
            )
        )
    ).one_or_none()
    if recording is None:
        raise WSTicketError("recording is not available in this tenant")
    if role == "agent" and recording.agent_user_id not in (None, user_id):
        raise WSTicketError("agent cannot stream another agent's recording")

    issued_at = now or datetime.now(UTC)
    expires_at = issued_at + timedelta(seconds=ttl_sec)
    token = secrets.token_urlsafe(32)
    db.add(
        StreamingWSTicket(
            tenant_id=tenant_id,
            token_hash=_token_hash(token),
            recording_id=recording_id,
            user_id=user_id,
            role=role,
            consent_token_hash=consent_token_hash,
            state="ISSUED",
            expires_at=expires_at,
        )
    )
    await db.commit()
    return IssuedWSTicket(token=token, expires_at=expires_at)


async def consume_ws_ticket(
    session_factory: async_sessionmaker[AsyncSession],
    token: str,
    *,
    now: datetime | None = None,
) -> ConsumedWSTicket:
    """Consume exactly once under a row lock, safe across workers."""
    if not token:
        raise WSTicketError("missing ticket")
    consumed_at = now or datetime.now(UTC)
    expired = False
    async with session_factory() as db, db.begin():
        row = (
            await db.execute(
                select(StreamingWSTicket)
                .where(StreamingWSTicket.token_hash == _token_hash(token))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise WSTicketError("unknown ticket")
        if row.state != "ISSUED":
            raise WSTicketError("ticket already consumed or revoked")
        expires_at = row.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= consumed_at:
            row.state = "EXPIRED"
            expired = True
        else:
            row.state = "CONSUMED"
            row.consumed_at = consumed_at
            result = ConsumedWSTicket(
                tenant_id=str(row.tenant_id),
                recording_id=int(row.recording_id),
                user_id=int(row.user_id),
                role=str(row.role),
                consent_token_hash=str(row.consent_token_hash),
            )
    if expired:
        raise WSTicketError("ticket expired")
    return result


__all__ = [
    "ConsumedWSTicket",
    "IssuedWSTicket",
    "WSTicketError",
    "consume_ws_ticket",
    "issue_ws_ticket",
]
