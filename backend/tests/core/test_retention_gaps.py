"""Coverage gap-fill tests for RetentionEnforcer.

Targets the uncovered branches:
- error path (delete raises → captured in report.errors + retention_delete_failed audit)
- audio file unlink OSError (best-effort: logged but not raised)
- graph_store factory returns a real store → _remove_graph_refs exercise
- override retention_days=None falls back to Settings.recording_retention_days
- recorded_at=None rows skipped from candidate query
- archived status included (not just 'indexed')
"""

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

import audio_graphy.models  # noqa: F401
from audio_graphy.core.audit import AuditWriter
from audio_graphy.core.crypto import AudioCrypto
from audio_graphy.core.retention import RetentionEnforcer
from audio_graphy.models.base import Base
from audio_graphy.models.recording import Recording
from audio_graphy.models.tenant import Tenant


@pytest_asyncio.fixture
async def rg_engine() -> AsyncIterator[Any]:
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
async def rg_factory(rg_engine: Any) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(rg_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def rg_crypto(tmp_path: Path) -> AudioCrypto:
    return AudioCrypto(tmp_path / "master.key", dev_mode=True)


@pytest_asyncio.fixture
async def rg_audit(rg_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AuditWriter]:
    writer = AuditWriter(rg_factory, flush_batch_size=10, flush_interval_sec=0.05)
    await writer.start()
    yield writer
    await writer.aclose()


def _noop_graph_factory(_tenant: str) -> Any:
    return None


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    *,
    days_ago: int,
    path: str,
    rec_id: int = 1,
    status: str = "indexed",
    recorded_at_set: bool = True,
) -> Recording:
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
            recorded_at=(datetime.now(UTC) - timedelta(days=days_ago)) if recorded_at_set else None,
            indexed_at=datetime.now(UTC),
            prompt_version="v1",
        )
        session.add(rec)
        await session.commit()
        await session.refresh(rec)
    return rec


@pytest.mark.asyncio
async def test_delete_failure_recorded_in_errors(
    rg_factory: async_sessionmaker[AsyncSession],
    rg_crypto: AudioCrypto,
    rg_audit: AuditWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When _delete_one raises, the error is captured in report.errors + audit."""
    audio = tmp_path / "fail.wav"
    audio.write_bytes(b"\x00" * 100)
    await _seed(rg_factory, days_ago=400, path=str(audio))

    enforcer = RetentionEnforcer(
        rg_factory, rg_crypto, rg_audit, _noop_graph_factory, retention_days=90
    )

    # Force _delete_one to raise.
    async def _boom(_rec: Recording) -> None:
        raise RuntimeError("simulated delete crash")

    monkeypatch.setattr(enforcer, "_delete_one", _boom)

    report = await enforcer.run_sweep()
    assert report.total_scanned == 1
    assert report.deleted == 0
    assert len(report.errors) == 1
    assert "simulated delete crash" in report.errors[0]

    # Failure audit row.
    await rg_audit.flush()
    from audio_graphy.models.audit_log import AuditLog

    async with rg_factory() as session:
        rows = list((await session.execute(select(AuditLog))).scalars().all())
    actions = {r.action for r in rows}
    assert "retention_delete_failed" in actions


@pytest.mark.asyncio
async def test_unlink_oserror_swallowed(
    rg_factory: async_sessionmaker[AsyncSession],
    rg_crypto: AudioCrypto,
    rg_audit: AuditWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSError during audio unlink is logged but not re-raised."""
    audio = tmp_path / "oserror.wav"
    audio.write_bytes(b"\x00" * 100)
    await _seed(rg_factory, days_ago=400, path=str(audio))

    enforcer = RetentionEnforcer(
        rg_factory, rg_crypto, rg_audit, _noop_graph_factory, retention_days=90
    )

    def _raise_unlink(_self: Path) -> None:
        raise OSError("permission denied")

    # Path.unlink is the call site; monkeypatch via tmp_path subclass is fragile,
    # so patch the bound method on the audio object's class via monkeypatch.setattr
    # targeting the standard library. Use a wrapper approach instead.
    original_unlink = Path.unlink

    def patched_unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == audio:
            raise OSError("simulated permission denied")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", patched_unlink)

    # Should not raise — OSError is swallowed inside _delete_one.
    report = await enforcer.run_sweep()
    assert report.deleted == 1
    assert report.errors == []


@pytest.mark.asyncio
async def test_archived_status_also_swept(
    rg_factory: async_sessionmaker[AsyncSession],
    rg_crypto: AudioCrypto,
    rg_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    """status='archived' is also swept (not just 'indexed')."""
    audio = tmp_path / "archived.wav"
    audio.write_bytes(b"\x00" * 100)
    await _seed(rg_factory, days_ago=400, path=str(audio), status="archived")

    enforcer = RetentionEnforcer(
        rg_factory, rg_crypto, rg_audit, _noop_graph_factory, retention_days=90
    )
    report = await enforcer.run_sweep()
    assert report.total_scanned == 1
    assert report.deleted == 1


@pytest.mark.asyncio
async def test_recorded_at_null_skipped(
    rg_factory: async_sessionmaker[AsyncSession],
    rg_crypto: AudioCrypto,
    rg_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    """A row with recorded_at=None is skipped by the sweep."""
    audio = tmp_path / "null_ts.wav"
    audio.write_bytes(b"\x00" * 100)
    await _seed(rg_factory, days_ago=0, path=str(audio), recorded_at_set=False)

    enforcer = RetentionEnforcer(
        rg_factory, rg_crypto, rg_audit, _noop_graph_factory, retention_days=90
    )
    report = await enforcer.run_sweep()
    assert report.total_scanned == 0
    assert report.deleted == 0


@pytest.mark.asyncio
async def test_empty_candidates_short_circuit(
    rg_factory: async_sessionmaker[AsyncSession],
    rg_crypto: AudioCrypto,
    rg_audit: AuditWriter,
) -> None:
    """No candidates → returns zero-report without touching audit/graph."""
    enforcer = RetentionEnforcer(
        rg_factory, rg_crypto, rg_audit, _noop_graph_factory, retention_days=90
    )
    report = await enforcer.run_sweep()
    assert report.total_scanned == 0
    assert report.deleted == 0
    assert report.errors == []
    assert report.duration_sec >= 0.0


@pytest.mark.asyncio
async def test_audio_encrypted_path_preferred(
    rg_factory: async_sessionmaker[AsyncSession],
    rg_crypto: AudioCrypto,
    rg_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    """When audio_encrypted_path is set, that file is unlinked (not the raw path)."""
    raw = tmp_path / "raw.wav"
    raw.write_bytes(b"\x00" * 100)
    enc = tmp_path / "raw.wav.enc"
    enc.write_bytes(b"\x00" * 100)
    await _seed(rg_factory, days_ago=400, path=str(raw))

    # Update recording with encrypted path.
    async with rg_factory() as session:
        rec_row = (
            await session.execute(select(Recording).where(Recording.id == 1))
        ).scalar_one()
        rec_row.audio_encrypted_path = str(enc)
        await session.commit()

    enforcer = RetentionEnforcer(
        rg_factory, rg_crypto, rg_audit, _noop_graph_factory, retention_days=90
    )
    await enforcer.run_sweep()
    # Encrypted file unlinked; raw file still on disk (not the deletion target).
    assert not enc.exists()
    assert raw.exists()


@pytest.mark.asyncio
async def test_graph_store_factory_returns_store_exercises_remove(
    rg_factory: async_sessionmaker[AsyncSession],
    rg_crypto: AudioCrypto,
    rg_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    """When graph_store_factory returns a store, _remove_graph_refs is exercised."""
    import networkx as nx

    from audio_graphy.core.types import _list_to_str

    graph = nx.Graph()
    # Use the production-serialised form so _str_to_list round-trips.
    graph.add_node("n1", recording_ids=_list_to_str(["1"]))  # only this → drop
    graph.add_node("n2", recording_ids=_list_to_str(["1", "2"]))  # shared → strip
    graph.add_node("n3", recording_ids=_list_to_str(["99"]))  # other → keep
    graph.add_node("n4")  # no attr → skip

    class _Store:
        def __init__(self, g: Any) -> None:
            self.graph = g

    fake_store = _Store(graph)

    def factory(_tenant: str) -> Any:
        return fake_store

    audio = tmp_path / "graph.wav"
    audio.write_bytes(b"\x00" * 100)
    await _seed(rg_factory, days_ago=400, path=str(audio))

    enforcer = RetentionEnforcer(
        rg_factory, rg_crypto, rg_audit, factory, retention_days=90
    )
    report = await enforcer.run_sweep()
    assert report.deleted == 1
    # n1 dropped (only source); n2 still present with stripped recording_ids.
    assert "n1" not in set(graph.nodes)
    assert "n2" in set(graph.nodes)
    # n2 should no longer reference recording 1.
    from audio_graphy.core.types import _str_to_list

    n2_rec = _str_to_list(str(graph.nodes["n2"]["recording_ids"]))
    assert "1" not in n2_rec
    assert "2" in n2_rec


@pytest.mark.asyncio
async def test_graph_cleanup_exception_does_not_fail_delete(
    rg_factory: async_sessionmaker[AsyncSession],
    rg_crypto: AudioCrypto,
    rg_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    """Exception inside graph cleanup is logged + downgraded (delete still counts)."""

    class _BrokenStore:
        @property
        def graph(self) -> Any:
            raise RuntimeError("graph load failed")

    def factory(_tenant: str) -> Any:
        return _BrokenStore()

    audio = tmp_path / "gfail.wav"
    audio.write_bytes(b"\x00" * 100)
    await _seed(rg_factory, days_ago=400, path=str(audio))

    enforcer = RetentionEnforcer(
        rg_factory, rg_crypto, rg_audit, factory, retention_days=90
    )
    report = await enforcer.run_sweep()
    # Delete succeeded; graph cleanup failure was swallowed.
    assert report.deleted == 1
    assert report.errors == []
