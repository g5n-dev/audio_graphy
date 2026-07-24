"""Automatic reception discovery, review and acceptance API contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import _run_async, seed_recording


async def _set_recording_metadata(
    factory: Any,
    recording_id: int,
    *,
    recorded_at: datetime | None,
    customer_hash: str | None = "customer-a",
) -> None:
    from sqlalchemy import select

    from audio_graphy.models import Recording

    async with factory() as session:
        result = await session.execute(select(Recording).where(Recording.id == recording_id))
        recording = result.scalar_one()
        recording.recorded_at = recorded_at
        recording.customer_hash = customer_hash
        await session.commit()


async def _seed_segments(
    factory: Any,
    recording_id: int,
    items: list[tuple[float, float, str]],
) -> None:
    from audio_graphy.models import Segment

    async with factory() as session:
        session.add_all(
            [
                Segment(
                    tenant_id="chang_an",
                    recording_id=recording_id,
                    idx=index,
                    start_sec=start_sec,
                    end_sec=end_sec,
                    transcript=text,
                    text_scrubbed=text,
                    speaker="agent_ca" if index % 2 == 0 else "customer",
                    vad_conf=0.98,
                )
                for index, (start_sec, end_sec, text) in enumerate(items)
            ]
        )
        await session.commit()


async def _clear_recording_segment_scrubbed_text(
    factory: Any,
    recording_id: int,
) -> None:
    from sqlalchemy import select

    from audio_graphy.models import Segment

    async with factory() as session:
        segments = list(
            (await session.execute(select(Segment).where(Segment.recording_id == recording_id)))
            .scalars()
            .all()
        )
        assert segments
        for segment in segments:
            segment.text_scrubbed = None
        await session.commit()


async def _seed_reception(
    factory: Any,
    *,
    tenant_id: str,
    store_id: str,
    agent_name: str,
    status: str,
    started_at: datetime,
) -> int:
    from audio_graphy.models import Reception

    async with factory() as session:
        reception = Reception(
            tenant_id=tenant_id,
            external_session_id=None,
            scenario="automotive",
            store_id=store_id,
            agent_name=agent_name,
            agent_user_id=(
                3
                if tenant_id == "chang_an" and agent_name == "agent_ca"
                else 7
                if tenant_id == "byd" and agent_name == "agent_byd"
                else None
            ),
            customer_hash=None,
            status=status,
            merge_mode="logical",
            merge_confidence=0.9,
            started_at=started_at,
            ended_at=started_at + timedelta(minutes=10),
            version=1,
        )
        session.add(reception)
        await session.commit()
        await session.refresh(reception)
        return reception.id


async def _reception_count(factory: Any) -> int:
    from sqlalchemy import func, select

    from audio_graphy.models import Reception

    async with factory() as session:
        result = await session.execute(select(func.count(Reception.id)))
        return int(result.scalar_one())


async def _split_persistence_counts(
    factory: Any,
    *,
    recording_id: int,
) -> tuple[int, int, int]:
    from sqlalchemy import func, select

    from audio_graphy.models import ProvenanceEvent, Reception, ReceptionRecording

    async with factory() as session:
        receptions = await session.scalar(select(func.count(Reception.id)))
        mappings = await session.scalar(
            select(func.count(ReceptionRecording.id)).where(
                ReceptionRecording.recording_id == recording_id
            )
        )
        provenance = await session.scalar(
            select(func.count(ProvenanceEvent.id)).where(
                ProvenanceEvent.object_type == "recording",
                ProvenanceEvent.object_ref == str(recording_id),
                ProvenanceEvent.event_type == "split",
            )
        )
        return int(receptions or 0), int(mappings or 0), int(provenance or 0)


async def _source_recording_snapshot(factory: Any, recording_id: int) -> dict[str, Any]:
    from sqlalchemy import select

    from audio_graphy.models import Recording

    async with factory() as session:
        recording = (
            await session.execute(select(Recording).where(Recording.id == recording_id))
        ).scalar_one()
        return recording.to_dict()


async def _split_provenance_payloads(
    factory: Any,
    *,
    recording_id: int,
) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from audio_graphy.models import ProvenanceEvent

    async with factory() as session:
        rows = await session.execute(
            select(ProvenanceEvent)
            .where(
                ProvenanceEvent.object_type == "recording",
                ProvenanceEvent.object_ref == str(recording_id),
                ProvenanceEvent.event_type == "split",
            )
            .order_by(ProvenanceEvent.id)
        )
        return [event.to_dict() for event in rows.scalars().all()]


async def _change_segment_end(
    factory: Any,
    *,
    recording_id: int,
    segment_index: int,
    end_sec: float,
) -> None:
    from sqlalchemy import select

    from audio_graphy.models import Segment

    async with factory() as session:
        segment = (
            await session.execute(
                select(Segment).where(
                    Segment.recording_id == recording_id,
                    Segment.idx == segment_index,
                )
            )
        ).scalar_one()
        segment.end_sec = end_sec
        await session.commit()


def _discover_recording_split(
    test_client: TestClient,
    headers: dict[str, str],
    *,
    base: datetime,
) -> dict[str, Any]:
    response = test_client.post(
        "/api/v1/receptions/proposals/discover",
        json={
            "scenario": "automotive",
            "store_id": "S001",
            "recorded_from": (base - timedelta(minutes=1)).isoformat(),
            "recorded_to": (base + timedelta(hours=1)).isoformat(),
            "short_recording_max_sec": 30,
        },
        headers=headers,
    )
    assert response.status_code == 200
    return next(
        item for item in response.json()["items"] if item["candidate_type"] == "recording_split"
    )


@pytest.mark.integration
class TestReceptionListing:
    def test_list_is_tenant_and_agent_scoped_with_filters_and_pagination(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        base = datetime(2026, 7, 23, 9, 0, tzinfo=UTC)
        own_ready = _run_async(
            _seed_reception(
                db_session_factory,
                tenant_id="chang_an",
                store_id="S001",
                agent_name="agent_ca",
                status="ready",
                started_at=base,
            )
        )
        _run_async(
            _seed_reception(
                db_session_factory,
                tenant_id="chang_an",
                store_id="S001",
                agent_name="another_agent",
                status="ready",
                started_at=base + timedelta(hours=1),
            )
        )
        own_review = _run_async(
            _seed_reception(
                db_session_factory,
                tenant_id="chang_an",
                store_id="S002",
                agent_name="agent_ca",
                status="needs_review",
                started_at=base + timedelta(hours=2),
            )
        )
        _run_async(
            _seed_reception(
                db_session_factory,
                tenant_id="byd",
                store_id="S001",
                agent_name="agent_byd",
                status="ready",
                started_at=base,
            )
        )

        agent_response = test_client.get(
            "/api/v1/receptions?page=1&page_size=20",
            headers=auth_headers["agent_t1"],
        )
        assert agent_response.status_code == 200
        assert agent_response.json()["total"] == 2
        assert [item["id"] for item in agent_response.json()["items"]] == [
            own_review,
            own_ready,
        ]

        inspector_response = test_client.get(
            "/api/v1/receptions",
            params={
                "store_id": "S001",
                "status": "ready",
                "started_from": (base - timedelta(minutes=1)).isoformat(),
                "started_to": (base + timedelta(hours=3)).isoformat(),
                "page": 2,
                "page_size": 1,
            },
            headers=auth_headers["inspector_t1"],
        )
        assert inspector_response.status_code == 200
        body = inspector_response.json()
        assert body["total"] == 2
        assert body["page"] == 2
        assert body["page_size"] == 1
        assert body["items"][0]["id"] == own_ready
        assert all(item["tenant_id"] == "chang_an" for item in body["items"])

        invalid_range = test_client.get(
            "/api/v1/receptions",
            params={
                "started_from": (base + timedelta(days=1)).isoformat(),
                "started_to": base.isoformat(),
            },
            headers=auth_headers["inspector_t1"],
        )
        assert invalid_range.status_code == 422
        assert invalid_range.json()["error"]["code"] == "RECEPTION_TIME_RANGE_INVALID"


@pytest.mark.integration
class TestAutomaticReceptionDiscovery:
    def test_discovers_short_recording_group_and_exposes_missing_duration_review(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        base = datetime(2026, 7, 23, 9, 0, tzinfo=UTC)
        for recording_id, offset_sec, duration_sec in (
            (201, 0, 10.0),
            (202, 20, 8.0),
        ):
            _run_async(seed_recording(db_session_factory, recording_id=recording_id))
            _run_async(
                _set_recording_metadata(
                    db_session_factory,
                    recording_id,
                    recorded_at=base + timedelta(seconds=offset_sec),
                )
            )
            _run_async(
                _seed_segments(
                    db_session_factory,
                    recording_id,
                    [(0.0, duration_sec, "客户继续了解车型")],
                )
            )

        _run_async(seed_recording(db_session_factory, recording_id=203))
        _run_async(
            _set_recording_metadata(
                db_session_factory,
                203,
                recorded_at=base + timedelta(minutes=2),
            )
        )
        _run_async(
            seed_recording(
                db_session_factory,
                recording_id=204,
                store_id="OTHER",
            )
        )
        before = _run_async(_reception_count(db_session_factory))

        response = test_client.post(
            "/api/v1/receptions/proposals/discover",
            json={
                "scenario": "automotive",
                "store_id": "S001",
                "recorded_from": (base - timedelta(minutes=1)).isoformat(),
                "recorded_to": (base + timedelta(hours=1)).isoformat(),
                "short_recording_max_sec": 60,
                "limit": 100,
            },
            headers=auth_headers["inspector_t1"],
        )

        assert response.status_code == 200
        body = response.json()
        assert body["scanned_recordings"] == 3
        assert body["total"] == len(body["items"])
        assert body["truncated"] is False
        merge = next(item for item in body["items"] if item["candidate_type"] == "merge_group")
        assert merge["recording_ids"] == [201, 202]
        assert merge["decision"] == "merge"
        assert merge["confidence"] > 0.8
        assert "same_customer_voiceprint" in {reason["code"] for reason in merge["reasons"]}

        duration_review = next(
            item for item in body["items"] if item["candidate_type"] == "duration_review"
        )
        assert duration_review["recording_ids"] == [203]
        assert duration_review["decision"] == "needs_review"
        assert duration_review["duration_status"] == "unavailable"
        assert duration_review["reasons"][0]["code"] == "duration_unavailable"
        assert _run_async(_reception_count(db_session_factory)) == before

        forbidden = test_client.post(
            "/api/v1/receptions/proposals/discover",
            json={
                "scenario": "automotive",
                "store_id": "S001",
                "recorded_from": base.isoformat(),
                "recorded_to": (base + timedelta(hours=1)).isoformat(),
            },
            headers=auth_headers["viewer_t1"],
        )
        assert forbidden.status_code == 403

        timezone_mismatch = test_client.post(
            "/api/v1/receptions/proposals/discover",
            json={
                "scenario": "automotive",
                "store_id": "S001",
                "recorded_from": base.isoformat(),
                "recorded_to": "2026-07-23T10:00:00",
            },
            headers=auth_headers["inspector_t1"],
        )
        assert timezone_mismatch.status_code == 422

    def test_split_discovery_scrubs_legacy_raw_transcript_before_analysis(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from audio_graphy.core.reception_merge import ReceptionMerger

        base = datetime(2026, 7, 23, 10, 0, tzinfo=UTC)
        recording_id = _run_async(seed_recording(db_session_factory, recording_id=300))
        _run_async(
            _set_recording_metadata(
                db_session_factory,
                recording_id,
                recorded_at=base,
            )
        )
        _run_async(
            _seed_segments(
                db_session_factory,
                recording_id,
                [
                    (0.0, 10.0, "上一位客户电话 13812345678"),
                    (70.0, 80.0, "您好，欢迎光临，今天想看什么车型"),
                ],
            )
        )
        _run_async(
            _clear_recording_segment_scrubbed_text(
                db_session_factory,
                recording_id,
            )
        )
        captured_texts: list[str] = []
        original_detect = ReceptionMerger.detect_recording_splits

        def capture_detect(
            self: ReceptionMerger,
            turns: Any,
            *,
            max_signals: int | None = None,
        ) -> Any:
            captured_texts.extend(turn.transcript for turn in turns)
            return original_detect(self, turns, max_signals=max_signals)

        monkeypatch.setattr(ReceptionMerger, "detect_recording_splits", capture_detect)
        response = test_client.post(
            "/api/v1/receptions/proposals/discover",
            json={
                "scenario": "automotive",
                "store_id": "S001",
                "recorded_from": (base - timedelta(minutes=1)).isoformat(),
                "recorded_to": (base + timedelta(hours=1)).isoformat(),
                "short_recording_max_sec": 30,
            },
            headers=auth_headers["inspector_t1"],
        )

        assert response.status_code == 200
        assert captured_texts[0] == "上一位客户电话 138****5678"
        assert all("13812345678" not in text for text in captured_texts)

    def test_long_recording_split_is_review_only_and_keeps_source_immutable(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        base = datetime(2026, 7, 23, 11, 0, tzinfo=UTC)
        recording_id = _run_async(seed_recording(db_session_factory, recording_id=301))
        _run_async(
            _set_recording_metadata(
                db_session_factory,
                recording_id,
                recorded_at=base,
            )
        )
        _run_async(
            _seed_segments(
                db_session_factory,
                recording_id,
                [
                    (0.0, 10.0, "上一位客户结束沟通"),
                    (70.0, 80.0, "您好，欢迎光临，今天想看什么车型"),
                ],
            )
        )
        before = _run_async(_reception_count(db_session_factory))

        response = test_client.post(
            "/api/v1/receptions/proposals/discover",
            json={
                "scenario": "automotive",
                "store_id": "S001",
                "recorded_from": (base - timedelta(minutes=1)).isoformat(),
                "recorded_to": (base + timedelta(hours=1)).isoformat(),
                "short_recording_max_sec": 30,
            },
            headers=auth_headers["inspector_t1"],
        )

        assert response.status_code == 200
        split = next(
            item for item in response.json()["items"] if item["candidate_type"] == "recording_split"
        )
        assert split["recording_ids"] == [recording_id]
        assert split["decision"] == "needs_review"
        assert split["split_at_sec"] == 70.0
        assert split["at_segment_id"] is not None
        assert split["confidence"] >= 0.7
        assert {"long_pause", "re_greeting"} <= {reason["code"] for reason in split["reasons"]}
        assert _run_async(_reception_count(db_session_factory)) == before


@pytest.mark.integration
class TestReceptionProposalAcceptance:
    def test_accept_recomputes_proposal_and_builds_complete_ordered_timeline(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        base = datetime(2026, 7, 23, 13, 0, tzinfo=UTC)
        for recording_id, offset_sec, duration_sec in (
            (401, 0, 10.0),
            (402, 20, 8.0),
        ):
            _run_async(seed_recording(db_session_factory, recording_id=recording_id))
            _run_async(
                _set_recording_metadata(
                    db_session_factory,
                    recording_id,
                    recorded_at=base + timedelta(seconds=offset_sec),
                )
            )
            _run_async(
                _seed_segments(
                    db_session_factory,
                    recording_id,
                    [(0.0, duration_sec, "同一客户的连续沟通")],
                )
            )

        response = test_client.post(
            "/api/v1/receptions/proposals/accept",
            json={
                "scenario": "automotive",
                "recording_ids": [402, 401],
                "external_session_id": "AUTO-401-402",
            },
            headers=auth_headers["inspector_t1"],
        )

        assert response.status_code == 201
        created = response.json()
        assert created["store_id"] == "S001"
        assert created["agent_name"] == "agent_ca"
        assert created["status"] == "confirmed"
        assert created["merge_mode"] == "logical"
        assert created["merge_confidence"] > 0.8
        assert [item["recording_id"] for item in created["recordings"]] == [
            401,
            402,
        ]
        assert [
            (
                item["timeline_start_sec"],
                item["timeline_end_sec"],
                item["source_start_sec"],
                item["source_end_sec"],
                item["gap_before_sec"],
            )
            for item in created["recordings"]
        ] == [
            (0.0, 10.0, 0.0, 10.0, 0.0),
            (10.0, 18.0, 0.0, 8.0, 0.0),
        ]
        assert all(item["decision_source"] == "manual" for item in created["recordings"])
        assert all(
            item["merge_reasons"]["server_constructed_timeline"] is True
            for item in created["recordings"]
        )

        duplicate = test_client.post(
            "/api/v1/receptions/proposals/accept",
            json={
                "scenario": "automotive",
                "recording_ids": [401, 402],
            },
            headers=auth_headers["inspector_t1"],
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == "RECORDING_ALREADY_ASSIGNED"

    def test_accept_supports_one_recording_without_client_timeline_geometry(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        base = datetime(2026, 7, 23, 14, 0, tzinfo=UTC)
        recording_id = _run_async(seed_recording(db_session_factory, recording_id=451))
        _run_async(
            _set_recording_metadata(
                db_session_factory,
                recording_id,
                recorded_at=base,
            )
        )
        _run_async(
            _seed_segments(
                db_session_factory,
                recording_id,
                [(0.0, 12.5, "一次完整接待")],
            )
        )

        response = test_client.post(
            "/api/v1/receptions/proposals/accept",
            json={
                "scenario": "gold",
                "recording_ids": [recording_id],
            },
            headers=auth_headers["inspector_t1"],
        )

        assert response.status_code == 201
        created = response.json()
        assert created["scenario"] == "gold"
        assert created["merge_confidence"] == 1.0
        assert created["recordings"][0]["source_end_sec"] == 12.5
        assert (
            created["recordings"][0]["merge_reasons"]["reasons"][0]["code"]
            == "single_recording_reception"
        )

    def test_accept_rejects_cross_store_and_stale_noncandidate_group(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        base = datetime(2026, 7, 23, 14, 30, tzinfo=UTC)
        specifications = (
            (461, "S001", 0),
            (462, "S002", 20),
            (463, "S001", 7_200),
        )
        for recording_id, store_id, offset_sec in specifications:
            _run_async(
                seed_recording(
                    db_session_factory,
                    recording_id=recording_id,
                    store_id=store_id,
                )
            )
            _run_async(
                _set_recording_metadata(
                    db_session_factory,
                    recording_id,
                    recorded_at=base + timedelta(seconds=offset_sec),
                )
            )
            _run_async(
                _seed_segments(
                    db_session_factory,
                    recording_id,
                    [(0.0, 10.0, "测试接待")],
                )
            )

        cross_store = test_client.post(
            "/api/v1/receptions/proposals/accept",
            json={
                "scenario": "automotive",
                "recording_ids": [461, 462],
            },
            headers=auth_headers["inspector_t1"],
        )
        assert cross_store.status_code == 422
        assert cross_store.json()["error"]["code"] == "RECEPTION_STORE_MISMATCH"

        stale_noncandidate = test_client.post(
            "/api/v1/receptions/proposals/accept",
            json={
                "scenario": "automotive",
                "recording_ids": [461, 463],
            },
            headers=auth_headers["inspector_t1"],
        )
        assert stale_noncandidate.status_code == 422
        assert stale_noncandidate.json()["error"]["code"] == "RECEPTION_PROPOSAL_NOT_ACCEPTABLE"

    def test_accept_rejects_missing_duration_and_cross_tenant_recordings(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        base = datetime(2026, 7, 23, 15, 0, tzinfo=UTC)
        _run_async(seed_recording(db_session_factory, recording_id=501))
        _run_async(
            _set_recording_metadata(
                db_session_factory,
                501,
                recorded_at=base,
            )
        )
        _run_async(
            seed_recording(
                db_session_factory,
                tenant_id="byd",
                store_id="S001",
                agent_name="agent_byd",
                recording_id=502,
            )
        )
        _run_async(seed_recording(db_session_factory, recording_id=503))
        _run_async(
            _set_recording_metadata(
                db_session_factory,
                503,
                recorded_at=None,
            )
        )
        _run_async(
            _seed_segments(
                db_session_factory,
                503,
                [(0.0, 10.0, "缺少录制时间")],
            )
        )

        missing_duration = test_client.post(
            "/api/v1/receptions/proposals/accept",
            json={
                "scenario": "automotive",
                "recording_ids": [501],
            },
            headers=auth_headers["inspector_t1"],
        )
        assert missing_duration.status_code == 422
        assert missing_duration.json()["error"]["code"] == "RECORDING_DURATION_UNAVAILABLE"

        cross_tenant = test_client.post(
            "/api/v1/receptions/proposals/accept",
            json={
                "scenario": "automotive",
                "recording_ids": [502],
            },
            headers=auth_headers["inspector_t1"],
        )
        assert cross_tenant.status_code == 404
        assert cross_tenant.json()["error"]["code"] == "RECORDING_NOT_FOUND"

        missing_time = test_client.post(
            "/api/v1/receptions/proposals/accept",
            json={
                "scenario": "automotive",
                "recording_ids": [503],
            },
            headers=auth_headers["inspector_t1"],
        )
        assert missing_time.status_code == 422
        assert missing_time.json()["error"]["code"] == "RECORDING_TIME_UNAVAILABLE"


@pytest.mark.integration
class TestRecordingSplitProposalAcceptance:
    @staticmethod
    def _seed_split_candidate(factory: Any, recording_id: int, base: datetime) -> None:
        _run_async(seed_recording(factory, recording_id=recording_id))
        _run_async(
            _set_recording_metadata(
                factory,
                recording_id,
                recorded_at=base,
            )
        )
        _run_async(
            _seed_segments(
                factory,
                recording_id,
                [
                    (0.0, 10.0, "上一位客户结束沟通"),
                    (70.0, 80.0, "您好，欢迎光临，今天想看什么车型"),
                ],
            )
        )

    def test_accept_split_atomically_creates_two_source_spans_and_provenance(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        base = datetime(2026, 7, 23, 16, 0, tzinfo=UTC)
        recording_id = 601
        self._seed_split_candidate(db_session_factory, recording_id, base)
        source_before = _run_async(_source_recording_snapshot(db_session_factory, recording_id))
        proposal = _discover_recording_split(
            test_client,
            auth_headers["inspector_t1"],
            base=base,
        )

        assert proposal["proposal_token"]
        response = test_client.post(
            "/api/v1/receptions/proposals/accept",
            json={
                "scenario": "automotive",
                "recording_ids": [recording_id],
                "candidate_type": "recording_split",
                "split_at_sec": proposal["split_at_sec"],
                "at_segment_id": proposal["at_segment_id"],
                "proposal_token": proposal["proposal_token"],
            },
            headers=auth_headers["inspector_t1"],
        )

        assert response.status_code == 201
        created = response.json()
        assert created["candidate_type"] == "recording_split"
        assert created["recording_id"] == recording_id
        assert created["split_at_sec"] == 70.0
        assert created["source_duration_sec"] == 80.0
        assert created["at_segment_id"] == proposal["at_segment_id"]
        assert len(created["receptions"]) == 2
        assert len({item["id"] for item in created["receptions"]}) == 2
        assert [
            (
                item["recordings"][0]["timeline_start_sec"],
                item["recordings"][0]["timeline_end_sec"],
                item["recordings"][0]["source_start_sec"],
                item["recordings"][0]["source_end_sec"],
            )
            for item in created["receptions"]
        ] == [
            (0.0, 70.0, 0.0, 70.0),
            (0.0, 10.0, 70.0, 80.0),
        ]
        assert all(
            item["recordings"][0]["recording_id"] == recording_id
            and item["recordings"][0]["decision_source"] == "manual"
            for item in created["receptions"]
        )
        assert (
            _run_async(_source_recording_snapshot(db_session_factory, recording_id))
            == source_before
        )
        assert _run_async(
            _split_persistence_counts(
                db_session_factory,
                recording_id=recording_id,
            )
        ) == (2, 2, 1)
        provenance = _run_async(
            _split_provenance_payloads(
                db_session_factory,
                recording_id=recording_id,
            )
        )
        assert provenance[0]["payload"]["source_recording_immutable"] is True
        assert provenance[0]["payload"]["child_reception_ids"] == [
            item["id"] for item in created["receptions"]
        ]
        assert len(set(created["provenance_event_ids"])) == 3
        assert provenance[0]["id"] in created["provenance_event_ids"]

    def test_accept_split_rejects_stale_snapshot_after_segment_change(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        base = datetime(2026, 7, 23, 17, 0, tzinfo=UTC)
        recording_id = 602
        self._seed_split_candidate(db_session_factory, recording_id, base)
        proposal = _discover_recording_split(
            test_client,
            auth_headers["inspector_t1"],
            base=base,
        )
        _run_async(
            _change_segment_end(
                db_session_factory,
                recording_id=recording_id,
                segment_index=1,
                end_sec=85.0,
            )
        )

        response = test_client.post(
            "/api/v1/receptions/proposals/accept",
            json={
                "scenario": "automotive",
                "recording_ids": [recording_id],
                "candidate_type": "recording_split",
                "split_at_sec": proposal["split_at_sec"],
                "at_segment_id": proposal["at_segment_id"],
                "proposal_token": proposal["proposal_token"],
            },
            headers=auth_headers["inspector_t1"],
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "RECEPTION_PROPOSAL_STALE"
        assert _run_async(
            _split_persistence_counts(
                db_session_factory,
                recording_id=recording_id,
            )
        ) == (0, 0, 0)

    def test_accept_split_rejects_expired_signed_proposal(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import audio_graphy.services.reception_automation as automation_module

        base = datetime(2026, 7, 23, 17, 30, tzinfo=UTC)
        recording_id = 605
        self._seed_split_candidate(db_session_factory, recording_id, base)
        proposal = _discover_recording_split(
            test_client,
            auth_headers["inspector_t1"],
            base=base,
        )
        expired_at = int(datetime.fromisoformat(proposal["proposal_expires_at"]).timestamp())

        class ExpiredClock:
            @staticmethod
            def time() -> int:
                return expired_at + 1

        monkeypatch.setattr(automation_module, "time", ExpiredClock)
        response = test_client.post(
            "/api/v1/receptions/proposals/accept",
            json={
                "scenario": "automotive",
                "recording_ids": [recording_id],
                "candidate_type": "recording_split",
                "split_at_sec": proposal["split_at_sec"],
                "at_segment_id": proposal["at_segment_id"],
                "proposal_token": proposal["proposal_token"],
            },
            headers=auth_headers["inspector_t1"],
        )

        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "RECEPTION_PROPOSAL_STALE"
        assert error["message"] == "The recording split proposal has expired"
        assert error["detail"]["recording_id"] == recording_id
        assert error["detail"]["reason"] == "token_expired"
        assert _run_async(
            _split_persistence_counts(
                db_session_factory,
                recording_id=recording_id,
            )
        ) == (0, 0, 0)

    def test_accept_split_rejects_out_of_bounds_boundary(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        base = datetime(2026, 7, 23, 18, 0, tzinfo=UTC)
        recording_id = 603
        self._seed_split_candidate(db_session_factory, recording_id, base)
        proposal = _discover_recording_split(
            test_client,
            auth_headers["inspector_t1"],
            base=base,
        )

        response = test_client.post(
            "/api/v1/receptions/proposals/accept",
            json={
                "scenario": "automotive",
                "recording_ids": [recording_id],
                "candidate_type": "recording_split",
                "split_at_sec": 81.0,
                "at_segment_id": proposal["at_segment_id"],
                "proposal_token": proposal["proposal_token"],
            },
            headers=auth_headers["inspector_t1"],
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "RECEPTION_SPLIT_BOUNDARY_INVALID"
        assert _run_async(
            _split_persistence_counts(
                db_session_factory,
                recording_id=recording_id,
            )
        ) == (0, 0, 0)

    def test_accept_split_rejects_tampered_snapshot_token(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
    ) -> None:
        base = datetime(2026, 7, 23, 18, 30, tzinfo=UTC)
        recording_id = 606
        self._seed_split_candidate(db_session_factory, recording_id, base)
        proposal = _discover_recording_split(
            test_client,
            auth_headers["inspector_t1"],
            base=base,
        )
        proposal_token = proposal["proposal_token"]
        replacement = "A" if proposal_token[-1] != "A" else "B"
        tampered_token = f"{proposal_token[:-1]}{replacement}"

        response = test_client.post(
            "/api/v1/receptions/proposals/accept",
            json={
                "scenario": "automotive",
                "recording_ids": [recording_id],
                "candidate_type": "recording_split",
                "split_at_sec": proposal["split_at_sec"],
                "at_segment_id": proposal["at_segment_id"],
                "proposal_token": tampered_token,
            },
            headers=auth_headers["inspector_t1"],
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "RECEPTION_PROPOSAL_TOKEN_INVALID"
        assert _run_async(
            _split_persistence_counts(
                db_session_factory,
                recording_id=recording_id,
            )
        ) == (0, 0, 0)

    def test_accept_split_rolls_back_both_receptions_when_provenance_fails(
        self,
        test_client: TestClient,
        auth_headers: dict[str, dict[str, str]],
        db_session_factory: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import audio_graphy.services.reception_automation as automation_module

        base = datetime(2026, 7, 23, 19, 0, tzinfo=UTC)
        recording_id = 604
        self._seed_split_candidate(db_session_factory, recording_id, base)
        proposal = _discover_recording_split(
            test_client,
            auth_headers["inspector_t1"],
            base=base,
        )
        provenance_model = automation_module.ProvenanceEvent

        def fail_on_recording_provenance(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("object_type") == "recording":
                raise RuntimeError("injected split provenance failure")
            return provenance_model(*args, **kwargs)

        monkeypatch.setattr(
            automation_module,
            "ProvenanceEvent",
            fail_on_recording_provenance,
        )
        response = test_client.post(
            "/api/v1/receptions/proposals/accept",
            json={
                "scenario": "automotive",
                "recording_ids": [recording_id],
                "candidate_type": "recording_split",
                "split_at_sec": proposal["split_at_sec"],
                "at_segment_id": proposal["at_segment_id"],
                "proposal_token": proposal["proposal_token"],
            },
            headers=auth_headers["inspector_t1"],
        )
        assert response.status_code == 500

        assert _run_async(
            _split_persistence_counts(
                db_session_factory,
                recording_id=recording_id,
            )
        ) == (0, 0, 0)
