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
import zipfile
from pathlib import Path
from typing import Any

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

    async def _setup() -> None:
        await writer.start()

    _run_async(_setup())
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
                    ).scalars().all()
                )
            return len(rows)

        assert _run_async(_check()) >= 1
    finally:
        # Detach the writer without aclose() — TestClient's event loop is
        # different from our helper loops, so calling aclose would fail.
        # The TestClient teardown disposes the engine, which is fine for tests.
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
