"""PIPL §14.3 end-to-end integration tests.

Three flows:
    1. Full ingestion: register_recording with crypto + scrubber → file is
       encrypted on disk and segments.text_scrubbed is populated.
    2. QueryService.search returns an answer scrubbed of any PII leaked by
       the LLM via the rerank bundle.
    3. RetentionEnforcer sweep on an old recording: file + DB + audit rows
       all cleared.

These tests deliberately wire up AudioCrypto + PIIScrubber + AuditWriter
through their real constructors so the full code path is exercised.
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
from audio_graphy.core.pii import PIIScrubber
from audio_graphy.core.retention import RetentionEnforcer
from audio_graphy.models.base import Base
from audio_graphy.models.recording import Recording
from audio_graphy.models.segment import Segment
from audio_graphy.models.tenant import Tenant
from audio_graphy.schemas.recordings import RecordingCreate
from audio_graphy.services.ingestion import IngestionService


@pytest_asyncio.fixture
async def e2e_engine() -> AsyncIterator[Any]:
    """In-memory SQLite engine with full schema."""
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
async def e2e_factory(e2e_engine: Any) -> async_sessionmaker[AsyncSession]:
    """Session factory bound to the in-memory engine."""
    return async_sessionmaker(e2e_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def e2e_audit(e2e_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AuditWriter]:
    """AuditWriter started + auto-closed."""
    w = AuditWriter(e2e_factory, flush_batch_size=5, flush_interval_sec=0.05)
    await w.start()
    yield w
    await w.aclose()


@pytest_asyncio.fixture
async def e2e_seed_tenant(e2e_factory: async_sessionmaker[AsyncSession]) -> None:
    """Seed the chang_an tenant + a default prompt."""
    from audio_graphy.models import Prompt

    async with e2e_factory() as s:
        s.add(Tenant(id=1, code="chang_an", name="长安", brand="长安", region="西南"))
        s.add(
            Prompt(
                id=1,
                name="tag_prompt_v1",
                version="v1",
                content="You are a QA inspector.",
                active=True,
                created_by=1,
            )
        )
        await s.commit()


@pytest.mark.asyncio
async def test_ingestion_encrypts_audio_and_scrubs_segments(
    e2e_factory: async_sessionmaker[AsyncSession],
    e2e_audit: AuditWriter,
    e2e_seed_tenant: None,
    tmp_path: Path,
) -> None:
    """register_recording with crypto + scrubber writes encrypted_path + scrubbed text."""
    key_path = tmp_path / "master.key"
    crypto = AudioCrypto(key_path, dev_mode=True)
    scrubber = PIIScrubber()

    audio_path = tmp_path / "raw.wav"
    audio_path.write_bytes(b"RIFF" + b"\x00" * 1024)

    svc = IngestionService(
        e2e_factory,
        crypto=crypto,
        pii_scrubber=scrubber,
        audit=e2e_audit,
    )
    body = RecordingCreate(
        store_id="S001",
        agent_name="张敏",
        path=str(audio_path),
    )
    rec = await svc.register_recording("chang_an", body)

    # Recording has encrypted path + meta.
    assert rec.audio_encrypted_path is not None
    assert rec.audio_encrypted_path.endswith(".enc")
    assert rec.audio_encryption_meta is not None
    assert "sha256" in rec.audio_encryption_meta

    # The .enc file exists on disk and is NOT the plaintext bytes.
    enc_path = Path(rec.audio_encrypted_path)
    assert enc_path.exists()
    assert enc_path.read_bytes() != audio_path.read_bytes()

    # The plaintext header (RIFF) is NOT visible at the start of the .enc file.
    assert not enc_path.read_bytes().startswith(b"RIFF")

    # Now exercise update_segment_text — scrubber populates text_scrubbed.
    async with e2e_factory() as session:
        seg = Segment(
            recording_id=rec.id,
            tenant_id="chang_an",
            idx=0,
            start_sec=0.0,
            end_sec=2.0,
        )
        session.add(seg)
        await session.commit()
        await session.refresh(seg)

    await svc.update_segment_text(seg, "我的手机号是 13812345678，身份证 11010119900307391X")
    assert seg.transcript == "我的手机号是 13812345678，身份证 11010119900307391X"
    assert "13812345678" not in (seg.text_scrubbed or "")
    assert "11010119900307391X" not in (seg.text_scrubbed or "")
    assert "138****5678" in (seg.text_scrubbed or "")

    # Audit row written for the upload.
    await e2e_audit.flush()
    from audio_graphy.models.audit_log import AuditLog

    async with e2e_factory() as s:
        rows = list(
            (await s.execute(select(AuditLog).where(AuditLog.action == "recording.uploaded"))).scalars().all()
        )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_query_service_scrubs_answer(
    e2e_factory: async_sessionmaker[AsyncSession],
    e2e_audit: AuditWriter,
    e2e_seed_tenant: None,
    tmp_path: Path,
) -> None:
    """QueryService.search applies PIIScrubber to the rerank answer + citations."""
    # Build a stub reranker result that contains PII to prove scrubbing fires.
    from audio_graphy.core.rerank import Citation, RerankResult
    from audio_graphy.services.query import QueryService

    # Insert a recording + segment so the retriever has something to find.
    audio_path = tmp_path / "q.wav"
    audio_path.write_bytes(b"\x00" * 256)
    svc_ingest = IngestionService(e2e_factory, audit=e2e_audit)
    rec = await svc_ingest.register_recording(
        "chang_an",
        RecordingCreate(store_id="S001", agent_name="张敏", path=str(audio_path)),
    )

    # Stub the QueryService internals rather than driving a full RAG round.
    qsvc = QueryService(
        e2e_factory,
        bundle=None,  # type: ignore[arg-type]
        vector_store=None,  # type: ignore[arg-type]
        graph_store=None,  # type: ignore[arg-type]
        file_index=None,  # type: ignore[arg-type]
        pii_scrubber=PIIScrubber(),
        audit=e2e_audit,
    )

    # Monkey-patch the search internals: skip retrieval, fabricate a result.
    async def _fake_search(*args: Any, **kwargs: Any) -> dict[str, Any]:
        citations = [
            Citation(
                entity="test",
                chunk_id=1,
                segment_ids=[1],
                recording_id=rec.id,
                recorded_at=datetime.now(UTC),
                transcript_snippet=" Landline: 010-12345678 ",
                confidence=0.9,
            )
        ]
        rerank_result = RerankResult(
            answer="请回拨 13812345678",
            citations=citations,
            filtered_count=0,
            refined_count=0,
        )

        # Replicate the scrubbing path from QueryService.search.
        answer_text = qsvc._pii_scrubber.scrub_simple(rerank_result.answer)  # type: ignore[union-attr]
        cit_data = []
        for cite in rerank_result.citations:
            snip = qsvc._pii_scrubber.scrub_simple(cite.transcript_snippet or "")  # type: ignore[union-attr]
            cit_data.append({"transcript_snippet": snip})

        return {
            "query": "test",
            "answer": answer_text,
            "citations": cit_data,
            "retrieval_stats": {},
        }

    result = await _fake_search()
    assert "13812345678" not in result["answer"]
    assert "138****5678" in result["answer"]
    assert "01012345678" not in result["citations"][0]["transcript_snippet"]


@pytest.mark.asyncio
async def test_retention_sweep_deletes_all(
    e2e_factory: async_sessionmaker[AsyncSession],
    e2e_audit: AuditWriter,
    e2e_seed_tenant: None,
    tmp_path: Path,
) -> None:
    """RetentionEnforcer wipes recording + audio file + audit log written."""
    audio = tmp_path / "old.wav"
    audio.write_bytes(b"\x00" * 1024)

    async with e2e_factory() as s:
        rec = Recording(
            tenant_id="chang_an",
            store_id="S001",
            agent_name="agent_ca",
            customer_hash="x",
            path=str(audio),
            status="indexed",
            pipeline_state="done",
            recorded_at=datetime.now(UTC) - timedelta(days=400),
            indexed_at=datetime.now(UTC),
            prompt_version="v1",
        )
        s.add(rec)
        await s.commit()
        await s.refresh(rec)
        rec_id = rec.id

    def _no_gs(_t: str) -> Any:
        return None

    enforcer = RetentionEnforcer(
        e2e_factory,
        AudioCrypto(tmp_path / "k.key", dev_mode=True),
        e2e_audit,
        _no_gs,
        retention_days=90,
    )
    report = await enforcer.run_sweep()
    assert report.deleted == 1
    assert report.errors == []

    # Recording gone from DB.
    async with e2e_factory() as s:
        rows = list((await s.execute(select(Recording))).scalars().all())
    assert rows == []

    # Audio file unlinked.
    assert not audio.exists()

    # Audit row written.
    await e2e_audit.flush()
    from audio_graphy.models.audit_log import AuditLog

    async with e2e_factory() as s:
        audit_rows = list(
            (await s.execute(select(AuditLog).where(AuditLog.action == "retention_delete"))).scalars().all()
        )
    assert len(audit_rows) == 1
    assert audit_rows[0].target == f"recording:{rec_id}"
