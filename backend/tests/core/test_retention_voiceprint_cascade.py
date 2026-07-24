"""M7 retention cascade tests — voiceprint / speaker_node cleanup.

Validates that ``RetentionEnforcer._cascade_voiceprint_for_recording``:
- Removes the recording from speaker_nodes.recordings_list.
- Hard-deletes speaker_nodes when recordings_list becomes empty.
- Deletes voiceprint_vector rows.
- Deletes speaker_link rows.
- Writes a ``recording_voiceprint_cascade`` audit entry.
- Honours ``cascade_voiceprint=False`` (skip the cascade).
"""

from __future__ import annotations

import struct
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import audio_graphy.models  # noqa: F401
from audio_graphy.core.audit import AuditWriter
from audio_graphy.core.crypto import AudioCrypto
from audio_graphy.core.retention import RetentionEnforcer
from audio_graphy.models.base import Base
from audio_graphy.models.recording import Recording
from audio_graphy.models.speaker_link import SpeakerLink
from audio_graphy.models.speaker_node import SpeakerNode
from audio_graphy.models.tenant import Tenant
from audio_graphy.models.voiceprint_vector import VoiceprintVector

# ============================================================
# Fixtures (SQLite in-memory)
# ============================================================


@pytest_asyncio.fixture
async def vp_engine() -> AsyncIterator[Any]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        future=True,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def vp_factory(vp_engine: Any) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(vp_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def vp_crypto(tmp_path: Path) -> AudioCrypto:
    return AudioCrypto(tmp_path / "master.key", dev_mode=True)


@pytest_asyncio.fixture
async def vp_audit(
    vp_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AuditWriter]:
    writer = AuditWriter(vp_factory, flush_batch_size=10, flush_interval_sec=0.05)
    await writer.start()
    yield writer
    await writer.aclose()


def _noop_graph_factory(_tenant: str) -> Any:
    return None


# ============================================================
# Helpers
# ============================================================


async def _seed_tenant_and_recording(
    factory: async_sessionmaker[AsyncSession],
    *,
    rec_id: int = 1,
    days_ago: int = 400,
    tenant: str = "chang_an",
) -> Recording:
    async with factory() as session:
        # Idempotent tenant insert.
        existing = await session.get(Tenant, 1)
        if existing is None:
            session.add(Tenant(id=1, code=tenant, name="长安", brand="长安", region="西南"))
            await session.flush()
        rec = Recording(
            id=rec_id,
            tenant_id=tenant,
            store_id="S001",
            path="/data/audio/old.wav",
            status="indexed",
            pipeline_state="done",
            recorded_at=datetime.now(UTC) - timedelta(days=days_ago),
            indexed_at=datetime.now(UTC),
        )
        session.add(rec)
        await session.commit()
        await session.refresh(rec)
    return rec


async def _seed_speaker(
    factory: async_sessionmaker[AsyncSession],
    crypto: AudioCrypto,
    *,
    speaker_id: int,
    recording_id: int,
    voiceprint_id: str,
    recordings_list: list[int],
    tenant: str = "chang_an",
) -> int:
    """Insert a speaker_node + a voiceprint_vector row + a speaker_link row."""
    async with factory() as session:
        node = SpeakerNode(
            id=speaker_id,
            tenant_id=tenant,
            voiceprint_id=voiceprint_id,
            display_name=f"speaker:vp_{voiceprint_id[:8]}",
            speaker_role="agent",
            recordings_list=recordings_list,
            recordings_count=len(recordings_list),
        )
        session.add(node)
        await session.flush()

        # Voiceprint vector.
        vec = tuple(float(i) / 200.0 for i in range(192))
        plain = struct.pack(f"<{len(vec)}f", *vec)
        ct, meta = crypto.encrypt_bytes(plain)
        vp_row = VoiceprintVector(
            tenant_id=tenant,
            recording_id=recording_id,
            speaker_entity_id=speaker_id,
            voiceprint_id=voiceprint_id + "_v",
            vector_encrypted=ct,
            encryption_meta=meta,
        )
        session.add(vp_row)

        link = SpeakerLink(
            tenant_id=tenant,
            canonical_speaker_id=speaker_id,
            source_speaker_id=speaker_id,
            recording_id=recording_id,
            merge_confidence=1.0,
            strategy="single_recording",
        )
        session.add(link)
        await session.commit()
    return speaker_id


# ============================================================
# Tests
# ============================================================


@pytest.mark.asyncio
async def test_cascade_deletes_speaker_when_recordings_list_becomes_empty(
    vp_factory: async_sessionmaker[AsyncSession],
    vp_crypto: AudioCrypto,
    vp_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    """If the cascade makes recordings_list empty, the speaker_node is hard-deleted."""
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"\x00" * 16)
    rec = await _seed_tenant_and_recording(vp_factory, rec_id=10, days_ago=400)

    # Speaker only appears in this recording → list becomes empty after cascade.
    await _seed_speaker(
        vp_factory,
        vp_crypto,
        speaker_id=100,
        recording_id=rec.id,
        voiceprint_id="hash-A",
        recordings_list=[rec.id],
    )

    enforcer = RetentionEnforcer(
        vp_factory, vp_crypto, vp_audit, _noop_graph_factory, retention_days=90
    )
    report = await enforcer.run_sweep()
    assert report.deleted == 1

    async with vp_factory() as session:
        nodes = list((await session.execute(select(SpeakerNode))).scalars().all())
        vps = list((await session.execute(select(VoiceprintVector))).scalars().all())
        links = list((await session.execute(select(SpeakerLink))).scalars().all())

    assert nodes == []
    assert vps == []
    assert links == []


@pytest.mark.asyncio
async def test_cascade_keeps_speaker_when_other_recordings_reference_it(
    vp_factory: async_sessionmaker[AsyncSession],
    vp_crypto: AudioCrypto,
    vp_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    """Speaker appears in 2 recordings; deleting one keeps the speaker."""
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"\x00" * 16)
    rec = await _seed_tenant_and_recording(vp_factory, rec_id=11, days_ago=400)

    # Speaker appears in [rec.id, 9999] — 9999 is not being deleted.
    await _seed_speaker(
        vp_factory,
        vp_crypto,
        speaker_id=200,
        recording_id=rec.id,
        voiceprint_id="hash-B",
        recordings_list=[rec.id, 9999],
    )

    enforcer = RetentionEnforcer(
        vp_factory, vp_crypto, vp_audit, _noop_graph_factory, retention_days=90
    )
    report = await enforcer.run_sweep()
    assert report.deleted == 1

    async with vp_factory() as session:
        node = await session.get(SpeakerNode, 200)
        assert node is not None
        assert node.recordings_list == [9999]
        assert node.recordings_count == 1


@pytest.mark.asyncio
async def test_cascade_voiceprint_false_skips_cleanup(
    vp_factory: async_sessionmaker[AsyncSession],
    vp_crypto: AudioCrypto,
    vp_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    """When cascade_voiceprint=False, the cascade step is skipped entirely."""
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"\x00" * 16)
    rec = await _seed_tenant_and_recording(vp_factory, rec_id=12, days_ago=400)

    await _seed_speaker(
        vp_factory,
        vp_crypto,
        speaker_id=300,
        recording_id=rec.id,
        voiceprint_id="hash-C",
        recordings_list=[rec.id],
    )

    enforcer = RetentionEnforcer(
        vp_factory,
        vp_crypto,
        vp_audit,
        _noop_graph_factory,
        retention_days=90,
        cascade_voiceprint=False,
    )
    report = await enforcer.run_sweep()
    assert report.deleted == 1

    async with vp_factory() as session:
        # Speaker + voiceprint rows are still present (FK CASCADE on recording
        # delete still wipes the voiceprint_vector row, but the speaker_node
        # has no FK to recording so it stays — that's the doc contract for
        # cascade_voiceprint=False).
        node = await session.get(SpeakerNode, 300)
        assert node is not None


@pytest.mark.asyncio
async def test_cascade_audit_written(
    vp_factory: async_sessionmaker[AsyncSession],
    vp_crypto: AudioCrypto,
    vp_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    """The cascade writes a 'recording_voiceprint_cascade' audit row."""
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"\x00" * 16)
    rec = await _seed_tenant_and_recording(vp_factory, rec_id=13, days_ago=400)

    await _seed_speaker(
        vp_factory,
        vp_crypto,
        speaker_id=400,
        recording_id=rec.id,
        voiceprint_id="hash-D",
        recordings_list=[rec.id],
    )

    enforcer = RetentionEnforcer(
        vp_factory, vp_crypto, vp_audit, _noop_graph_factory, retention_days=90
    )
    await enforcer.run_sweep()
    await vp_audit.flush()

    from audio_graphy.models.audit_log import AuditLog

    async with vp_factory() as session:
        rows = list((await session.execute(select(AuditLog))).scalars().all())
    actions = {r.action for r in rows}
    assert "recording_voiceprint_cascade" in actions


@pytest.mark.asyncio
async def test_cascade_no_voiceprint_rows_no_audit(
    vp_factory: async_sessionmaker[AsyncSession],
    vp_crypto: AudioCrypto,
    vp_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    """Recording without any voiceprint rows → cascade is a no-op (no audit)."""
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"\x00" * 16)
    await _seed_tenant_and_recording(vp_factory, rec_id=14, days_ago=400)

    enforcer = RetentionEnforcer(
        vp_factory, vp_crypto, vp_audit, _noop_graph_factory, retention_days=90
    )
    await enforcer.run_sweep()
    await vp_audit.flush()

    from audio_graphy.models.audit_log import AuditLog

    async with vp_factory() as session:
        rows = list((await session.execute(select(AuditLog))).scalars().all())
    actions = {r.action for r in rows}
    assert "recording_voiceprint_cascade" not in actions
