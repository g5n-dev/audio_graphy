"""Coverage gap-fill tests for RetentionEnforcer.

Targets the uncovered branches:
- error path (delete raises → captured in report.errors + retention_delete_failed audit)
- audio file unlink OSError (best-effort: logged but not raised)
- graph_store factory returns a real store → erase is persisted
- graph persistence failures enter the failure/retry path
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
async def test_unlink_oserror_keeps_recording_retryable(
    rg_factory: async_sessionmaker[AsyncSession],
    rg_crypto: AudioCrypto,
    rg_audit: AuditWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSError during audio unlink must not falsely complete retention."""
    audio = tmp_path / "oserror.wav"
    audio.write_bytes(b"\x00" * 100)
    await _seed(rg_factory, days_ago=400, path=str(audio))

    enforcer = RetentionEnforcer(
        rg_factory, rg_crypto, rg_audit, _noop_graph_factory, retention_days=90
    )

    original_unlink = Path.unlink

    def patched_unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == audio:
            raise OSError("simulated permission denied")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", patched_unlink)

    report = await enforcer.run_sweep()
    assert report.deleted == 0
    assert len(report.errors) == 1
    async with rg_factory() as session:
        remaining = await session.get(Recording, 1)
        assert remaining is not None


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
    """Encrypted and transient plaintext copies are both unlinked."""
    raw = tmp_path / "raw.wav"
    raw.write_bytes(b"\x00" * 100)
    enc = tmp_path / "raw.wav.enc"
    enc.write_bytes(b"\x00" * 100)
    await _seed(rg_factory, days_ago=400, path=str(raw))

    # Update recording with encrypted path.
    async with rg_factory() as session:
        rec_row = (await session.execute(select(Recording).where(Recording.id == 1))).scalar_one()
        rec_row.audio_encrypted_path = str(enc)
        await session.commit()

    enforcer = RetentionEnforcer(
        rg_factory, rg_crypto, rg_audit, _noop_graph_factory, retention_days=90
    )
    await enforcer.run_sweep()
    # Both representations contain personal audio and must be erased.
    assert not enc.exists()
    assert not raw.exists()


@pytest.mark.asyncio
async def test_retention_durably_clears_file_index_and_llm_cache(
    rg_factory: async_sessionmaker[AsyncSession],
    rg_crypto: AudioCrypto,
    rg_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    from audio_graphy.storage.file_index import (
        STORE_LLM_RESPONSE_CACHE,
        STORE_TEXT_CHUNKS,
        STORE_VIDEO_SEGMENTS,
        FileIndex,
    )

    audio = tmp_path / "indexed.wav"
    audio.write_bytes(b"\x00" * 100)
    await _seed(rg_factory, days_ago=400, path=str(audio))
    index = FileIndex(tmp_path, tenant_id="chang_an")
    await index.set(
        STORE_VIDEO_SEGMENTS,
        "1_0",
        {"recording_id": 1, "transcript": "private"},
    )
    await index.set(
        STORE_TEXT_CHUNKS,
        "1_1",
        {"recording_id": 1, "text": "private"},
    )
    await index.set_llm_cache("opaque", "private response")
    await index.flush()

    enforcer = RetentionEnforcer(
        rg_factory,
        rg_crypto,
        rg_audit,
        _noop_graph_factory,
        retention_days=90,
        working_dir=tmp_path,
        file_index_factory=lambda _tenant: index,
    )
    report = await enforcer.run_sweep()

    assert report.deleted == 1
    assert await index.get_all(STORE_VIDEO_SEGMENTS) == {}
    assert await index.get_all(STORE_TEXT_CHUNKS) == {}
    assert await index.get_all(STORE_LLM_RESPONSE_CACHE) == {}


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
            self.saved = False

        async def save(self) -> None:
            self.saved = True

        def invalidate_path_projection(self) -> None:
            return None

    fake_store = _Store(graph)

    def factory(_tenant: str) -> Any:
        return fake_store

    audio = tmp_path / "graph.wav"
    audio.write_bytes(b"\x00" * 100)
    await _seed(rg_factory, days_ago=400, path=str(audio))

    enforcer = RetentionEnforcer(rg_factory, rg_crypto, rg_audit, factory, retention_days=90)
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
    assert fake_store.saved is True


@pytest.mark.asyncio
async def test_graph_cleanup_exception_is_reported_and_recording_is_retryable(
    rg_factory: async_sessionmaker[AsyncSession],
    rg_crypto: AudioCrypto,
    rg_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    """Graph persistence failure enters the existing failure/retry reporting path."""

    class _BrokenStore:
        def __init__(self) -> None:
            import networkx as nx

            self.graph = nx.MultiDiGraph()

        async def save(self) -> None:
            raise RuntimeError("graph save failed")

        def invalidate_path_projection(self) -> None:
            return None

    def factory(_tenant: str) -> Any:
        return _BrokenStore()

    audio = tmp_path / "gfail.wav"
    audio.write_bytes(b"\x00" * 100)
    await _seed(rg_factory, days_ago=400, path=str(audio))

    enforcer = RetentionEnforcer(rg_factory, rg_crypto, rg_audit, factory, retention_days=90)
    report = await enforcer.run_sweep()
    assert report.deleted == 0
    assert len(report.errors) == 1
    assert "graph save failed" in report.errors[0]

    async with rg_factory() as session:
        recording = await session.get(Recording, 1)
    assert recording is not None

    await rg_audit.flush()
    from audio_graphy.models.audit_log import AuditLog

    async with rg_factory() as session:
        failure_actions = {
            row.action
            for row in (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.target == "recording:1",
                    )
                )
            )
            .scalars()
            .all()
        }
    assert "retention_delete_failed" in failure_actions


@pytest.mark.asyncio
async def test_async_factory_loads_cold_graph_and_cleanup_survives_reload(
    rg_factory: async_sessionmaker[AsyncSession],
    rg_crypto: AudioCrypto,
    rg_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    """Retention awaits the cold-store factory and flushes erasure to GraphML."""
    from audio_graphy.core.types import _list_to_str, _str_to_list
    from audio_graphy.storage.graph_networkx import NetworkXGraphStore

    audio = tmp_path / "cold-graph.wav"
    audio.write_bytes(b"\x00" * 100)
    await _seed(rg_factory, days_ago=400, path=str(audio))

    seeded = NetworkXGraphStore(tmp_path, tenant_id="chang_an")
    await seeded.load()
    seeded.graph.add_node("exclusive", recording_ids=_list_to_str(["1"]))
    seeded.graph.add_node("shared", recording_ids=_list_to_str(["1", "2"]))
    seeded.invalidate_path_projection()
    await seeded.save()

    cold_store = NetworkXGraphStore(tmp_path, tenant_id="chang_an")
    factory_calls = 0

    async def factory(_tenant: str) -> Any:
        nonlocal factory_calls
        factory_calls += 1
        await cold_store.load()
        return cold_store

    enforcer = RetentionEnforcer(rg_factory, rg_crypto, rg_audit, factory, retention_days=90)
    report = await enforcer.run_sweep()

    assert report.deleted == 1
    assert report.errors == []
    assert factory_calls == 1

    reloaded = NetworkXGraphStore(tmp_path, tenant_id="chang_an")
    await reloaded.load()
    assert "exclusive" not in reloaded.graph
    assert _str_to_list(reloaded.graph.nodes["shared"]["recording_ids"]) == ["2"]


@pytest.mark.asyncio
async def test_absent_audio_is_a_warned_noop_not_a_silent_one(
    rg_factory: async_sessionmaker[AsyncSession],
    rg_crypto: AudioCrypto,
    rg_audit: AuditWriter,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A missing file must stay tolerated — and must stop being invisible.

    The no-op is load-bearing: the unlink runs before the DB transaction, so a
    transient DB failure leaves audio-gone/row-present, and the retry only
    converges because the missing file is not an error. But the same branch also
    covers a file that lives on ANOTHER deployment's working_dir volume (two
    stacks, one database): the row is deleted, the sweep reports success, and
    the audio survives where nothing can find it by. Same outcome, opposite
    meaning — the log line is what lets an operator tell them apart.
    """
    missing = tmp_path / "never-written.wav"
    await _seed(rg_factory, days_ago=400, path=str(missing))

    enforcer = RetentionEnforcer(
        rg_factory, rg_crypto, rg_audit, _noop_graph_factory, retention_days=90
    )
    with caplog.at_level("WARNING", logger="audio_graphy.core.retention"):
        report = await enforcer.run_sweep()

    # Still a success — the deletion contract is unchanged.
    assert report.deleted == 1
    assert report.errors == []
    # ...but no longer a silent one.
    assert any("already absent" in r.message for r in caplog.records)
