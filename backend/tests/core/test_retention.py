"""Unit tests for RetentionEnforcer — daily cron hard-delete (PIPL §14.3)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import audio_graphy.models  # noqa: F401 — register all models on Base.metadata
from audio_graphy.core.audit import AuditWriter
from audio_graphy.core.crypto import AudioCrypto
from audio_graphy.core.retention import RetentionEnforcer
from audio_graphy.models.base import Base
from audio_graphy.models.recording import Recording
from audio_graphy.models.segment import Segment
from audio_graphy.models.tenant import Tenant


@pytest_asyncio.fixture
async def ret_engine() -> AsyncIterator[Any]:
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
async def ret_factory(ret_engine: Any) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(ret_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def ret_crypto(tmp_path: Path) -> AudioCrypto:
    """AudioCrypto with dev_mode key (not actually used for delete)."""
    return AudioCrypto(tmp_path / "master.key", dev_mode=True)


@pytest_asyncio.fixture
async def ret_audit(ret_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AuditWriter]:
    """Started AuditWriter; closed at teardown."""
    writer = AuditWriter(ret_factory, flush_batch_size=10, flush_interval_sec=0.05)
    await writer.start()
    yield writer
    await writer.aclose()


def _noop_graph_factory(_tenant: str) -> Any:
    """Graph store factory returning None — disables GraphML cleanup."""
    return None


async def _seed_recording(
    factory: async_sessionmaker[AsyncSession],
    *,
    days_ago: int,
    path: str,
    rec_id: int = 1,
    status: str = "indexed",
    audio_encrypted_path: str | None = None,
) -> Recording:
    """Insert a tenant + recording whose recorded_at is N days in the past."""
    async with factory() as session:
        tenant = Tenant(id=1, code="chang_an", name="长安", brand="长安", region="西南")
        session.add(tenant)

        rec = Recording(
            id=rec_id,
            tenant_id="chang_an",
            store_id="S001",
            agent_name="agent_ca",
            customer_hash="cust_hash_001",
            path=path,
            status=status,
            pipeline_state="done",
            recorded_at=datetime.now(UTC) - timedelta(days=days_ago),
            indexed_at=datetime.now(UTC),
            prompt_version="v1",
            audio_encrypted_path=audio_encrypted_path,
        )
        session.add(rec)
        await session.commit()
        await session.refresh(rec)
    return rec


@pytest.mark.asyncio
async def test_within_retention_no_delete(
    ret_factory: async_sessionmaker[AsyncSession],
    ret_crypto: AudioCrypto,
    ret_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    """recording 30 days old, retention=90 → not deleted."""
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"\x00" * 100)
    await _seed_recording(ret_factory, days_ago=30, path=str(audio))

    enforcer = RetentionEnforcer(
        ret_factory, ret_crypto, ret_audit, _noop_graph_factory, retention_days=90
    )
    report = await enforcer.run_sweep()

    assert report.total_scanned == 0
    assert report.deleted == 0
    assert audio.exists()


@pytest.mark.asyncio
async def test_at_boundary_delete(
    ret_factory: async_sessionmaker[AsyncSession],
    ret_crypto: AudioCrypto,
    ret_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    """recording recorded_at slightly older than retention_days → deleted."""
    audio = tmp_path / "boundary.wav"
    audio.write_bytes(b"\x00" * 100)
    await _seed_recording(ret_factory, days_ago=2, path=str(audio))

    enforcer = RetentionEnforcer(
        ret_factory, ret_crypto, ret_audit, _noop_graph_factory, retention_days=1
    )
    report = await enforcer.run_sweep()

    assert report.total_scanned == 1
    assert report.deleted == 1


@pytest.mark.asyncio
async def test_older_recording_deleted(
    ret_factory: async_sessionmaker[AsyncSession],
    ret_crypto: AudioCrypto,
    ret_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    """recording 365 days old, retention=90 → deleted + DB row gone."""
    audio = tmp_path / "old.wav"
    audio.write_bytes(b"\x00" * 100)
    await _seed_recording(ret_factory, days_ago=365, path=str(audio))

    enforcer = RetentionEnforcer(
        ret_factory, ret_crypto, ret_audit, _noop_graph_factory, retention_days=90
    )
    report = await enforcer.run_sweep()

    assert report.deleted == 1
    assert report.errors == []

    async with ret_factory() as session:
        rows = list((await session.execute(select(Recording))).scalars().all())
    assert rows == []


@pytest.mark.asyncio
async def test_file_actually_unlinked(
    ret_factory: async_sessionmaker[AsyncSession],
    ret_crypto: AudioCrypto,
    ret_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    """After sweep, the audio file no longer exists on disk."""
    audio = tmp_path / "gone.wav"
    audio.write_bytes(b"\x00" * 256)
    await _seed_recording(ret_factory, days_ago=400, path=str(audio))

    enforcer = RetentionEnforcer(
        ret_factory, ret_crypto, ret_audit, _noop_graph_factory, retention_days=90
    )
    await enforcer.run_sweep()

    assert not audio.exists()


@pytest.mark.asyncio
async def test_audit_log_written_after_delete(
    ret_factory: async_sessionmaker[AsyncSession],
    ret_crypto: AudioCrypto,
    ret_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    """A retention_delete audit_log row is written for each deleted recording."""
    audio = tmp_path / "audited.wav"
    audio.write_bytes(b"\x00" * 256)
    await _seed_recording(ret_factory, days_ago=400, path=str(audio))

    enforcer = RetentionEnforcer(
        ret_factory, ret_crypto, ret_audit, _noop_graph_factory, retention_days=90
    )
    await enforcer.run_sweep()

    # AuditWriter is async; force drain.
    await ret_audit.flush()

    from audio_graphy.models.audit_log import AuditLog

    async with ret_factory() as session:
        rows = list((await session.execute(select(AuditLog))).scalars().all())
    actions = {r.action for r in rows}
    assert "retention_delete" in actions


@pytest.mark.asyncio
async def test_segments_cleared_with_recording(
    ret_factory: async_sessionmaker[AsyncSession],
    ret_crypto: AudioCrypto,
    ret_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    """Deleting a recording also clears its segments."""
    audio = tmp_path / "with_seg.wav"
    audio.write_bytes(b"\x00" * 256)
    rec = await _seed_recording(ret_factory, days_ago=400, path=str(audio))

    async with ret_factory() as session:
        session.add(
            Segment(
                recording_id=rec.id,
                tenant_id="chang_an",
                idx=0,
                start_sec=0.0,
                end_sec=1.0,
                transcript="hello",
                speaker="agent",
                vad_conf=0.9,
            )
        )
        await session.commit()

    enforcer = RetentionEnforcer(
        ret_factory, ret_crypto, ret_audit, _noop_graph_factory, retention_days=90
    )
    await enforcer.run_sweep()

    async with ret_factory() as session:
        seg_rows = list((await session.execute(select(Segment))).scalars().all())
    assert seg_rows == []
