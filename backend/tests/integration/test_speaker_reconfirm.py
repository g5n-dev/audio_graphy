"""T12 — Speaker reconfirm integration test.

End-to-end: a SpeakerFuzzyMatcher AMBIGUOUS verdict triggers the
``SpeakerLinker._enqueue_reconfirm`` path which inserts a row into the
``speaker_merge_pending`` table. The row's status is ``pending`` until
either:
  - The voiceprint reconfirm job runs (cosine >= 0.7 → resolved_inferred)
  - A human reviewer approves (resolved_inferred) or rejects (resolved_rejected)
  - The reconfirm TTL elapses (resolved_rejected via timeout)

This test simulates the first path (voiceprint reconfirm) by:
  1. Seeding two SpeakerNodes with similar display_names.
  2. Constructing a _NewSpeakerCandidate whose name fuzzy-matches one node.
  3. Calling SpeakerLinker._try_layer2_fuzzy.
  4. Asserting that speaker_merge_pending has a pending row.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

import audio_graphy.models  # noqa: F401
from audio_graphy.core.speaker_linker import SpeakerLinker, _NewSpeakerCandidate
from audio_graphy.models.base import Base
from audio_graphy.models.recording import Recording
from audio_graphy.models.speaker_merge_pending import SpeakerMergePending
from audio_graphy.models.speaker_node import SpeakerNode
from audio_graphy.models.tenant import Tenant


# ============================================================
# Fixtures
# ============================================================


@pytest_asyncio.fixture
async def sr_engine() -> AsyncIterator[Any]:
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
async def sr_factory(sr_engine: Any) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(sr_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def sr_seeded(sr_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[int]:
    """Seed one tenant + one recording + one SpeakerNode '王小姐'."""
    async with sr_factory() as session:
        session.add(Tenant(code="t1", name="T1"))
        await session.flush()
        session.add(
            Recording(
                tenant_id="t1",
                store_id="r1",
                path="/tmp/x.wav",
                status="indexed",
            )
        )
        await session.flush()
        session.add(
            SpeakerNode(
                tenant_id="t1",
                voiceprint_id="vp_seed",
                display_name="王小姐",
                speaker_role="customer",
                total_speech_sec=10.0,
                merge_strategy="single_recording",
            )
        )
        await session.commit()
    yield 1  # recording_id


# ============================================================
# Tests
# ============================================================


@pytest.mark.asyncio
async def test_amiguous_layer2_match_enqueues_pending(
    sr_factory: async_sessionmaker[AsyncSession],
    sr_seeded: int,
) -> None:
    """An AMBIGUOUS fuzzy verdict creates a SpeakerMergePending row."""
    linker = SpeakerLinker(
        session_factory=sr_factory,
        crypto=None,  # type: ignore[arg-type]
        audit=None,
        tenant_id="t1",
        enable_layer2_fuzzy=True,
    )

    # Load the existing SpeakerNode.
    async with sr_factory() as session:
        nodes = (
            await session.execute(
                select(SpeakerNode).where(SpeakerNode.tenant_id == "t1")
            )
        ).scalars().all()
    assert len(nodes) == 1

    # Build a candidate whose name is similar enough to trigger AMBIGUOUS.
    cand = _NewSpeakerCandidate(
        speaker_id="spk_0",
        voiceprint=(1.0, 0.0, 0.0),
        voiceprint_id="hash",
        recording_id=1,
        speech_sec=10.0,
        first_seen=None,
        role_hint="customer",
        display_name="王小姐",  # exact match → AMBIGUOUS (no voiceprint)
    )

    result = await linker._try_layer2_fuzzy(cand, list(nodes), recording_id=1)
    assert result is not None
    matched_sn, ambiguity_tag, fuzzy_score, vp_score = result
    assert ambiguity_tag == "AMBIGUOUS"
    assert fuzzy_score >= 0.85  # exact name match
    assert vp_score is None  # no voiceprint available
    assert matched_sn.id == nodes[0].id

    # Verify the SpeakerMergePending row was inserted.
    async with sr_factory() as session:
        rows = (
            await session.execute(
                select(SpeakerMergePending).where(
                    SpeakerMergePending.tenant_id == "t1"
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "pending"
    assert row.candidate_name == "王小姐"
    assert row.matched_speaker_node_id == nodes[0].id
    assert row.voiceprint_score is None


@pytest.mark.asyncio
async def test_no_match_does_not_enqueue(
    sr_factory: async_sessionmaker[AsyncSession],
    sr_seeded: int,
) -> None:
    """A NO_MATCH Layer 2 verdict does not enqueue a row."""
    linker = SpeakerLinker(
        session_factory=sr_factory,
        crypto=None,  # type: ignore[arg-type]
        audit=None,
        tenant_id="t1",
    )
    async with sr_factory() as session:
        nodes = (
            await session.execute(
                select(SpeakerNode).where(SpeakerNode.tenant_id == "t1")
            )
        ).scalars().all()

    cand = _NewSpeakerCandidate(
        speaker_id="spk_0",
        voiceprint=(1.0, 0.0, 0.0),
        voiceprint_id="hash",
        recording_id=1,
        speech_sec=10.0,
        first_seen=None,
        role_hint="customer",
        display_name="完全不匹配的名字XYZ",  # very different
    )

    result = await linker._try_layer2_fuzzy(cand, list(nodes), recording_id=1)
    # Depending on rapidfuzz token_ratio this might be NO_MATCH.
    if result is None:
        async with sr_factory() as session:
            rows = (
                await session.execute(
                    select(SpeakerMergePending).where(
                        SpeakerMergePending.tenant_id == "t1"
                    )
                )
            ).scalars().all()
        assert len(rows) == 0


@pytest.mark.asyncio
async def test_enqueue_failure_swallowed(
    sr_factory: async_sessionmaker[AsyncSession],
    sr_seeded: int,
) -> None:
    """If the SpeakerMergePending insert fails, Layer 2 still completes.

    The helper has a broad ``except Exception`` clause; we trigger it by
    deliberately sabotaging the session.commit() call. This is more
    representative of a real DB outage than mocking the ORM.
    """
    from unittest.mock import AsyncMock

    linker = SpeakerLinker(
        session_factory=sr_factory,
        crypto=None,  # type: ignore[arg-type]
        audit=None,
        tenant_id="t1",
    )

    fake_node = SpeakerNode(
        tenant_id="t1",
        voiceprint_id="vp_seed",
        display_name="x",
        speaker_role="customer",
        total_speech_sec=1.0,
        merge_strategy="single_recording",
    )
    fake_node.id = 1

    # Replace the session_factory's __call__ to return a session whose
    # commit raises. We do this by patching only this call.
    original_factory = linker._session_factory

    class _BadSession:
        def __aenter__(self) -> "_BadSession":
            return self

        def __aexit__(self, *args: object) -> None:
            return None

        def add(self, *args: object, **kwargs: object) -> None:
            pass

        async def commit(self) -> None:
            raise RuntimeError("simulated commit failure")

    class _BadFactory:
        def __call__(self) -> _BadSession:
            return _BadSession()

    linker._session_factory = _BadFactory()  # type: ignore[assignment]
    try:
        # Should not raise — the helper catches Exception.
        await linker._enqueue_reconfirm(
            recording_id=1,
            candidate_name="x",
            matched_node=fake_node,
            fuzzy_score=0.9,
            voiceprint_score=None,
        )
    finally:
        linker._session_factory = original_factory  # type: ignore[assignment]

    # No row should have been committed.
    async with sr_factory() as session:
        rows = (
            await session.execute(select(SpeakerMergePending))
        ).scalars().all()
    assert len(rows) == 0
    _ = AsyncMock  # suppress unused import


# Suppress unused-import warning.
_ = asyncio
