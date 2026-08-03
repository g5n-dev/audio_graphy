"""Legacy write APIs may only enqueue deterministic canonical jobs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from audio_graphy.models import Base
from audio_graphy.models.reception import DialogueUnit, Reception, ReceptionRecording
from audio_graphy.models.recording import Recording
from audio_graphy.models.tag_current import TagCurrent
from audio_graphy.models.tag_fact import TagFact
from audio_graphy.models.tag_governance import (
    LegacyTagMapping,
    TagDeployment,
    TagEvaluationRun,
    TaggerVersion,
    TagSchema,
    TagSchemaVersion,
)
from audio_graphy.services.legacy_tag_compatibility import (
    LegacyTagCompatibilityService,
)
from audio_graphy.services.tag_governance import GovernanceConflictError


@pytest.fixture
async def compatibility_factory() -> async_sessionmaker[AsyncSession]:
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


async def _seed_compatibility(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[int, int, int]:
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        recording = Recording(
            tenant_id="chang_an",
            store_id="S001",
            agent_name="agent",
            path="/tmp/legacy-compatible.wav",
            status="indexed",
            pipeline_state="done",
            recorded_at=now,
        )
        session.add(recording)
        await session.flush()
        reception = Reception(
            tenant_id="chang_an",
            scenario="automotive",
            store_id="S001",
            status="ready",
            merge_mode="logical",
            started_at=now,
            ended_at=now + timedelta(seconds=10),
            version=1,
        )
        session.add(reception)
        await session.flush()
        session.add(
            ReceptionRecording(
                tenant_id="chang_an",
                reception_id=reception.id,
                recording_id=recording.id,
                sequence_no=0,
                timeline_start_sec=0,
                timeline_end_sec=10,
                source_start_sec=0,
                source_end_sec=10,
                gap_before_sec=0,
                decision_source="manual",
                merge_confidence=1,
                merge_reasons={},
            )
        )
        unit = DialogueUnit(
            tenant_id="chang_an",
            reception_id=reception.id,
            source_recording_id=recording.id,
            unit_index=0,
            version=1,
            start_sec=0,
            end_sec=10,
            topic="需求",
            business_stage="需求发现",
            segment_refs=[],
            speaker_refs=[],
            edit_status="auto",
        )
        session.add(unit)
        schema = TagSchema(
            tenant_id="chang_an",
            key="canonical",
            name="Canonical",
            status="published",
            created_by=1,
        )
        session.add(schema)
        await session.flush()
        version = TagSchemaVersion(
            tenant_id="chang_an",
            schema_id=schema.id,
            version="1",
            definitions=[
                {
                    "key": "intent",
                    "value_type": "enum",
                    "allowed_values": ["browse", "purchase"],
                    "subject_types": ["dialogue_unit"],
                }
            ],
            checksum="a" * 64,
            status="published",
            created_by=1,
        )
        session.add(version)
        await session.flush()
        schema.active_version_id = version.id
        tagger = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=version.id,
            version="qualified",
            engine="rule",
            prompt_content="",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="rules",
            thresholds={"intent": 0.7},
            config_checksum="b" * 64,
            status="qualified",
            created_by=1,
            qualified_at=now,
        )
        session.add(tagger)
        session.add(
            LegacyTagMapping(
                tenant_id="chang_an",
                legacy_tag_path="dialogue_tag_assignments.intent",
                schema_version_id=version.id,
                tag_key="intent",
                mapping={
                    "mode": "identity",
                    "source_subject": "dialogue_unit",
                    "target_subject": "dialogue_unit",
                },
                deterministic=True,
            )
        )
        await session.flush()
        return recording.id, reception.id, unit.id


@pytest.mark.asyncio
async def test_unambiguous_legacy_targets_enqueue_jobs_without_legacy_writes(
    compatibility_factory: async_sessionmaker[AsyncSession],
) -> None:
    recording_id, reception_id, unit_id = await _seed_compatibility(compatibility_factory)
    service = LegacyTagCompatibilityService(compatibility_factory)

    reception_job = await service.enqueue_reception(
        tenant_id="chang_an",
        reception_id=reception_id,
        legacy_paths=["intent"],
        actor_user_id=1,
        idempotency_key="derive-intent",
    )
    recording_job = await service.enqueue_recordings(
        tenant_id="chang_an",
        recording_ids=[recording_id],
        legacy_paths=["dialogue_tag_assignments.intent"],
        actor_user_id=1,
        operation="legacy_recording_auto",
        idempotency_key="recording-intent",
    )

    assert reception_job.status == "queued"
    assert reception_job.scope["dialogue_unit_ids"] == [unit_id]
    assert reception_job.scope["target_tag_keys"] == ["intent"]
    assert recording_job.scope["dialogue_unit_ids"] == [unit_id]
    async with compatibility_factory() as session:
        legacy_fact_count = int(
            (await session.execute(select(func.count(TagFact.id)))).scalar_one()
        )
        legacy_current_count = int(
            (await session.execute(select(func.count(TagCurrent.id)))).scalar_one()
        )
    assert legacy_fact_count == 0
    assert legacy_current_count == 0


@pytest.mark.asyncio
async def test_default_legacy_idempotency_key_changes_with_resolved_tagger_version(
    compatibility_factory: async_sessionmaker[AsyncSession],
) -> None:
    _recording_id, reception_id, _unit_id = await _seed_compatibility(compatibility_factory)
    service = LegacyTagCompatibilityService(compatibility_factory)

    first = await service.enqueue_reception(
        tenant_id="chang_an",
        reception_id=reception_id,
        legacy_paths=["intent"],
        actor_user_id=1,
    )
    async with compatibility_factory() as session, session.begin():
        baseline = await session.get(TaggerVersion, first.tagger_version_id)
        assert baseline is not None
        replacement = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=baseline.schema_version_id,
            version="qualified-replacement",
            engine="rule",
            prompt_content="",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="rules-v2",
            thresholds={"intent": 0.7},
            config_checksum="c" * 64,
            status="qualified",
            created_by=1,
            qualified_at=datetime.now(UTC) + timedelta(seconds=1),
        )
        session.add(replacement)
        await session.flush()
        replacement_id = int(replacement.id)

    second = await service.enqueue_reception(
        tenant_id="chang_an",
        reception_id=reception_id,
        legacy_paths=["intent"],
        actor_user_id=1,
    )

    assert first.id != second.id
    assert second.tagger_version_id == replacement_id


@pytest.mark.asyncio
async def test_legacy_recording_mapping_refuses_multi_unit_copy_and_unmapped_paths(
    compatibility_factory: async_sessionmaker[AsyncSession],
) -> None:
    recording_id, reception_id, _unit_id = await _seed_compatibility(compatibility_factory)
    async with compatibility_factory() as session, session.begin():
        session.add(
            DialogueUnit(
                tenant_id="chang_an",
                reception_id=reception_id,
                source_recording_id=recording_id,
                unit_index=1,
                version=1,
                start_sec=5,
                end_sec=10,
                topic="报价",
                business_stage="方案推荐",
                segment_refs=[],
                speaker_refs=[],
                edit_status="auto",
            )
        )
    service = LegacyTagCompatibilityService(compatibility_factory)

    with pytest.raises(GovernanceConflictError, match="cannot be copied"):
        await service.enqueue_recordings(
            tenant_id="chang_an",
            recording_ids=[recording_id],
            legacy_paths=["intent"],
            actor_user_id=1,
            operation="legacy_recording_auto",
        )
    with pytest.raises(GovernanceConflictError, match="workbench"):
        await service.enqueue_recordings(
            tenant_id="chang_an",
            recording_ids=[recording_id],
            legacy_paths=["quality.greeting"],
            actor_user_id=1,
            operation="legacy_recording_auto",
        )


@pytest.mark.asyncio
async def test_prompt_scope_is_stratifiable_and_activation_binds_matching_production_candidate(
    compatibility_factory: async_sessionmaker[AsyncSession],
) -> None:
    _recording_id, reception_id, first_unit_id = await _seed_compatibility(compatibility_factory)
    now = datetime.now(UTC)
    async with compatibility_factory() as session, session.begin():
        first_unit = await session.get(DialogueUnit, first_unit_id)
        assert first_unit is not None
        second_unit = DialogueUnit(
            tenant_id="chang_an",
            reception_id=reception_id,
            source_recording_id=None,
            unit_index=1,
            version=1,
            start_sec=10,
            end_sec=20,
            topic="报价",
            business_stage="方案推荐",
            segment_refs=[],
            speaker_refs=[],
            edit_status="auto",
        )
        session.add(second_unit)
        await session.flush()
        second_unit_id = int(second_unit.id)
        baseline = (
            await session.execute(
                select(TaggerVersion).where(
                    TaggerVersion.tenant_id == "chang_an",
                    TaggerVersion.version == "qualified",
                )
            )
        ).scalar_one()
        candidate = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=baseline.schema_version_id,
            version="prompt-production",
            engine="llm",
            prompt_content="canonical candidate prompt",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="weak-v2",
            thresholds={"intent": 0.7},
            config_checksum="d" * 64,
            status="qualified",
            created_by=1,
            qualified_at=now,
        )
        session.add(candidate)
        await session.flush()
        evaluation = TagEvaluationRun(
            tenant_id="chang_an",
            tagger_version_id=candidate.id,
            baseline_tagger_version_id=baseline.id,
            gold_set_version_id=1,
            status="completed",
            metrics={},
            baseline_metrics={},
            passed=True,
            started_at=now,
            finished_at=now,
            created_by=1,
        )
        session.add(evaluation)
        await session.flush()
        session.add(
            TagDeployment(
                tenant_id="chang_an",
                tagger_version_id=candidate.id,
                evaluation_run_id=evaluation.id,
                baseline_tagger_version_id=baseline.id,
                status="production",
                traffic_percent=100,
                revision=1,
                created_by=1,
                approved_by=1,
                approved_at=now,
            )
        )
        await session.flush()
        candidate_id = int(candidate.id)

    service = LegacyTagCompatibilityService(compatibility_factory)
    target = await service.resolve_prompt_scope(
        tenant_id="chang_an",
        legacy_paths=["intent"],
    )

    assert target.dialogue_unit_ids == (first_unit_id, second_unit_id)
    assert target.tagger_version_id == candidate_id
    validated = await service.validate_prompt_candidate(
        tenant_id="chang_an",
        resolved_target=target,
        candidate_tagger_version_id=candidate_id,
        prompt_content="canonical candidate prompt\r\n",
    )
    assert validated.tagger_version_id == candidate_id
    job = await service.enqueue_recordings(
        tenant_id="chang_an",
        recording_ids=[],
        legacy_paths=["intent"],
        actor_user_id=1,
        operation="legacy_prompt_activation",
        resolved_target=validated,
        prompt_id=17,
    )
    assert job.tagger_version_id == candidate_id
    assert job.scope["prompt_candidate_tagger_version_id"] == candidate_id
    assert job.scope["prompt_id"] == 17

    with pytest.raises(GovernanceConflictError, match=r"Prompt\.content"):
        await service.validate_prompt_candidate(
            tenant_id="chang_an",
            resolved_target=target,
            candidate_tagger_version_id=candidate_id,
            prompt_content="different prompt content",
        )
    with pytest.raises(GovernanceConflictError, match="production"):
        await service.validate_prompt_candidate(
            tenant_id="chang_an",
            resolved_target=target,
            candidate_tagger_version_id=int(baseline.id),
            prompt_content="",
        )
