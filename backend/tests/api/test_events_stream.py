"""The SSE feed: cursor semantics, tenant walls, and the agent's narrow window.

Streaming is read with the TestClient's stream API — a few frames, then close;
the endpoint's tail loop must tolerate that without leaking (the generator is
closed with the response).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit


def _seed_events(test_client: TestClient, rows: list[dict]) -> None:
    from audio_graphy.models.domain_event import DomainEvent

    from .conftest import _run_async

    factory = test_client.app.state.session_factory

    async def _write() -> None:
        async with factory() as session, session.begin():
            for row in rows:
                session.add(DomainEvent(**row))

    _run_async(_write())


def _read_frames(test_client: TestClient, url: str, headers: dict, count: int) -> list[dict]:
    """Read exactly ``count`` frames via a finite stream.

    TestClient buffers a response until the ASGI app returns, so the request
    must bound the stream itself: max_events closes it after the frames we
    want, and idle_timeout_sec is the safety net that turns "the filter ate
    everything" into an empty list instead of a hung test."""

    bounded = f"{url}&max_events={count}&idle_timeout_sec=3"
    frames: list[dict] = []
    with test_client.stream("GET", bounded, headers=headers) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if line.startswith("data: "):
                frames.append(json.loads(line.removeprefix("data: ")))
                if len(frames) >= count:
                    break
    return frames


_EVENT = {
    "tenant_id": "chang_an",
    "event_type": "recording.indexed",
    "aggregate_type": "recording",
    "aggregate_id": "11",
    "payload": {"recording_id": 11, "status": "indexed", "agent_user_id": None},
}


class TestStream:
    def test_replays_from_the_cursor_in_order(
        self, test_client: TestClient, auth_headers: dict
    ) -> None:
        _seed_events(
            test_client,
            [
                _EVENT,
                {
                    **_EVENT,
                    "event_type": "tag_job.completed",
                    "aggregate_type": "tag_job",
                    "aggregate_id": "5",
                    "payload": {"job_id": 5, "status": "completed"},
                },
            ],
        )
        frames = _read_frames(
            test_client,
            "/api/v1/events/stream?after=0",
            auth_headers["admin_t1"],
            count=2,
        )
        assert [frame["event_type"] for frame in frames] == [
            "recording.indexed",
            "tag_job.completed",
        ]
        assert frames[0]["id"] < frames[1]["id"]

        # Resume from the first frame's id: only the second comes back.
        resumed = _read_frames(
            test_client,
            f"/api/v1/events/stream?after={frames[0]['id']}",
            auth_headers["admin_t1"],
            count=1,
        )
        assert resumed[0]["id"] == frames[1]["id"]

    def test_type_filter_narrows_the_feed(
        self, test_client: TestClient, auth_headers: dict
    ) -> None:
        _seed_events(
            test_client,
            [
                _EVENT,
                {
                    **_EVENT,
                    "event_type": "tag_job.completed",
                    "aggregate_type": "tag_job",
                    "aggregate_id": "5",
                    "payload": {"job_id": 5, "status": "completed"},
                },
            ],
        )
        frames = _read_frames(
            test_client,
            "/api/v1/events/stream?after=0&types=tag_job.completed",
            auth_headers["admin_t1"],
            count=1,
        )
        assert frames[0]["event_type"] == "tag_job.completed"

    def test_the_feed_is_tenant_scoped(self, test_client: TestClient, auth_headers: dict) -> None:
        _seed_events(
            test_client,
            [
                _EVENT,
                {
                    **_EVENT,
                    "tenant_id": "byd",
                    "aggregate_id": "99",
                    "payload": {"recording_id": 99, "status": "indexed", "agent_user_id": None},
                },
            ],
        )
        frames = _read_frames(
            test_client,
            "/api/v1/events/stream?after=0",
            auth_headers["admin_t2"],
            count=1,
        )
        assert frames[0]["payload"]["recording_id"] == 99, (
            "tenant byd must see its own event first — never chang_an's"
        )

    def test_an_agent_sees_only_their_own_recordings(
        self, test_client: TestClient, auth_headers: dict
    ) -> None:
        """Payloads are id-and-status only, but WHICH recordings finish and
        when is itself information the role model does not grant an agent for
        other agents' work — the stream filters, not just the detail APIs."""

        from sqlalchemy import select

        from audio_graphy.models.user import User

        from .conftest import _run_async

        factory = test_client.app.state.session_factory

        async def _agent_id() -> int:
            async with factory() as session:
                return (
                    (
                        await session.execute(
                            select(User.id).where(
                                User.tenant_id == "chang_an", User.role == "agent"
                            )
                        )
                    )
                    .scalars()
                    .first()
                )

        agent_id = _run_async(_agent_id())
        assert agent_id is not None

        _seed_events(
            test_client,
            [
                {
                    **_EVENT,
                    "payload": {"recording_id": 11, "status": "indexed", "agent_user_id": 999_999},
                },
                {
                    **_EVENT,
                    "event_type": "tag_job.completed",
                    "aggregate_type": "tag_job",
                    "aggregate_id": "5",
                    "payload": {"job_id": 5, "status": "completed"},
                },
                {
                    **_EVENT,
                    "aggregate_id": "12",
                    "payload": {"recording_id": 12, "status": "indexed", "agent_user_id": agent_id},
                },
            ],
        )
        frames = _read_frames(
            test_client,
            "/api/v1/events/stream?after=0",
            auth_headers["agent_t1"],
            count=1,
        )
        assert frames[0]["payload"]["recording_id"] == 12, (
            "the agent's first visible event skips another agent's recording "
            "AND the governance event"
        )

    def test_the_stream_requires_authentication(self, test_client: TestClient) -> None:
        response = test_client.get("/api/v1/events/stream")
        assert response.status_code == 401
