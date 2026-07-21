"""M7 DSAR + voiceprint integration tests.

Validates that:
- ``/dsar/export`` ZIP includes ``voiceprints.json`` (with metadata only,
  no raw vectors per PIPL §14.3).
- ``/dsar/erase`` cascades the speaker_node.recordings_list when recording
  is deleted.
- When no voiceprint rows exist (enable_voiceprint=False), the export ZIP
  either omits ``voiceprints.json`` or contains an empty list.
"""

from __future__ import annotations

import io
import json
import struct
import zipfile
from pathlib import Path
from typing import Any

import pytest

from tests.api.conftest import (  # type: ignore[import-not-found]
    _run_async,
    seed_recording,
    seed_segment,
)


@pytest.fixture
def dev_crypto_fixture(tmp_path: Path) -> Any:
    """AudioCrypto in dev mode for encrypting test voiceprint vectors."""
    from audio_graphy.core.crypto import AudioCrypto

    return AudioCrypto(tmp_path / "master.key", dev_mode=True)


async def _seed_rec_with_voiceprint(
    factory: Any,
    *,
    crypto: Any,
    voiceprint_id: str = "vp-hash-xyz",
    tenant: str = "chang_an",
) -> int:
    """Seed a recording + a speaker_node + a voiceprint_vector row."""

    from audio_graphy.models.speaker_link import SpeakerLink
    from audio_graphy.models.speaker_node import SpeakerNode
    from audio_graphy.models.voiceprint_vector import VoiceprintVector

    rec_id = await seed_recording(
        factory,
        tenant_id=tenant,
        store_id="S001",
        agent_name="agent_ca",
        status="indexed",
        pipeline_state="done",
    )
    await seed_segment(
        factory,
        recording_id=rec_id,
        tenant_id=tenant,
        transcript="hello world",
    )

    async with factory() as session:
        node = SpeakerNode(
            tenant_id=tenant,
            voiceprint_id=voiceprint_id,
            display_name=f"speaker:vp_{voiceprint_id[:8]}",
            speaker_role="agent",
            recordings_list=[rec_id],
            recordings_count=1,
        )
        session.add(node)
        await session.flush()

        vec = tuple(float(i) / 200.0 for i in range(192))
        plain = struct.pack(f"<{len(vec)}f", *vec)
        ct, meta = crypto.encrypt_bytes(plain, context=f"voiceprint:{voiceprint_id}")

        vp = VoiceprintVector(
            tenant_id=tenant,
            recording_id=rec_id,
            speaker_entity_id=node.id,
            voiceprint_id=voiceprint_id,
            vector_encrypted=ct,
            encryption_meta=meta,
            duration_sec=10.0,
        )
        session.add(vp)

        link = SpeakerLink(
            tenant_id=tenant,
            canonical_speaker_id=node.id,
            source_speaker_id=node.id,
            recording_id=rec_id,
            merge_confidence=1.0,
            strategy="single_recording",
        )
        session.add(link)
        await session.commit()

    return rec_id


# ============================================================
# Tests
# ============================================================


def test_export_includes_voiceprints_json(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
    dev_crypto_fixture: Any,
) -> None:
    """Export ZIP contains voiceprints.json with voiceprint_id metadata only."""
    factory = db_session_factory
    rec_id = _run_async(
        _seed_rec_with_voiceprint(factory, crypto=dev_crypto_fixture)
    )

    resp = test_client.post(
        f"/api/v1/dsar/export/{rec_id}",
        json={"reason": "voiceprint export test"},
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 200, resp.text

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
        vp_files = [n for n in names if n.endswith("voiceprints.json")]
        assert len(vp_files) == 1, f"expected voiceprints.json, got {names}"

        content = json.loads(zf.read(vp_files[0]).decode("utf-8"))
        assert isinstance(content, list)
        # At least one entry — and it must NOT include the raw vector.
        assert len(content) >= 1
        for entry in content:
            assert "voiceprint_id" in entry
            # Raw vector bytes / array must never be exported.
            assert "vector_encrypted" not in entry
            assert "vector" not in entry
            assert "encryption_meta" not in entry


def test_erase_cascades_speaker_recordings_list(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
    dev_crypto_fixture: Any,
) -> None:
    """POST /dsar/erase/{id} removes recording_id from speaker_nodes.recordings_list."""
    from sqlalchemy import select

    from audio_graphy.models.speaker_node import SpeakerNode

    factory = db_session_factory
    rec_id = _run_async(
        _seed_rec_with_voiceprint(
            factory, crypto=dev_crypto_fixture, voiceprint_id="vp-erase-test"
        )
    )

    # Confirm speaker_node has the recording.
    async def _list_speakers() -> list[int]:
        async with factory() as session:
            nodes = list(
                (await session.execute(select(SpeakerNode))).scalars().all()
            )
            return [n.id for n in nodes]

    speakers_before = _run_async(_list_speakers())
    assert len(speakers_before) == 1

    resp = test_client.post(
        f"/api/v1/dsar/erase/{rec_id}",
        json={"reason": "voiceprint erase cascade"},
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp.status_code in (200, 204), resp.text

    # After erase, the speaker_node should be gone (recordings_list became empty).
    speakers_after = _run_async(_list_speakers())
    assert speakers_after == [], (
        f"speaker_node should have been hard-deleted, got {speakers_after}"
    )


def test_export_without_voiceprint_rows_still_succeeds(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
) -> None:
    """A recording with no voiceprint rows exports successfully (no crash)."""
    factory = db_session_factory

    async def _seed_plain() -> int:
        return await seed_recording(
            factory,
            tenant_id="chang_an",
            store_id="S001",
            agent_name="agent_ca",
            status="indexed",
            pipeline_state="done",
        )

    rec_id = _run_async(_seed_plain())

    resp = test_client.post(
        f"/api/v1/dsar/export/{rec_id}",
        json={"reason": "no voiceprint"},
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 200, resp.text

    # voiceprints.json may be present but empty, or absent — either is valid.
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
        vp_files = [n for n in names if n.endswith("voiceprints.json")]
        if vp_files:
            content = json.loads(zf.read(vp_files[0]).decode("utf-8"))
            assert content == []
