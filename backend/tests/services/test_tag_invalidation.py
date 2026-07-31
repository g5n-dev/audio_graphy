"""Targeted invalidation for dialogue edits keeps facts and queues one recompute."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from audio_graphy.models import Base
from audio_graphy.models.tag_governance import (
    TagAssignmentCurrent,
    TagAssignmentFact,
    TagExtractionJob,
)


@pytest.fixture
async def invalidation_factory() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_dialogue_edit_invalidation_is_targeted_idempotent_and_append_only(
    invalidation_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_invalidation import (
        invalidate_dialogue_units_in_session,
    )

    async with invalidation_factory() as session, session.begin():
        facts: list[TagAssignmentFact] = []
        for subject_id in (101, 102, 999):
            fact = TagAssignmentFact(
                tenant_id="chang_an",
                subject_type="dialogue_unit",
                subject_id=subject_id,
                reception_id=51,
                dialogue_unit_id=subject_id,
                tag_key="intent",
                tag_value="purchase",
                confidence=0.91,
                evidence_refs=[{"segment_id": subject_id, "start_sec": 0, "end_sec": 1}],
                source="llm",
                schema_version_id=1,
                tagger_version_id=2,
                extraction_run_id=None,
                deployment_id=None,
                input_hash=f"{subject_id:064d}",
                revision=1,
                tombstone=False,
                actor_user_id=None,
                assigned_at=datetime.now(UTC),
            )
            session.add(fact)
            facts.append(fact)
        await session.flush()
        for fact in facts:
            session.add(
                TagAssignmentCurrent(
                    tenant_id="chang_an",
                    subject_type="dialogue_unit",
                    subject_id=fact.subject_id,
                    tag_key=fact.tag_key,
                    fact_id=fact.id,
                    revision=fact.revision,
                )
            )

    async with invalidation_factory() as session, session.begin():
        first = await invalidate_dialogue_units_in_session(
            session,
            tenant_id="chang_an",
            reception_id=51,
            dialogue_unit_ids=[102, 101, 101],
            cause="manual_split",
            reception_version=7,
            actor_user_id=2,
        )
        replay = await invalidate_dialogue_units_in_session(
            session,
            tenant_id="chang_an",
            reception_id=51,
            dialogue_unit_ids=[101, 102],
            cause="manual_split",
            reception_version=7,
            actor_user_id=2,
        )

    async with invalidation_factory() as session:
        current_ids = set(
            (
                await session.execute(
                    select(TagAssignmentCurrent.subject_id).order_by(
                        TagAssignmentCurrent.subject_id
                    )
                )
            ).scalars()
        )
        fact_count = int(
            (await session.execute(select(func.count(TagAssignmentFact.id)))).scalar_one()
        )
        jobs = list((await session.execute(select(TagExtractionJob))).scalars())

    assert first.id == replay.id
    assert current_ids == {999}
    assert fact_count == 3
    assert len(jobs) == 1
    assert jobs[0].status == "queued"
    assert jobs[0].job_type == "recompute"
    assert jobs[0].scope == {
        "cause": "manual_split",
        "dialogue_unit_ids": [101, 102],
        "invalidated_dialogue_unit_ids": [101, 102],
        "reception_id": 51,
        "reception_version": 7,
        "subject_type": "dialogue_unit",
    }
    assert jobs[0].total_items == 2


@pytest.mark.asyncio
async def test_removed_units_are_invalidated_but_not_enqueued_for_recompute(
    invalidation_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_invalidation import (
        invalidate_dialogue_units_in_session,
    )

    async with invalidation_factory() as session, session.begin():
        job = await invalidate_dialogue_units_in_session(
            session,
            tenant_id="chang_an",
            reception_id=51,
            dialogue_unit_ids=[101, 102, 201, 202],
            recompute_dialogue_unit_ids=[201, 202],
            cause="automatic_resegmentation",
            reception_version=8,
            actor_user_id=2,
        )

    assert job.scope["invalidated_dialogue_unit_ids"] == [101, 102, 201, 202]
    assert job.scope["dialogue_unit_ids"] == [201, 202]
    assert job.total_items == 2
