"""Regression tests for batched SpeakerLinker voiceprint loading."""

from __future__ import annotations

import struct
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from audio_graphy.core.crypto import AudioCrypto
from audio_graphy.core.speaker_linker import SpeakerLinker
from audio_graphy.models.recording import Recording
from audio_graphy.models.speaker_node import SpeakerNode
from audio_graphy.models.voiceprint_vector import VoiceprintVector


@pytest.fixture
def dev_crypto(tmp_path: Path) -> AudioCrypto:
    return AudioCrypto(tmp_path / "master.key", dev_mode=True)


def _vector(value: float) -> tuple[float, ...]:
    return (value,) * 192


def _encrypted_vector(
    crypto: AudioCrypto,
    vector: tuple[float, ...],
    *,
    context: str,
) -> tuple[bytes, dict[str, Any]]:
    payload = struct.pack(f"<{len(vector)}f", *vector)
    return crypto.encrypt_bytes(payload, context=context)


async def _seed_recording(
    session: AsyncSession,
    *,
    tenant_id: str,
    suffix: str,
) -> Recording:
    recording = Recording(
        tenant_id=tenant_id,
        store_id=f"STORE-{suffix}",
        path=f"/data/audio/{suffix}.wav.enc",
        audio_encryption_meta={"algo": "AES-256-GCM"},
        recorded_at=datetime.now(UTC),
    )
    session.add(recording)
    await session.flush()
    return recording


async def _seed_speaker(
    session: AsyncSession,
    *,
    tenant_id: str,
    suffix: str,
) -> SpeakerNode:
    node = SpeakerNode(
        tenant_id=tenant_id,
        voiceprint_id=f"node-{suffix}".ljust(64, "0"),
        display_name=f"speaker:{suffix}",
        speaker_role="agent",
        recordings_list=[],
        recordings_count=0,
        total_speech_sec=0.0,
        merge_confidence=1.0,
        merge_strategy="single_recording",
        attrs={},
    )
    session.add(node)
    await session.flush()
    return node


async def _seed_voiceprint(
    session: AsyncSession,
    crypto: AudioCrypto,
    *,
    tenant_id: str,
    recording_id: int,
    speaker_entity_id: int,
    vector: tuple[float, ...],
    created_at: datetime,
    suffix: str,
) -> VoiceprintVector:
    ciphertext, metadata = _encrypted_vector(
        crypto,
        vector,
        context=f"voiceprint:test:{suffix}",
    )
    row = VoiceprintVector(
        tenant_id=tenant_id,
        recording_id=recording_id,
        speaker_entity_id=speaker_entity_id,
        voiceprint_id=f"vp-{suffix}".ljust(64, "0"),
        vector_encrypted=ciphertext,
        encryption_meta=metadata,
        duration_sec=1.0,
        created_at=created_at,
    )
    session.add(row)
    await session.flush()
    return row


def _select_counter() -> tuple[list[str], Callable[..., None]]:
    statements: list[str] = []

    def count_selects(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    return statements, count_selects


@pytest.mark.asyncio
@pytest.mark.integration
async def test_load_existing_speakers_uses_two_selects_for_any_speaker_count(
    async_engine: AsyncEngine,
    async_session_factory: async_sessionmaker[AsyncSession],
    dev_crypto: AudioCrypto,
) -> None:
    """Node count must not increase the number of voiceprint SELECTs."""
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        recording = await _seed_recording(
            session,
            tenant_id="default",
            suffix="batch",
        )
        for index in range(4):
            node = await _seed_speaker(
                session,
                tenant_id="default",
                suffix=f"batch-{index}",
            )
            await _seed_voiceprint(
                session,
                dev_crypto,
                tenant_id="default",
                recording_id=recording.id,
                speaker_entity_id=node.id,
                vector=_vector(float(index + 1)),
                created_at=now + timedelta(seconds=index),
                suffix=f"batch-{index}",
            )
        await session.commit()

    statements, listener = _select_counter()
    event.listen(async_engine.sync_engine, "before_cursor_execute", listener)
    try:
        nodes = await SpeakerLinker(
            async_session_factory,
            dev_crypto,
        )._load_existing_speakers()
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", listener)

    assert len(nodes) == 4
    assert len(statements) == 2
    assert sum("vectors_voiceprint" in statement for statement in statements) == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_load_existing_speakers_selects_latest_per_speaker_and_tenant(
    async_session_factory: async_sessionmaker[AsyncSession],
    dev_crypto: AudioCrypto,
) -> None:
    """Each node gets its newest in-tenant vector, with id breaking time ties."""
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        default_recording = await _seed_recording(
            session,
            tenant_id="default",
            suffix="latest-default",
        )
        other_recording = await _seed_recording(
            session,
            tenant_id="other",
            suffix="latest-other",
        )
        default_node = await _seed_speaker(
            session,
            tenant_id="default",
            suffix="latest-default",
        )
        other_node = await _seed_speaker(
            session,
            tenant_id="other",
            suffix="latest-other",
        )

        await _seed_voiceprint(
            session,
            dev_crypto,
            tenant_id="default",
            recording_id=default_recording.id,
            speaker_entity_id=default_node.id,
            vector=_vector(1.0),
            created_at=now - timedelta(minutes=1),
            suffix="old",
        )
        await _seed_voiceprint(
            session,
            dev_crypto,
            tenant_id="default",
            recording_id=default_recording.id,
            speaker_entity_id=default_node.id,
            vector=_vector(2.0),
            created_at=now,
            suffix="tie-lower-id",
        )
        expected = await _seed_voiceprint(
            session,
            dev_crypto,
            tenant_id="default",
            recording_id=default_recording.id,
            speaker_entity_id=default_node.id,
            vector=_vector(3.0),
            created_at=now,
            suffix="tie-higher-id",
        )
        await _seed_voiceprint(
            session,
            dev_crypto,
            tenant_id="other",
            recording_id=other_recording.id,
            speaker_entity_id=default_node.id,
            vector=_vector(99.0),
            created_at=now + timedelta(days=1),
            suffix="cross-tenant",
        )
        await _seed_voiceprint(
            session,
            dev_crypto,
            tenant_id="other",
            recording_id=other_recording.id,
            speaker_entity_id=other_node.id,
            vector=_vector(88.0),
            created_at=now,
            suffix="other-node",
        )
        await session.commit()
        expected_vector = expected.decrypted_vector(dev_crypto)

    nodes = await SpeakerLinker(
        async_session_factory,
        dev_crypto,
        tenant_id="default",
    )._load_existing_speakers()

    assert [node.id for node in nodes] == [default_node.id]
    assert vars(nodes[0])["_runtime_vec"] == pytest.approx(tuple(expected_vector))


def test_voiceprint_model_has_batch_latest_lookup_index() -> None:
    """The ORM metadata must preserve the production lookup index."""
    indexes = {
        index.name: tuple(column.name for column in index.columns)
        for index in VoiceprintVector.__table__.indexes
    }
    assert indexes["ix_vp_tenant_speaker_created"] == (
        "tenant_id",
        "speaker_entity_id",
        "created_at",
    )
