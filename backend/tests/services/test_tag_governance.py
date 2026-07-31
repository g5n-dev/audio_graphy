"""Tag governance closed-loop service tests.

These tests intentionally exercise the invariants that make the governance
loop trustworthy: deterministic extraction inputs, evidence validation,
append-only facts/current projection atomicity, and durable job leases.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from audio_graphy.models import Base


@pytest.fixture
async def governance_factory() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from audio_graphy.models.user import User

    async with factory() as session, session.begin():
        session.add_all(
            [
                User(
                    id=user_id,
                    tenant_id="chang_an",
                    name=f"reviewer-{user_id}",
                    email=f"reviewer-{user_id}@example.test",
                    role="inspector",
                    password_hash="test",
                )
                for user_id in (2, 3, 7, 8, 9)
            ]
        )
    yield factory
    await engine.dispose()


async def _seed_dialogue_context(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[int, list[int], int]:
    from audio_graphy.models.reception import (
        DialogueUnit,
        Reception,
        ReceptionRecording,
    )
    from audio_graphy.models.recording import Recording
    from audio_graphy.models.segment import Segment

    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        recording = Recording(
            tenant_id="chang_an",
            store_id="S001",
            agent_name="agent_ca",
            path=f"/tmp/governance-{now.timestamp()}.wav",
            status="indexed",
            pipeline_state="done",
            recorded_at=now,
        )
        session.add(recording)
        await session.flush()
        segments = [
            Segment(
                tenant_id="chang_an",
                recording_id=recording.id,
                idx=index,
                start_sec=float(index),
                end_sec=float(index + 1),
                transcript=text,
                text_scrubbed=text,
                speaker="customer",
                vad_conf=0.99,
            )
            for index, text in enumerate(("客户想试驾", "客户决定购买"))
        ]
        session.add_all(segments)
        reception = Reception(
            tenant_id="chang_an",
            scenario="automotive",
            store_id="S001",
            status="ready",
            merge_mode="logical",
            started_at=now,
            ended_at=now + timedelta(seconds=2),
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
                timeline_end_sec=2,
                source_start_sec=0,
                source_end_sec=2,
                gap_before_sec=0,
                decision_source="manual",
                merge_confidence=1,
                merge_reasons={},
            )
        )
        await session.flush()
        unit = DialogueUnit(
            tenant_id="chang_an",
            reception_id=reception.id,
            source_recording_id=recording.id,
            unit_index=0,
            version=1,
            start_sec=0,
            end_sec=2,
            topic="购买",
            business_stage="成交意向",
            segment_refs=[
                {"segment_id": segment.id, "recording_id": recording.id} for segment in segments
            ],
            speaker_refs=["customer"],
            edit_status="auto",
        )
        session.add(unit)
        await session.flush()
        return unit.id, [segment.id for segment in segments], reception.id


async def _seed_single_tag_schema(
    factory: async_sessionmaker[AsyncSession],
    *,
    key: str,
    extra_definitions: list[dict[str, object]] | None = None,
) -> int:
    from audio_graphy.models.tag_governance import TagSchema, TagSchemaVersion

    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        schema = TagSchema(
            tenant_id="chang_an",
            key=key,
            name="单标签复核体系",
            status="published",
            created_by=1,
        )
        session.add(schema)
        await session.flush()
        definitions: list[dict[str, object]] = [
            {
                "key": "intent",
                "value_type": "enum",
                "allowed_values": ["browse", "purchase"],
                "evidence_required": True,
                "subject_types": ["dialogue_unit"],
                "scenarios": ["automotive"],
            }
        ]
        definitions.extend(extra_definitions or [])
        version = TagSchemaVersion(
            tenant_id="chang_an",
            schema_id=schema.id,
            version="1",
            definitions=definitions,
            checksum="6" * 64,
            status="published",
            created_by=1,
            published_by=1,
            published_at=now,
        )
        session.add(version)
        await session.flush()
        return int(version.id)


def test_compute_input_hash_is_stable_and_content_sensitive() -> None:
    from audio_graphy.services.tag_governance import compute_input_hash

    first = compute_input_hash(
        transcript=" 客户   想试驾\nCS75 ",
        segment_snapshot=[
            {"segment_id": 2, "version": 1, "start_sec": 2.0, "end_sec": 4.0},
            {"segment_id": 1, "version": 1, "start_sec": 0.0, "end_sec": 2.0},
        ],
        dialogue_unit_version=3,
        schema_checksum="schema-a",
        tagger_checksum="tagger-a",
        model_version="model-a",
    )
    equivalent = compute_input_hash(
        transcript="客户 想试驾 CS75",
        segment_snapshot=[
            {"segment_id": 1, "version": 1, "start_sec": 0, "end_sec": 2},
            {"segment_id": 2, "version": 1, "start_sec": 2, "end_sec": 4},
        ],
        dialogue_unit_version=3,
        schema_checksum="schema-a",
        tagger_checksum="tagger-a",
        model_version="model-a",
    )
    changed = compute_input_hash(
        transcript="客户 不想试驾 CS75",
        segment_snapshot=[
            {"segment_id": 1, "version": 1, "start_sec": 0, "end_sec": 2},
            {"segment_id": 2, "version": 1, "start_sec": 2, "end_sec": 4},
        ],
        dialogue_unit_version=3,
        schema_checksum="schema-a",
        tagger_checksum="tagger-a",
        model_version="model-a",
    )
    context_changed = compute_input_hash(
        transcript="客户 想试驾 CS75",
        segment_snapshot=[
            {"segment_id": 1, "version": 1, "start_sec": 0, "end_sec": 2},
            {"segment_id": 2, "version": 1, "start_sec": 2, "end_sec": 4},
        ],
        dialogue_unit_version=3,
        schema_checksum="schema-a",
        tagger_checksum="tagger-a",
        model_version="model-a",
        context_snapshot={
            "subject_type": "dialogue_unit",
            "scenario": "retail",
            "store_id": "S002",
        },
    )

    assert first == equivalent
    assert len(first) == 64
    assert changed != first
    assert context_changed != first


def test_canary_bucket_and_gold_split_are_process_independent() -> None:
    from audio_graphy.services.tag_governance import (
        deterministic_gold_split,
        stable_canary_bucket,
    )

    assert stable_canary_bucket("chang_an", 1001, 9) == stable_canary_bucket("chang_an", 1001, 9)
    assert 0 <= stable_canary_bucket("chang_an", 1001, 9) < 100
    assert deterministic_gold_split("chang_an", 1001) in {
        "train",
        "validation",
        "holdout",
    }
    assert deterministic_gold_split("chang_an", 1001) == deterministic_gold_split("chang_an", 1001)


def test_efficiency_policy_pauses_after_two_complete_soft_regression_windows() -> None:
    from audio_graphy.services.tag_governance import (
        _evaluate_efficiency_policy,
        _PolicyObservation,
    )

    end = datetime(2026, 7, 27, 12, 30, tzinfo=UTC)
    samples = []
    for index in range(6):
        window_start = end - timedelta(minutes=30 - index * 5)
        candidate_tokens = 111 if index < 3 else 112
        samples.append(
            _PolicyObservation(
                window_start=window_start,
                window_end=window_start + timedelta(minutes=5),
                metrics={
                    "efficiency_measurement_complete": True,
                    "efficiency_paired_subject_count": 1,
                    "candidate_provider_tokens": candidate_tokens,
                    "baseline_provider_tokens": 100,
                    "candidate_cost_microunits": 100,
                    "baseline_cost_microunits": 100,
                },
            )
        )

    policy = _evaluate_efficiency_policy(samples, window_end=end, required=True)

    assert policy["complete"] is True
    assert policy["consecutive_breach"] is True
    assert policy["hard_breach"] is False


def test_efficiency_policy_hard_breach_needs_only_latest_complete_window() -> None:
    from audio_graphy.services.tag_governance import (
        _evaluate_efficiency_policy,
        _PolicyObservation,
    )

    end = datetime(2026, 7, 27, 12, 30, tzinfo=UTC)
    samples = [
        _PolicyObservation(
            window_start=end - timedelta(minutes=15 - index * 5),
            window_end=end - timedelta(minutes=10 - index * 5),
            metrics={
                "efficiency_measurement_complete": True,
                "efficiency_paired_subject_count": 1,
                "candidate_provider_tokens": 130,
                "baseline_provider_tokens": 100,
                "candidate_cost_microunits": 100,
                "baseline_cost_microunits": 100,
            },
        )
        for index in range(3)
    ]

    policy = _evaluate_efficiency_policy(samples, window_end=end, required=True)

    assert policy["complete"] is False
    assert policy["hard_breach"] is True
    assert policy["reason"] == "incomplete_trusted_coverage"


@pytest.mark.asyncio
async def test_shadow_sampling_completion_is_persisted_before_duration_gate(
    governance_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from audio_graphy.models.tag_governance import (
        TagDeployment,
        TagEvaluationRun,
        TaggerVersion,
    )
    from audio_graphy.services import tag_governance as governance_module
    from audio_graphy.services.tag_governance import TagGovernanceService

    unit_id, _segment_ids, _reception_id = await _seed_dialogue_context(governance_factory)
    schema_version_id = await _seed_single_tag_schema(
        governance_factory,
        key="shadow-sampling-complete",
    )
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    async with governance_factory() as session, session.begin():
        baseline = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=schema_version_id,
            version="shadow-sampling-baseline",
            engine="rule",
            prompt_content="",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="rules-v1",
            thresholds={},
            config_checksum="a" * 64,
            status="qualified",
            created_by=1,
            qualified_at=now,
        )
        candidate = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=schema_version_id,
            version="shadow-sampling-candidate",
            engine="rule",
            prompt_content="",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="rules-v2",
            thresholds={},
            config_checksum="b" * 64,
            status="qualified",
            created_by=1,
            qualified_at=now,
        )
        session.add_all([baseline, candidate])
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
        deployment = TagDeployment(
            tenant_id="chang_an",
            tagger_version_id=candidate.id,
            evaluation_run_id=evaluation.id,
            baseline_tagger_version_id=baseline.id,
            status="shadow",
            traffic_percent=0,
            revision=1,
            created_at=now,
            created_by=1,
        )
        session.add(deployment)
        await session.flush()
        deployment_id = int(deployment.id)

    monkeypatch.setitem(
        governance_module._PROMOTION_REQUIREMENTS,
        "shadow",
        {
            "duration_hours": 24,
            "served_count": 0,
            "paired_count": 1,
            "audited_count": 1,
        },
    )
    service = TagGovernanceService(governance_factory)
    _, untrusted = await service.record_deployment_observation(
        tenant_id="chang_an",
        deployment_id=deployment_id,
        sample_reception_ids=[],
        metrics={"run_count": 1, "failed_run_count": 0, "error_rate": 0.0},
        breach_codes=[],
        window_start=now,
        window_end=now + timedelta(minutes=5),
        actor_user_id=1,
        source="monitor",
        is_trusted=False,
        paired_subject_keys=[("dialogue_unit", unit_id)],
        audited_subject_keys=[("dialogue_unit", unit_id)],
    )
    assert untrusted.sampling_complete_at is None

    window_end = now + timedelta(minutes=10)
    observation, sampled = await service.record_deployment_observation(
        tenant_id="chang_an",
        deployment_id=deployment_id,
        sample_reception_ids=[],
        metrics={"run_count": 1, "failed_run_count": 0, "error_rate": 0.0},
        breach_codes=[],
        window_start=now + timedelta(minutes=5),
        window_end=window_end,
        actor_user_id=1,
        source="monitor",
        is_trusted=True,
        paired_subject_keys=[("dialogue_unit", unit_id)],
        audited_subject_keys=[("dialogue_unit", unit_id)],
    )

    assert sampled.status == "shadow"
    assert sampled.sampling_complete_at == window_end
    assert "duration_hours" in observation.metrics["promotion_readiness"]["unmet"]
    async with governance_factory() as session:
        persisted = await session.get(TagDeployment, deployment_id)
        assert persisted is not None
        persisted_at = persisted.sampling_complete_at
        assert persisted_at is not None
        assert persisted_at.replace(tzinfo=persisted_at.tzinfo or UTC) == window_end


def test_validate_assignment_rejects_missing_required_evidence() -> None:
    from audio_graphy.services.tag_governance import (
        AssignmentValidationError,
        validate_assignment,
    )

    definition = {
        "key": "compliance_risk",
        "value_type": "enum",
        "allowed_values": ["none", "personal_transfer"],
        "evidence_required": True,
    }
    with pytest.raises(AssignmentValidationError, match="evidence"):
        validate_assignment(
            definition=definition,
            label_value="personal_transfer",
            confidence=0.93,
            evidence_refs=[],
        )


@pytest.mark.asyncio
async def test_append_fact_atomically_replaces_current_without_mutating_history(
    governance_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.models.tag_governance import (
        TagAssignmentCurrent,
        TagAssignmentFact,
        TagSchema,
        TagSchemaVersion,
    )
    from audio_graphy.services.tag_governance import (
        TagGovernanceService,
    )

    service = TagGovernanceService(governance_factory)
    unit_id, segment_ids, _reception_id = await _seed_dialogue_context(governance_factory)
    async with governance_factory() as session:
        schema = TagSchema(
            tenant_id="chang_an",
            key="test",
            name="测试",
            status="published",
            created_by=1,
        )
        session.add(schema)
        await session.flush()
        schema_version = TagSchemaVersion(
            tenant_id="chang_an",
            schema_id=schema.id,
            version="1",
            definitions=[
                {
                    "key": "intent",
                    "value_type": "enum",
                    "allowed_values": ["test_drive", "purchase"],
                    "evidence_required": True,
                    "subject_types": ["dialogue_unit"],
                    "scenarios": ["automotive"],
                }
            ],
            checksum="c" * 64,
            status="published",
            created_by=1,
        )
        session.add(schema_version)
        await session.commit()
    first = await service.append_assignment(
        tenant_id="chang_an",
        subject_type="dialogue_unit",
        subject_id=unit_id,
        tag_key="intent",
        tag_value="test_drive",
        confidence=0.72,
        evidence_refs=[{"segment_id": segment_ids[0], "start_sec": 0.0, "end_sec": 1.0}],
        source="imported",
        schema_version_id=schema_version.id,
        tagger_version_id=None,
        extraction_run_id=None,
        deployment_id=None,
        input_hash="a" * 64,
        actor_user_id=1,
    )
    second = await service.append_assignment(
        tenant_id="chang_an",
        subject_type="dialogue_unit",
        subject_id=unit_id,
        tag_key="intent",
        tag_value="purchase",
        confidence=0.91,
        evidence_refs=[{"segment_id": segment_ids[1], "start_sec": 1.0, "end_sec": 2.0}],
        source="manual",
        schema_version_id=schema_version.id,
        tagger_version_id=None,
        extraction_run_id=None,
        deployment_id=None,
        input_hash="b" * 64,
        actor_user_id=1,
    )

    unit_id, segment_ids, _reception_id = await _seed_dialogue_context(governance_factory)
    async with governance_factory() as session:
        facts = list(
            (await session.execute(select(TagAssignmentFact).order_by(TagAssignmentFact.revision)))
            .scalars()
            .all()
        )
        current = (await session.execute(select(TagAssignmentCurrent))).scalar_one()

    assert [fact.id for fact in facts] == [first.id, second.id]
    assert facts[0].superseded_fact_id is None
    assert facts[1].superseded_fact_id == first.id
    assert current.fact_id == second.id
    assert second.revision == 2


@pytest.mark.asyncio
async def test_automatic_assignment_never_replaces_manual_current(
    governance_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.models.tag_governance import (
        TagAssignmentCurrent,
        TagAssignmentFact,
        TagExtractionJob,
        TagExtractionRun,
        TaggerVersion,
        TagSchema,
        TagSchemaVersion,
    )
    from audio_graphy.services.tag_governance import TagGovernanceService

    unit_id, segment_ids, _reception_id = await _seed_dialogue_context(governance_factory)
    now = datetime.now(UTC)
    async with governance_factory() as session, session.begin():
        schema = TagSchema(
            tenant_id="chang_an",
            key="manual-wins",
            name="人工事实优先",
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
                    "evidence_required": True,
                    "subject_types": ["dialogue_unit"],
                    "scenarios": ["automotive"],
                }
            ],
            checksum="1" * 64,
            status="published",
            created_by=1,
        )
        session.add(version)
        await session.flush()
        tagger = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=version.id,
            version="manual-wins-rules-v1",
            engine="rule",
            prompt_content="",
            rule_bundle={
                "dsl_version": "1",
                "rules": [
                    {
                        "tag_key": "intent",
                        "value": "purchase",
                        "contains_any": ["购买"],
                    }
                ],
            },
            model_version="rules-local",
            thresholds={"intent": 0.7},
            config_checksum="2" * 64,
            status="qualified",
            created_by=1,
            qualified_at=now,
        )
        session.add(tagger)
        await session.flush()
        job = TagExtractionJob(
            tenant_id="chang_an",
            job_type="extract",
            status="running",
            scope={"dialogue_unit_ids": [unit_id]},
            tagger_version_id=tagger.id,
            idempotency_key="manual-wins-extract",
            total_items=1,
            completed_items=0,
            failed_items=0,
            attempt_count=1,
            max_attempts=3,
            revision=1,
            lease_owner="test",
            created_by=1,
        )
        session.add(job)
        await session.flush()
        run = TagExtractionRun(
            tenant_id="chang_an",
            job_id=job.id,
            subject_type="dialogue_unit",
            subject_id=unit_id,
            tagger_version_id=tagger.id,
            deployment_id=None,
            input_hash="3" * 64,
            input_snapshot={},
            output_snapshot={},
            status="running",
            started_at=now,
        )
        session.add(run)
        await session.flush()
        version_id = version.id
        tagger_id = tagger.id
        run_id = run.id

    service = TagGovernanceService(governance_factory)
    automatic = await service.append_assignment(
        tenant_id="chang_an",
        subject_type="dialogue_unit",
        subject_id=unit_id,
        tag_key="intent",
        tag_value="browse",
        confidence=0.8,
        evidence_refs=[{"segment_id": segment_ids[0], "start_sec": 0.0, "end_sec": 1.0}],
        source="rule",
        schema_version_id=version_id,
        tagger_version_id=tagger_id,
        extraction_run_id=run_id,
        deployment_id=None,
        input_hash="4" * 64,
        actor_user_id=1,
    )
    manual = await service.append_assignment(
        tenant_id="chang_an",
        subject_type="dialogue_unit",
        subject_id=unit_id,
        tag_key="intent",
        tag_value="purchase",
        confidence=1.0,
        evidence_refs=[{"segment_id": segment_ids[1], "start_sec": 1.0, "end_sec": 2.0}],
        source="manual",
        schema_version_id=version_id,
        tagger_version_id=None,
        extraction_run_id=None,
        deployment_id=None,
        input_hash="5" * 64,
        actor_user_id=2,
    )
    rerun = await service.append_assignment(
        tenant_id="chang_an",
        subject_type="dialogue_unit",
        subject_id=unit_id,
        tag_key="intent",
        tag_value="browse",
        confidence=0.95,
        evidence_refs=[{"segment_id": segment_ids[0], "start_sec": 0.0, "end_sec": 1.0}],
        source="rule",
        schema_version_id=version_id,
        tagger_version_id=tagger_id,
        extraction_run_id=run_id,
        deployment_id=None,
        input_hash="6" * 64,
        actor_user_id=1,
    )

    async def _current_fact_id() -> int:
        async with governance_factory() as session:
            return (
                await session.execute(
                    select(TagAssignmentCurrent.fact_id).where(
                        TagAssignmentCurrent.subject_id == unit_id,
                        TagAssignmentCurrent.tag_key == "intent",
                    )
                )
            ).scalar_one()

    assert automatic.revision == 1
    assert manual.revision == 2
    assert rerun.revision == 3
    assert await _current_fact_id() == manual.id

    await service.ensure_current_fact(
        tenant_id="chang_an",
        fact_id=rerun.id,
        extraction_run_id=run_id,
    )
    assert await _current_fact_id() == manual.id

    async with governance_factory() as session:
        facts = list(
            (
                await session.execute(
                    select(TagAssignmentFact)
                    .where(TagAssignmentFact.subject_id == unit_id)
                    .order_by(TagAssignmentFact.revision)
                )
            )
            .scalars()
            .all()
        )
    assert [fact.id for fact in facts] == [automatic.id, manual.id, rerun.id]


@pytest.mark.asyncio
async def test_auto_fact_requires_published_schema_and_valid_value(
    governance_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.models.tag_governance import TagSchema, TagSchemaVersion
    from audio_graphy.services.tag_governance import (
        AssignmentValidationError,
        TagGovernanceService,
    )

    unit_id, segment_ids, _reception_id = await _seed_dialogue_context(governance_factory)
    async with governance_factory() as session:
        schema = TagSchema(
            tenant_id="chang_an",
            key="test",
            name="测试",
            status="draft",
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
                    "allowed_values": ["purchase"],
                    "evidence_required": True,
                    "subject_types": ["dialogue_unit"],
                    "scenarios": ["automotive"],
                }
            ],
            checksum="d" * 64,
            status="draft",
            created_by=1,
        )
        session.add(version)
        await session.commit()

    service = TagGovernanceService(governance_factory)
    with pytest.raises(AssignmentValidationError, match="published"):
        await service.append_assignment(
            tenant_id="chang_an",
            subject_type="dialogue_unit",
            subject_id=unit_id,
            tag_key="intent",
            tag_value="browse",
            confidence=0.8,
            evidence_refs=[{"segment_id": segment_ids[0], "start_sec": 0, "end_sec": 1}],
            source="rule",
            schema_version_id=version.id,
            tagger_version_id=1,
            extraction_run_id=None,
            deployment_id=None,
            input_hash="a" * 64,
            actor_user_id=1,
        )


@pytest.mark.asyncio
async def test_manual_correction_can_select_new_evidence_from_same_reception(
    governance_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.models.tag_governance import (
        TagAssignmentCurrent,
        TagSchema,
        TagSchemaVersion,
    )
    from audio_graphy.services.tag_governance import TagGovernanceService

    unit_id, segment_ids, _reception_id = await _seed_dialogue_context(governance_factory)
    async with governance_factory() as session, session.begin():
        schema = TagSchema(
            tenant_id="chang_an",
            key="new-correction-evidence",
            name="新证据纠正",
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
                    "evidence_required": True,
                    "subject_types": ["dialogue_unit"],
                    "scenarios": ["automotive"],
                }
            ],
            checksum="a" * 64,
            status="published",
            created_by=1,
        )
        session.add(version)
        await session.flush()
        version_id = version.id
    service = TagGovernanceService(governance_factory)
    original = await service.append_assignment(
        tenant_id="chang_an",
        subject_type="dialogue_unit",
        subject_id=unit_id,
        tag_key="intent",
        tag_value="browse",
        confidence=1,
        evidence_refs=[{"segment_id": segment_ids[0], "start_sec": 0, "end_sec": 1}],
        source="manual",
        schema_version_id=version_id,
        tagger_version_id=None,
        extraction_run_id=None,
        deployment_id=None,
        input_hash="b" * 64,
        actor_user_id=1,
    )
    async with governance_factory() as session, session.begin():
        _superseded, corrected = await service.append_manual_correction_in_session(
            session,
            tenant_id="chang_an",
            expected_fact_id=original.id,
            tag_value="purchase",
            evidence_refs=[{"segment_id": segment_ids[1], "start_sec": 1, "end_sec": 2}],
            reason="第二段录音提供了更直接的购买证据",
            actor_user_id=2,
        )
    async with governance_factory() as session:
        current = (await session.execute(select(TagAssignmentCurrent))).scalar_one()
    assert corrected.evidence_refs[0]["segment_id"] == segment_ids[1]
    assert current.fact_id == corrected.id


@pytest.mark.asyncio
async def test_evidence_cannot_cross_two_reception_spans_of_same_recording(
    governance_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.models.reception import DialogueUnit, Reception, ReceptionRecording
    from audio_graphy.models.recording import Recording
    from audio_graphy.models.segment import Segment
    from audio_graphy.models.tag_governance import TagSchema, TagSchemaVersion
    from audio_graphy.services.tag_governance import (
        AssignmentValidationError,
        TagGovernanceService,
    )

    now = datetime.now(UTC)
    async with governance_factory() as session, session.begin():
        recording = Recording(
            tenant_id="chang_an",
            store_id="S001",
            agent_name="agent",
            path="/tmp/split-reception.wav",
            status="indexed",
            pipeline_state="done",
            recorded_at=now,
        )
        session.add(recording)
        await session.flush()
        segment = Segment(
            tenant_id="chang_an",
            recording_id=recording.id,
            idx=0,
            start_sec=4,
            end_sec=6,
            transcript="跨接待边界",
            text_scrubbed="跨接待边界",
            speaker="customer",
            vad_conf=0.99,
        )
        session.add(segment)
        receptions = [
            Reception(
                tenant_id="chang_an",
                scenario="automotive",
                store_id="S001",
                status="ready",
                merge_mode="logical",
                started_at=now + timedelta(seconds=offset),
                ended_at=now + timedelta(seconds=offset + 5),
                version=1,
            )
            for offset in (0, 5)
        ]
        session.add_all(receptions)
        await session.flush()
        session.add_all(
            [
                ReceptionRecording(
                    tenant_id="chang_an",
                    reception_id=reception.id,
                    recording_id=recording.id,
                    sequence_no=0,
                    timeline_start_sec=0,
                    timeline_end_sec=5,
                    source_start_sec=float(index * 5),
                    source_end_sec=float((index + 1) * 5),
                    gap_before_sec=0,
                    decision_source="manual",
                    merge_confidence=1,
                    merge_reasons={},
                )
                for index, reception in enumerate(receptions)
            ]
        )
        unit = DialogueUnit(
            tenant_id="chang_an",
            reception_id=receptions[0].id,
            source_recording_id=recording.id,
            unit_index=0,
            version=1,
            start_sec=4,
            end_sec=5,
            topic="边界",
            business_stage="需求发现",
            segment_refs=[
                {
                    "segment_id": segment.id,
                    "recording_id": recording.id,
                    "source_start_sec": 4,
                    "source_end_sec": 5,
                }
            ],
            speaker_refs=["customer"],
            edit_status="auto",
        )
        session.add(unit)
        schema = TagSchema(
            tenant_id="chang_an",
            key="split-evidence",
            name="切分证据",
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
                    "allowed_values": ["browse"],
                    "evidence_required": True,
                    "subject_types": ["dialogue_unit"],
                    "scenarios": ["automotive"],
                }
            ],
            checksum="c" * 64,
            status="published",
            created_by=1,
        )
        session.add(version)
        await session.flush()
        unit_id = unit.id
        segment_id = segment.id
        version_id = version.id

    service = TagGovernanceService(governance_factory)
    with pytest.raises(AssignmentValidationError, match="recording span"):
        await service.append_assignment(
            tenant_id="chang_an",
            subject_type="dialogue_unit",
            subject_id=unit_id,
            tag_key="intent",
            tag_value="browse",
            confidence=1,
            evidence_refs=[{"segment_id": segment_id, "start_sec": 5, "end_sec": 6}],
            source="manual",
            schema_version_id=version_id,
            tagger_version_id=None,
            extraction_run_id=None,
            deployment_id=None,
            input_hash="d" * 64,
            actor_user_id=1,
        )
    valid = await service.append_assignment(
        tenant_id="chang_an",
        subject_type="dialogue_unit",
        subject_id=unit_id,
        tag_key="intent",
        tag_value="browse",
        confidence=1,
        evidence_refs=[{"segment_id": segment_id, "start_sec": 4, "end_sec": 5}],
        source="manual",
        schema_version_id=version_id,
        tagger_version_id=None,
        extraction_run_id=None,
        deployment_id=None,
        input_hash="e" * 64,
        actor_user_id=1,
    )
    assert valid.reception_id == receptions[0].id


@pytest.mark.asyncio
async def test_job_lease_and_compare_and_swap_are_owner_scoped(
    governance_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_governance import TagGovernanceService

    service = TagGovernanceService(governance_factory)
    job = await service.enqueue_job(
        tenant_id="chang_an",
        job_type="extract",
        scope={"dialogue_unit_ids": [101]},
        idempotency_key="extract-unit-101",
        created_by=1,
    )
    duplicate = await service.enqueue_job(
        tenant_id="chang_an",
        job_type="extract",
        scope={"dialogue_unit_ids": [101]},
        idempotency_key="extract-unit-101",
        created_by=1,
    )
    assert duplicate.id == job.id

    claimed = await service.claim_next_job(
        worker_id="worker-a",
        now=datetime.now(UTC),
        lease_for=timedelta(seconds=30),
    )
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.status == "running"
    assert claimed.lease_owner == "worker-a"

    assert not await service.heartbeat_job(
        job.id,
        tenant_id="chang_an",
        worker_id="worker-b",
        expected_revision=claimed.revision,
        now=datetime.now(UTC),
        lease_for=timedelta(seconds=30),
    )
    assert await service.heartbeat_job(
        job.id,
        tenant_id="chang_an",
        worker_id="worker-a",
        expected_revision=claimed.revision,
        now=datetime.now(UTC),
        lease_for=timedelta(seconds=30),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("job_type", ["optimize", "evaluate"])
async def test_generic_job_cancel_rejects_optimizer_owned_jobs(
    governance_factory: async_sessionmaker[AsyncSession],
    job_type: str,
) -> None:
    from audio_graphy.models.tag_governance import TagExtractionJob
    from audio_graphy.services.tag_governance import (
        GovernanceConflictError,
        TagGovernanceService,
    )

    async with governance_factory() as session, session.begin():
        job = TagExtractionJob(
            tenant_id="chang_an",
            job_type=job_type,
            origin="system",
            status="running",
            scope={"optimization_run_id": 91},
            idempotency_key=f"optimizer-owned-{job_type}",
            total_items=1,
            completed_items=0,
            failed_items=0,
            attempt_count=1,
            max_attempts=3,
            revision=1,
            lease_owner="optimizer-worker",
            created_by=1,
        )
        session.add(job)
        await session.flush()
        job_id = int(job.id)

    service = TagGovernanceService(governance_factory)
    with pytest.raises(GovernanceConflictError, match="optimization-run cancel"):
        await service.cancel_job(
            tenant_id="chang_an",
            job_id=job_id,
            actor_user_id=1,
        )

    async with governance_factory() as session:
        persisted = await session.get(TagExtractionJob, job_id)
    assert persisted is not None
    assert persisted.status == "running"
    assert persisted.lease_owner == "optimizer-worker"
    assert persisted.revision == 1


@pytest.mark.asyncio
async def test_evaluation_gates_block_critical_recall_regression(
    governance_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_governance import evaluate_quality_gates

    results = evaluate_quality_gates(
        metrics={
            "macro_f1": 0.88,
            "critical_recall": 0.93,
            "evidence_coverage": 0.99,
            "error_rate": 0.002,
        },
        baseline={"macro_f1": 0.87, "critical_recall": 0.96},
        supported_label_f1={},
        baseline_label_f1={},
    )

    assert not results.passed
    assert any(gate.code == "critical_recall" and not gate.passed for gate in results.gates)


@pytest.mark.asyncio
async def test_evaluation_gates_require_zero_schema_evidence_and_lineage_violations(
    governance_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_governance import evaluate_quality_gates

    results = evaluate_quality_gates(
        metrics={
            "macro_f1": 0.99,
            "critical_recall": 0.99,
            "evidence_coverage": 1.0,
            "error_rate": 0.0,
            "schema_violation_count": 1,
            "evidence_violation_count": 0,
            "lineage_violation_count": 0,
        },
        baseline={"macro_f1": 0.98, "critical_recall": 0.98},
        supported_label_f1={},
        baseline_label_f1={},
    )

    assert not results.passed
    assert any(gate.code == "schema_integrity" and not gate.passed for gate in results.gates)
    assert any(gate.code == "evidence_integrity" and gate.passed for gate in results.gates)
    assert any(gate.code == "lineage_integrity" and gate.passed for gate in results.gates)


@pytest.mark.asyncio
async def test_paused_deployment_cannot_be_approved(
    governance_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.models.tag_governance import (
        TagDeployment,
        TagEvaluationRun,
        TaggerVersion,
    )
    from audio_graphy.services.tag_governance import (
        GovernanceConflictError,
        TagGovernanceService,
    )

    schema_version_id = await _seed_single_tag_schema(
        governance_factory,
        key="paused-approval",
    )
    now = datetime.now(UTC)
    async with governance_factory() as session, session.begin():
        baseline = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=schema_version_id,
            version="paused-baseline",
            engine="rule",
            prompt_content="",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="rules-v1",
            thresholds={"intent": 0.7},
            config_checksum="7" * 64,
            status="qualified",
            created_by=1,
            qualified_at=now,
        )
        candidate = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=schema_version_id,
            version="paused-candidate",
            engine="rule",
            prompt_content="",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="rules-v2",
            thresholds={"intent": 0.7},
            config_checksum="8" * 64,
            status="qualified",
            created_by=1,
            qualified_at=now,
        )
        session.add_all([baseline, candidate])
        await session.flush()
        evaluation = TagEvaluationRun(
            tenant_id="chang_an",
            tagger_version_id=candidate.id,
            baseline_tagger_version_id=baseline.id,
            gold_set_version_id=1,
            dataset_snapshot_hash="9" * 64,
            status="completed",
            metrics={"holdout_only": True, "sealed_release": True},
            baseline_metrics={},
            passed=True,
            started_at=now,
            finished_at=now,
            created_by=1,
        )
        session.add(evaluation)
        await session.flush()
        deployment = TagDeployment(
            tenant_id="chang_an",
            tagger_version_id=candidate.id,
            evaluation_run_id=evaluation.id,
            baseline_tagger_version_id=baseline.id,
            status="awaiting_admin",
            traffic_percent=25,
            revision=4,
            promotion_paused=True,
            pause_reason="monitoring breach",
            created_by=1,
        )
        session.add(deployment)
        await session.flush()
        deployment_id = deployment.id

    with pytest.raises(GovernanceConflictError, match="paused"):
        await TagGovernanceService(governance_factory).transition_deployment(
            tenant_id="chang_an",
            deployment_id=deployment_id,
            action="approve",
            actor_user_id=9,
            expected_revision=4,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("review_value", "resume_allowed"),
    [("purchase", True), ("browse", False)],
)
async def test_admin_can_resume_drift_pause_only_after_definitive_review(
    governance_factory: async_sessionmaker[AsyncSession],
    review_value: str,
    resume_allowed: bool,
) -> None:
    from audio_graphy.models.tag_governance import (
        TagDeployment,
        TagDeploymentObservation,
        TagEvaluationRun,
        TagExtractionJob,
        TaggerVersion,
        TagReviewTask,
    )
    from audio_graphy.services.tag_governance import (
        GovernanceConflictError,
        TagGovernanceService,
    )

    unit_id, segment_ids, reception_id = await _seed_dialogue_context(governance_factory)
    schema_version_id = await _seed_single_tag_schema(
        governance_factory,
        key="resume-drift-review",
    )
    now = datetime.now(UTC)
    async with governance_factory() as session, session.begin():
        baseline = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=schema_version_id,
            version="resume-baseline",
            engine="rule",
            prompt_content="",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="rules-v1",
            thresholds={"intent": 0.7},
            config_checksum="a" * 64,
            status="qualified",
            created_by=1,
            qualified_at=now,
        )
        candidate = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=schema_version_id,
            version="resume-candidate",
            engine="rule",
            prompt_content="",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="rules-v2",
            thresholds={"intent": 0.7},
            config_checksum="b" * 64,
            status="qualified",
            created_by=1,
            qualified_at=now,
        )
        session.add_all([baseline, candidate])
        await session.flush()
        evaluation = TagEvaluationRun(
            tenant_id="chang_an",
            tagger_version_id=candidate.id,
            baseline_tagger_version_id=baseline.id,
            gold_set_version_id=1,
            status="completed",
            metrics={"holdout_only": True, "sealed_release": True},
            baseline_metrics={},
            passed=True,
            started_at=now,
            finished_at=now,
            created_by=1,
        )
        session.add(evaluation)
        await session.flush()
        deployment = TagDeployment(
            tenant_id="chang_an",
            tagger_version_id=candidate.id,
            evaluation_run_id=evaluation.id,
            baseline_tagger_version_id=baseline.id,
            status="canary_5",
            traffic_percent=5,
            revision=2,
            promotion_paused=True,
            pause_reason="distribution drift requires review",
            created_by=1,
        )
        session.add(deployment)
        await session.flush()
        observation = TagDeploymentObservation(
            tenant_id="chang_an",
            deployment_id=deployment.id,
            deployment_revision=1,
            stage="canary_5",
            window_start=now - timedelta(hours=1),
            window_end=now,
            sample_count=1,
            source="monitor",
            is_trusted=True,
            metrics={"drift_affected_tags": ["intent"]},
            breach_codes=["drift"],
            action="pause",
        )
        session.add(observation)
        await session.flush()
        review_job = TagExtractionJob(
            tenant_id="chang_an",
            job_type="review_batch",
            origin="monitor",
            status="completed",
            scope={
                "deployment_id": deployment.id,
                "trusted_observation_id": observation.id,
                "review_bundle_id": "resume-drift-bundle",
                "selection_policy": "drift_audit",
            },
            tagger_version_id=candidate.id,
            idempotency_key="resume-drift-review",
            total_items=1,
            completed_items=1,
            failed_items=0,
            failed_subset=[],
            attempt_count=1,
            max_attempts=3,
            revision=2,
            created_by=1,
            finished_at=now,
        )
        session.add(review_job)
        task = TagReviewTask(
            tenant_id="chang_an",
            batch_id="resume-drift-review",
            review_bundle_id="resume-drift-bundle",
            selection_policy="drift_audit",
            selection_policy_version="1",
            blind_mode=True,
            subject_type="dialogue_unit",
            subject_id=unit_id,
            reception_id=reception_id,
            tag_key="intent",
            proposed_value="purchase",
            confidence=0.9,
            evidence_refs=[],
            schema_version_id=schema_version_id,
            tagger_version_id=candidate.id,
            source_deployment_id=deployment.id,
            reason="drift",
            status="pending",
            priority=100,
            created_by=1,
        )
        session.add(task)
        await session.flush()
        deployment_id = deployment.id
        task_id = task.id

    service = TagGovernanceService(governance_factory)
    with pytest.raises(GovernanceConflictError, match="not complete"):
        await service.transition_deployment(
            tenant_id="chang_an",
            deployment_id=deployment_id,
            action="resume",
            actor_user_id=9,
            expected_revision=2,
            reason="人工复核确认分布变化不影响标签正确性",
        )

    await service.claim_review(
        tenant_id="chang_an",
        task_id=task_id,
        reviewer_user_id=7,
    )
    resolved_task, decision, fact = await service.decide_review(
        tenant_id="chang_an",
        task_id=task_id,
        reviewer_user_id=7,
        action="correct",
        corrected_value=review_value,
        reason_code="distribution_shift_valid",
        note=None,
        evidence_refs=[{"segment_id": segment_ids[0], "start_sec": 0.0, "end_sec": 1.0}],
        truth_state="present",
    )
    assert resolved_task.status == "resolved"
    assert decision.truth_tier == "t2"
    assert fact is not None
    assert fact.tag_value == review_value

    if not resume_allowed:
        with pytest.raises(GovernanceConflictError, match="candidate label errors"):
            await service.transition_deployment(
                tenant_id="chang_an",
                deployment_id=deployment_id,
                action="resume",
                actor_user_id=9,
                expected_revision=2,
                reason="人工复核确认候选输出存在错误，禁止恢复流量",
            )
        async with governance_factory() as session:
            blocked = await session.get(TagDeployment, deployment_id)
            assert blocked is not None
            assert blocked.promotion_paused is True
            assert blocked.revision == 2
        return

    resumed = await service.transition_deployment(
        tenant_id="chang_an",
        deployment_id=deployment_id,
        action="resume",
        actor_user_id=9,
        expected_revision=2,
        reason="人工复核确认分布变化不影响标签正确性",
    )

    assert resumed.promotion_paused is False
    assert resumed.pause_reason is None
    assert resumed.status == "canary_5"
    assert resumed.revision == 3


@pytest.mark.asyncio
async def test_optimizer_rejects_holdout_and_does_not_mutate_production(
    governance_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.models.tag_governance import (
        TagAssignmentFact,
        TaggerVersion,
        TagGoldLabel,
        TagGoldSet,
        TagGoldSetVersion,
        TagReviewDecision,
        TagReviewTask,
        TagSchema,
        TagSchemaVersion,
    )
    from audio_graphy.services.tag_governance import (
        GovernanceError,
        TagGovernanceService,
    )

    async with governance_factory() as session:
        schema = TagSchema(
            tenant_id="chang_an",
            key="opt-schema",
            name="优化体系",
            status="published",
            created_by=1,
        )
        session.add(schema)
        await session.flush()
        schema_version = TagSchemaVersion(
            tenant_id="chang_an",
            schema_id=schema.id,
            version="1",
            definitions=[
                {
                    "key": "intent",
                    "value_type": "enum",
                    "allowed_values": ["browse", "purchase"],
                    "evidence_required": True,
                    "subject_types": ["dialogue_unit"],
                    "scenarios": ["automotive"],
                    "threshold": 0.7,
                }
            ],
            checksum="e" * 64,
            status="published",
            created_by=1,
        )
        session.add(schema_version)
        await session.flush()
        production = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=schema_version.id,
            version="prod-1",
            engine="hybrid",
            prompt_content="production prompt",
            rule_bundle={"dsl_version": "1"},
            model_version="model-1",
            thresholds={"intent": 0.7},
            config_checksum="f" * 64,
            status="qualified",
            created_by=1,
        )
        session.add(production)
        gold_set = TagGoldSet(
            tenant_id="chang_an",
            key="gold",
            name="黄金集",
            schema_version_id=schema_version.id,
            created_by=1,
        )
        session.add(gold_set)
        await session.flush()
        snapshot = TagGoldSetVersion(
            tenant_id="chang_an",
            gold_set_id=gold_set.id,
            version="1",
            status="frozen",
            checksum="1" * 64,
            item_count=2,
            frozen_by=1,
            frozen_at=datetime.now(UTC),
        )
        session.add(snapshot)
        await session.flush()
        holdout = TagGoldLabel(
            tenant_id="chang_an",
            gold_set_version_id=snapshot.id,
            review_decision_id=999,
            reception_id=123,
            subject_type="dialogue_unit",
            subject_id=777,
            tag_key="intent",
            tag_value="purchase",
            evidence_refs=[{"segment_id": 1}],
            split="holdout",
        )
        session.add(holdout)
        await session.commit()
        production_id = production.id
        schema_version_id = schema_version.id
        snapshot_id = snapshot.id

    service = TagGovernanceService(governance_factory)
    with pytest.raises(GovernanceError, match="no persisted train/validation"):
        await service.create_optimization_candidate(
            tenant_id="chang_an",
            gold_set_version_id=snapshot_id,
            production_tagger_version_id=production_id,
            actor_user_id=1,
        )

    now = datetime.now(UTC)
    async with governance_factory() as session, session.begin():
        proposed = TagAssignmentFact(
            tenant_id="chang_an",
            subject_type="dialogue_unit",
            subject_id=778,
            reception_id=124,
            dialogue_unit_id=778,
            tag_key="intent",
            tag_value="browse",
            confidence=0.82,
            evidence_refs=[
                {
                    "segment_id": 2,
                    "text_excerpt": "客户今天准备购买",
                }
            ],
            source="llm",
            schema_version_id=schema_version_id,
            tagger_version_id=production_id,
            input_hash="2" * 64,
            revision=1,
            tombstone=False,
            assigned_at=now,
        )
        session.add(proposed)
        await session.flush()
        task = TagReviewTask(
            tenant_id="chang_an",
            batch_id="optimizer-feedback",
            subject_type="dialogue_unit",
            subject_id=778,
            reception_id=124,
            tag_key="intent",
            proposed_value="browse",
            confidence=0.82,
            evidence_refs=proposed.evidence_refs,
            proposed_fact_id=proposed.id,
            schema_version_id=schema_version_id,
            tagger_version_id=production_id,
            reason="conflict",
            status="resolved",
            priority=1,
            resolved_at=now,
            created_by=1,
        )
        session.add(task)
        await session.flush()
        decision = TagReviewDecision(
            tenant_id="chang_an",
            task_id=task.id,
            action="correct",
            corrected_value="purchase",
            reason_code="evidence_confirmed",
            evidence_refs=proposed.evidence_refs,
            resulting_fact_id=proposed.id,
            reviewer_user_id=2,
            adjudication=False,
            decided_at=now,
        )
        session.add(decision)
        await session.flush()
        session.add(
            TagGoldLabel(
                tenant_id="chang_an",
                gold_set_version_id=snapshot_id,
                review_decision_id=decision.id,
                reception_id=124,
                subject_type="dialogue_unit",
                subject_id=778,
                tag_key="intent",
                tag_value="purchase",
                evidence_refs=decision.evidence_refs,
                truth_state="present",
                truth_tier="t2",
                split="train",
            )
        )
        missing_manual = TagAssignmentFact(
            tenant_id="chang_an",
            subject_type="dialogue_unit",
            subject_id=779,
            reception_id=125,
            dialogue_unit_id=779,
            tag_key="intent",
            tag_value="purchase",
            confidence=1,
            evidence_refs=[
                {
                    "segment_id": 3,
                    "text_excerpt": "客户明确说今天下单",
                }
            ],
            source="manual",
            schema_version_id=schema_version_id,
            tagger_version_id=None,
            input_hash="3" * 64,
            revision=1,
            tombstone=False,
            assigned_at=now,
        )
        session.add(missing_manual)
        await session.flush()
        missing_task = TagReviewTask(
            tenant_id="chang_an",
            batch_id="optimizer-missing-feedback",
            subject_type="dialogue_unit",
            subject_id=779,
            reception_id=125,
            tag_key="intent",
            proposed_value=None,
            confidence=None,
            evidence_refs=[],
            proposed_fact_id=None,
            schema_version_id=schema_version_id,
            tagger_version_id=production_id,
            reason="missing",
            status="resolved",
            priority=1,
            resolved_at=now,
            created_by=1,
        )
        session.add(missing_task)
        await session.flush()
        missing_decision = TagReviewDecision(
            tenant_id="chang_an",
            task_id=missing_task.id,
            action="correct",
            corrected_value="purchase",
            reason_code="false_negative",
            evidence_refs=missing_manual.evidence_refs,
            resulting_fact_id=missing_manual.id,
            reviewer_user_id=2,
            adjudication=False,
            decided_at=now,
        )
        session.add(missing_decision)
        await session.flush()
        session.add(
            TagGoldLabel(
                tenant_id="chang_an",
                gold_set_version_id=snapshot_id,
                review_decision_id=missing_decision.id,
                reception_id=125,
                subject_type="dialogue_unit",
                subject_id=779,
                tag_key="intent",
                tag_value="purchase",
                evidence_refs=missing_decision.evidence_refs,
                truth_state="present",
                truth_tier="t2",
                split="train",
            )
        )
        false_positive = TagAssignmentFact(
            tenant_id="chang_an",
            subject_type="dialogue_unit",
            subject_id=780,
            reception_id=126,
            dialogue_unit_id=780,
            tag_key="intent",
            tag_value="browse",
            confidence=0.93,
            evidence_refs=[
                {
                    "segment_id": 4,
                    "text_excerpt": "客户只是路过看看",
                }
            ],
            source="llm",
            schema_version_id=schema_version_id,
            tagger_version_id=production_id,
            input_hash="4" * 64,
            revision=1,
            tombstone=False,
            assigned_at=now,
        )
        session.add(false_positive)
        await session.flush()
        false_positive_tombstone = TagAssignmentFact(
            tenant_id="chang_an",
            subject_type="dialogue_unit",
            subject_id=780,
            reception_id=126,
            dialogue_unit_id=780,
            tag_key="intent",
            tag_value=None,
            confidence=1,
            evidence_refs=false_positive.evidence_refs,
            source="manual",
            schema_version_id=schema_version_id,
            tagger_version_id=None,
            input_hash="5" * 64,
            superseded_fact_id=false_positive.id,
            revision=2,
            tombstone=True,
            assigned_at=now,
        )
        session.add(false_positive_tombstone)
        await session.flush()
        false_positive_task = TagReviewTask(
            tenant_id="chang_an",
            batch_id="optimizer-false-positive",
            subject_type="dialogue_unit",
            subject_id=780,
            reception_id=126,
            tag_key="intent",
            proposed_value="browse",
            confidence=0.93,
            evidence_refs=false_positive.evidence_refs,
            proposed_fact_id=false_positive.id,
            schema_version_id=schema_version_id,
            tagger_version_id=production_id,
            reason="low_confidence",
            status="resolved",
            priority=1,
            resolved_at=now,
            created_by=1,
        )
        session.add(false_positive_task)
        await session.flush()
        false_positive_decision = TagReviewDecision(
            tenant_id="chang_an",
            task_id=false_positive_task.id,
            action="reject",
            corrected_value=None,
            reason_code="false_positive",
            evidence_refs=false_positive_tombstone.evidence_refs,
            resulting_fact_id=false_positive_tombstone.id,
            reviewer_user_id=2,
            adjudication=False,
            decided_at=now,
        )
        session.add(false_positive_decision)
        await session.flush()
        session.add(
            TagGoldLabel(
                tenant_id="chang_an",
                gold_set_version_id=snapshot_id,
                review_decision_id=false_positive_decision.id,
                reception_id=126,
                subject_type="dialogue_unit",
                subject_id=780,
                tag_key="intent",
                tag_value=None,
                evidence_refs=false_positive_decision.evidence_refs,
                truth_state="absent",
                truth_tier="t2",
                split="train",
            )
        )

    # These legacy feedback rows intentionally have no persisted Harness usage
    # ledger. Missing cold-cache/cost measurements must stop optimization
    # before the sealed holdout is consumed or a candidate is materialized.
    with pytest.raises(
        GovernanceError,
        match=r"measurement is incomplete.*reservation retained",
    ):
        await service.create_optimization_candidate(
            tenant_id="chang_an",
            gold_set_version_id=snapshot_id,
            production_tagger_version_id=production_id,
            actor_user_id=1,
        )
    async with governance_factory() as session:
        unchanged = await session.get(TaggerVersion, production_id)
        assert unchanged is not None
        assert unchanged.prompt_content == "production prompt"
        assert unchanged.thresholds == {"intent": 0.7}
        candidates = (
            await session.execute(
                select(TaggerVersion.id).where(
                    TaggerVersion.tenant_id == "chang_an",
                    TaggerVersion.id != production_id,
                )
            )
        ).scalars().all()
        assert candidates == []


@pytest.mark.asyncio
async def test_reject_freezes_as_negative_gold_and_duplicate_label_is_blocked(
    governance_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.models.tag_governance import (
        TagAssignmentCurrent,
        TagAssignmentFact,
        TagFeedbackEvent,
        TaggerVersion,
        TagGoldLabel,
        TagReviewDecision,
        TagReviewTask,
        TagSchema,
        TagSchemaVersion,
    )
    from audio_graphy.services.tag_governance import (
        GovernanceConflictError,
        TagGovernanceService,
    )

    unit_id, segment_ids, reception_id = await _seed_dialogue_context(governance_factory)
    now = datetime.now(UTC)
    evidence = [
        {
            "segment_id": segment_ids[0],
            "start_sec": 0,
            "end_sec": 1,
            "text_excerpt": "客户只是看看",
        }
    ]
    async with governance_factory() as session, session.begin():
        schema = TagSchema(
            tenant_id="chang_an",
            key="negative-gold",
            name="负样本黄金集",
            status="published",
            created_by=1,
        )
        session.add(schema)
        await session.flush()
        schema_version = TagSchemaVersion(
            tenant_id="chang_an",
            schema_id=schema.id,
            version="1",
            definitions=[
                {
                    "key": "intent",
                    "value_type": "enum",
                    "allowed_values": ["browse", "purchase"],
                    "evidence_required": True,
                    "subject_types": ["dialogue_unit"],
                    "scenarios": ["automotive"],
                }
            ],
            checksum="7" * 64,
            status="published",
            created_by=1,
        )
        session.add(schema_version)
        await session.flush()
        tagger = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=schema_version.id,
            version="prod",
            engine="llm",
            prompt_content="JSON",
            rule_bundle={"dsl_version": "1"},
            model_version="model",
            thresholds={"intent": 0.7},
            config_checksum="8" * 64,
            status="qualified",
            created_by=1,
        )
        session.add(tagger)
        await session.flush()
        proposed = TagAssignmentFact(
            tenant_id="chang_an",
            subject_type="dialogue_unit",
            subject_id=unit_id,
            reception_id=reception_id,
            dialogue_unit_id=unit_id,
            tag_key="intent",
            tag_value="purchase",
            confidence=0.88,
            evidence_refs=evidence,
            source="llm",
            schema_version_id=schema_version.id,
            tagger_version_id=tagger.id,
            input_hash="9" * 64,
            revision=1,
            tombstone=False,
            assigned_at=now,
        )
        session.add(proposed)
        await session.flush()
        session.add(
            TagAssignmentCurrent(
                tenant_id="chang_an",
                subject_type="dialogue_unit",
                subject_id=unit_id,
                tag_key="intent",
                fact_id=proposed.id,
                revision=1,
            )
        )
        schema_version_id = schema_version.id
        tagger_id = tagger.id
        proposed_id = proposed.id

    service = TagGovernanceService(governance_factory)
    review = (
        await service.create_review_batch(
            tenant_id="chang_an",
            reason="low_confidence",
            subjects=[
                {
                    "subject_type": "dialogue_unit",
                    "subject_id": unit_id,
                    "tag_key": "intent",
                    "proposed_fact_id": proposed_id,
                }
            ],
            actor_user_id=1,
        )
    )[0]
    await service.claim_review(
        tenant_id="chang_an",
        task_id=review.id,
        reviewer_user_id=2,
    )
    _task, decision, tombstone = await service.decide_review(
        tenant_id="chang_an",
        task_id=review.id,
        reviewer_user_id=2,
        action="reject",
        corrected_value=None,
        reason_code="false_positive",
        note=None,
        evidence_refs=evidence,
    )
    assert tombstone.tombstone is True
    assert tombstone.superseded_fact_id == proposed_id
    async with governance_factory() as session:
        current = (await session.execute(select(TagAssignmentCurrent))).scalar_one()
        feedback = (await session.execute(select(TagFeedbackEvent))).scalar_one()
    assert current.fact_id == tombstone.id
    assert feedback.review_decision_id == decision.id
    assert feedback.truth_state == "absent"
    assert feedback.training_eligible is False

    gold_set = await service.create_gold_set(
        tenant_id="chang_an",
        key="negative-gold",
        name="负样本黄金集",
        description=None,
        schema_version_id=schema_version_id,
        actor_user_id=1,
    )
    frozen = await service.freeze_gold_set(
        tenant_id="chang_an",
        gold_set_id=gold_set.id,
        version="1",
        decision_ids=[decision.id],
        actor_user_id=1,
    )
    async with governance_factory() as session, session.begin():
        label = (
            await session.execute(
                select(TagGoldLabel).where(TagGoldLabel.gold_set_version_id == frozen.id)
            )
        ).scalar_one()
        assert label.tag_value is None
        assert label.truth_state == "absent"
        assert label.evidence_refs == evidence
        duplicate_task = TagReviewTask(
            tenant_id="chang_an",
            batch_id="duplicate-negative",
            subject_type="dialogue_unit",
            subject_id=unit_id,
            reception_id=reception_id,
            tag_key="intent",
            proposed_value="purchase",
            confidence=0.88,
            evidence_refs=evidence,
            proposed_fact_id=proposed_id,
            schema_version_id=schema_version_id,
            tagger_version_id=tagger_id,
            reason="low_confidence",
            status="resolved",
            priority=1,
            resolved_at=now,
            created_by=1,
        )
        session.add(duplicate_task)
        await session.flush()
        duplicate_decision = TagReviewDecision(
            tenant_id="chang_an",
            task_id=duplicate_task.id,
            action="reject",
            corrected_value=None,
            reason_code="duplicate",
            evidence_refs=evidence,
            resulting_fact_id=tombstone.id,
            reviewer_user_id=3,
            adjudication=True,
            decided_at=now,
        )
        session.add(duplicate_decision)
        await session.flush()
        duplicate_decision_id = duplicate_decision.id

    with pytest.raises(GovernanceConflictError, match="multiple decisions"):
        await service.freeze_gold_set(
            tenant_id="chang_an",
            gold_set_id=gold_set.id,
            version="2",
            decision_ids=[decision.id, duplicate_decision_id],
            actor_user_id=1,
        )


@pytest.mark.asyncio
async def test_same_reviewer_cannot_preclaim_both_double_blind_tasks(
    governance_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_governance import (
        GovernanceConflictError,
        TagGovernanceService,
    )

    unit_id, _segment_ids, _reception_id = await _seed_dialogue_context(governance_factory)
    schema_version_id = await _seed_single_tag_schema(
        governance_factory,
        key="double-blind-claim",
    )
    service = TagGovernanceService(governance_factory)
    tasks = await service.create_review_batch(
        tenant_id="chang_an",
        reason="critical",
        subjects=[
            {
                "subject_type": "dialogue_unit",
                "subject_id": unit_id,
                "tag_key": "intent",
                "schema_version_id": schema_version_id,
            }
        ],
        actor_user_id=1,
        review_bundle_id="critical-preclaim",
        blind_mode=True,
    )

    assert len(tasks) == 2
    await service.claim_review(
        tenant_id="chang_an",
        task_id=tasks[0].id,
        reviewer_user_id=7,
    )
    with pytest.raises(GovernanceConflictError, match=r"active task|same reviewer"):
        await service.claim_review(
            tenant_id="chang_an",
            task_id=tasks[1].id,
            reviewer_user_id=7,
        )
    released = await service.release_review(
        tenant_id="chang_an",
        task_id=tasks[0].id,
        actor_user_id=7,
    )
    assert released.status == "pending"
    assert released.claimed_by is None
    assert released.claimed_at is None

    await service.claim_review(
        tenant_id="chang_an",
        task_id=tasks[1].id,
        reviewer_user_id=7,
    )
    with pytest.raises(GovernanceConflictError, match="current claimant"):
        await service.release_review(
            tenant_id="chang_an",
            task_id=tasks[1].id,
            actor_user_id=8,
        )
    force_released = await service.release_review(
        tenant_id="chang_an",
        task_id=tasks[1].id,
        actor_user_id=9,
        force=True,
    )
    assert force_released.status == "pending"
    assert force_released.claimed_by is None


@pytest.mark.asyncio
async def test_active_review_queue_does_not_permanently_poison_blind_eligibility(
    governance_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.models.tag_governance import TagGovernanceAuditEvent
    from audio_graphy.services.tag_governance import (
        GovernanceConflictError,
        TagGovernanceService,
    )

    service = TagGovernanceService(governance_factory)
    schema_version_id = await _seed_single_tag_schema(
        governance_factory,
        key="active-blind-queue",
    )

    async def create_task(*, bundle: str, blind: bool = True) -> int:
        unit_id, _segment_ids, _reception_id = await _seed_dialogue_context(governance_factory)
        return (
            await service.create_review_batch(
                tenant_id="chang_an",
                reason="random" if blind else "low_confidence",
                subjects=[
                    {
                        "subject_type": "dialogue_unit",
                        "subject_id": unit_id,
                        "tag_key": "intent",
                        "schema_version_id": schema_version_id,
                        "proposed_value": "browse",
                        "confidence": 0.77,
                    }
                ],
                actor_user_id=1,
                review_bundle_id=bundle,
                blind_mode=blind,
            )
        )[0].id

    first_id = await create_task(bundle="active-blind-first")
    unrelated_nonblind_id = await create_task(
        bundle="active-unrelated-nonblind",
        blind=False,
    )
    second_id = await create_task(bundle="active-blind-second")

    active = await service.list_reviews_for_viewer(
        tenant_id="chang_an",
        reviewer_user_id=7,
        status="active",
    )
    assert {row.id for row in active} == {
        first_id,
        unrelated_nonblind_id,
        second_id,
    }
    async with governance_factory() as session:
        semantic_reads = (
            await session.execute(
                select(TagGovernanceAuditEvent).where(
                    TagGovernanceAuditEvent.tenant_id == "chang_an",
                    TagGovernanceAuditEvent.actor_user_id == 7,
                    TagGovernanceAuditEvent.action == "blind_sensitive_read",
                )
            )
        ).scalars()
        assert list(semantic_reads) == []

    await service.claim_review(
        tenant_id="chang_an",
        task_id=first_id,
        reviewer_user_id=7,
    )
    await service.decide_review(
        tenant_id="chang_an",
        task_id=first_id,
        reviewer_user_id=7,
        action="reject",
        corrected_value=None,
        truth_state="absent",
        reason_code="verified_absent",
        note=None,
        evidence_refs=[],
    )
    refreshed = await service.list_reviews_for_viewer(
        tenant_id="chang_an",
        reviewer_user_id=7,
        status="active",
    )
    assert first_id not in {row.id for row in refreshed}

    await service.claim_review(
        tenant_id="chang_an",
        task_id=second_id,
        reviewer_user_id=7,
    )
    await service.decide_review(
        tenant_id="chang_an",
        task_id=second_id,
        reviewer_user_id=7,
        action="reject",
        corrected_value=None,
        truth_state="absent",
        reason_code="verified_absent",
        note=None,
        evidence_refs=[],
    )

    existing_blind_id = await create_task(bundle="history-existing-blind")
    history = await service.list_reviews_for_viewer(
        tenant_id="chang_an",
        reviewer_user_id=7,
        status="resolved",
    )
    assert {row.id for row in history} >= {first_id, second_id}
    with pytest.raises(GovernanceConflictError, match="previously accessed"):
        await service.claim_review(
            tenant_id="chang_an",
            task_id=existing_blind_id,
            reviewer_user_id=7,
        )

    future_blind_id = await create_task(bundle="history-future-blind")
    await service.claim_review(
        tenant_id="chang_an",
        task_id=future_blind_id,
        reviewer_user_id=7,
    )


@pytest.mark.asyncio
async def test_reception_semantic_history_permanently_blocks_future_blind_scope(
    governance_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_governance import (
        GovernanceConflictError,
        TagGovernanceService,
    )

    service = TagGovernanceService(governance_factory)
    schema_version_id = await _seed_single_tag_schema(
        governance_factory,
        key="scoped-semantic-history",
    )
    unit_id, _segment_ids, reception_id = await _seed_dialogue_context(governance_factory)
    assert await service.record_blind_sensitive_access(
        tenant_id="chang_an",
        actor_user_id=7,
        access_kind="reception_tag_history",
        reception_id=reception_id,
    )
    task = (
        await service.create_review_batch(
            tenant_id="chang_an",
            reason="random",
            subjects=[
                {
                    "subject_type": "dialogue_unit",
                    "subject_id": unit_id,
                    "tag_key": "intent",
                    "schema_version_id": schema_version_id,
                }
            ],
            actor_user_id=1,
            review_bundle_id="future-scoped-blind",
            blind_mode=True,
        )
    )[0]

    with pytest.raises(GovernanceConflictError, match="previously accessed"):
        await service.claim_review(
            tenant_id="chang_an",
            task_id=task.id,
            reviewer_user_id=7,
        )

    unrelated_unit_id, _other_segments, _other_reception_id = await _seed_dialogue_context(
        governance_factory
    )
    unrelated = (
        await service.create_review_batch(
            tenant_id="chang_an",
            reason="random",
            subjects=[
                {
                    "subject_type": "dialogue_unit",
                    "subject_id": unrelated_unit_id,
                    "tag_key": "intent",
                    "schema_version_id": schema_version_id,
                }
            ],
            actor_user_id=1,
            review_bundle_id="future-unrelated-blind",
            blind_mode=True,
        )
    )[0]
    await service.claim_review(
        tenant_id="chang_an",
        task_id=unrelated.id,
        reviewer_user_id=7,
    )


@pytest.mark.asyncio
async def test_t3_adjudication_rejects_non_blind_non_t2_predecessors(
    governance_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.models.tag_governance import TagReviewDecision, TagReviewTask
    from audio_graphy.services.tag_governance import (
        GovernanceConflictError,
        TagGovernanceService,
    )

    unit_id, segment_ids, reception_id = await _seed_dialogue_context(governance_factory)
    schema_version_id = await _seed_single_tag_schema(
        governance_factory,
        key="forged-adjudication-predecessors",
    )
    now = datetime.now(UTC)
    evidence = [{"segment_id": segment_ids[1], "start_sec": 1, "end_sec": 2}]
    async with governance_factory() as session, session.begin():
        predecessors = [
            TagReviewTask(
                tenant_id="chang_an",
                batch_id="forged-predecessors",
                review_bundle_id="forged-bundle",
                selection_policy="critical_positive",
                selection_policy_version="1",
                blind_mode=False,
                subject_type="dialogue_unit",
                subject_id=unit_id,
                reception_id=reception_id,
                tag_key="intent",
                proposed_value=None,
                confidence=None,
                evidence_refs=[],
                proposed_fact_id=None,
                schema_version_id=schema_version_id,
                tagger_version_id=None,
                reason="critical",
                status="resolved",
                priority=100,
                resolved_at=now,
                created_by=1,
            )
            for _ in range(2)
        ]
        session.add_all(predecessors)
        await session.flush()
        session.add_all(
            [
                TagReviewDecision(
                    tenant_id="chang_an",
                    task_id=task.id,
                    action="correct",
                    corrected_value="purchase",
                    reason_code="forged_predecessor",
                    evidence_refs=evidence,
                    resulting_fact_id=None,
                    reviewer_user_id=reviewer_id,
                    adjudication=False,
                    truth_state="present",
                    truth_tier="t1",
                    annotator_round=round_no,
                    decided_at=now,
                )
                for task, reviewer_id, round_no in zip(
                    predecessors,
                    (7, 8),
                    (1, 2),
                    strict=True,
                )
            ]
        )
        adjudication_task = TagReviewTask(
            tenant_id="chang_an",
            batch_id="forged-adjudication",
            review_bundle_id="forged-bundle",
            selection_policy="critical_positive",
            selection_policy_version="1",
            blind_mode=True,
            subject_type="dialogue_unit",
            subject_id=unit_id,
            reception_id=reception_id,
            tag_key="intent",
            proposed_value=None,
            confidence=None,
            evidence_refs=[],
            proposed_fact_id=None,
            schema_version_id=schema_version_id,
            tagger_version_id=None,
            reason="adjudication",
            status="pending",
            priority=100,
            created_by=1,
        )
        session.add(adjudication_task)
        await session.flush()
        adjudication_task_id = adjudication_task.id

    service = TagGovernanceService(governance_factory)
    await service.claim_review(
        tenant_id="chang_an",
        task_id=adjudication_task_id,
        reviewer_user_id=9,
    )
    with pytest.raises(GovernanceConflictError, match="blind T2"):
        await service.decide_review(
            tenant_id="chang_an",
            task_id=adjudication_task_id,
            reviewer_user_id=9,
            action="correct",
            corrected_value="purchase",
            reason_code="forged_t3",
            note=None,
            evidence_refs=evidence,
            adjudication=True,
            truth_state="present",
            truth_tier="t3",
            annotator_round=3,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "reception_offset",
        "expected_split",
        "review_action",
        "review_value",
        "truth_state",
        "failure_stage",
    ),
    [
        (0, "validation", "correct", "purchase", "present", "tag_reasoning"),
        (0, "validation", "reject", None, "absent", "tag_reasoning"),
        (0, "validation", "reject", None, "not_applicable", "tag_reasoning"),
        (5, "holdout", "correct", "purchase", "present", "tag_reasoning"),
        (5, "holdout", "reject", None, "absent", "tag_reasoning"),
        (5, "holdout", "reject", None, "not_applicable", "tag_reasoning"),
        (0, "validation", "correct", "purchase", "present", "asr"),
        (5, "holdout", "correct", "purchase", "present", "asr"),
    ],
)
async def test_complete_gold_freeze_collapses_blind_t2_rounds_into_t3_with_source_snapshot(
    governance_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    reception_offset: int,
    expected_split: str,
    review_action: str,
    review_value: str | None,
    truth_state: str,
    failure_stage: str,
) -> None:
    from audio_graphy.models.reception import DialogueTagAssignment, Reception
    from audio_graphy.models.tag_governance import (
        TagAssignmentCurrent,
        TagAssignmentFact,
        TagBadcase,
        TagExperienceCase,
        TagExtractionJob,
        TagExtractionRun,
        TagFeedbackEvent,
        TagFeedbackLaneAssignment,
        TaggerVersion,
        TagGoldLabel,
        TagHarnessExecution,
        TagReviewTask,
    )
    from audio_graphy.services.receptions import ReceptionService
    from audio_graphy.services.tag_governance import (
        GovernanceConflictError,
        GovernanceError,
        TagGovernanceService,
    )

    if reception_offset:
        seeded_at = datetime.now(UTC)
        async with governance_factory() as session, session.begin():
            session.add_all(
                [
                    Reception(
                        tenant_id="chang_an",
                        scenario="automotive",
                        store_id=f"OFFSET-{index}",
                        status="ready",
                        merge_mode="logical",
                        started_at=seeded_at,
                        ended_at=seeded_at + timedelta(seconds=1),
                        version=1,
                    )
                    for index in range(reception_offset)
                ]
            )
    unit_id, segment_ids, _reception_id = await _seed_dialogue_context(governance_factory)
    schema_version_id = await _seed_single_tag_schema(
        governance_factory,
        key="certified-t3-gold",
        extra_definitions=[
            {
                "key": "retail_only_intent",
                "value_type": "enum",
                "allowed_values": ["browse"],
                "evidence_required": True,
                "subject_types": ["dialogue_unit"],
                "scenarios": ["retail"],
            }
        ],
    )
    now = datetime.now(UTC)
    input_hash = "d" * 64
    input_snapshot = {
        "dialogue_unit_id": unit_id,
        "transcript": "客户决定购买",
        "segment_ids": segment_ids,
    }
    async with governance_factory() as session, session.begin():
        tagger = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=schema_version_id,
            version="certified-t3-source",
            engine="hybrid",
            prompt_content="return JSON",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="model",
            thresholds={"intent": 0.7},
            harness_spec={"context": {"neighbor_units": 1}},
            config_checksum="e" * 64,
            status="qualified",
            created_by=1,
            qualified_at=now,
        )
        session.add(tagger)
        await session.flush()
        old_job = TagExtractionJob(
            tenant_id="chang_an",
            job_type="extract",
            status="completed",
            scope={"dialogue_unit_ids": [unit_id]},
            tagger_version_id=tagger.id,
            idempotency_key="certified-t3-old-source",
            total_items=1,
            completed_items=1,
            failed_items=0,
            failed_subset=[],
            attempt_count=1,
            max_attempts=3,
            revision=2,
            created_by=1,
            finished_at=now - timedelta(minutes=1),
        )
        session.add(old_job)
        await session.flush()
        old_run = TagExtractionRun(
            tenant_id="chang_an",
            job_id=old_job.id,
            subject_type="dialogue_unit",
            subject_id=unit_id,
            tagger_version_id=tagger.id,
            input_hash="c" * 64,
            input_snapshot={"transcript": "旧输入"},
            output_snapshot={},
            status="completed",
            started_at=now - timedelta(minutes=1),
            finished_at=now - timedelta(minutes=1),
        )
        session.add(old_run)
        await session.flush()
        old_fact = TagAssignmentFact(
            tenant_id="chang_an",
            subject_type="dialogue_unit",
            subject_id=unit_id,
            reception_id=_reception_id,
            dialogue_unit_id=unit_id,
            tag_key="intent",
            tag_value="browse",
            confidence=0.9,
            evidence_refs=[],
            source="rule",
            schema_version_id=schema_version_id,
            tagger_version_id=tagger.id,
            extraction_run_id=old_run.id,
            deployment_id=None,
            input_hash=old_run.input_hash,
            superseded_fact_id=None,
            revision=1,
            tombstone=False,
            actor_user_id=1,
            assigned_at=now - timedelta(minutes=1),
        )
        session.add(old_fact)
        await session.flush()
        old_fact_id = old_fact.id
        session.add(
            TagAssignmentCurrent(
                tenant_id="chang_an",
                subject_type="dialogue_unit",
                subject_id=unit_id,
                tag_key="intent",
                fact_id=old_fact.id,
                revision=1,
            )
        )
        job = TagExtractionJob(
            tenant_id="chang_an",
            job_type="extract",
            status="completed",
            scope={"dialogue_unit_ids": [unit_id]},
            tagger_version_id=tagger.id,
            idempotency_key="certified-t3-source",
            total_items=1,
            completed_items=1,
            failed_items=0,
            failed_subset=[],
            attempt_count=1,
            max_attempts=3,
            revision=2,
            created_by=1,
            finished_at=now,
        )
        session.add(job)
        await session.flush()
        extraction_run = TagExtractionRun(
            tenant_id="chang_an",
            job_id=job.id,
            subject_type="dialogue_unit",
            subject_id=unit_id,
            tagger_version_id=tagger.id,
            input_hash=input_hash,
            input_snapshot=input_snapshot,
            output_snapshot={},
            status="completed",
            started_at=now,
            finished_at=now,
        )
        session.add(extraction_run)
        await session.flush()
        harness_execution = TagHarnessExecution(
            tenant_id="chang_an",
            extraction_run_id=extraction_run.id,
            tagger_version_id=tagger.id,
            subject_type="dialogue_unit",
            subject_id=unit_id,
            input_hash=input_hash,
            scene_profile={"scenario": "automotive"},
            resolved_harness_spec={"context": {"neighbor_units": 1}},
            route="rule_llm_fusion",
            status="completed",
            output_snapshot={},
            latency_ms=10,
            token_count=5,
            cost_units=0.01,
            started_at=now,
            finished_at=now,
        )
        session.add(harness_execution)
        await session.flush()
        tagger_id = tagger.id
        extraction_run_id = extraction_run.id
        harness_execution_id = harness_execution.id

    service = TagGovernanceService(governance_factory)
    bundle_id = "critical-certified-t3"
    tasks = await service.create_review_batch(
        tenant_id="chang_an",
        reason="gold",
        subjects=[
            {
                "subject_type": "dialogue_unit",
                "subject_id": unit_id,
                "tag_key": "intent",
                "schema_version_id": schema_version_id,
            }
        ],
        actor_user_id=1,
        review_bundle_id=bundle_id,
    )
    assert len(tasks) == 2
    assert {task.source_extraction_run_id for task in tasks} == {extraction_run_id}
    assert {task.source_harness_execution_id for task in tasks} == {harness_execution_id}
    assert {task.proposed_fact_id for task in tasks} == {None}
    assert {task.proposed_value for task in tasks} == {None}

    evidence = [{"segment_id": segment_ids[1], "start_sec": 1, "end_sec": 2}]
    t2_decision_ids: list[int] = []
    for task, reviewer_id in zip(tasks, (7, 8), strict=True):
        await service.claim_review(
            tenant_id="chang_an",
            task_id=task.id,
            reviewer_user_id=reviewer_id,
        )
        _resolved, decision, fact = await service.decide_review(
            tenant_id="chang_an",
            task_id=task.id,
            reviewer_user_id=reviewer_id,
            action=review_action,
            corrected_value=review_value,
            reason_code="independent_label",
            note=None,
            evidence_refs=evidence,
            truth_state=truth_state,
            truth_tier="t2",
            annotator_round=1,
            primary_failure_stage=failure_stage,
        )
        assert fact is None
        t2_decision_ids.append(decision.id)

    async with governance_factory() as session:
        adjudication_task = (
            await session.execute(
                select(TagReviewTask).where(
                    TagReviewTask.review_bundle_id == bundle_id,
                    TagReviewTask.reason == "adjudication",
                )
            )
        ).scalar_one()
        adjudication_task_id = adjudication_task.id
    await service.claim_review(
        tenant_id="chang_an",
        task_id=adjudication_task_id,
        reviewer_user_id=9,
    )
    with pytest.raises(GovernanceError, match="definitive"):
        await service.decide_review(
            tenant_id="chang_an",
            task_id=adjudication_task_id,
            reviewer_user_id=9,
            action="uncertain",
            corrected_value=None,
            reason_code="still_ambiguous",
            note=None,
            evidence_refs=evidence,
            adjudication=True,
            truth_state="uncertain",
            truth_tier="t3",
            annotator_round=3,
            primary_failure_stage=failure_stage,
        )
    _resolved, t3_decision, t3_fact = await service.decide_review(
        tenant_id="chang_an",
        task_id=adjudication_task_id,
        reviewer_user_id=9,
        action=review_action,
        corrected_value=review_value,
        reason_code="third_reviewer_consensus",
        note=None,
        evidence_refs=evidence,
        adjudication=True,
        truth_state=truth_state,
        truth_tier="t3",
        annotator_round=3,
        primary_failure_stage=failure_stage,
    )
    assert t3_fact is None

    async with governance_factory() as session:
        current_before_freeze = (await session.execute(select(TagAssignmentCurrent))).scalar_one()
        fact_ids_before_freeze = list(
            (await session.execute(select(TagAssignmentFact.id).order_by(TagAssignmentFact.id)))
            .scalars()
            .all()
        )
        legacy_count_before_freeze = int(
            (await session.execute(select(func.count(DialogueTagAssignment.id)))).scalar_one()
        )
        t3_feedback = (
            await session.execute(
                select(TagFeedbackEvent).where(
                    TagFeedbackEvent.review_decision_id == t3_decision.id
                )
            )
        ).scalar_one()
        pending_badcase_count = int(
            (
                await session.execute(
                    select(func.count(TagBadcase.id)).where(
                        TagBadcase.source_feedback_event_id == t3_feedback.id
                    )
                )
            ).scalar_one()
        )
        pending_experience_count = int(
            (
                await session.execute(
                    select(func.count(TagExperienceCase.id)).where(
                        TagExperienceCase.source_feedback_event_id == t3_feedback.id
                    )
                )
            ).scalar_one()
        )
        pending_remediation_count = int(
            (
                await session.execute(
                    select(func.count(TagExtractionJob.id)).where(
                        TagExtractionJob.idempotency_key == f"feedback-remediation:{t3_feedback.id}"
                    )
                )
            ).scalar_one()
        )
    assert current_before_freeze.fact_id == old_fact_id
    assert fact_ids_before_freeze == [old_fact_id]
    assert legacy_count_before_freeze == 0
    assert t3_feedback.training_eligible is False
    assert pending_badcase_count == 0
    assert pending_experience_count == 0
    assert pending_remediation_count == (1 if failure_stage == "asr" else 0)

    if expected_split == "holdout" and failure_stage == "tag_reasoning":
        async with governance_factory() as session, session.begin():
            leaked_badcase = TagBadcase(
                tenant_id="chang_an",
                source_feedback_event_id=t3_feedback.id,
                subject_type="dialogue_unit",
                subject_id=unit_id,
                tag_key="intent",
                failure_stage="tag_reasoning",
                failure_mode="correct:legacy_leak",
                signature_hash="7" * 64,
                cluster_key="tag_reasoning:intent:legacy_leak",
                dataset_split="operational",
                root_cause={"latest_feedback_event_id": t3_feedback.id},
                status="open",
                regression_result={},
                occurrence_count=1,
                first_seen_at=datetime.now(UTC),
                last_seen_at=datetime.now(UTC),
            )
            session.add(leaked_badcase)
            await session.flush()
            session.add(
                TagExperienceCase(
                    tenant_id="chang_an",
                    source_badcase_id=leaked_badcase.id,
                    source_feedback_event_id=t3_feedback.id,
                    scene_signature={},
                    failure_signature={},
                    harness_spec={},
                    reward_vector={},
                    outcome="successful",
                    quality_tier="t3",
                    dataset_split="operational",
                    eligible=True,
                    checksum="8" * 64,
                    materialized_at=datetime.now(UTC),
                )
            )

    gold_set = await service.create_gold_set(
        tenant_id="chang_an",
        key="certified-t3",
        name="认证 T3 金标",
        description=None,
        schema_version_id=schema_version_id,
        actor_user_id=1,
    )
    frozen = await service.freeze_gold_set(
        tenant_id="chang_an",
        gold_set_id=gold_set.id,
        version="1",
        decision_ids=[],
        cohort={"review_bundle_ids": [bundle_id], "truth_tiers": ["t3"]},
        actor_user_id=1,
        require_complete=True,
    )

    assert frozen.item_count == 1
    assert frozen.completeness_manifest["complete"] is True
    async with governance_factory() as session:
        label = (
            await session.execute(
                select(TagGoldLabel).where(TagGoldLabel.gold_set_version_id == frozen.id)
            )
        ).scalar_one()
        lane_assignment = (
            await session.execute(
                select(TagFeedbackLaneAssignment).where(
                    TagFeedbackLaneAssignment.feedback_event_id == t3_feedback.id
                )
            )
        ).scalar_one()
        current_after_freeze = (await session.execute(select(TagAssignmentCurrent))).scalar_one()
        fact_ids_after_freeze = list(
            (await session.execute(select(TagAssignmentFact.id).order_by(TagAssignmentFact.id)))
            .scalars()
            .all()
        )
        legacy_count_after_freeze = int(
            (await session.execute(select(func.count(DialogueTagAssignment.id)))).scalar_one()
        )
        raw_badcases = list(
            (
                await session.execute(
                    select(TagBadcase).where(TagBadcase.source_feedback_event_id == t3_feedback.id)
                )
            )
            .scalars()
            .all()
        )
        raw_experiences = list(
            (
                await session.execute(
                    select(TagExperienceCase).where(
                        TagExperienceCase.source_feedback_event_id == t3_feedback.id
                    )
                )
            )
            .scalars()
            .all()
        )
        t3_remediation_jobs = list(
            (
                await session.execute(
                    select(TagExtractionJob).where(
                        TagExtractionJob.idempotency_key == f"feedback-remediation:{t3_feedback.id}"
                    )
                )
            )
            .scalars()
            .all()
        )
        feedback_coverage = await service._optimization_feedback_coverage(
            session,
            tenant_id="chang_an",
            cohort={},
        )
    visible_badcases = await service.list_badcases(tenant_id="chang_an")
    visible_experiences = await service.list_experience_cases(tenant_id="chang_an")
    public_workspace = await ReceptionService(
        governance_factory,
        audio_root=tmp_path,
    ).get_workspace(
        _reception_id,
        "chang_an",
    )
    public_lineage = await service.get_fact_lineage(
        tenant_id="chang_an",
        fact_id=old_fact_id,
        actor_user_id=1,
        actor_role="admin",
    )
    assert label.review_decision_id == t3_decision.id
    assert label.truth_tier == "t3"
    assert label.truth_state == truth_state
    assert label.tag_value == review_value
    assert label.split == expected_split
    assert lane_assignment.split == expected_split
    assert label.input_hash == input_hash
    assert label.input_snapshot == input_snapshot
    assert sorted(label.annotation_quality["predecessor_decision_ids"]) == sorted(t2_decision_ids)
    assert label.annotation_quality["source_extraction_run_id"] == extraction_run_id
    assert label.annotation_quality["source_harness_execution_id"] == harness_execution_id
    assert current_after_freeze.fact_id == old_fact_id
    assert fact_ids_after_freeze == [old_fact_id]
    assert legacy_count_after_freeze == 0
    assert {
        (assignment.label_key, assignment.label_value)
        for assignment in public_workspace.tag_assignments
    } == {("intent", "browse")}
    assert public_lineage["fact"].id == old_fact_id
    assert public_lineage["fact"].tag_value == "browse"
    expected_feedback_coverage = (
        1
        if (
            expected_split == "validation"
            and failure_stage == "tag_reasoning"
            and truth_state in {"present", "absent"}
        )
        else 0
    )
    assert feedback_coverage.total == expected_feedback_coverage
    if failure_stage == "asr":
        assert len(t3_remediation_jobs) == 1
        assert t3_remediation_jobs[0].scope["reception_ids"] == [_reception_id]
        assert t3_remediation_jobs[0].scope["source_feedback_event_id"] == t3_feedback.id
        assert raw_experiences == []
        if expected_split == "validation":
            assert len(raw_badcases) == 1
            assert raw_badcases[0].dataset_split == "validation"
            assert raw_badcases[0].id in {item.id for item in visible_badcases}
        else:
            assert raw_badcases == []
    elif expected_split == "holdout":
        assert len(raw_badcases) == 1
        assert raw_badcases[0].status == "ignored"
        assert raw_badcases[0].dataset_split == "holdout"
        assert len(raw_experiences) == 1
        assert raw_experiences[0].eligible is False
        assert raw_experiences[0].dataset_split == "holdout"
        assert all(item.id != raw_badcases[0].id for item in visible_badcases)
        assert all(item.id != raw_experiences[0].id for item in visible_experiences)
    elif truth_state == "not_applicable":
        assert t3_remediation_jobs == []
        assert len(raw_badcases) == 1
        assert raw_badcases[0].dataset_split == expected_split
        assert raw_badcases[0].id in {item.id for item in visible_badcases}
        assert raw_experiences == []
    else:
        assert t3_remediation_jobs == []
        assert len(raw_badcases) == 1
        assert raw_badcases[0].dataset_split == expected_split
        assert raw_badcases[0].status in {"open", "reopened"}
        assert len(raw_experiences) == 1
        assert raw_experiences[0].dataset_split == expected_split
        assert raw_experiences[0].eligible is True
        assert raw_badcases[0].id in {item.id for item in visible_badcases}
        assert raw_experiences[0].id in {item.id for item in visible_experiences}

    async with governance_factory() as session, session.begin():
        session.add(
            TagReviewTask(
                tenant_id="chang_an",
                batch_id="unfinished-certified-t3",
                review_bundle_id=bundle_id,
                selection_policy="critical_positive",
                selection_policy_version="1",
                blind_mode=True,
                subject_type="dialogue_unit",
                subject_id=unit_id,
                reception_id=_reception_id,
                tag_key="intent",
                proposed_value=None,
                confidence=None,
                evidence_refs=[],
                proposed_fact_id=None,
                schema_version_id=schema_version_id,
                tagger_version_id=tagger_id,
                source_extraction_run_id=extraction_run_id,
                source_harness_execution_id=harness_execution_id,
                reason="critical",
                status="pending",
                priority=1,
                created_by=10,
            )
        )

    with pytest.raises(GovernanceConflictError, match="gold cohort is incomplete"):
        await service.freeze_gold_set(
            tenant_id="chang_an",
            gold_set_id=gold_set.id,
            version="2",
            decision_ids=[],
            cohort={"review_bundle_ids": [bundle_id], "truth_tiers": ["t3"]},
            actor_user_id=1,
            require_complete=True,
        )


@pytest.mark.asyncio
async def test_gold_matrix_batch_fails_early_without_a_server_owned_input_snapshot(
    governance_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_governance import (
        AssignmentValidationError,
        TagGovernanceService,
    )

    unit_id, _segment_ids, _reception_id = await _seed_dialogue_context(governance_factory)
    schema_version_id = await _seed_single_tag_schema(
        governance_factory,
        key="gold-without-source-snapshot",
    )

    with pytest.raises(AssignmentValidationError, match="server-owned completed"):
        await TagGovernanceService(governance_factory).create_review_batch(
            tenant_id="chang_an",
            reason="gold",
            subjects=[
                {
                    "subject_type": "dialogue_unit",
                    "subject_id": unit_id,
                    "tag_key": "intent",
                    "schema_version_id": schema_version_id,
                }
            ],
            actor_user_id=1,
            review_bundle_id="gold-without-source-snapshot",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected_adjudication_tasks"),
    [("uncertain", 0), ("escalate", 1)],
)
async def test_uncertain_or_escalated_review_does_not_mutate_tag_facts(
    governance_factory: async_sessionmaker[AsyncSession],
    action: str,
    expected_adjudication_tasks: int,
) -> None:
    from audio_graphy.models.tag_governance import (
        TagAssignmentFact,
        TagBadcase,
        TagExperienceCase,
        TagExtractionJob,
        TagFeedbackEvent,
        TagReviewTask,
    )
    from audio_graphy.services.tag_governance import TagGovernanceService

    unit_id, _segment_ids, reception_id = await _seed_dialogue_context(governance_factory)
    async with governance_factory() as session, session.begin():
        task = TagReviewTask(
            tenant_id="chang_an",
            batch_id="blind-audit",
            review_bundle_id="audit-2026-07",
            selection_policy="representative_random",
            selection_policy_version="1",
            sampling_probability=0.1,
            blind_mode=True,
            subject_type="dialogue_unit",
            subject_id=unit_id,
            reception_id=reception_id,
            tag_key="intent",
            proposed_value=None,
            confidence=None,
            evidence_refs=[],
            proposed_fact_id=None,
            schema_version_id=None,
            tagger_version_id=None,
            reason="audit",
            status="pending",
            priority=0,
            created_by=1,
        )
        session.add(task)
        await session.flush()
        task_id = task.id

    service = TagGovernanceService(governance_factory)
    await service.claim_review(
        tenant_id="chang_an",
        task_id=task_id,
        reviewer_user_id=2,
    )
    resolved, decision, fact = await service.decide_review(
        tenant_id="chang_an",
        task_id=task_id,
        reviewer_user_id=2,
        action=action,
        corrected_value=None,
        reason_code="insufficient_audio",
        note=None,
        evidence_refs=[],
        truth_state="uncertain",
        primary_failure_stage="insufficient_audio",
    )

    async with governance_factory() as session:
        fact_count = int(
            (await session.execute(select(func.count(TagAssignmentFact.id)))).scalar_one()
        )
        feedback = (
            await session.execute(
                select(TagFeedbackEvent).where(TagFeedbackEvent.review_decision_id == decision.id)
            )
        ).scalar_one()
        adjudication_task_count = int(
            (
                await session.execute(
                    select(func.count(TagReviewTask.id)).where(
                        TagReviewTask.reason == "adjudication"
                    )
                )
            ).scalar_one()
        )
        badcase = (
            await session.execute(
                select(TagBadcase).where(TagBadcase.source_feedback_event_id == feedback.id)
            )
        ).scalar_one()
        remediation_job = (
            await session.execute(
                select(TagExtractionJob).where(
                    TagExtractionJob.idempotency_key == f"feedback-remediation:{feedback.id}"
                )
            )
        ).scalar_one()
        experience_count = int(
            (
                await session.execute(
                    select(func.count(TagExperienceCase.id)).where(
                        TagExperienceCase.source_feedback_event_id == feedback.id
                    )
                )
            ).scalar_one()
        )
    assert resolved.status == "resolved"
    assert decision.truth_state == "uncertain"
    assert fact is None
    assert fact_count == 0
    assert feedback.truth_state == "uncertain"
    assert feedback.training_eligible is False
    assert badcase.failure_stage == "insufficient_audio"
    assert remediation_job.job_type == "remediate"
    assert remediation_job.scope["reception_ids"] == [reception_id]
    assert remediation_job.scope["source_feedback_event_id"] == feedback.id
    assert experience_count == 0
    assert adjudication_task_count == expected_adjudication_tasks


@pytest.mark.asyncio
async def test_two_fifteen_minute_error_windows_roll_back_current_visibility(
    governance_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.models.tag_governance import (
        TagAssignmentCurrent,
        TagDeployment,
        TagEvaluationRun,
        TagExtractionJob,
        TagExtractionRun,
        TaggerVersion,
        TagSchema,
        TagSchemaVersion,
    )
    from audio_graphy.services.reception_tagging import ReceptionTaggingService
    from audio_graphy.services.tag_governance import TagGovernanceService

    now = datetime.now(UTC)
    unit_id, segment_ids, reception_id = await _seed_dialogue_context(governance_factory)
    async with governance_factory() as session:
        schema = TagSchema(
            tenant_id="chang_an",
            key="rollback",
            name="回滚体系",
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
                    "evidence_required": True,
                    "subject_types": ["dialogue_unit"],
                    "scenarios": ["automotive"],
                }
            ],
            checksum="2" * 64,
            status="published",
            created_by=1,
        )
        session.add(version)
        await session.flush()
        baseline_tagger = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=version.id,
            version="rollback-baseline",
            engine="rule",
            prompt_content="",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="rules-v1",
            thresholds={"intent": 0.7},
            config_checksum="7" * 64,
            status="qualified",
            created_by=1,
            qualified_at=now,
        )
        candidate_tagger = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=version.id,
            version="rollback-candidate",
            engine="rule",
            prompt_content="",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="rules-v2",
            thresholds={"intent": 0.7},
            config_checksum="8" * 64,
            status="qualified",
            created_by=1,
            qualified_at=now,
        )
        session.add_all([baseline_tagger, candidate_tagger])
        await session.flush()
        evaluation = TagEvaluationRun(
            tenant_id="chang_an",
            tagger_version_id=candidate_tagger.id,
            baseline_tagger_version_id=baseline_tagger.id,
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
        deployment = TagDeployment(
            tenant_id="chang_an",
            tagger_version_id=candidate_tagger.id,
            evaluation_run_id=evaluation.id,
            baseline_tagger_version_id=baseline_tagger.id,
            status="canary_5",
            traffic_percent=5,
            revision=1,
            created_by=1,
        )
        session.add(deployment)
        await session.flush()
        candidate_job = TagExtractionJob(
            tenant_id="chang_an",
            job_type="extract",
            status="completed",
            scope={"dialogue_unit_ids": [unit_id]},
            tagger_version_id=candidate_tagger.id,
            idempotency_key="rollback-candidate-serving",
            total_items=1,
            completed_items=1,
            failed_items=0,
            failed_subset=[],
            attempt_count=1,
            max_attempts=3,
            revision=2,
            created_by=1,
            finished_at=now,
        )
        session.add(candidate_job)
        await session.flush()
        candidate_run = TagExtractionRun(
            tenant_id="chang_an",
            job_id=candidate_job.id,
            subject_type="dialogue_unit",
            subject_id=unit_id,
            tagger_version_id=candidate_tagger.id,
            deployment_id=deployment.id,
            input_hash="4" * 64,
            input_snapshot={"subject_id": unit_id},
            output_snapshot={},
            status="completed",
            served_current=True,
            deployment_stage="canary_5",
            deployment_revision=1,
            started_at=now,
            finished_at=now,
        )
        session.add(candidate_run)
        await session.commit()
        version_id = version.id
        deployment_id = deployment.id
        candidate_run_id = candidate_run.id
        baseline_tagger_id = baseline_tagger.id
        candidate_tagger_id = candidate_tagger.id

    service = TagGovernanceService(governance_factory)
    baseline = await service.append_assignment(
        tenant_id="chang_an",
        subject_type="dialogue_unit",
        subject_id=unit_id,
        tag_key="intent",
        tag_value="browse",
        confidence=0.8,
        evidence_refs=[{"segment_id": segment_ids[0], "start_sec": 0, "end_sec": 1}],
        source="imported",
        schema_version_id=version_id,
        tagger_version_id=baseline_tagger_id,
        extraction_run_id=None,
        deployment_id=None,
        input_hash="3" * 64,
        actor_user_id=1,
    )
    candidate = await service.append_assignment(
        tenant_id="chang_an",
        subject_type="dialogue_unit",
        subject_id=unit_id,
        tag_key="intent",
        tag_value="purchase",
        confidence=0.9,
        evidence_refs=[{"segment_id": segment_ids[1], "start_sec": 1, "end_sec": 2}],
        source="imported",
        schema_version_id=version_id,
        tagger_version_id=candidate_tagger_id,
        extraction_run_id=candidate_run_id,
        deployment_id=deployment_id,
        input_hash="4" * 64,
        actor_user_id=1,
    )
    assert candidate.id != baseline.id
    insight_service = ReceptionTaggingService(governance_factory)

    async def current_insight_value() -> str:
        result = await insight_service.insights(
            tenant_id="chang_an",
            store_ids=[],
            agent_names=[],
            scenarios=[],
            started_from=None,
            started_to=None,
            reception_ids=[reception_id],
            group_keys=[],
            group_ids=[],
            forced_agent_user_id=None,
            page=1,
            page_size=20,
            assignment_limit=100,
            matrix_limit=96,
            difference_limit=100,
            evidence_summary_limit=100,
            merge_strategy="priority",
            trend_granularity="day",
            top_n_co_occurrences=10,
        )
        assert len(result.evidence_summary) == 1
        return result.evidence_summary[0].label_value

    assert await current_insight_value() == "purchase"

    policy_start = now.replace(minute=0, second=0, microsecond=0)
    for offset in range(6):
        end = policy_start + timedelta(minutes=5 * (offset + 1))
        observation, monitored = await service.record_deployment_observation(
            tenant_id="chang_an",
            deployment_id=deployment_id,
            sample_reception_ids=[reception_id],
            metrics={
                "run_count": 100,
                "failed_run_count": 1,
                "error_rate": 0.01,
            },
            breach_codes=["error_rate"],
            window_start=end - timedelta(minutes=5),
            window_end=end,
            actor_user_id=1,
            source="monitor",
            is_trusted=True,
        )
        if offset < 5:
            assert monitored.status == "canary_5"
        if offset == 0:
            replay, replayed_deployment = await service.record_deployment_observation(
                tenant_id="chang_an",
                deployment_id=deployment_id,
                sample_reception_ids=[reception_id],
                metrics={
                    "run_count": 100,
                    "failed_run_count": 1,
                    "error_rate": 0.01,
                },
                breach_codes=["error_rate"],
                window_start=end - timedelta(minutes=5),
                window_end=end,
                actor_user_id=1,
                source="monitor",
                is_trusted=True,
            )
            assert replay.id == observation.id
            assert replayed_deployment.status == "canary_5"

    async with governance_factory() as session:
        from audio_graphy.models.tag_governance import TagGovernanceAuditEvent

        current = (await session.execute(select(TagAssignmentCurrent))).scalar_one()
        rolled_back = await session.get(TagDeployment, deployment_id)
        fallback_audit = (
            await session.execute(
                select(TagGovernanceAuditEvent).where(
                    TagGovernanceAuditEvent.resource_id == deployment_id,
                    TagGovernanceAuditEvent.action == "baseline_route_fallback",
                )
            )
        ).scalar_one()
        assert current.fact_id == baseline.id
        assert rolled_back is not None
        assert rolled_back.status == "rolled_back"
        assert fallback_audit.payload["baseline_tagger_version_id"] == baseline_tagger_id
    assert await current_insight_value() == "browse"
    stale_fact = await service.append_assignment(
        tenant_id="chang_an",
        subject_type="dialogue_unit",
        subject_id=unit_id,
        tag_key="intent",
        tag_value="purchase",
        confidence=0.99,
        evidence_refs=[{"segment_id": segment_ids[1], "start_sec": 1, "end_sec": 2}],
        source="imported",
        schema_version_id=version_id,
        tagger_version_id=candidate_tagger_id,
        extraction_run_id=candidate_run_id,
        deployment_id=deployment_id,
        input_hash="4" * 64,
        actor_user_id=1,
    )
    async with governance_factory() as session:
        current_after_stale_append = (
            await session.execute(select(TagAssignmentCurrent))
        ).scalar_one()
    assert stale_fact.id != baseline.id
    assert current_after_stale_append.fact_id == baseline.id

    fallback_job = await service.enqueue_job(
        tenant_id="chang_an",
        job_type="extract",
        scope={"dialogue_unit_ids": [unit_id]},
        idempotency_key="post-rollback-route",
        created_by=1,
    )
    from audio_graphy.tag_worker import TagJobWorker

    routed_tagger, routed_deployment = await TagJobWorker(
        governance_factory,
        worker_id="route-probe",
    )._resolve_route(fallback_job)
    assert routed_tagger == baseline_tagger_id
    assert routed_deployment is None


@pytest.mark.asyncio
async def test_extractor_reads_scrubbed_dialogue_segments_and_applies_versioned_rule_dsl(
    governance_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.models.reception import DialogueUnit, Reception, ReceptionRecording
    from audio_graphy.models.recording import Recording
    from audio_graphy.models.segment import Segment
    from audio_graphy.models.tag_governance import (
        TagExtractionJob,
        TaggerVersion,
        TagSchema,
        TagSchemaVersion,
    )
    from audio_graphy.services.tag_extractor import TagExtractor

    now = datetime.now(UTC)
    async with governance_factory() as session:
        recording = Recording(
            tenant_id="chang_an",
            store_id="S001",
            agent_name="agent_ca",
            path="/tmp/extractor.wav",
            status="indexed",
            pipeline_state="done",
            recorded_at=now,
        )
        session.add(recording)
        await session.flush()
        segment = Segment(
            tenant_id="chang_an",
            recording_id=recording.id,
            idx=0,
            start_sec=0,
            end_sec=4,
            transcript="RAW-PII-13800000000",
            text_scrubbed="客户明确表示今天签约",
            speaker="customer",
            vad_conf=0.9,
        )
        session.add(segment)
        reception = Reception(
            tenant_id="chang_an",
            scenario="automotive",
            store_id="S001",
            status="ready",
            merge_mode="logical",
            started_at=now,
            ended_at=now + timedelta(seconds=4),
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
                timeline_end_sec=4,
                source_start_sec=0,
                source_end_sec=4,
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
            end_sec=4,
            topic="成交",
            business_stage="成交意向",
            segment_refs=[{"segment_id": segment.id, "recording_id": recording.id}],
            speaker_refs=["customer"],
            edit_status="auto",
        )
        session.add(unit)
        schema = TagSchema(
            tenant_id="chang_an",
            key="extractor",
            name="抽取体系",
            status="published",
            created_by=1,
        )
        session.add(schema)
        await session.flush()
        schema_version = TagSchemaVersion(
            tenant_id="chang_an",
            schema_id=schema.id,
            version="1",
            definitions=[
                {
                    "key": "intent",
                    "value_type": "enum",
                    "allowed_values": ["browse", "purchase"],
                    "evidence_required": True,
                    "subject_types": ["dialogue_unit"],
                    "scenarios": ["automotive"],
                    "threshold": 0.7,
                }
            ],
            checksum="5" * 64,
            status="published",
            created_by=1,
        )
        session.add(schema_version)
        await session.flush()
        tagger = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=schema_version.id,
            version="rules-1",
            engine="rule",
            prompt_content="必须返回结构化 JSON",
            rule_bundle={
                "dsl_version": "1",
                "rules": [
                    {
                        "tag_key": "intent",
                        "value": "purchase",
                        "contains_any": ["签约", "订购"],
                        "confidence": 0.94,
                    }
                ],
            },
            model_version="rules-local",
            thresholds={"intent": 0.7},
            config_checksum="6" * 64,
            status="qualified",
            created_by=1,
        )
        session.add(tagger)
        job = TagExtractionJob(
            tenant_id="chang_an",
            job_type="extract",
            status="running",
            scope={"dialogue_unit_ids": [unit.id]},
            tagger_version_id=None,
            idempotency_key="extractor-test",
            total_items=1,
            completed_items=0,
            failed_items=0,
            attempt_count=1,
            max_attempts=3,
            revision=1,
            lease_owner="test",
            created_by=1,
        )
        session.add(job)
        await session.commit()
        unit_id = unit.id
        tagger_id = tagger.id
        job_id = job.id

    result = await TagExtractor(governance_factory).extract_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=unit_id,
        tagger_version_id=tagger_id,
        job_id=job_id,
        deployment_id=None,
        actor_user_id=1,
    )

    assert result.assignments[0]["tag_key"] == "intent"
    assert result.assignments[0]["tag_value"] == "purchase"
    assert result.assignments[0]["evidence_refs"][0]["text"] == "客户明确表示今天签约"
    assert "RAW-PII" not in str(result.input_snapshot)


@pytest.mark.asyncio
async def test_schema_relations_and_tagger_recipe_are_validated_before_persistence(
    governance_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_governance import (
        GovernanceError,
        TagGovernanceService,
    )

    service = TagGovernanceService(governance_factory)
    schema = await service.create_schema(
        tenant_id="chang_an",
        key="relations",
        name="关系约束",
        description=None,
        created_by=1,
    )
    version = await service.create_schema_version(
        tenant_id="chang_an",
        schema_id=schema.id,
        version="1",
        definitions=[
            {
                "key": "intent.browse",
                "value_type": "enum",
                "allowed_values": ["yes"],
                "subject_types": ["dialogue_unit"],
                "scenarios": ["automotive"],
                "mutually_exclusive_with": ["intent.purchase"],
            },
            {
                "key": "intent.purchase",
                "value_type": "enum",
                "allowed_values": ["yes"],
                "subject_types": ["dialogue_unit"],
                "scenarios": ["automotive"],
            },
            {
                "key": "quote.accepted",
                "value_type": "boolean",
                "subject_types": ["dialogue_unit"],
                "scenarios": ["automotive"],
                "depends_on": ["intent.purchase"],
            },
        ],
        created_by=1,
    )
    definitions = {item["key"]: item for item in version.definitions}
    assert definitions["intent.purchase"]["mutually_exclusive_with"] == ["intent.browse"]
    published = await service.publish_schema_version(
        tenant_id="chang_an",
        schema_id=schema.id,
        version_id=version.id,
        actor_user_id=1,
    )

    with pytest.raises(GovernanceError, match="versioned prompt"):
        await service.create_tagger_version(
            tenant_id="chang_an",
            schema_version_id=published.id,
            version="bad-empty-prompt",
            engine="hybrid",
            prompt_content="",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="local",
            thresholds={},
            created_by=1,
        )
    with pytest.raises(GovernanceError, match="unknown tag_key"):
        await service.create_tagger_version(
            tenant_id="chang_an",
            schema_version_id=published.id,
            version="bad-rule-key",
            engine="rule",
            prompt_content="",
            rule_bundle={
                "dsl_version": "1",
                "rules": [
                    {
                        "tag_key": "unknown",
                        "value": "yes",
                        "contains_any": ["x"],
                    }
                ],
            },
            model_version="local",
            thresholds={},
            created_by=1,
        )
    with pytest.raises(GovernanceError, match="allowed_values"):
        await service.create_tagger_version(
            tenant_id="chang_an",
            schema_version_id=published.id,
            version="bad-rule-value",
            engine="rule",
            prompt_content="",
            rule_bundle={
                "dsl_version": "1",
                "rules": [
                    {
                        "tag_key": "intent.purchase",
                        "value": "no",
                        "contains_any": ["x"],
                    }
                ],
            },
            model_version="local",
            thresholds={},
            created_by=1,
        )
    with pytest.raises(GovernanceError, match="unknown tags"):
        await service.create_tagger_version(
            tenant_id="chang_an",
            schema_version_id=published.id,
            version="bad-threshold-key",
            engine="rule",
            prompt_content="",
            rule_bundle={
                "dsl_version": "1",
                "rules": [
                    {
                        "tag_key": "intent.purchase",
                        "value": "yes",
                        "contains_any": ["购买"],
                    }
                ],
            },
            model_version="local",
            thresholds={"not-defined": 0.7},
            created_by=1,
        )
    with pytest.raises(GovernanceError, match="finite"):
        await service.create_tagger_version(
            tenant_id="chang_an",
            schema_version_id=published.id,
            version="bad-threshold-value",
            engine="rule",
            prompt_content="",
            rule_bundle={
                "dsl_version": "1",
                "rules": [
                    {
                        "tag_key": "intent.purchase",
                        "value": "yes",
                        "contains_any": ["购买"],
                    }
                ],
            },
            model_version="local",
            thresholds={"intent.purchase": float("nan")},
            created_by=1,
        )
    with pytest.raises(GovernanceError, match="acyclic"):
        await service.create_schema_version(
            tenant_id="chang_an",
            schema_id=schema.id,
            version="2",
            definitions=[
                {
                    "key": "a",
                    "value_type": "boolean",
                    "subject_types": ["dialogue_unit"],
                    "depends_on": ["b"],
                },
                {
                    "key": "b",
                    "value_type": "boolean",
                    "subject_types": ["dialogue_unit"],
                    "depends_on": ["a"],
                },
            ],
            created_by=1,
        )


@pytest.mark.asyncio
async def test_automatic_recompute_cannot_override_manual_current(
    governance_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.models.tag_governance import (
        TagAssignmentCurrent,
        TagExtractionJob,
        TagExtractionRun,
        TaggerVersion,
        TagReviewTask,
        TagSchema,
        TagSchemaVersion,
    )
    from audio_graphy.services.tag_governance import TagGovernanceService

    unit_id, segment_ids, _reception_id = await _seed_dialogue_context(governance_factory)
    now = datetime.now(UTC)
    async with governance_factory() as session, session.begin():
        schema = TagSchema(
            tenant_id="chang_an",
            key="manual-protection",
            name="人工保护",
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
                    "scenarios": ["automotive"],
                    "evidence_required": True,
                }
            ],
            checksum="9" * 64,
            status="published",
            created_by=1,
        )
        session.add(version)
        await session.flush()
        tagger = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=version.id,
            version="manual-protection-rules",
            engine="rule",
            prompt_content="",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="rules",
            thresholds={"intent": 0.7},
            config_checksum="a" * 64,
            status="qualified",
            created_by=1,
            qualified_at=now,
        )
        session.add(tagger)
        await session.flush()
        job = TagExtractionJob(
            tenant_id="chang_an",
            job_type="recompute",
            status="running",
            scope={"dialogue_unit_ids": [unit_id]},
            tagger_version_id=tagger.id,
            idempotency_key="manual-protection",
            total_items=1,
            completed_items=0,
            failed_items=0,
            failed_subset=[],
            attempt_count=1,
            max_attempts=3,
            revision=1,
            created_by=1,
        )
        session.add(job)
        await session.flush()
        run = TagExtractionRun(
            tenant_id="chang_an",
            job_id=job.id,
            subject_type="dialogue_unit",
            subject_id=unit_id,
            tagger_version_id=tagger.id,
            input_hash="b" * 64,
            input_snapshot={"dialogue_unit_id": unit_id},
            output_snapshot={},
            status="running",
            started_at=now,
        )
        session.add(run)
        await session.flush()
        version_id = version.id
        tagger_id = tagger.id
        run_id = run.id

    service = TagGovernanceService(governance_factory)
    manual = await service.append_assignment(
        tenant_id="chang_an",
        subject_type="dialogue_unit",
        subject_id=unit_id,
        tag_key="intent",
        tag_value="purchase",
        confidence=1,
        evidence_refs=[{"segment_id": segment_ids[1], "start_sec": 1, "end_sec": 2}],
        source="manual",
        schema_version_id=version_id,
        tagger_version_id=None,
        extraction_run_id=None,
        deployment_id=None,
        input_hash="c" * 64,
        actor_user_id=2,
    )
    automatic = await service.append_assignment(
        tenant_id="chang_an",
        subject_type="dialogue_unit",
        subject_id=unit_id,
        tag_key="intent",
        tag_value="browse",
        confidence=0.92,
        evidence_refs=[{"segment_id": segment_ids[0], "start_sec": 0, "end_sec": 1}],
        source="rule",
        schema_version_id=version_id,
        tagger_version_id=tagger_id,
        extraction_run_id=run_id,
        deployment_id=None,
        input_hash="b" * 64,
        actor_user_id=0,
    )
    async with governance_factory() as session:
        current = (await session.execute(select(TagAssignmentCurrent))).scalar_one()
        review = (await session.execute(select(TagReviewTask))).scalar_one()
    assert automatic.id != manual.id
    assert current.fact_id == manual.id
    assert review.proposed_fact_id == automatic.id
    assert review.reason == "conflict"


@pytest.mark.asyncio
async def test_nullable_lineage_recipe_is_database_idempotent(
    governance_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.models.tag_governance import (
        TagAssignmentFact,
        TagSchema,
        TagSchemaVersion,
    )
    from audio_graphy.services.tag_governance import TagGovernanceService

    unit_id, segment_ids, _reception_id = await _seed_dialogue_context(governance_factory)
    async with governance_factory() as session, session.begin():
        schema = TagSchema(
            tenant_id="chang_an",
            key="nullable-recipe",
            name="幂等",
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
                    "allowed_values": ["purchase"],
                    "subject_types": ["dialogue_unit"],
                    "scenarios": ["automotive"],
                    "evidence_required": True,
                }
            ],
            checksum="3" * 64,
            status="published",
            created_by=1,
        )
        session.add(version)
        await session.flush()
        version_id = version.id

    service = TagGovernanceService(governance_factory)
    payload = {
        "tenant_id": "chang_an",
        "subject_type": "dialogue_unit",
        "subject_id": unit_id,
        "tag_key": "intent",
        "tag_value": "purchase",
        "confidence": 1.0,
        "evidence_refs": [{"segment_id": segment_ids[1], "start_sec": 1, "end_sec": 2}],
        "source": "manual",
        "schema_version_id": version_id,
        "tagger_version_id": None,
        "extraction_run_id": None,
        "deployment_id": None,
        "input_hash": "7" * 64,
        "actor_user_id": 2,
    }
    first = await service.append_assignment(**payload)
    replay = await service.append_assignment(**payload)
    async with governance_factory() as session:
        facts = list((await session.execute(select(TagAssignmentFact))).scalars().all())
    assert replay.id == first.id
    assert first.recipe_hash == replay.recipe_hash
    assert len(facts) == 1
