"""Server-sent events — the frontend's subscription to domain events.

    GET /api/v1/events/stream?after=<cursor>&types=recording.indexed,…

One consumer model: the client keeps the last event ``id`` it saw and
reconnects with ``after=<id>``; the server replays everything newer and then
tails the feed. Transport is SSE over a plain authenticated GET — the web
client uses ``fetch`` streaming rather than ``EventSource`` because the
latter cannot send an Authorization header.

Implementation is a cursor poll over ``domain_events`` (1s), not a broker:
the deployment story is single-host compose and the feed's write rate is
bounded by audio processing. A heartbeat comment every 15s keeps proxies from
reaping the connection.

Role scoping: admin and inspector see the tenant's whole feed. An agent sees
only recording events that carry their own ``agent_user_id`` — the feed's
payloads are id-and-status only, but which recordings exist and when they
finish is itself information the role model does not grant an agent for
other agents' work.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.tenants import get_tenant_id
from audio_graphy.models.domain_event import DomainEvent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["events"])

_POLL_SEC = 1.0
_HEARTBEAT_SEC = 15.0
_BATCH = 100


def _visible(event: DomainEvent, user: AuthUser) -> bool:
    if user.role != "agent":
        return True
    if event.aggregate_type != "recording":
        return False
    return event.payload.get("agent_user_id") == user.id


def _frame(event: DomainEvent) -> str:
    body = json.dumps(
        {
            "id": event.id,
            "event_type": event.event_type,
            "aggregate_type": event.aggregate_type,
            "aggregate_id": event.aggregate_id,
            "payload": event.payload,
            "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"id: {event.id}\nevent: {event.event_type}\ndata: {body}\n\n"


@router.get("/stream", summary="Subscribe to domain events (SSE)")
async def stream_events(
    request: Request,
    after: int | None = Query(default=None, ge=0),
    types: str | None = Query(default=None, max_length=512),
    max_events: int | None = Query(default=None, ge=1, le=10_000),
    idle_timeout_sec: float | None = Query(default=None, ge=0, le=3_600),
) -> StreamingResponse:
    """Tail the tenant's event feed from a cursor.

    ``after`` omitted means "from now": the default subscriber wants changes,
    not history — replay is an explicit choice, not a surprise burst of every
    event since the deployment went live.

    ``max_events`` / ``idle_timeout_sec`` make the stream finite: close after
    N delivered events, or after that long with nothing to deliver. They exist
    for curl debugging and for tests — Starlette's TestClient (and httpx's
    ASGITransport) buffer a response until the app returns, so an unbounded
    stream can never be exercised in-process. The browser client passes
    neither and tails forever.
    """

    tenant_id = get_tenant_id(request)
    user: AuthUser = request.state.user
    wanted = {t.strip() for t in types.split(",") if t.strip()} if types else None
    factory = request.app.state.session_factory

    async def _tail() -> AsyncIterator[str]:
        cursor = after
        if cursor is None:
            async with factory() as session:
                newest = (
                    await session.execute(
                        select(DomainEvent.id)
                        .where(DomainEvent.tenant_id == tenant_id)
                        .order_by(DomainEvent.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
            cursor = newest or 0
        idle = 0.0
        heartbeat_due = _HEARTBEAT_SEC
        delivered = 0
        yield ": connected\n\n"
        while True:
            if await request.is_disconnected():
                return
            async with factory() as session:
                events = (
                    (
                        await session.execute(
                            select(DomainEvent)
                            .where(
                                DomainEvent.tenant_id == tenant_id,
                                DomainEvent.id > cursor,
                            )
                            .order_by(DomainEvent.id)
                            .limit(_BATCH)
                        )
                    )
                    .scalars()
                    .all()
                )
            emitted = False
            for event in events:
                cursor = event.id
                if wanted is not None and event.event_type not in wanted:
                    continue
                if not _visible(event, user):
                    continue
                emitted = True
                delivered += 1
                yield _frame(event)
                if max_events is not None and delivered >= max_events:
                    return
            if emitted:
                idle = 0.0
                continue
            if idle_timeout_sec is not None and idle >= idle_timeout_sec:
                return
            heartbeat_due -= _POLL_SEC
            if heartbeat_due <= 0:
                heartbeat_due = _HEARTBEAT_SEC
                yield ": ping\n\n"
            idle += _POLL_SEC
            await asyncio.sleep(_POLL_SEC)

    return StreamingResponse(
        _tail(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Nginx-family proxies buffer streaming responses to death without it.
            "X-Accel-Buffering": "no",
        },
    )
