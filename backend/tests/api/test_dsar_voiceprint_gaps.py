"""M7 QA gap-fill — dsar.py uncovered branches.

Targets lines flagged by coverage report (post-M7):
- 414-416: voiceprint export exception swallowed (table absent)
- 421-429: audio_encrypted_path branch + decryption failure
- 430-436: raw rec.path read with OSError swallowed
- 553, 605, 610-611: speaker_node recordings_list decrement + audit row CSV
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
    from audio_graphy.core.crypto import AudioCrypto

    return AudioCrypto(tmp_path / "master.key", dev_mode=True)


async def _seed_rec_with_voiceprint_two_recordings(
    factory: Any,
    *,
    crypto: Any,
    voiceprint_id: str = "vp-shared",
    tenant: str = "chang_an",
) -> tuple[int, int, int]:
    """Seed ONE speaker_node linked to TWO recordings.

    Returns (rec_id_a, rec_id_b, speaker_node_id). Erasing rec_id_b should
    decrement recordings_list from 2 to 1 (not delete the node).
    """
    from audio_graphy.models.speaker_link import SpeakerLink
    from audio_graphy.models.speaker_node import SpeakerNode
    from audio_graphy.models.voiceprint_vector import VoiceprintVector

    rec_a = await seed_recording(
        factory,
        tenant_id=tenant,
        store_id="S001",
        agent_name="agent_ca",
        status="indexed",
        pipeline_state="done",
    )
    rec_b = await seed_recording(
        factory,
        tenant_id=tenant,
        store_id="S001",
        agent_name="agent_ca",
        status="indexed",
        pipeline_state="done",
    )
    await seed_segment(factory, recording_id=rec_a, tenant_id=tenant, transcript="a")
    await seed_segment(factory, recording_id=rec_b, tenant_id=tenant, transcript="b")

    async with factory() as session:
        node = SpeakerNode(
            tenant_id=tenant,
            voiceprint_id=voiceprint_id,
            display_name=f"speaker:vp_{voiceprint_id[:8]}",
            speaker_role="agent",
            recordings_list=[rec_a, rec_b],
            recordings_count=2,
        )
        session.add(node)
        await session.flush()

        for rid in (rec_a, rec_b):
            vec = tuple(float(i) / 200.0 for i in range(192))
            plain = struct.pack(f"<{len(vec)}f", *vec)
            ct, meta = crypto.encrypt_bytes(plain, context=f"voiceprint:{voiceprint_id}:{rid}")
            # voiceprint_id is unique per (tenant, voiceprint_id); suffix with rid
            # so we can insert two rows (one per recording) sharing the same
            # speaker_node.
            session.add(
                VoiceprintVector(
                    tenant_id=tenant,
                    recording_id=rid,
                    speaker_entity_id=node.id,
                    voiceprint_id=f"{voiceprint_id}_{rid}",
                    vector_encrypted=ct,
                    encryption_meta=meta,
                    duration_sec=5.0,
                )
            )
            session.add(
                SpeakerLink(
                    tenant_id=tenant,
                    canonical_speaker_id=node.id,
                    source_speaker_id=node.id,
                    recording_id=rid,
                    merge_confidence=1.0,
                    strategy="voiceprint",
                )
            )
        await session.commit()
        return rec_a, rec_b, node.id


# ============================================================
# Tests
# ============================================================


def test_erase_partial_speaker_decrements_recordings_list(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
    dev_crypto_fixture: Any,
) -> None:
    """Erase one of two recordings → speaker_node stays, recordings_list=[rec_a]."""
    from sqlalchemy import select

    from audio_graphy.models.speaker_node import SpeakerNode

    factory = db_session_factory
    rec_a, rec_b, node_id = _run_async(
        _seed_rec_with_voiceprint_two_recordings(factory, crypto=dev_crypto_fixture)
    )

    resp = test_client.post(
        f"/api/v1/dsar/erase/{rec_b}",
        json={"reason": "partial cascade test"},
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp.status_code in (200, 204), resp.text

    async def _inspect() -> tuple[bool, list[int]]:
        async with factory() as session:
            node = (
                await session.execute(select(SpeakerNode).where(SpeakerNode.id == node_id))
            ).scalar_one_or_none()
            if node is None:
                return False, []
            return True, list(node.recordings_list or [])

    exists, rids = _run_async(_inspect())
    assert exists, "speaker_node must remain (recordings_list has 1 entry)"
    assert rec_a in rids
    assert rec_b not in rids
    assert len(rids) == 1


def test_export_voiceprint_metadata_only_no_raw_vector(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
    dev_crypto_fixture: Any,
) -> None:
    """Export ZIP must NOT include raw encrypted bytes or decrypted vector.

    Regression for PIPL §14.3 — ensures export payload schema stays metadata-only.
    """
    factory = db_session_factory
    rec_a, _, _ = _run_async(
        _seed_rec_with_voiceprint_two_recordings(
            factory, crypto=dev_crypto_fixture, voiceprint_id="vp-export-check"
        )
    )

    resp = test_client.post(
        f"/api/v1/dsar/export/{rec_a}",
        json={"reason": "metadata-only check"},
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 200, resp.text

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        vp_files = [n for n in zf.namelist() if n.endswith("voiceprints.json")]
        assert len(vp_files) == 1
        content = json.loads(zf.read(vp_files[0]).decode("utf-8"))
        for entry in content:
            # Allowed keys: metadata only.
            assert "voiceprint_id" in entry
            assert "vector_encrypted" not in entry
            assert "vector" not in entry
            assert "encryption_meta" not in entry
            assert "raw_bytes" not in entry
