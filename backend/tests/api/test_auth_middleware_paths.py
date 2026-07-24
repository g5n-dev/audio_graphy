"""Regression tests for the authentication middleware public-path allow list."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from audio_graphy.auth.jwt_utils import JWTManager
from audio_graphy.auth.middleware import AuthMiddleware


def _client() -> TestClient:
    app = FastAPI()
    app.add_middleware(
        AuthMiddleware,
        jwt_manager=JWTManager("test-secret-32-chars-minimum-length!!"),
    )

    @app.get("/")
    async def root() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/v1/auth/login")
    async def login() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/private/")
    @app.get("/private/health")
    async def private() -> dict[str, bool]:
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


def test_exact_public_paths_remain_available_without_a_token() -> None:
    with _client() as client:
        assert client.get("/").status_code == 200
        assert client.get("/health").status_code == 200
        assert client.post("/api/v1/auth/login").status_code == 200


def test_public_looking_suffixes_do_not_bypass_authentication() -> None:
    with _client() as client:
        trailing_slash = client.get("/private/")
        health_suffix = client.get("/private/health")

    assert trailing_slash.status_code == 401
    assert health_suffix.status_code == 401
    assert trailing_slash.json()["error"]["code"] == "INVALID_TOKEN"
    assert health_suffix.json()["error"]["code"] == "INVALID_TOKEN"
