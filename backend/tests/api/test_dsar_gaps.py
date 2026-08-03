"""Coverage gap-fill tests for DSAR endpoints.

Targets uncovered branches:
- _write_audit direct-insert fallback (no AuditWriter on app.state)
- _build_export_bundle with audio_encrypted_path + decrypt success
- _build_export_bundle raw read OSError fallback
- audio_target empty string
- audit list filter combos (recording_id, user_id, action together)
- cross-tenant erase returns 404
- export response Content-Disposition header
"""

from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path
from typing import Any

import networkx as nx
import pytest

from tests.api.conftest import (  # type: ignore[import-not-found]
    _run_async,
    seed_recording,
    seed_segment,
    seed_tag,
)


async def _seed_rec_with_audio(
    factory: Any,
    *,
    audio_path: str | None = None,
    audio_encrypted_path: str | None = None,
    tag_suffix: str = "g",
) -> int:
    """Seed a recording with explicit audio paths set."""
    from sqlalchemy import select

    from audio_graphy.models import Recording

    rec_id = await seed_recording(
        factory,
        tenant_id="chang_an",
        store_id="S001",
        agent_name="agent_ca",
        status="indexed",
        pipeline_state="done",
    )
    await seed_segment(
        factory,
        recording_id=rec_id,
        tenant_id="chang_an",
        transcript="hello",
    )
    await seed_tag(
        factory,
        recording_id=rec_id,
        tenant_id="chang_an",
        tag_path=f"q.{tag_suffix}",
        tag_value="ok",
    )

    # Patch the recording to set path fields explicitly.
    if audio_path is not None or audio_encrypted_path is not None:
        async with factory() as session:
            row = (
                await session.execute(select(Recording).where(Recording.id == rec_id))
            ).scalar_one()
            if audio_path is not None:
                row.path = audio_path
            if audio_encrypted_path is not None:
                row.audio_encrypted_path = audio_encrypted_path
            await session.commit()
    return rec_id


def test_audit_filter_by_user_and_action(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
) -> None:
    """GET /dsar/audit filters by user_id + action simultaneously."""
    factory = db_session_factory
    rec_id = _run_async(_seed_rec_with_audio(factory, tag_suffix="filter"))
    resp = test_client.post(
        f"/api/v1/dsar/export/{rec_id}",
        json={"reason": "filter test"},
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 200

    # Now query with user filter that matches the admin (user_id=1).
    resp = test_client.get(
        "/api/v1/dsar/audit?user_id=1&action=dsar.export",
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    # Filter that doesn't match returns 0.
    resp2 = test_client.get(
        "/api/v1/dsar/audit?user_id=99999",
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp2.status_code == 200
    assert resp2.json()["total"] == 0


def test_export_with_raw_audio_path(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
    tmp_path: Path,
) -> None:
    """Export includes audio bytes when raw path exists (no encryption)."""
    audio = tmp_path / "raw.wav"
    audio.write_bytes(b"RIFF...." * 10)

    factory = db_session_factory
    rec_id = _run_async(_seed_rec_with_audio(factory, audio_path=str(audio), tag_suffix="raw"))

    resp = test_client.post(
        f"/api/v1/dsar/export/{rec_id}",
        json={"reason": "raw audio"},
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 200
    # The ZIP should contain audio/recording.wav.
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
        assert any("audio/recording.wav" in n for n in names), names
        audio_data = next(zf.read(n) for n in names if "audio/recording.wav" in n)
        assert audio_data.startswith(b"RIFF")


def test_export_when_raw_audio_unreadable(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
) -> None:
    """Export returns 200 even when the raw audio file does not exist on disk."""
    factory = db_session_factory
    rec_id = _run_async(
        _seed_rec_with_audio(factory, audio_path="/nonexistent/path.wav", tag_suffix="noaudio")
    )

    resp = test_client.post(
        f"/api/v1/dsar/export/{rec_id}",
        json={"reason": "missing file"},
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
        # No audio entry present.
        assert not any("audio/recording.wav" in n for n in names), names


def test_export_content_disposition_header(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
) -> None:
    """The Content-Disposition header carries the dated filename."""
    factory = db_session_factory
    rec_id = _run_async(_seed_rec_with_audio(factory, tag_suffix="hdr"))

    resp = test_client.post(
        f"/api/v1/dsar/export/{rec_id}",
        json={"reason": "header"},
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 200
    cd = resp.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert "filename=" in cd
    assert str(rec_id) in cd


def test_erase_audio_file_unlink_silent_on_failure(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
) -> None:
    """Erase succeeds even if the audio file is missing on disk (OSError suppressed)."""
    factory = db_session_factory
    rec_id = _run_async(
        _seed_rec_with_audio(
            factory,
            audio_path="/tmp/nonexistent_for_erase.wav",
            tag_suffix="unlinkfail",
        )
    )
    resp = test_client.post(
        f"/api/v1/dsar/erase/{rec_id}",
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is True


def test_erase_removes_recording_references_from_cached_graph(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
) -> None:
    """Erase drops exclusive nodes and strips the id from shared graph nodes."""
    from audio_graphy.core.types import _list_to_str, _str_to_list

    factory = db_session_factory
    rec_id = _run_async(_seed_rec_with_audio(factory, tag_suffix="graph-cleanup"))
    graph = nx.MultiDiGraph()
    graph.add_node(
        "exclusive",
        recording_ids=_list_to_str([str(rec_id)]),
    )
    graph.add_node(
        "shared",
        recording_ids=_list_to_str([str(rec_id), "99999"]),
    )
    graph.add_node(
        "unrelated",
        recording_ids=_list_to_str(["99999"]),
    )
    graph.add_edge("exclusive", "shared")

    class PersistableGraphStore:
        def __init__(self) -> None:
            self.graph = graph
            self.saved = False

        async def save(self) -> None:
            self.saved = True

        def invalidate_path_projection(self) -> None:
            return None

    store = PersistableGraphStore()
    test_client.app.state.graph_stores["chang_an"] = store

    resp = test_client.post(
        f"/api/v1/dsar/erase/{rec_id}",
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )

    assert resp.status_code == 200, resp.text
    assert "exclusive" not in graph
    assert _str_to_list(graph.nodes["shared"]["recording_ids"]) == ["99999"]
    assert _str_to_list(graph.nodes["unrelated"]["recording_ids"]) == ["99999"]
    assert store.saved is True


def test_erase_durably_clears_file_index_and_opaque_llm_cache(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
) -> None:
    """DSAR includes JSON transcript/chunk copies and tenant LLM cache."""
    from audio_graphy.storage.file_index import (
        STORE_LLM_RESPONSE_CACHE,
        STORE_TEXT_CHUNKS,
        STORE_VIDEO_PATH,
        STORE_VIDEO_SEGMENTS,
        FileIndex,
    )

    factory = db_session_factory
    rec_id = _run_async(_seed_rec_with_audio(factory, tag_suffix="file-index"))
    index = FileIndex(
        Path(test_client.app.state.settings.working_dir),
        tenant_id="chang_an",
    )
    _run_async(
        index.set(
            STORE_VIDEO_SEGMENTS,
            f"{rec_id}_0",
            {"recording_id": rec_id, "transcript": "private"},
        )
    )
    _run_async(
        index.set(
            STORE_TEXT_CHUNKS,
            f"{rec_id}_1",
            {"recording_id": rec_id, "text": "private"},
        )
    )
    _run_async(index.set(STORE_VIDEO_PATH, str(rec_id), {"recording_id": rec_id}))
    _run_async(index.set_llm_cache("opaque", "private response"))
    _run_async(index.flush())
    test_client.app.state.file_indexes["chang_an"] = index

    response = test_client.post(
        f"/api/v1/dsar/erase/{rec_id}",
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )

    assert response.status_code == 200, response.text
    assert _run_async(index.get_all(STORE_VIDEO_SEGMENTS)) == {}
    assert _run_async(index.get_all(STORE_TEXT_CHUNKS)) == {}
    assert _run_async(index.get_all(STORE_VIDEO_PATH)) == {}
    assert _run_async(index.get_all(STORE_LLM_RESPONSE_CACHE)) == {}


def test_erase_loads_cold_tenant_graph_and_persists_cleanup(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
) -> None:
    """A tenant absent from the process cache is still erased from GraphML."""
    from audio_graphy.core.types import _list_to_str, _str_to_list
    from audio_graphy.storage.graph_networkx import NetworkXGraphStore

    rec_id = _run_async(_seed_rec_with_audio(db_session_factory, tag_suffix="cold-graph-cleanup"))
    working_dir = Path(test_client.app.state.settings.working_dir)

    async def _seed_graphml() -> None:
        seed_store = NetworkXGraphStore(working_dir, tenant_id="chang_an")
        await seed_store.load()
        seed_store.graph.add_node(
            "exclusive",
            recording_ids=_list_to_str([str(rec_id)]),
        )
        seed_store.graph.add_node(
            "shared",
            recording_ids=_list_to_str([str(rec_id), "99999"]),
        )
        seed_store.invalidate_path_projection()
        await seed_store.save()

    _run_async(_seed_graphml())
    test_client.app.state.graph_stores.pop("chang_an", None)

    resp = test_client.post(
        f"/api/v1/dsar/erase/{rec_id}",
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )

    assert resp.status_code == 200, resp.text
    assert "chang_an" in test_client.app.state.graph_stores

    async def _read_persisted_graph() -> nx.MultiDiGraph:
        reloaded = NetworkXGraphStore(working_dir, tenant_id="chang_an")
        await reloaded.load()
        return reloaded.graph

    persisted = _run_async(_read_persisted_graph())
    assert "exclusive" not in persisted
    assert _str_to_list(persisted.nodes["shared"]["recording_ids"]) == ["99999"]


def test_erase_voiceprint_cascade_is_in_the_database_transaction(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
) -> None:
    """Biometric rows and their canonical node cannot outlive the recording."""
    from sqlalchemy import select

    from audio_graphy.models.speaker_link import SpeakerLink
    from audio_graphy.models.speaker_node import SpeakerNode
    from audio_graphy.models.voiceprint_vector import VoiceprintVector

    rec_id = _run_async(_seed_rec_with_audio(db_session_factory, tag_suffix="cascade-log"))

    async def _seed_voiceprint() -> int:
        async with db_session_factory() as session, session.begin():
            speaker = SpeakerNode(
                tenant_id="chang_an",
                voiceprint_id="a" * 64,
                display_name="private speaker",
                speaker_role="customer",
                recordings_list=[rec_id],
                recordings_count=1,
                total_speech_sec=3,
                merge_confidence=1,
                merge_strategy="voiceprint",
                attrs={},
            )
            session.add(speaker)
            await session.flush()
            session.add_all(
                [
                    VoiceprintVector(
                        tenant_id="chang_an",
                        recording_id=rec_id,
                        speaker_entity_id=speaker.id,
                        voiceprint_id="b" * 64,
                        vector_encrypted=b"encrypted-biometric",
                        encryption_meta={"version": 1},
                        duration_sec=3,
                    ),
                    SpeakerLink(
                        tenant_id="chang_an",
                        canonical_speaker_id=speaker.id,
                        source_speaker_id=speaker.id,
                        recording_id=rec_id,
                        merge_confidence=1,
                        strategy="voiceprint",
                    ),
                ]
            )
            return speaker.id

    speaker_id = _run_async(_seed_voiceprint())
    resp = test_client.post(
        f"/api/v1/dsar/erase/{rec_id}",
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )

    assert resp.status_code == 200, resp.text

    async def _biometric_rows_are_gone() -> bool:
        async with db_session_factory() as session:
            speaker = await session.get(SpeakerNode, speaker_id)
            voiceprint = await session.scalar(
                select(VoiceprintVector.id).where(VoiceprintVector.recording_id == rec_id)
            )
            link = await session.scalar(
                select(SpeakerLink.id).where(SpeakerLink.recording_id == rec_id)
            )
            return speaker is None and voiceprint is None and link is None

    assert _run_async(_biometric_rows_are_gone()) is True


def test_erase_graph_save_failure_is_persisted_for_outbox_retry(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Graph failure cannot roll back DB erasure and remains recoverable."""
    from sqlalchemy import select

    from audio_graphy.models.erasure_outbox import ErasureOutbox
    from audio_graphy.models.recording import Recording

    class FlakyGraphStore:
        def __init__(self) -> None:
            self.graph = nx.MultiDiGraph()
            self.graph.add_node("pii", recording_ids="[]")
            self.fail = True

        async def save(self) -> None:
            if self.fail:
                raise RuntimeError("graph store unavailable")

        def invalidate_path_projection(self) -> None:
            return None

    rec_id = _run_async(_seed_rec_with_audio(db_session_factory, tag_suffix="graph-log"))
    graph_store = FlakyGraphStore()
    test_client.app.state.graph_stores["chang_an"] = graph_store

    with caplog.at_level(logging.WARNING, logger="audio_graphy.services.erasure_outbox"):
        resp = test_client.post(
            f"/api/v1/dsar/erase/{rec_id}",
            headers=auth_headers["admin_t1"],  # type: ignore[index]
        )

    assert resp.status_code == 200, resp.text

    async def _state() -> tuple[bool, str, int]:
        async with db_session_factory() as session:
            recording = (
                await session.execute(
                    select(Recording).where(
                        Recording.id == rec_id,
                        Recording.tenant_id == "chang_an",
                    )
                )
            ).scalar_one_or_none()
            outbox = (
                await session.execute(
                    select(ErasureOutbox).where(
                        ErasureOutbox.tenant_id == "chang_an",
                        ErasureOutbox.subject_type == "recording",
                        ErasureOutbox.subject_id == str(rec_id),
                    )
                )
            ).scalar_one()
            return recording is None, outbox.status, outbox.attempts

    assert _run_async(_state()) == (True, "failed", 1)
    assert any("Erasure outbox cleanup deferred" in row.message for row in caplog.records)

    graph_store.fail = False
    retried = test_client.post(
        f"/api/v1/dsar/erase/{rec_id}",
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert retried.status_code == 200, retried.text
    assert _run_async(_state()) == (True, "succeeded", 2)


def test_cross_tenant_erase_returns_404(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
) -> None:
    """Admin of tenant B cannot erase tenant A's recording."""
    factory = db_session_factory
    rec_id = _run_async(_seed_rec_with_audio(factory, tag_suffix="ct"))

    resp = test_client.post(
        f"/api/v1/dsar/erase/{rec_id}",
        headers=auth_headers["admin_t2"],  # type: ignore[index]
    )
    assert resp.status_code == 404


def test_export_with_audit_writer_attached(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
) -> None:
    """When app.state.audit_writer is set, _write_audit uses writer.record+flush path."""
    from audio_graphy.core.audit import AuditWriter

    writer = AuditWriter(db_session_factory, flush_batch_size=10, flush_interval_sec=10.0)
    test_client.app.state.audit_writer = writer

    try:
        factory = db_session_factory
        rec_id = _run_async(_seed_rec_with_audio(factory, tag_suffix="aw"))
        resp = test_client.post(
            f"/api/v1/dsar/export/{rec_id}",
            json={"reason": "with writer"},
            headers=auth_headers["admin_t1"],  # type: ignore[index]
        )
        assert resp.status_code == 200

        async def _drain() -> int:
            return await writer.flush()

        _run_async(_drain())

        # Audit row should be present.
        from sqlalchemy import select

        from audio_graphy.models.audit_log import AuditLog

        async def _check() -> int:
            async with factory() as session:
                rows = list(
                    (
                        await session.execute(
                            select(AuditLog).where(AuditLog.action == "dsar.export")
                        )
                    )
                    .scalars()
                    .all()
                )
            return len(rows)

        assert _run_async(_check()) >= 1
    finally:
        # This endpoint flushes the injected writer synchronously, so the test
        # deliberately does not start a background task on a short-lived loop.
        test_client.app.state.audit_writer = None


def test_audit_filter_by_recording_id(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
) -> None:
    """GET /dsar/audit?recording_id=N filters by target='recording:N'."""
    factory = db_session_factory
    rec_id = _run_async(_seed_rec_with_audio(factory, tag_suffix="rid"))
    resp = test_client.post(
        f"/api/v1/dsar/export/{rec_id}",
        json={"reason": "rid test"},
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 200

    resp = test_client.get(
        f"/api/v1/dsar/audit?recording_id={rec_id}",
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    # Each item's target matches recording:rec_id.
    for item in body["items"]:
        assert item["target"] == f"recording:{rec_id}"
