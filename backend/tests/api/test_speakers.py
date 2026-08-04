"""API tests for M7 WS-3 T12: /speakers endpoints.

Coverage:
    - GET /speakers list — happy path (inspector+ only).
    - GET /speakers list — empty list returns valid response.
    - GET /speakers list — role filter works.
    - GET /speakers list — ambiguity filter works.
    - GET /speakers list — agent role forbidden (403).
    - GET /speakers list — viewer role allowed (read-only).
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
    source_speaker_label: str | None = "spk_0",
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
            source_speaker_label=source_speaker_label,
        )
        session.add(link)
        await session.commit()
        await session.refresh(link)
        return int(link.id)


async def seed_voiceprint_vector(
    factory: Any,
    *,
    speaker_id: int,
    recording_id: int,
    voiceprint_id: str,
    tenant_id: str = "chang_an",
    duration_sec: float = 12.5,
) -> None:
    """Seed the per-recording voiceprint row a detail row reports from.

    The ciphertext is a placeholder: nothing under test decrypts it, and the
    endpoint reads only the hash and the duration.
    """
    from audio_graphy.models.voiceprint_vector import VoiceprintVector

    async with factory() as session:
        session.add(
            VoiceprintVector(
                tenant_id=tenant_id,
                recording_id=recording_id,
                speaker_entity_id=speaker_id,
                voiceprint_id=voiceprint_id,
                vector_encrypted=b"\x00" * 32,
                encryption_meta={"alg": "test"},
                duration_sec=duration_sec,
            )
        )
        await session.commit()


# ============================================================
# GET /speakers list
# ============================================================


class TestListSpeakers:
    """GET /api/v1/speakers — list endpoint."""

    def test_empty_list_returns_valid_response(
        self,
        test_client: Any,
        auth_headers: Any,
        db_session_factory: Any,
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
        self,
        test_client: Any,
        auth_headers: Any,
        db_session_factory: Any,
    ) -> None:
        """Seeded speaker shows up in the list."""
        speaker_id = _run_async(seed_speaker_node(db_session_factory, tenant_id="chang_an"))
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

    def test_total_counts_the_tenant_not_the_page(
        self,
        test_client: Any,
        auth_headers: Any,
        db_session_factory: Any,
    ) -> None:
        """``total`` must survive paging, or the roster ends where the page does.

        It was ``len(nodes)`` after limit/offset, so a client asking for one row
        was told the tenant has one speaker. The UI derives its pager from this
        number, which is how 300 speakers became unreachable with nothing on
        screen saying so.
        """
        for index in range(3):
            _run_async(
                seed_speaker_node(
                    db_session_factory,
                    tenant_id="chang_an",
                    voiceprint_id=f"{index}" * 64,
                    display_name=f"speaker:page{index}",
                )
            )
        body = test_client.get(
            "/api/v1/speakers?limit=1",
            headers=auth_headers["inspector_t1"],
        ).json()
        assert len(body["items"]) == 1
        assert body["total"] >= 3

    def test_voiceprint_hash_truncated_to_8_chars(
        self,
        test_client: Any,
        auth_headers: Any,
        db_session_factory: Any,
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
        self,
        test_client: Any,
        auth_headers: Any,
        db_session_factory: Any,
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
        self,
        test_client: Any,
        auth_headers: Any,
        db_session_factory: Any,
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
        self,
        test_client: Any,
        auth_headers: Any,
    ) -> None:
        """Agent role cannot list speakers (403)."""
        resp = test_client.get(
            "/api/v1/speakers",
            headers=auth_headers["agent_t1"],
        )
        assert resp.status_code == 403

    def test_viewer_role_can_list(
        self,
        test_client: Any,
        auth_headers: Any,
    ) -> None:
        """Viewer can read the roster.

        The response holds no biometric data, and the reconfirm queue has
        always been viewer+ — gating the roster higher only let a viewer see
        a merge decision without seeing the speaker it was about.
        """
        resp = test_client.get(
            "/api/v1/speakers",
            headers=auth_headers["viewer_t1"],
        )
        assert resp.status_code == 200

    def test_viewer_role_can_read_detail(
        self,
        test_client: Any,
        auth_headers: Any,
        db_session_factory: Any,
    ) -> None:
        speaker_id = _run_async(seed_speaker_node(db_session_factory, tenant_id="chang_an"))
        resp = test_client.get(
            f"/api/v1/speakers/{speaker_id}",
            headers=auth_headers["viewer_t1"],
        )
        assert resp.status_code == 200

    def test_viewer_still_cannot_write(
        self,
        test_client: Any,
        auth_headers: Any,
    ) -> None:
        """Read access does not imply review authority."""
        resp = test_client.post(
            "/api/v1/speakers/1/reject-merge",
            headers=auth_headers["viewer_t1"],
            json={},
        )
        assert resp.status_code == 403

    def test_unauthenticated_returns_401(self, test_client: Any) -> None:
        """No Authorization header → 401."""
        resp = test_client.get("/api/v1/speakers")
        assert resp.status_code == 401

    def test_tenant_isolation(
        self,
        test_client: Any,
        auth_headers: Any,
        db_session_factory: Any,
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
        self,
        test_client: Any,
        auth_headers: Any,
        db_session_factory: Any,
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
        self,
        test_client: Any,
        auth_headers: Any,
    ) -> None:
        """Non-existent speaker_id → 404."""
        resp = test_client.get(
            "/api/v1/speakers/99999",
            headers=auth_headers["inspector_t1"],
        )
        assert resp.status_code == 404

    def test_404_when_cross_tenant(
        self,
        test_client: Any,
        auth_headers: Any,
        db_session_factory: Any,
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
        self,
        test_client: Any,
        auth_headers: Any,
        db_session_factory: Any,
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

    def test_voiceprint_is_per_recording_not_the_node_hash(
        self,
        test_client: Any,
        auth_headers: Any,
        db_session_factory: Any,
    ) -> None:
        """Each row reports the voiceprint that recording contributed, or none.

        This used to print the canonical node's hash on every row, so a speaker
        assembled from one voiceprint link and two fuzzy links showed three
        identical hashes under a column headed "Voiceprint" — beside a cosine
        column that correctly read "—" for the fuzzy rows. The two columns
        contradicted each other and the hash was the one asserting evidence that
        did not exist.
        """
        speaker_id = _run_async(
            seed_speaker_node(
                db_session_factory,
                tenant_id="chang_an",
                voiceprint_id="n" * 64,
                recordings_list=[301, 302],
                recordings_count=2,
            )
        )
        _run_async(
            seed_speaker_link(
                db_session_factory,
                canonical_id=speaker_id,
                source_id=speaker_id,
                recording_id=301,
                tenant_id="chang_an",
            )
        )
        _run_async(
            seed_speaker_link(
                db_session_factory,
                canonical_id=speaker_id,
                source_id=speaker_id,
                recording_id=302,
                tenant_id="chang_an",
                strategy="fuzzy",
            )
        )
        # Only recording 301 contributed a voiceprint.
        _run_async(
            seed_voiceprint_vector(
                db_session_factory,
                speaker_id=speaker_id,
                recording_id=301,
                voiceprint_id="c0ffee" + "0" * 58,
                duration_sec=42.0,
            )
        )

        body = test_client.get(
            f"/api/v1/speakers/{speaker_id}",
            headers=auth_headers["inspector_t1"],
        ).json()
        refs = {ref["recording_id"]: ref for ref in body["related_recordings"]}

        assert refs[301]["voiceprint_id"] == "vp_c0ffee00"
        assert refs[301]["duration_sec"] == 42.0
        # Not the node hash ("n"*64 -> vp_nnnnnnnn), and not the sibling's.
        assert refs[302]["voiceprint_id"] is None
        assert refs[302]["duration_sec"] == 0.0

    def test_related_links_are_tenant_scoped(
        self,
        test_client: Any,
        auth_headers: Any,
        db_session_factory: Any,
    ) -> None:
        """A link whose tenant disagrees with its speaker is not rendered.

        The query relied on SpeakerNode ids being globally unique rather than
        carrying the predicate every other SpeakerLink query in the module
        carries. No in-application writer can produce such a row today; a
        restored dump or a hand-written repair can.
        """
        speaker_id = _run_async(
            seed_speaker_node(
                db_session_factory,
                tenant_id="chang_an",
                voiceprint_id="t" * 64,
                recordings_list=[401],
            )
        )
        _run_async(
            seed_speaker_link(
                db_session_factory,
                canonical_id=speaker_id,
                source_id=speaker_id,
                recording_id=401,
                tenant_id="chang_an",
            )
        )
        _run_async(
            seed_speaker_link(
                db_session_factory,
                canonical_id=speaker_id,
                source_id=speaker_id,
                recording_id=999,
                tenant_id="byd",
            )
        )

        body = test_client.get(
            f"/api/v1/speakers/{speaker_id}",
            headers=auth_headers["inspector_t1"],
        ).json()
        assert [ref["recording_id"] for ref in body["related_recordings"]] == [401]


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


class TestRecordingFilter:
    """GET /speakers?recording_id=N — powers the ?focus=录音:N deep link."""

    def test_filters_to_speakers_linked_to_that_recording(
        self,
        test_client: Any,
        auth_headers: Any,
        db_session_factory: Any,
    ) -> None:
        wanted = _run_async(
            seed_speaker_node(
                db_session_factory,
                tenant_id="chang_an",
                voiceprint_id="c" * 64,
                display_name="speaker:vp_cccccccc",
            )
        )
        other = _run_async(
            seed_speaker_node(
                db_session_factory,
                tenant_id="chang_an",
                voiceprint_id="d" * 64,
                display_name="speaker:vp_dddddddd",
            )
        )
        _run_async(
            seed_speaker_link(
                db_session_factory,
                canonical_id=wanted,
                source_id=wanted,
                recording_id=8801,
            )
        )
        _run_async(
            seed_speaker_link(
                db_session_factory,
                canonical_id=other,
                source_id=other,
                recording_id=8802,
            )
        )

        resp = test_client.get(
            "/api/v1/speakers?recording_id=8801",
            headers=auth_headers["inspector_t1"],
        )
        assert resp.status_code == 200, resp.text
        assert [i["id"] for i in resp.json()["items"]] == [wanted]

    def test_unknown_recording_returns_empty(
        self,
        test_client: Any,
        auth_headers: Any,
        db_session_factory: Any,
    ) -> None:
        _run_async(seed_speaker_node(db_session_factory, tenant_id="chang_an"))
        resp = test_client.get(
            "/api/v1/speakers?recording_id=999999",
            headers=auth_headers["inspector_t1"],
        )
        assert resp.json()["items"] == []


class TestRecordingRefScores:
    """The per-link cosine was stored but never exposed."""

    def test_detail_exposes_link_cosine_and_confidence(
        self,
        test_client: Any,
        auth_headers: Any,
        db_session_factory: Any,
    ) -> None:
        speaker_id = _run_async(seed_speaker_node(db_session_factory, tenant_id="chang_an"))
        _run_async(
            seed_speaker_link(
                db_session_factory,
                canonical_id=speaker_id,
                source_id=speaker_id,
                recording_id=8810,
                cosine=0.62,
            )
        )
        resp = test_client.get(
            f"/api/v1/speakers/{speaker_id}",
            headers=auth_headers["inspector_t1"],
        )
        assert resp.status_code == 200, resp.text
        ref = resp.json()["related_recordings"][0]
        assert ref["cosine_similarity"] == pytest.approx(0.62)
        assert ref["merge_confidence"] == pytest.approx(0.62)


class TestRecordingSpeakers:
    """GET /recordings/{id}/speakers — resolves spk_N to a real speaker."""

    def test_maps_diarization_labels_to_speakers(
        self,
        test_client: Any,
        auth_headers: Any,
        db_session_factory: Any,
    ) -> None:
        speaker_id = _run_async(
            seed_speaker_node(
                db_session_factory,
                tenant_id="chang_an",
                voiceprint_id="e" * 64,
                display_name="speaker:vp_eeeeeeee",
                speaker_role="customer",
            )
        )
        _run_async(
            seed_speaker_link(
                db_session_factory,
                canonical_id=speaker_id,
                source_id=speaker_id,
                recording_id=8820,
                cosine=0.64,
                ambiguity_tag="AMBIGUOUS",
                source_speaker_label="spk_1",
            )
        )

        resp = test_client.get(
            "/api/v1/recordings/8820/speakers",
            headers=auth_headers["viewer_t1"],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["recording_id"] == 8820
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert item["source_speaker_label"] == "spk_1"
        assert item["speaker_node_id"] == speaker_id
        assert item["speaker_role"] == "customer"
        assert item["ambiguity_tag"] == "AMBIGUOUS"
        assert item["cosine_similarity"] == pytest.approx(0.64)

    def test_links_without_a_label_are_omitted(
        self,
        test_client: Any,
        auth_headers: Any,
        db_session_factory: Any,
    ) -> None:
        """Pre-0035 links cannot be mapped; guessing would misattribute speech."""
        speaker_id = _run_async(
            seed_speaker_node(
                db_session_factory,
                tenant_id="chang_an",
                voiceprint_id="f" * 64,
            )
        )
        _run_async(
            seed_speaker_link(
                db_session_factory,
                canonical_id=speaker_id,
                source_id=speaker_id,
                recording_id=8821,
                source_speaker_label=None,
            )
        )
        resp = test_client.get(
            "/api/v1/recordings/8821/speakers",
            headers=auth_headers["viewer_t1"],
        )
        assert resp.json()["items"] == []

    def test_agent_role_allowed(
        self,
        test_client: Any,
        auth_headers: Any,
    ) -> None:
        """Agents are the role this endpoint exists for.

        ``require_role`` matches names, not levels, so agent (level 2) was being
        refused while viewer (level 1) was admitted. Agents create the streaming
        recordings and the reception workspace is open to them; without this
        every timeline line renders a raw ``spk_N`` for the people who recorded
        it. The roster endpoints stay closed to agent — they carry no
        agent-ownership filter — which is asserted separately above.
        """
        resp = test_client.get(
            "/api/v1/recordings/8820/speakers",
            headers=auth_headers["agent_t1"],
        )
        assert resp.status_code == 200

    def test_cross_tenant_returns_empty(
        self,
        test_client: Any,
        auth_headers: Any,
        db_session_factory: Any,
    ) -> None:
        speaker_id = _run_async(
            seed_speaker_node(
                db_session_factory,
                tenant_id="chang_an",
                voiceprint_id="9" * 64,
            )
        )
        _run_async(
            seed_speaker_link(
                db_session_factory,
                canonical_id=speaker_id,
                source_id=speaker_id,
                recording_id=8822,
                source_speaker_label="spk_0",
            )
        )
        resp = test_client.get(
            "/api/v1/recordings/8822/speakers",
            headers=auth_headers["viewer_t2"],
        )
        assert resp.json()["items"] == []

    def test_conflicting_links_report_the_later_one(
        self,
        test_client: Any,
        auth_headers: Any,
        db_session_factory: Any,
    ) -> None:
        """A confirmed merge supersedes the machine guess for the same label."""
        guessed = _run_async(
            seed_speaker_node(
                db_session_factory,
                tenant_id="chang_an",
                voiceprint_id="1" * 64,
                display_name="speaker:vp_11111111",
            )
        )
        confirmed = _run_async(
            seed_speaker_node(
                db_session_factory,
                tenant_id="chang_an",
                voiceprint_id="2" * 64,
                display_name="speaker:vp_22222222",
            )
        )
        _run_async(
            seed_speaker_link(
                db_session_factory,
                canonical_id=guessed,
                source_id=guessed,
                recording_id=8830,
                strategy="voiceprint",
                source_speaker_label="spk_0",
            )
        )
        _run_async(
            seed_speaker_link(
                db_session_factory,
                canonical_id=confirmed,
                source_id=confirmed,
                recording_id=8830,
                strategy="fuzzy",
                source_speaker_label="spk_0",
            )
        )

        resp = test_client.get(
            "/api/v1/recordings/8830/speakers",
            headers=auth_headers["inspector_t1"],
        )
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["speaker_node_id"] == confirmed
        assert items[0]["strategy"] == "fuzzy"
