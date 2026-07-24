"""API contract and security tests for dialogue-tag insight analysis."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from audio_graphy.api.tag_insights import router
from audio_graphy.auth.jwt_utils import JWTManager
from audio_graphy.auth.middleware import AuthMiddleware
from audio_graphy.errors import register_exception_handlers
from audio_graphy.models.user import User
from audio_graphy.schemas.tag_insights import MAX_ASSIGNMENTS


def _body(*, tenant_id: str = "chang_an") -> dict[str, object]:
    return {
        "tenant_id": tenant_id,
        "merge_strategy": "manual_wins",
        "groups": [
            {"group_key": "model-v1", "version": "v1", "source": "llm", "priority": 10},
            {"group_key": "review", "version": "v2", "source": "manual", "priority": 20},
        ],
        "assignments": [
            {
                "group_key": "model-v1",
                "target_id": "reception-1",
                "window": {"start_ms": 0, "end_ms": 1_000},
                "label_key": "stage.greeting",
                "value": "fail",
                "confidence": 0.7,
                "evidence_refs": [
                    {
                        "ref_id": "audio-1",
                        "kind": "audio",
                        "recording_id": "rec-1",
                        "start_ms": 0,
                        "end_ms": 1_000,
                    }
                ],
            },
            {
                "group_key": "review",
                "target_id": "reception-1",
                "window": {"start_ms": 0, "end_ms": 1_000},
                "label_key": "stage.greeting",
                "value": "pass",
                "confidence": 1,
                "is_manual": True,
                "evidence_refs": [
                    {
                        "ref_id": "text-1",
                        "kind": "text",
                        "recording_id": "rec-1",
                        "start_ms": 100,
                        "end_ms": 500,
                        "text_excerpt": "您好，欢迎光临",
                    }
                ],
            },
        ],
    }


@pytest.fixture
def tag_insights_client() -> Iterator[TestClient]:
    manager = JWTManager("test-secret-32-chars-minimum-length!!")
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with engine.begin() as connection:
            await connection.run_sync(User.__table__.create)
        async with session_factory() as session:
            session.add_all(
                [
                    User(
                        id=1,
                        tenant_id="chang_an",
                        name="测试管理员",
                        email="admin@chang-an.test",
                        role="admin",
                    ),
                    User(
                        id=2,
                        tenant_id="chang_an",
                        name="测试质检员",
                        email="inspector@chang-an.test",
                        role="inspector",
                    ),
                    User(
                        id=3,
                        tenant_id="chang_an",
                        name="测试查看者",
                        email="viewer@chang-an.test",
                        role="viewer",
                    ),
                ]
            )
            await session.commit()
        app.state.session_factory = session_factory
        yield
        await engine.dispose()

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(AuthMiddleware, jwt_manager=manager)
    register_exception_handlers(app)
    app.include_router(router, prefix="/api/v1")
    with TestClient(app, raise_server_exceptions=False) as client:
        client.app.state.jwt_manager = manager
        yield client


@pytest.fixture
def insight_auth_headers() -> dict[str, dict[str, str]]:
    manager = JWTManager("test-secret-32-chars-minimum-length!!")

    def header(*, user_id: int, tenant_id: str, role: str) -> dict[str, str]:
        token = manager.create_access_token(user_id, tenant_id, role)
        return {"Authorization": f"Bearer {token}"}

    return {
        "admin_t1": header(user_id=1, tenant_id="chang_an", role="admin"),
        "inspector_t1": header(
            user_id=2,
            tenant_id="chang_an",
            role="inspector",
        ),
        "viewer_t1": header(user_id=3, tenant_id="chang_an", role="viewer"),
    }


def test_analyze_requires_authentication(tag_insights_client: TestClient) -> None:
    response = tag_insights_client.post("/api/v1/tag-insights/analyze", json=_body())
    assert response.status_code == 401


def test_analyze_requires_inspector_or_admin(
    tag_insights_client: TestClient,
    insight_auth_headers: dict[str, dict[str, str]],
) -> None:
    response = tag_insights_client.post(
        "/api/v1/tag-insights/analyze",
        json=_body(),
        headers=insight_auth_headers["viewer_t1"],
    )
    assert response.status_code == 403


def test_analyze_rejects_cross_tenant_payload(
    tag_insights_client: TestClient,
    insight_auth_headers: dict[str, dict[str, str]],
) -> None:
    response = tag_insights_client.post(
        "/api/v1/tag-insights/analyze",
        json=_body(tenant_id="byd"),
        headers=insight_auth_headers["admin_t1"],
    )
    assert response.status_code == 403


def test_analyze_returns_matrix_and_evidence(
    tag_insights_client: TestClient,
    insight_auth_headers: dict[str, dict[str, str]],
) -> None:
    response = tag_insights_client.post(
        "/api/v1/tag-insights/analyze",
        json=_body(),
        headers=insight_auth_headers["inspector_t1"],
    )

    assert response.status_code == 200
    data = response.json()
    assert data["tenant_id"] == "chang_an"
    assert data["overview"]["conflict_cells"] == 1
    assert data["matrix"][0]["merged"]["values"] == ["pass"]
    assert data["matrix"][0]["merged"]["evidence_refs"][0]["ref_id"] == "text-1"


def test_analyze_rejects_empty_and_oversized_payloads(
    tag_insights_client: TestClient,
    insight_auth_headers: dict[str, dict[str, str]],
) -> None:
    empty = tag_insights_client.post(
        "/api/v1/tag-insights/analyze",
        json={"tenant_id": "chang_an", "groups": [], "assignments": []},
        headers=insight_auth_headers["admin_t1"],
    )
    assert empty.status_code == 422

    body = _body()
    first_assignment = body["assignments"][0]  # type: ignore[index]
    body["groups"] = [body["groups"][0]]  # type: ignore[index]
    body["assignments"] = [first_assignment] * (MAX_ASSIGNMENTS + 1)
    oversized = tag_insights_client.post(
        "/api/v1/tag-insights/analyze",
        json=body,
        headers=insight_auth_headers["admin_t1"],
    )
    assert oversized.status_code == 422
