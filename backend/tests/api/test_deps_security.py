"""Security regression tests for request dependencies."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from audio_graphy.api.deps import get_current_user
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.roles import require_admin
from audio_graphy.errors import ForbiddenError, InvalidTokenError


class _Result:
    def __init__(self, row: object | None) -> None:
        self._row = row

    def scalar_one_or_none(self) -> object | None:
        return self._row


class _Session:
    def __init__(self, row: object | None) -> None:
        self._row = row

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, _statement: object) -> _Result:
        return _Result(self._row)


class _SessionFactory:
    def __init__(self, row: object | None) -> None:
        self._row = row

    def __call__(self) -> _Session:
        return _Session(self._row)


def _request(row: object | None) -> Request:
    app = FastAPI()
    app.state.session_factory = _SessionFactory(row)
    scope: dict[str, Any] = {
        "type": "http",
        "app": app,
        "method": "GET",
        "path": "/api/v1/receptions/1/audio",
        "headers": [],
        "query_string": b"",
    }
    request = Request(scope)
    request.state.user = AuthUser(
        id=7,
        name="",
        email="",
        role="admin",
        tenant_id="tenant-a",
    )
    return request


@pytest.mark.unit
async def test_deleted_user_cannot_reuse_access_token_or_playback_grant() -> None:
    with pytest.raises(InvalidTokenError, match="no longer exists"):
        await get_current_user(_request(None))


@pytest.mark.unit
async def test_database_role_replaces_stale_token_role() -> None:
    row = SimpleNamespace(
        id=7,
        name="销售甲",
        email="agent@example.com",
        role="agent",
        tenant_id="tenant-a",
    )
    request = _request(row)

    user = await get_current_user(request)

    assert user.role == "agent"
    assert request.state.agent_filter == "销售甲"


@pytest.mark.unit
async def test_role_guard_rejects_downgraded_admin_token() -> None:
    row = SimpleNamespace(
        id=7,
        name="原管理员",
        email="former-admin@example.com",
        role="viewer",
        tenant_id="tenant-a",
    )

    with pytest.raises(ForbiddenError):
        await require_admin()(_request(row))
