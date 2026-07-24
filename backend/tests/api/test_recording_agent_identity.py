"""Regression coverage for stable, server-owned recording agent identity."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from audio_graphy.models import Recording, User
from tests.api.conftest import _run_async, seed_segment, seed_tag


def _agent_headers(jwt_manager: Any, *, user_id: int) -> dict[str, str]:
    token = jwt_manager.create_access_token(user_id, "chang_an", "agent")
    return {"Authorization": f"Bearer {token}"}


async def _add_agent(
    factory: Any,
    *,
    user_id: int,
    name: str,
) -> None:
    async with factory() as session:
        session.add(
            User(
                id=user_id,
                tenant_id="chang_an",
                name=name,
                email=f"agent-{user_id}@changan.example",
                role="agent",
                password_hash="mock",
            )
        )
        await session.commit()


async def _rename_agent(
    factory: Any,
    *,
    user_id: int,
    name: str,
) -> None:
    async with factory() as session:
        user = (
            await session.execute(
                select(User).where(
                    User.id == user_id,
                    User.tenant_id == "chang_an",
                )
            )
        ).scalar_one()
        user.name = name
        await session.commit()


async def _seed_owned_recording(
    factory: Any,
    *,
    recording_id: int,
    agent_user_id: int | None,
    agent_name: str = "agent_ca",
) -> int:
    async with factory() as session:
        session.add(
            Recording(
                id=recording_id,
                tenant_id="chang_an",
                store_id="S001",
                agent_name=agent_name,
                agent_user_id=agent_user_id,
                customer_hash=f"customer-{recording_id}",
                path=f"/tmp/recording-agent-identity-{recording_id}.wav",
                status="indexed",
                pipeline_state="done",
                recorded_at=datetime.now(UTC),
                indexed_at=datetime.now(UTC),
                prompt_version="tag_prompt_v1/v1",
            )
        )
        await session.commit()
    return recording_id


@pytest.mark.integration
def test_same_name_agents_are_isolated_across_recording_read_surfaces(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    jwt_manager: Any,
    db_session_factory: Any,
) -> None:
    """A display-name collision must not widen recording access."""

    _run_async(_add_agent(db_session_factory, user_id=9, name="agent_ca"))
    first = _run_async(
        _seed_owned_recording(
            db_session_factory,
            recording_id=201,
            agent_user_id=3,
        )
    )
    second = _run_async(
        _seed_owned_recording(
            db_session_factory,
            recording_id=202,
            agent_user_id=9,
        )
    )
    _run_async(seed_segment(db_session_factory, first, "chang_an", transcript="first"))
    _run_async(seed_segment(db_session_factory, second, "chang_an", transcript="second"))
    _run_async(seed_tag(db_session_factory, first, "chang_an", tag_value="pass"))
    _run_async(seed_tag(db_session_factory, second, "chang_an", tag_value="fail"))

    second_headers = _agent_headers(jwt_manager, user_id=9)
    expected_by_headers = (
        (auth_headers["agent_t1"], first, second, "pass"),
        (second_headers, second, first, "fail"),
    )
    for headers, own_id, other_id, own_tag_value in expected_by_headers:
        queue = test_client.get("/api/v1/recordings", headers=headers)
        assert queue.status_code == 200
        assert [item["id"] for item in queue.json()["items"]] == [own_id]

        for suffix in ("", "/status", "/segments", "/tags"):
            assert (
                test_client.get(
                    f"/api/v1/recordings/{own_id}{suffix}",
                    headers=headers,
                ).status_code
                == 200
            )
            assert (
                test_client.get(
                    f"/api/v1/recordings/{other_id}{suffix}",
                    headers=headers,
                ).status_code
                == 404
            )

        stats = test_client.get(
            "/api/v1/tags/stats?group_by=tag_value",
            headers=headers,
        )
        assert stats.status_code == 200
        assert len(stats.json()["items"]) == 1
        assert stats.json()["items"][0]["tag_value"] == own_tag_value
        assert stats.json()["items"][0]["tag_count"] == 1


@pytest.mark.integration
def test_agent_rename_preserves_historical_recording_access(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    """Authorization follows the immutable user id, not the name snapshot."""

    recording_id = _run_async(
        _seed_owned_recording(
            db_session_factory,
            recording_id=203,
            agent_user_id=3,
        )
    )
    _run_async(seed_segment(db_session_factory, recording_id, "chang_an"))
    _run_async(seed_tag(db_session_factory, recording_id, "chang_an"))
    _run_async(_rename_agent(db_session_factory, user_id=3, name="renamed_agent"))

    queue = test_client.get(
        "/api/v1/recordings",
        headers=auth_headers["agent_t1"],
    )
    assert queue.status_code == 200
    assert [item["id"] for item in queue.json()["items"]] == [recording_id]
    assert queue.json()["items"][0]["agent_name"] == "agent_ca"

    for suffix in ("", "/status", "/segments", "/tags"):
        assert (
            test_client.get(
                f"/api/v1/recordings/{recording_id}{suffix}",
                headers=auth_headers["agent_t1"],
            ).status_code
            == 200
        )
    stats = test_client.get(
        "/api/v1/tags/stats?group_by=tag_value",
        headers=auth_headers["agent_t1"],
    )
    assert stats.status_code == 200
    assert stats.json()["total_records"] == 1


@pytest.mark.integration
def test_recording_registration_resolves_unique_owner_and_ambiguity_fails_closed(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    jwt_manager: Any,
    db_session_factory: Any,
) -> None:
    """The server owns the authorization key and never guesses on duplicate names."""

    working_dir = Path(test_client.app.state.settings.working_dir)
    unique_audio = working_dir / "unique-owner.wav"
    unique_audio.write_bytes(b"RIFF" + b"\x00" * 128)
    unique = test_client.post(
        "/api/v1/recordings",
        json={
            "store_id": "S001",
            "path": unique_audio.name,
            "agent_name": "agent_ca",
        },
        headers=auth_headers["admin_t1"],
    )
    assert unique.status_code == 201
    assert unique.json()["agent_user_id"] == 3

    spoofed_audio = working_dir / "spoofed-owner.wav"
    spoofed_audio.write_bytes(b"RIFF" + b"\x00" * 128)
    spoofed = test_client.post(
        "/api/v1/recordings",
        json={
            "store_id": "S001",
            "path": spoofed_audio.name,
            "agent_name": "agent_ca",
            "agent_user_id": 9,
        },
        headers=auth_headers["admin_t1"],
    )
    assert spoofed.status_code == 422

    _run_async(_add_agent(db_session_factory, user_id=9, name="agent_ca"))
    ambiguous_audio = working_dir / "ambiguous-owner.wav"
    ambiguous_audio.write_bytes(b"RIFF" + b"\x00" * 128)
    ambiguous = test_client.post(
        "/api/v1/recordings",
        json={
            "store_id": "S001",
            "path": ambiguous_audio.name,
            "agent_name": "agent_ca",
        },
        headers=auth_headers["admin_t1"],
    )
    assert ambiguous.status_code == 201
    assert ambiguous.json()["agent_user_id"] is None
    recording_id = ambiguous.json()["id"]

    for headers in (
        auth_headers["agent_t1"],
        _agent_headers(jwt_manager, user_id=9),
    ):
        assert (
            test_client.get(
                f"/api/v1/recordings/{recording_id}",
                headers=headers,
            ).status_code
            == 404
        )
    assert (
        test_client.get(
            f"/api/v1/recordings/{recording_id}",
            headers=auth_headers["inspector_t1"],
        ).status_code
        == 200
    )
