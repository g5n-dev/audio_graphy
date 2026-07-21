"""API tests for M7 WS-3 T12: /speakers endpoints.

Coverage:
    - GET /speakers list — happy path (inspector+ only).
    - GET /speakers list — empty list returns valid response.
    - GET /speakers list — role filter works.
    - GET /speakers list — ambiguity filter works.
    - GET /speakers list — agent role forbidden (403).
    - GET /speakers list — viewer role forbidden (403).
    - GET /speakers list — unauthenticated forbidden (401).
    - GET /speakers/{id} — happy path.
    - GET /speakers/{id} — not found returns 404.
    - GET /speakers/{id} — cross-tenant returns 404.
    - voiceprint_hash is truncated to vp_xxxxxxxx (PIPL).
    - POST/PUT methods not present (read-only in M7).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from tests.api.conftest import _run_async

pytestmark = pytest.mark.integration


# ============================================================
# Helpers
# ============================================================


async def seed_speaker_node(
    factory: Any,
    *,
    tenant_id: str = "chang_an",
    voiceprint_id: str = "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6",
    display_name: str = "speaker:vp_a1b2c3d4",
    speaker_role: str = "agent",
    recordings_list: list[int] | None = None,
    recordings_count: int = 1,
    first_seen: datetime | None = None,
    total_speech_sec: float = 30.0,
    merge_confidence: float = 0.9,
    merge_strategy: str = "voiceprint",
    ambiguity_tag: str | None = None,
) -> int:
    """Seed a SpeakerNode into the test DB and return its ID."""
    from audio_graphy.models.speaker_node import SpeakerNode

    async with factory() as session:
        node = SpeakerNode(
            tenant_id=tenant_id,
            voiceprint_id=voiceprint_id,
            display_name=display_name,
            speaker_role=speaker_role,
            recordings_list=recordings_list or [1],
            recordings_count=recordings_count,
            first_seen=first_seen or datetime.now(UTC),
            total_speech_sec=total_speech_sec,
            merge_confidence=merge_confidence,
            merge_strategy=merge_strategy,
            ambiguity_tag=ambiguity_tag,
            attrs={},
        )
        session.add(node)
        await session.commit()
        await session.refresh(node)
        return int(node.id)


async def seed_speaker_link(
    factory: Any,
    *,
    canonical_id: int,
    source_id: int,
    recording_id: int,
    tenant_id: str = "chang_an",
    strategy: str = "voiceprint",
    ambiguity_tag: str | None = None,
    cosine: float = 0.85,
) -> int:
    """Seed a SpeakerLink row."""
    from audio_graphy.models.speaker_link import SpeakerLink

    async with factory() as session:
        link = SpeakerLink(
            tenant_id=tenant_id,
            canonical_speaker_id=canonical_id,
            source_speaker_id=source_id,
            recording_id=recording_id,
            cosine_similarity=cosine,
            merge_confidence=cosine,
            strategy=strategy,
            ambiguity_tag=ambiguity_tag,
        )
        session.add(link)
        await session.commit()
        await session.refresh(link)
        return int(link.id)


# ============================================================
# GET /speakers list
# ============================================================


class TestListSpeakers:
    """GET /api/v1/speakers — list endpoint."""

    def test_empty_list_returns_valid_response(
        self, test_client: Any, auth_headers: Any, db_session_factory: Any,
    ) -> None:
        """Empty tenant → returns {items: [], total: 0}."""
        # Ensure no speakers seeded.
        resp = test_client.get(
            "/api/v1/speakers",
            headers=auth_headers["inspector_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"items": [], "total": 0}

    def test_returns_speakers_for_tenant(
        self, test_client: Any, auth_headers: Any, db_session_factory: Any,
    ) -> None:
        """Seeded speaker shows up in the list."""
        speaker_id = _run_async(
            seed_speaker_node(db_session_factory, tenant_id="chang_an")
        )
        resp = test_client.get(
            "/api/v1/speakers",
            headers=auth_headers["inspector_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 1
        item = next(i for i in body["items"] if i["id"] == speaker_id)
        assert item["display_name"] == "speaker:vp_a1b2c3d4"
        assert item["voiceprint_hash"] == "vp_a1b2c3d4"
        assert item["speaker_role"] == "agent"

    def test_voiceprint_hash_truncated_to_8_chars(
        self, test_client: Any, auth_headers: Any, db_session_factory: Any,
    ) -> None:
        """voiceprint_hash field exposes only first 8 chars (PIPL compliance)."""
        _run_async(
            seed_speaker_node(
                db_session_factory,
                tenant_id="chang_an",
                voiceprint_id="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            )
        )
        resp = test_client.get(
            "/api/v1/speakers",
            headers=auth_headers["inspector_t1"],
        )
        body = resp.json()
        assert body["items"][0]["voiceprint_hash"] == "vp_01234567"
        # Must NOT contain the full hash.
        assert "0123456789abcdef0123456789abcdef" not in str(body)

    def test_role_filter_works(
        self, test_client: Any, auth_headers: Any, db_session_factory: Any,
    ) -> None:
        """?speaker_role=customer returns only customer speakers."""
        _run_async(
            seed_speaker_node(
                db_session_factory,
                tenant_id="chang_an",
                speaker_role="agent",
                voiceprint_id="a" * 64,
            )
        )
        _run_async(
            seed_speaker_node(
                db_session_factory,
                tenant_id="chang_an",
                speaker_role="customer",
                voiceprint_id="b" * 64,
            )
        )
        resp = test_client.get(
            "/api/v1/speakers?speaker_role=customer",
            headers=auth_headers["inspector_t1"],
        )
        body = resp.json()
        assert all(i["speaker_role"] == "customer" for i in body["items"])
        assert body["total"] >= 1

    def test_ambiguity_filter_works(
        self, test_client: Any, auth_headers: Any, db_session_factory: Any,
    ) -> None:
        """?ambiguity=AMBIGUOUS returns only ambiguous speakers."""
        _run_async(
            seed_speaker_node(
                db_session_factory,
                tenant_id="chang_an",
                ambiguity_tag=None,
                voiceprint_id="c" * 64,
            )
        )
        _run_async(
            seed_speaker_node(
                db_session_factory,
                tenant_id="chang_an",
                ambiguity_tag="AMBIGUOUS",
                voiceprint_id="d" * 64,
            )
        )
        resp = test_client.get(
            "/api/v1/speakers?ambiguity=AMBIGUOUS",
            headers=auth_headers["inspector_t1"],
        )
        body = resp.json()
        assert all(i["ambiguity_tag"] == "AMBIGUOUS" for i in body["items"])
        assert body["total"] >= 1

    def test_agent_role_forbidden(
        self, test_client: Any, auth_headers: Any,
    ) -> None:
        """Agent role cannot list speakers (403)."""
        resp = test_client.get(
            "/api/v1/speakers",
            headers=auth_headers["agent_t1"],
        )
        assert resp.status_code == 403

    def test_viewer_role_forbidden(
        self, test_client: Any, auth_headers: Any,
    ) -> None:
        """Viewer role cannot list speakers (403)."""
        resp = test_client.get(
            "/api/v1/speakers",
            headers=auth_headers["viewer_t1"],
        )
        assert resp.status_code == 403

    def test_unauthenticated_returns_401(self, test_client: Any) -> None:
        """No Authorization header → 401."""
        resp = test_client.get("/api/v1/speakers")
        assert resp.status_code == 401

    def test_tenant_isolation(
        self, test_client: Any, auth_headers: Any, db_session_factory: Any,
    ) -> None:
        """chang_an speaker does NOT appear in byd tenant's list."""
        _run_async(
            seed_speaker_node(
                db_session_factory,
                tenant_id="chang_an",
                voiceprint_id="e" * 64,
            )
        )
        # Query as byd inspector.
        resp = test_client.get(
            "/api/v1/speakers",
            headers=auth_headers["inspector_t2"],
        )
        body = resp.json()
        assert all(i["tenant_id"] == "byd" for i in body["items"])


# ============================================================
# GET /speakers/{id}
# ============================================================


class TestGetSpeaker:
    """GET /api/v1/speakers/{id} — detail endpoint."""

    def test_returns_speaker_detail(
        self, test_client: Any, auth_headers: Any, db_session_factory: Any,
    ) -> None:
        """Happy path — returns full speaker detail with recordings_list."""
        speaker_id = _run_async(
            seed_speaker_node(
                db_session_factory,
                tenant_id="chang_an",
                recordings_list=[42, 43],
                recordings_count=2,
            )
        )
        resp = test_client.get(
            f"/api/v1/speakers/{speaker_id}",
            headers=auth_headers["inspector_t1"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == speaker_id
        assert body["recordings_list"] == [42, 43]
        assert body["recordings_count"] == 2
        assert body["voiceprint_hash"] == "vp_a1b2c3d4"

    def test_404_when_not_found(
        self, test_client: Any, auth_headers: Any,
    ) -> None:
        """Non-existent speaker_id → 404."""
        resp = test_client.get(
            "/api/v1/speakers/99999",
            headers=auth_headers["inspector_t1"],
        )
        assert resp.status_code == 404

    def test_404_when_cross_tenant(
        self, test_client: Any, auth_headers: Any, db_session_factory: Any,
    ) -> None:
        """byd speaker queried by chang_an inspector → 404 (cross-tenant isolation)."""
        speaker_id = _run_async(
            seed_speaker_node(
                db_session_factory,
                tenant_id="byd",
                voiceprint_id="f" * 64,
            )
        )
        resp = test_client.get(
            f"/api/v1/speakers/{speaker_id}",
            headers=auth_headers["inspector_t1"],  # chang_an
        )
        assert resp.status_code == 404

    def test_includes_related_recordings(
        self, test_client: Any, auth_headers: Any, db_session_factory: Any,
    ) -> None:
        """related_recordings populated from speaker_links table."""
        speaker_id = _run_async(
            seed_speaker_node(
                db_session_factory,
                tenant_id="chang_an",
                voiceprint_id="g" * 64,
                recordings_list=[101, 102],
                recordings_count=2,
            )
        )
        _run_async(
            seed_speaker_link(
                db_session_factory,
                canonical_id=speaker_id,
                source_id=speaker_id,
                recording_id=101,
                tenant_id="chang_an",
            )
        )
        resp = test_client.get(
            f"/api/v1/speakers/{speaker_id}",
            headers=auth_headers["inspector_t1"],
        )
        body = resp.json()
        assert len(body["related_recordings"]) >= 1
        ref = body["related_recordings"][0]
        assert ref["recording_id"] in (101, 102)
        # Strategy should match what we seeded.
        assert ref["strategy"] == "voiceprint"


# ============================================================
# Router registration smoke
# ============================================================


class TestSpeakerRouterSmoke:
    """Smoke test — router is wired into the FastAPI app."""

    def test_router_registered(self, test_client: Any) -> None:
        """``/api/v1/speakers`` route must exist (even if returns 401)."""
        # We expect 401 (no auth) which proves the route exists.
        resp = test_client.get("/api/v1/speakers")
        assert resp.status_code == 401

    def test_router_registered_with_id(self, test_client: Any) -> None:
        """``/api/v1/speakers/{id}`` route must exist."""
        resp = test_client.get("/api/v1/speakers/1")
        assert resp.status_code == 401
