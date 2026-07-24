"""HTTP request-body resource-limit regression tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from audio_graphy.auth.middleware import RequestBodyLimitMiddleware


def test_declared_oversized_body_is_rejected_before_route_parsing() -> None:
    app = FastAPI()
    app.add_middleware(RequestBodyLimitMiddleware, max_body_bytes=8)

    @app.post("/body")
    async def read_body(request: Request) -> dict[str, int]:
        return {"size": len(await request.body())}

    response = TestClient(app).post(
        "/body",
        content=b"123456789",
        headers={"X-Request-ID": "body-limit-test"},
    )

    assert response.status_code == 413
    assert response.headers["X-Request-ID"] == "body-limit-test"
    assert response.json() == {
        "error": {
            "code": "REQUEST_BODY_TOO_LARGE",
            "message": "Request body exceeds the configured limit",
            "detail": {"max_bytes": 8, "request_id": "body-limit-test"},
        }
    }


def test_streaming_http_client_body_is_rejected_without_content_length() -> None:
    app = FastAPI()
    app.add_middleware(RequestBodyLimitMiddleware, max_body_bytes=8)

    @app.post("/body")
    async def read_body(request: Request) -> dict[str, int]:
        return {"size": len(await request.body())}

    response = TestClient(app).post(
        "/body",
        content=iter((b"12345", b"6789")),
    )

    assert response.status_code == 413
    assert response.headers["X-Request-ID"]
    assert response.json()["error"]["code"] == "REQUEST_BODY_TOO_LARGE"


@pytest.mark.asyncio
async def test_chunked_body_without_content_length_is_counted() -> None:
    messages = iter(
        (
            {"type": "http.request", "body": b"12345", "more_body": True},
            {"type": "http.request", "body": b"6789", "more_body": False},
        )
    )
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return next(messages)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def consume_body(
        _scope: dict[str, Any],
        receive_message: Callable[[], Awaitable[dict[str, Any]]],
        _send_message: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        while True:
            message = await receive_message()
            if not message.get("more_body", False):
                break

    middleware = RequestBodyLimitMiddleware(consume_body, max_body_bytes=8)
    await middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/body",
            "headers": [],
        },
        receive,
        send,
    )

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413
