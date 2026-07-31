"""Independent durable tag-worker tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from audio_graphy.models import Base
from audio_graphy.models.tag_governance import (
    TagAssignmentCurrent,
    TagAssignmentFact,
    TagExtractionJob,
    TagReviewTask,
)
from audio_graphy.services.tag_governance import TagGovernanceService


@pytest.mark.parametrize(
    ("status", "bucket", "traffic_percent", "shadow_sample_percent", "expected"),
    [
        ("shadow", 9, 0, 10, (True, False)),
        ("shadow", 10, 0, 10, (False, False)),
        ("canary_5", 4, 5, 10, (True, True)),
        ("canary_5", 5, 5, 10, (False, False)),
        ("canary_25", 24, 25, 10, (True, True)),
        ("canary_25", 25, 25, 10, (False, False)),
        ("production", 99, 100, 10, (True, True)),
    ],
)
def test_deployment_route_decision_uses_one_bucket_for_execution_and_publication(
    status: str,
    bucket: int,
    traffic_percent: int,
    shadow_sample_percent: int,
    expected: tuple[bool, bool],
) -> None:
    from audio_graphy.tag_worker import _deployment_route_decision

    assert (
        _deployment_route_decision(
            status=status,
            bucket=bucket,
            traffic_percent=traffic_percent,
            shadow_sample_percent=shadow_sample_percent,
        )
        == expected
    )


def test_shadow_candidate_execution_stops_after_sampling_completes() -> None:
    from audio_graphy.tag_worker import _deployment_route_decision

    assert _deployment_route_decision(
        status="shadow",
        bucket=0,
        traffic_percent=0,
        shadow_sample_percent=100,
        shadow_sampling_complete=True,
    ) == (False, False)


@pytest.mark.parametrize(
    ("status", "expected_rate", "tolerance"),
    [
        ("canary_5", 0.05, 0.01),
        ("canary_25", 0.25, 0.01),
    ],
)
def test_canary_candidate_execution_rate_matches_release_stage(
    status: str,
    expected_rate: float,
    tolerance: float,
) -> None:
    from audio_graphy.services.tag_governance import stable_canary_bucket
    from audio_graphy.tag_worker import _deployment_route_decision

    subject_count = 10_000
    executed = 0
    for reception_id in range(1, subject_count + 1):
        bucket = stable_canary_bucket("chang_an", reception_id, 97)
        execute, publish = _deployment_route_decision(
            status=status,
            bucket=bucket,
            traffic_percent=int(expected_rate * 100),
            shadow_sample_percent=10,
        )
        assert publish is execute
        executed += int(execute)

    actual_rate = executed / subject_count
    assert actual_rate == pytest.approx(expected_rate, abs=tolerance)


@pytest.mark.asyncio
async def test_worker_passes_target_tag_keys_and_empty_scope_makes_zero_extractor_calls() -> None:
    from audio_graphy.models.tag_governance import (
        TaggerVersion,
        TagSchema,
        TagSchemaVersion,
    )
    from audio_graphy.tag_worker import TagJobWorker

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        schema = TagSchema(
            tenant_id="chang_an",
            key="target-scope",
            name="目标范围",
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
                    "value_type": "boolean",
                    "allowed_values": [],
                    "subject_types": ["dialogue_unit"],
                },
                {
                    "key": "stage",
                    "value_type": "boolean",
                    "allowed_values": [],
                    "subject_types": ["dialogue_unit"],
                },
            ],
            checksum="a" * 64,
            status="published",
            created_by=1,
        )
        session.add(version)
        await session.flush()
        tagger = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=version.id,
            version="target-scope-v1",
            engine="hybrid",
            prompt_content="",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="weak",
            thresholds={},
            config_checksum="b" * 64,
            status="qualified",
            created_by=1,
            qualified_at=now,
        )
        session.add(tagger)
        await session.flush()
        tagger_id = int(tagger.id)

    class ProbeExtractor:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def extract_dialogue_unit(self, **kwargs: object) -> None:
            self.calls.append(dict(kwargs))

        async def record_failed_subject(self, **_kwargs: object) -> None:
            return None

    service = TagGovernanceService(factory)
    scoped_job = await service.enqueue_job(
        tenant_id="chang_an",
        job_type="extract",
        scope={
            "dialogue_unit_ids": [101],
            "target_tag_keys": ["stage", "intent", "stage"],
        },
        idempotency_key="target-scope-non-empty",
        created_by=1,
        tagger_version_id=tagger_id,
    )
    empty_job = await service.enqueue_job(
        tenant_id="chang_an",
        job_type="extract",
        scope={"dialogue_unit_ids": [102], "target_tag_keys": []},
        idempotency_key="target-scope-empty",
        created_by=1,
        tagger_version_id=tagger_id,
    )
    extractor = ProbeExtractor()
    worker = TagJobWorker(
        factory,
        worker_id="target-scope-worker",
        extractor=extractor,  # type: ignore[arg-type]
    )

    assert await worker.run_once(now=now)
    assert extractor.calls[0]["target_tag_keys"] == ("intent", "stage")
    assert await worker.run_once(now=datetime.now(UTC))
    assert len(extractor.calls) == 1

    persisted_empty = await service.get_job(
        tenant_id="chang_an",
        job_id=empty_job.id,
    )
    assert scoped_job.id != empty_job.id
    assert persisted_empty.status == "completed"
    assert persisted_empty.completed_items == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_budgeted_batch_second_item_exhaustion_never_partially_publishes_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from audio_graphy.services.tag_extractor import ExtractionResult
    from audio_graphy.tag_worker import TagJobWorker

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        old_fact = TagAssignmentFact(
            tenant_id="chang_an",
            subject_type="dialogue_unit",
            subject_id=101,
            reception_id=1,
            dialogue_unit_id=101,
            tag_key="intent",
            tag_value=True,
            confidence=1.0,
            evidence_refs=[],
            source="imported",
            schema_version_id=None,
            tagger_version_id=None,
            extraction_run_id=None,
            deployment_id=None,
            input_hash="a" * 64,
            recipe_hash="b" * 64,
            revision=1,
            tombstone=False,
            actor_user_id=1,
            assigned_at=now,
        )
        session.add(old_fact)
        await session.flush()
        old_fact_id = int(old_fact.id)
        session.add(
            TagAssignmentCurrent(
                tenant_id="chang_an",
                subject_type="dialogue_unit",
                subject_id=101,
                tag_key="intent",
                fact_id=old_fact_id,
                revision=1,
            )
        )

    class BudgetProbeExtractor:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def extract_dialogue_unit(
            self,
            **kwargs: object,
        ) -> ExtractionResult:
            self.calls.append(dict(kwargs))
            return ExtractionResult(
                run_id=999,
                input_hash="c" * 64,
                input_snapshot={},
                assignments=(),
                cached=False,
                provider_tokens=10,
                provider_calls=1,
                cost_microunits=5,
            )

        async def record_failed_subject(self, **_kwargs: object) -> None:
            return None

    service = TagGovernanceService(factory)
    job = await service.enqueue_job(
        tenant_id="chang_an",
        job_type="extract",
        scope={
            "dialogue_unit_ids": [101, 102],
            "budget": {
                "max_provider_tokens": 100,
                "max_provider_calls": 1,
                "max_cost_microunits": 100,
                "max_wall_seconds": 60,
            },
        },
        idempotency_key="budget-two-item-atomic-current",
        created_by=1,
    )
    extractor = BudgetProbeExtractor()
    worker = TagJobWorker(
        factory,
        worker_id="budget-worker",
        extractor=extractor,  # type: ignore[arg-type]
    )

    async def resolve_route(_job: TagExtractionJob) -> tuple[int, None]:
        return 1, None

    async def route_decision(**_kwargs: object) -> tuple[bool, bool]:
        return True, True

    monkeypatch.setattr(worker, "_resolve_route", resolve_route)
    monkeypatch.setattr(worker, "_route_decision", route_decision)

    assert await worker.run_once(now=now)

    persisted = await service.get_job(tenant_id="chang_an", job_id=job.id)
    assert persisted.status == "failed"
    assert persisted.last_error_code == "budget_exhausted"
    assert persisted.next_attempt_at is None
    assert persisted.completed_items == 1
    assert persisted.failed_items == 0
    assert persisted.budget_consumed_provider_tokens == 10
    assert persisted.budget_consumed_provider_calls == 1
    assert persisted.budget_consumed_cost_microunits == 5
    assert persisted.budget_reserved_provider_tokens == 0
    assert persisted.budget_reserved_provider_calls == 0
    assert persisted.current_published_at is None
    assert len(extractor.calls) == 1
    assert extractor.calls[0]["publish_current"] is False
    assert extractor.calls[0]["served_current"] is True
    assert extractor.calls[0]["budget_policy_override"] == {
        "max_provider_tokens": 100,
        "max_provider_calls": 1,
        "max_cost_microunits": 100,
        "max_wall_seconds": 60,
    }
    async with factory() as session:
        current_fact_id = (
            await session.execute(
                select(TagAssignmentCurrent.fact_id).where(
                    TagAssignmentCurrent.tenant_id == "chang_an",
                    TagAssignmentCurrent.subject_type == "dialogue_unit",
                    TagAssignmentCurrent.subject_id == 101,
                    TagAssignmentCurrent.tag_key == "intent",
                )
            )
        ).scalar_one()
    assert current_fact_id == old_fact_id
    await engine.dispose()


@pytest.mark.asyncio
async def test_expired_lease_consumes_unsettled_reservation_instead_of_resetting_budget() -> None:
    from audio_graphy.services.tag_governance import TagJobBudgetExhaustedError

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    service = TagGovernanceService(factory)
    started = datetime.now(UTC)
    job = await service.enqueue_job(
        tenant_id="chang_an",
        job_type="extract",
        scope={
            "dialogue_unit_ids": [101],
            "budget": {"max_provider_calls": 1},
        },
        idempotency_key="budget-crash-does-not-reset",
        created_by=1,
    )
    first_claim = await service.claim_next_job(
        worker_id="crashed-worker",
        now=started,
        lease_for=timedelta(seconds=5),
    )
    assert first_claim is not None
    reservation = await service.reserve_job_budget(
        tenant_id="chang_an",
        job_id=job.id,
        worker_id="crashed-worker",
        expected_revision=first_claim.revision,
        now=started,
    )
    assert reservation is not None
    assert reservation.max_provider_calls == 1
    reclaimed_at = started + timedelta(seconds=6)
    second_claim = await service.claim_next_job(
        worker_id="replacement-worker",
        now=reclaimed_at,
        lease_for=timedelta(seconds=5),
    )
    assert second_claim is not None
    assert second_claim.budget_reserved_provider_calls == 0
    assert second_claim.budget_consumed_provider_calls == 1

    with pytest.raises(TagJobBudgetExhaustedError, match="max_provider_calls"):
        await service.reserve_job_budget(
            tenant_id="chang_an",
            job_id=job.id,
            worker_id="replacement-worker",
            expected_revision=second_claim.revision,
            now=reclaimed_at,
        )
    persisted = await service.get_job(tenant_id="chang_an", job_id=job.id)
    assert persisted.last_error_code == "budget_exhausted"
    assert persisted.budget_exhausted_at is not None
    assert persisted.budget_consumed_provider_calls == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_budgeted_job_publishes_staged_facts_in_one_final_transaction() -> None:
    from audio_graphy.models.tag_governance import (
        TagExtractionRun,
        TaggerVersion,
        TagSchema,
        TagSchemaVersion,
    )

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        schema = TagSchema(
            tenant_id="chang_an",
            key="atomic-budget-schema",
            name="原子预算",
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
                    "value_type": "boolean",
                    "allowed_values": [],
                    "subject_types": ["dialogue_unit"],
                }
            ],
            checksum="d" * 64,
            status="published",
            created_by=1,
        )
        session.add(schema_version)
        await session.flush()
        tagger = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=schema_version.id,
            version="atomic-budget-v1",
            engine="llm",
            prompt_content="",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="weak",
            thresholds={},
            config_checksum="e" * 64,
            status="qualified",
            created_by=1,
            qualified_at=now,
        )
        session.add(tagger)
        old_fact = TagAssignmentFact(
            tenant_id="chang_an",
            subject_type="dialogue_unit",
            subject_id=101,
            reception_id=1,
            dialogue_unit_id=101,
            tag_key="intent",
            tag_value=False,
            confidence=1.0,
            evidence_refs=[],
            source="imported",
            schema_version_id=None,
            tagger_version_id=None,
            extraction_run_id=None,
            deployment_id=None,
            input_hash="f" * 64,
            recipe_hash="1" * 64,
            revision=1,
            tombstone=False,
            actor_user_id=1,
            assigned_at=now,
        )
        session.add(old_fact)
        await session.flush()
        tagger_id = int(tagger.id)
        schema_version_id = int(schema_version.id)
        old_fact_id = int(old_fact.id)
        session.add(
            TagAssignmentCurrent(
                tenant_id="chang_an",
                subject_type="dialogue_unit",
                subject_id=101,
                tag_key="intent",
                fact_id=old_fact_id,
                revision=1,
            )
        )

    service = TagGovernanceService(factory)
    job = await service.enqueue_job(
        tenant_id="chang_an",
        job_type="extract",
        scope={
            "dialogue_unit_ids": [101],
            "budget": {"max_provider_calls": 1},
        },
        idempotency_key="budget-final-atomic-publish",
        created_by=1,
        tagger_version_id=tagger_id,
    )
    claimed = await service.claim_next_job(
        worker_id="atomic-worker",
        now=now,
        lease_for=timedelta(minutes=1),
    )
    assert claimed is not None
    next_revision = await service.advance_job_progress(
        tenant_id="chang_an",
        job_id=job.id,
        worker_id="atomic-worker",
        expected_revision=claimed.revision,
        success=True,
        item_ref=101,
        now=now,
        lease_for=timedelta(minutes=1),
    )
    assert next_revision is not None
    input_hash = "2" * 64
    async with factory() as session, session.begin():
        run = TagExtractionRun(
            tenant_id="chang_an",
            job_id=job.id,
            origin="system",
            deployment_stage=None,
            deployment_revision=None,
            served_current=True,
            subject_type="dialogue_unit",
            subject_id=101,
            tagger_version_id=tagger_id,
            deployment_id=None,
            input_hash=input_hash,
            input_snapshot={
                "schema_version_id": schema_version_id,
                "target_tag_keys": ["intent"],
            },
            output_snapshot={},
            status="completed",
            started_at=now,
            finished_at=now,
        )
        session.add(run)
        await session.flush()
        staged_fact = TagAssignmentFact(
            tenant_id="chang_an",
            subject_type="dialogue_unit",
            subject_id=101,
            reception_id=1,
            dialogue_unit_id=101,
            tag_key="intent",
            tag_value=True,
            confidence=0.99,
            evidence_refs=[],
            source="llm",
            schema_version_id=schema_version_id,
            tagger_version_id=tagger_id,
            extraction_run_id=run.id,
            deployment_id=None,
            input_hash=input_hash,
            recipe_hash="3" * 64,
            revision=2,
            tombstone=False,
            actor_user_id=1,
            assigned_at=now,
        )
        session.add(staged_fact)
        await session.flush()
        staged_fact_id = int(staged_fact.id)
        run.output_snapshot = {
            "assignments": [{"fact_id": staged_fact_id, "tag_key": "intent"}],
            "publish_current": False,
        }

    published_revision = await service.publish_budgeted_job_current(
        tenant_id="chang_an",
        job_id=job.id,
        worker_id="atomic-worker",
        expected_revision=next_revision,
        actor_user_id=1,
        now=now,
    )
    assert published_revision == next_revision + 1
    async with factory() as session:
        current_fact_id = (
            await session.execute(
                select(TagAssignmentCurrent.fact_id).where(
                    TagAssignmentCurrent.tenant_id == "chang_an",
                    TagAssignmentCurrent.subject_type == "dialogue_unit",
                    TagAssignmentCurrent.subject_id == 101,
                    TagAssignmentCurrent.tag_key == "intent",
                )
            )
        ).scalar_one()
    persisted = await service.get_job(tenant_id="chang_an", job_id=job.id)
    assert current_fact_id == staged_fact_id
    assert current_fact_id != old_fact_id
    assert persisted.current_published_at is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_alert_only_job_collects_usage_without_changing_current_publication() -> None:
    from audio_graphy.services.tag_extractor import ExtractionResult
    from audio_graphy.tag_worker import TagJobWorker

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    class UsageProbeExtractor:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def extract_dialogue_unit(
            self,
            **kwargs: object,
        ) -> ExtractionResult:
            self.calls.append(dict(kwargs))
            return ExtractionResult(
                run_id=1001,
                input_hash="4" * 64,
                input_snapshot={},
                assignments=(),
                cached=False,
                provider_tokens=17,
                provider_calls=2,
                cost_microunits=9,
            )

        async def record_failed_subject(self, **_kwargs: object) -> None:
            return None

    service = TagGovernanceService(factory)
    job = await service.enqueue_job(
        tenant_id="chang_an",
        job_type="extract",
        scope={"dialogue_unit_ids": [101]},
        idempotency_key="alert-only-usage-collection",
        created_by=1,
    )
    extractor = UsageProbeExtractor()
    worker = TagJobWorker(
        factory,
        worker_id="alert-only-worker",
        extractor=extractor,  # type: ignore[arg-type]
    )

    async def resolve_route(_job: TagExtractionJob) -> tuple[int, None]:
        return 1, None

    async def route_decision(**_kwargs: object) -> tuple[bool, bool]:
        return True, True

    worker._resolve_route = resolve_route  # type: ignore[method-assign]
    worker._route_decision = route_decision  # type: ignore[method-assign]

    assert await worker.run_once(now=datetime.now(UTC))
    persisted = await service.get_job(tenant_id="chang_an", job_id=job.id)
    assert persisted.status == "completed"
    assert persisted.budget_source == "alert_only"
    assert persisted.budget_max_provider_tokens is None
    assert persisted.budget_max_provider_calls is None
    assert persisted.budget_consumed_provider_tokens == 17
    assert persisted.budget_consumed_provider_calls == 2
    assert persisted.budget_consumed_cost_microunits == 9
    assert persisted.budget_accounted_items == 1
    assert persisted.budget_usage_complete
    assert persisted.current_published_at is None
    assert extractor.calls[0]["publish_current"] is True
    assert "budget_policy_override" not in extractor.calls[0]
    await engine.dispose()


@pytest.mark.asyncio
async def test_mature_baseline_derives_p99_budget_and_explicit_budget_overrides_it() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        for index in range(1, 101):
            finished_at = now - timedelta(days=8) + timedelta(seconds=index)
            for tenant_id, sample_finished_at, key_prefix in (
                ("chang_an", finished_at, "baseline-complete"),
                (
                    "new_tenant",
                    now - timedelta(days=6) + timedelta(seconds=index),
                    "baseline-recent",
                ),
            ):
                session.add(
                    TagExtractionJob(
                        tenant_id=tenant_id,
                        job_type="extract",
                        origin="system",
                        status="completed",
                        scope={"dialogue_unit_ids": [index]},
                        tagger_version_id=None,
                        idempotency_key=f"{key_prefix}-{index}",
                        total_items=1,
                        completed_items=1,
                        failed_items=0,
                        failed_subset=[],
                        budget_consumed_provider_tokens=index,
                        budget_consumed_provider_calls=index,
                        budget_consumed_cost_microunits=index * 2,
                        budget_started_at=sample_finished_at
                        - timedelta(seconds=index),
                        budget_source="alert_only",
                        budget_purpose="extract",
                        budget_baseline_sample_count=0,
                        budget_accounted_items=1,
                        budget_usage_complete=True,
                        attempt_count=1,
                        max_attempts=3,
                        revision=3,
                        created_by=1,
                        finished_at=sample_finished_at,
                    )
                )

    service = TagGovernanceService(factory)
    alert_only = await service.enqueue_job(
        tenant_id="new_tenant",
        job_type="extract",
        scope={"dialogue_unit_ids": [1000]},
        idempotency_key="recent-baseline-still-alert-only",
        created_by=1,
    )
    assert alert_only.budget_source == "alert_only"
    assert alert_only.budget_baseline_sample_count == 100
    assert alert_only.budget_max_provider_tokens is None
    assert alert_only.budget_max_provider_calls is None

    derived = await service.enqueue_job(
        tenant_id="chang_an",
        job_type="extract",
        scope={"dialogue_unit_ids": [1001]},
        idempotency_key="derived-p99-budget",
        created_by=1,
    )
    assert derived.scope == {"dialogue_unit_ids": [1001]}
    assert derived.budget_source == "default_p99"
    assert derived.budget_baseline_sample_count == 100
    assert derived.budget_max_provider_tokens == 119
    assert derived.budget_max_provider_calls == 119
    assert derived.budget_max_cost_microunits == 238
    assert derived.budget_max_wall_seconds == 119

    explicit_scope = {
        "dialogue_unit_ids": [1002],
        "budget": {
            "max_provider_tokens": 7,
            "max_provider_calls": 3,
            "max_cost_microunits": 11,
            "max_wall_seconds": 13,
        },
    }
    explicit = await service.enqueue_job(
        tenant_id="chang_an",
        job_type="extract",
        scope=explicit_scope,
        idempotency_key="explicit-overrides-p99-budget",
        created_by=1,
    )
    replay = await service.enqueue_job(
        tenant_id="chang_an",
        job_type="extract",
        scope=explicit_scope,
        idempotency_key="explicit-overrides-p99-budget",
        created_by=1,
    )
    assert replay.id == explicit.id
    assert explicit.budget_source == "explicit"
    assert explicit.budget_baseline_sample_count == 0
    assert explicit.budget_max_provider_tokens == 7
    assert explicit.budget_max_provider_calls == 3
    assert explicit.budget_max_cost_microunits == 11
    assert explicit.budget_max_wall_seconds == 13
    await engine.dispose()


@pytest.mark.asyncio
async def test_high_throughput_budget_age_uses_full_history_not_recent_p99_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import audio_graphy.services.tag_governance as governance_module

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(
        governance_module,
        "_JOB_BUDGET_BASELINE_MAX_SAMPLES",
        100,
    )
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        for index in range(101):
            finished_at = (
                now - timedelta(days=8)
                if index == 0
                else now - timedelta(hours=1, seconds=index)
            )
            session.add(
                TagExtractionJob(
                    tenant_id="high_volume",
                    job_type="extract",
                    origin="system",
                    status="completed",
                    scope={"dialogue_unit_ids": [index + 1]},
                    idempotency_key=f"high-volume-baseline-{index}",
                    total_items=1,
                    completed_items=1,
                    failed_items=0,
                    failed_subset=[],
                    budget_consumed_provider_tokens=10,
                    budget_consumed_provider_calls=2,
                    budget_consumed_cost_microunits=5,
                    budget_started_at=finished_at - timedelta(seconds=4),
                    budget_source="alert_only",
                    budget_purpose="extract",
                    budget_baseline_sample_count=0,
                    budget_accounted_items=1,
                    budget_usage_complete=True,
                    attempt_count=1,
                    max_attempts=3,
                    revision=3,
                    created_by=1,
                    finished_at=finished_at,
                )
            )

    derived = await TagGovernanceService(factory).enqueue_job(
        tenant_id="high_volume",
        job_type="extract",
        scope={"dialogue_unit_ids": [999]},
        idempotency_key="high-volume-derived-budget",
        created_by=1,
    )

    assert derived.budget_source == "default_p99"
    assert derived.budget_baseline_sample_count == 101
    assert derived.budget_max_provider_tokens == 12
    await engine.dispose()


@pytest.mark.asyncio
async def test_worker_does_not_invoke_an_unselected_candidate_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from audio_graphy.tag_worker import TagJobWorker

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    worker = TagJobWorker(factory, worker_id="candidate-sampling-worker")
    job = TagExtractionJob(
        tenant_id="chang_an",
        job_type="extract",
        origin="serving",
        status="running",
        scope={"dialogue_unit_ids": [101]},
        idempotency_key="candidate-sampling",
        total_items=1,
        completed_items=0,
        failed_items=0,
        attempt_count=1,
        max_attempts=3,
        revision=1,
        created_by=1,
    )
    extracted_routes: list[tuple[int, int | None, bool]] = []

    async def resolve_route(_job: TagExtractionJob) -> tuple[int, int | None]:
        return 11, None

    async def active_routes(**_kwargs: object) -> list[tuple[int, int]]:
        return [(22, 202)]

    async def route_decision(**kwargs: object) -> tuple[bool, bool]:
        if kwargs["deployment_id"] is None:
            return True, True
        return False, False

    async def extract_route(**kwargs: object) -> None:
        extracted_routes.append(
            (
                int(kwargs["tagger_version_id"]),
                (int(kwargs["deployment_id"]) if kwargs["deployment_id"] is not None else None),
                bool(kwargs["publish_current"]),
            )
        )

    monkeypatch.setattr(worker, "_resolve_route", resolve_route)
    monkeypatch.setattr(worker, "_active_candidate_routes", active_routes)
    monkeypatch.setattr(worker, "_route_decision", route_decision)
    monkeypatch.setattr(worker, "_extract_route", extract_route)

    await worker._process_extraction_item(
        job=job,
        item_kind="dialogue_unit",
        item_id=101,
    )

    assert extracted_routes == [(11, None, True)]
    await engine.dispose()


@pytest.mark.asyncio
async def test_worker_runs_and_stops_deployment_monitor_with_job_loop() -> None:
    from audio_graphy.tag_worker import TagJobWorker

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    class ProbeMonitor:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.stopped = asyncio.Event()
            self.poll_seconds: float | None = None

        async def run_forever(
            self,
            *,
            stop: asyncio.Event,
            poll_seconds: float,
        ) -> None:
            self.poll_seconds = poll_seconds
            self.started.set()
            await stop.wait()
            self.stopped.set()

    monitor = ProbeMonitor()
    worker = TagJobWorker(
        factory,
        worker_id="monitor-lifecycle",
        deployment_monitor=monitor,
        monitor_poll_seconds=0.25,
        poll_seconds=0.01,
    )
    task = asyncio.create_task(worker.run_forever())
    await asyncio.wait_for(monitor.started.wait(), timeout=1)

    worker.stop()
    await asyncio.wait_for(task, timeout=1)

    assert monitor.poll_seconds == pytest.approx(0.25)
    assert monitor.stopped.is_set()
    await engine.dispose()


@pytest.mark.asyncio
async def test_worker_periodically_invokes_idempotent_weekly_optimization_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from audio_graphy.tag_worker import TagJobWorker

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    class ProbeMonitor:
        async def run_forever(
            self,
            *,
            stop: asyncio.Event,
            poll_seconds: float,
        ) -> None:
            del poll_seconds
            await stop.wait()

    worker = TagJobWorker(
        factory,
        worker_id="weekly-optimization-check",
        deployment_monitor=ProbeMonitor(),
        optimization_check_seconds=0.01,
        poll_seconds=0.01,
    )
    invoked = asyncio.Event()
    calls: list[tuple[datetime, int]] = []

    async def run_weekly_check(
        *,
        at: datetime,
        actor_user_id: int,
    ) -> list[object]:
        calls.append((at, actor_user_id))
        invoked.set()
        worker.stop()
        return []

    monkeypatch.setattr(
        worker._service,
        "run_weekly_optimization_checks",
        run_weekly_check,
    )
    task = asyncio.create_task(worker.run_forever())
    await asyncio.wait_for(invoked.wait(), timeout=1)
    await asyncio.wait_for(task, timeout=1)

    assert len(calls) == 1
    assert calls[0][0].tzinfo is not None
    assert calls[0][1] == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_worker_processes_real_review_items_and_completes_job() -> None:
    from audio_graphy.tag_worker import TagJobWorker

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from datetime import timedelta

    from audio_graphy.models.reception import DialogueUnit, Reception
    from audio_graphy.models.tag_governance import TagSchema, TagSchemaVersion

    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        first_reception = Reception(
            tenant_id="chang_an",
            scenario="automotive",
            store_id="S001",
            status="ready",
            merge_mode="logical",
            started_at=now,
            ended_at=now + timedelta(minutes=1),
            version=1,
        )
        second_reception = Reception(
            tenant_id="chang_an",
            scenario="automotive",
            store_id="S001",
            status="ready",
            merge_mode="logical",
            started_at=now,
            ended_at=now + timedelta(minutes=1),
            version=1,
        )
        session.add_all([first_reception, second_reception])
        await session.flush()
        unit = DialogueUnit(
            tenant_id="chang_an",
            reception_id=first_reception.id,
            unit_index=0,
            version=1,
            start_sec=0,
            end_sec=1,
            topic="需求",
            business_stage="需求发现",
            segment_refs=[],
            speaker_refs=[],
            edit_status="auto",
        )
        session.add(unit)
        schema = TagSchema(
            tenant_id="chang_an",
            key="worker-review",
            name="Worker复核",
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
                    "subject_types": ["dialogue_unit"],
                    "scenarios": ["automotive"],
                    "evidence_required": True,
                },
                {
                    "key": "compliance_risk",
                    "value_type": "enum",
                    "allowed_values": ["none"],
                    "subject_types": ["reception"],
                    "scenarios": ["automotive"],
                    "evidence_required": True,
                },
            ],
            checksum="1" * 64,
            status="published",
            created_by=1,
        )
        session.add(version)
        await session.flush()
        unit_id = unit.id
        reception_id = second_reception.id
        schema_version_id = version.id
    service = TagGovernanceService(factory)
    job = await service.enqueue_job(
        tenant_id="chang_an",
        job_type="review_batch",
        scope={
            "reason": "low_confidence",
            "subjects": [
                {
                    "subject_type": "dialogue_unit",
                    "subject_id": unit_id,
                    "tag_key": "intent",
                    "schema_version_id": schema_version_id,
                },
                {
                    "subject_type": "reception",
                    "subject_id": reception_id,
                    "tag_key": "compliance_risk",
                    "schema_version_id": schema_version_id,
                },
            ],
        },
        idempotency_key="worker-review-test",
        created_by=1,
    )

    worker = TagJobWorker(
        factory,
        worker_id="worker-test",
        actor_user_id=1,
    )
    assert await worker.run_once(now=datetime.now(UTC))

    async with factory() as session:
        persisted_job = await session.get(TagExtractionJob, job.id)
        tasks = list((await session.execute(select(TagReviewTask))).scalars().all())
        assert persisted_job is not None
        assert persisted_job.status == "completed"
        assert persisted_job.completed_items == 2
        assert persisted_job.failed_items == 0
        assert len(tasks) == 2
        assert {task.subject_type for task in tasks} == {"dialogue_unit", "reception"}
        assert {task.review_bundle_id for task in tasks} == {None}
        assert {task.selection_policy for task in tasks} == {"legacy"}
        assert {task.selection_policy_version for task in tasks} == {"1"}
        assert {task.sampling_probability for task in tasks} == {None}
        assert not any(task.blind_mode for task in tasks)
    await engine.dispose()


@pytest.mark.asyncio
async def test_worker_advances_every_evaluation_item_without_evaluator_job_writes() -> None:
    from audio_graphy.tag_worker import TagJobWorker

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        job = TagExtractionJob(
            tenant_id="chang_an",
            job_type="evaluate",
            status="queued",
            scope={"evaluation_run_id": 77},
            tagger_version_id=11,
            idempotency_key="worker-evaluation-test",
            total_items=3,
            completed_items=0,
            failed_items=0,
            attempt_count=0,
            max_attempts=3,
            revision=1,
            created_by=1,
        )
        session.add(job)
        await session.flush()
        job_id = job.id

    processed: list[int] = []

    async def process_evaluation(claimed: TagExtractionJob) -> None:
        processed.append(claimed.id)

    worker = TagJobWorker(
        factory,
        worker_id="worker-evaluation",
        actor_user_id=1,
        evaluation_processor=process_evaluation,
    )
    assert await worker.run_once(now=datetime.now(UTC))

    async with factory() as session:
        persisted_job = await session.get(TagExtractionJob, job_id)
        assert persisted_job is not None
        assert persisted_job.status == "completed"
        assert persisted_job.completed_items == 3
        assert persisted_job.failed_items == 0
    assert processed == [job_id]
    await engine.dispose()


@pytest.mark.asyncio
async def test_worker_executes_persistent_optimization_job_and_completes_lease() -> None:
    from audio_graphy.tag_worker import TagJobWorker

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        job = TagExtractionJob(
            tenant_id="chang_an",
            job_type="optimize",
            status="queued",
            scope={"optimization_run_id": 77},
            tagger_version_id=11,
            idempotency_key="worker-optimization-test",
            total_items=1,
            completed_items=0,
            failed_items=0,
            attempt_count=0,
            max_attempts=3,
            revision=1,
            created_by=1,
        )
        session.add(job)
        await session.flush()
        job_id = job.id

    processed: list[int] = []

    async def process_optimization(claimed: TagExtractionJob) -> None:
        processed.append(int(claimed.scope["optimization_run_id"]))

    worker = TagJobWorker(
        factory,
        worker_id="worker-optimization",
        actor_user_id=1,
        optimization_processor=process_optimization,
    )
    assert await worker.run_once(now=datetime.now(UTC))

    async with factory() as session:
        persisted_job = await session.get(TagExtractionJob, job_id)
        assert persisted_job is not None
        assert persisted_job.status == "completed"
        assert persisted_job.completed_items == 1
        assert persisted_job.failed_items == 0
    assert processed == [77]
    await engine.dispose()


@pytest.mark.asyncio
async def test_worker_idempotently_starts_shadow_after_optimizer_evaluation_passes() -> None:
    from audio_graphy.models.tag_governance import (
        TagDeployment,
        TagEvaluationRun,
        TaggerVersion,
        TagSchema,
        TagSchemaVersion,
    )
    from audio_graphy.tag_worker import TagJobWorker

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        schema = TagSchema(
            tenant_id="chang_an",
            key="auto-shadow",
            name="自动灰度",
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
                    "allowed_values": ["purchase", "none"],
                    "subject_types": ["dialogue_unit"],
                }
            ],
            checksum="a" * 64,
            status="published",
            created_by=1,
        )
        session.add(schema_version)
        await session.flush()
        baseline = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=schema_version.id,
            version="baseline",
            engine="hybrid",
            prompt_content="baseline prompt",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="weak",
            thresholds={},
            config_checksum="b" * 64,
            status="qualified",
            created_by=1,
        )
        candidate = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=schema_version.id,
            version="candidate",
            engine="hybrid",
            prompt_content="candidate prompt",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="weak",
            thresholds={},
            parent_version_id=None,
            origin="optimizer",
            optimization_run_id=77,
            config_checksum="c" * 64,
            status="qualified",
            created_by=1,
            qualified_at=now,
        )
        session.add_all([baseline, candidate])
        await session.flush()
        baseline_evaluation = TagEvaluationRun(
            tenant_id="chang_an",
            tagger_version_id=baseline.id,
            baseline_tagger_version_id=baseline.id,
            gold_set_version_id=1,
            evaluator_version="tag-evaluator-v2",
            dataset_snapshot_hash="e" * 64,
            status="completed",
            metrics={
                "evaluation_lane": "holdout",
                "sealed_release": True,
            },
            baseline_metrics={},
            passed=True,
            started_at=now,
            finished_at=now,
            created_by=1,
        )
        session.add(baseline_evaluation)
        await session.flush()
        session.add(
            TagDeployment(
                tenant_id="chang_an",
                tagger_version_id=baseline.id,
                evaluation_run_id=baseline_evaluation.id,
                baseline_tagger_version_id=baseline.id,
                status="production",
                traffic_percent=100,
                revision=1,
                created_by=1,
                approved_by=1,
                approved_at=now,
            )
        )
        evaluation = TagEvaluationRun(
            tenant_id="chang_an",
            tagger_version_id=candidate.id,
            baseline_tagger_version_id=baseline.id,
            gold_set_version_id=1,
            evaluator_version="tag-evaluator-v2",
            dataset_snapshot_hash="d" * 64,
            status="completed",
            metrics={
                "evaluation_lane": "holdout",
                "sealed_release": True,
            },
            baseline_metrics={},
            passed=True,
            started_at=now,
            finished_at=now,
            created_by=1,
        )
        session.add(evaluation)
        await session.flush()
        evaluation_id = evaluation.id

    worker = TagJobWorker(factory, worker_id="auto-shadow", actor_user_id=1)
    await worker._ensure_optimizer_shadow(
        tenant_id="chang_an",
        evaluation_run_id=evaluation_id,
    )
    await worker._ensure_optimizer_shadow(
        tenant_id="chang_an",
        evaluation_run_id=evaluation_id,
    )

    async with factory() as session:
        deployments = list(
            (
                await session.execute(
                    select(TagDeployment).where(TagDeployment.evaluation_run_id == evaluation_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(deployments) == 1
    assert deployments[0].status == "shadow"
    assert deployments[0].traffic_percent == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_real_extractor_keeps_gold_and_automotive_scenarios_isolated() -> None:
    from datetime import timedelta

    from audio_graphy.models.reception import (
        DialogueUnit,
        Reception,
        ReceptionRecording,
    )
    from audio_graphy.models.recording import Recording
    from audio_graphy.models.segment import Segment
    from audio_graphy.models.tag_governance import (
        TagDeployment,
        TagEvaluationRun,
        TaggerVersion,
        TagSchema,
        TagSchemaVersion,
    )
    from audio_graphy.tag_worker import TagJobWorker

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    scenario_inputs = [
        (
            "gold",
            "我想试戴这款18K项链，预算三万元，今天可以购买。",
            ["gold.try_on", "gold.budget", "gold.purchase"],
        ),
        (
            "automotive",
            "我想试驾SUV车型，请给我一份落地报价。",
            ["auto.test_drive", "auto.model", "auto.quote"],
        ),
    ]
    async with factory() as session, session.begin():
        unit_ids: list[int] = []
        segment_by_unit: dict[int, int] = {}
        for index, (scenario, transcript, _expected) in enumerate(scenario_inputs):
            recording = Recording(
                tenant_id="chang_an",
                store_id=f"S{index + 1:03d}",
                agent_name=f"agent-{scenario}",
                path=f"/tmp/{scenario}.wav",
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
                end_sec=8,
                transcript=transcript,
                text_scrubbed=transcript,
                speaker="customer",
                vad_conf=0.99,
            )
            reception = Reception(
                tenant_id="chang_an",
                scenario=scenario,
                store_id=recording.store_id,
                agent_name=recording.agent_name,
                status="ready",
                merge_mode="logical",
                started_at=now,
                ended_at=now + timedelta(seconds=8),
                version=1,
            )
            session.add_all([segment, reception])
            await session.flush()
            session.add(
                ReceptionRecording(
                    tenant_id="chang_an",
                    reception_id=reception.id,
                    recording_id=recording.id,
                    sequence_no=0,
                    timeline_start_sec=0,
                    timeline_end_sec=8,
                    source_start_sec=0,
                    source_end_sec=8,
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
                end_sec=8,
                topic="需求",
                business_stage="需求发现",
                segment_refs=[
                    {
                        "segment_id": segment.id,
                        "recording_id": recording.id,
                        "source_start_sec": 0,
                        "source_end_sec": 8,
                        "timeline_start_sec": 0,
                        "timeline_end_sec": 8,
                    }
                ],
                speaker_refs=["customer"],
                edit_status="auto",
            )
            session.add(unit)
            await session.flush()
            unit_ids.append(unit.id)
            segment_by_unit[unit.id] = segment.id
        schema = TagSchema(
            tenant_id="chang_an",
            key="sales-scenarios",
            name="销售场景标签",
            status="published",
            created_by=1,
        )
        session.add(schema)
        await session.flush()
        definitions = [
            {
                "key": key,
                "name": key,
                "category": scenario,
                "value_type": "boolean",
                "allowed_values": [],
                "subject_types": ["dialogue_unit"],
                "scenarios": [scenario],
                "evidence_required": True,
            }
            for scenario, _transcript, keys in scenario_inputs
            for key in keys
        ]
        version = TagSchemaVersion(
            tenant_id="chang_an",
            schema_id=schema.id,
            version="1",
            definitions=definitions,
            checksum="c" * 64,
            status="published",
            created_by=1,
            published_by=1,
            published_at=now,
        )
        session.add(version)
        await session.flush()
        rules = [
            ("gold.try_on", "试戴"),
            ("gold.budget", "预算"),
            ("gold.purchase", "购买"),
            ("auto.test_drive", "试驾"),
            ("auto.model", "SUV"),
            ("auto.quote", "报价"),
        ]
        tagger = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=version.id,
            version="scenario-rules-v1",
            engine="rule",
            prompt_content="",
            rule_bundle={
                "dsl_version": "1",
                "rules": [
                    {
                        "tag_key": key,
                        "value": True,
                        "contains_any": [token],
                        "confidence": 0.98,
                    }
                    for key, token in rules
                ],
            },
            model_version="rules-v1",
            thresholds={"default": 0.7},
            config_checksum="d" * 64,
            status="qualified",
            created_by=1,
            qualified_at=now,
        )
        candidate = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=version.id,
            version="scenario-rules-shadow-v2",
            engine="rule",
            prompt_content="",
            rule_bundle=tagger.rule_bundle,
            model_version="rules-v2",
            thresholds={"default": 0.7},
            config_checksum="b" * 64,
            status="qualified",
            created_by=1,
            qualified_at=now,
        )
        session.add_all([tagger, candidate])
        await session.flush()
        evaluation = TagEvaluationRun(
            tenant_id="chang_an",
            tagger_version_id=candidate.id,
            baseline_tagger_version_id=tagger.id,
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
            baseline_tagger_version_id=tagger.id,
            status="shadow",
            traffic_percent=0,
            revision=1,
            created_by=1,
        )
        session.add(deployment)
        await session.flush()
        tagger_id = tagger.id
        candidate_id = candidate.id
        deployment_id = deployment.id

    service = TagGovernanceService(factory)
    job = await service.enqueue_job(
        tenant_id="chang_an",
        job_type="extract",
        scope={"dialogue_unit_ids": unit_ids},
        idempotency_key="gold-auto-real-extraction",
        created_by=1,
        tagger_version_id=None,
        origin="serving",
    )
    worker = TagJobWorker(
        factory,
        worker_id="scenario-worker",
        actor_user_id=1,
        shadow_sample_percent=100,
    )
    async with factory() as session, session.begin():
        completed_shadow = await session.get(TagDeployment, deployment_id)
        assert completed_shadow is not None
        completed_shadow.sampling_complete_at = now
    for unit_id in unit_ids:
        assert await worker._route_decision(
            tenant_id="chang_an",
            dialogue_unit_id=unit_id,
            deployment_id=deployment_id,
        ) == (False, False)
    async with factory() as session, session.begin():
        active_shadow = await session.get(TagDeployment, deployment_id)
        assert active_shadow is not None
        active_shadow.sampling_complete_at = None

    assert await worker.run_once(now=datetime.now(UTC))

    async with factory() as session:
        facts = list(
            (
                await session.execute(
                    select(TagAssignmentFact).order_by(
                        TagAssignmentFact.subject_id,
                        TagAssignmentFact.tag_key,
                    )
                )
            )
            .scalars()
            .all()
        )
        currents = list((await session.execute(select(TagAssignmentCurrent))).scalars().all())
        persisted_job = await session.get(TagExtractionJob, job.id)
    assert persisted_job is not None and persisted_job.status == "completed"
    assert len(facts) == 12
    assert len(currents) == 6
    expected_by_unit = {
        unit_id: set(scenario_inputs[index][2]) for index, unit_id in enumerate(unit_ids)
    }
    for unit_id in unit_ids:
        unit_facts = [fact for fact in facts if fact.subject_id == unit_id]
        assert {fact.tag_key for fact in unit_facts} == expected_by_unit[unit_id]
        assert {fact.tagger_version_id for fact in unit_facts} == {
            tagger_id,
            candidate_id,
        }
        assert {
            fact.deployment_id for fact in unit_facts if fact.tagger_version_id == candidate_id
        } == {deployment_id}
        assert {
            fact.deployment_id for fact in unit_facts if fact.tagger_version_id == tagger_id
        } == {None}
        for fact in unit_facts:
            assert len(fact.evidence_refs) == 1
            evidence = fact.evidence_refs[0]
            assert evidence["segment_id"] == segment_by_unit[unit_id]
            assert evidence["ref_id"] == f"segment:{segment_by_unit[unit_id]}"
            assert evidence["kind"] == "audio_segment"
            assert evidence["text_excerpt"]
            assert evidence["source_start_ms"] == 0
            assert evidence["source_end_ms"] == 8_000
    current_fact_ids = {current.fact_id for current in currents}
    assert {fact.tagger_version_id for fact in facts if fact.id in current_fact_ids} == {tagger_id}
    await engine.dispose()


@pytest.mark.asyncio
async def test_retry_processes_only_failed_subset_and_resets_attempt_budget() -> None:
    from audio_graphy.models.tag_governance import (
        TaggerVersion,
        TagSchema,
        TagSchemaVersion,
    )
    from audio_graphy.tag_worker import TagJobWorker

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        schema = TagSchema(
            tenant_id="chang_an",
            key="retry",
            name="重试",
            status="published",
            created_by=1,
        )
        session.add(schema)
        await session.flush()
        version = TagSchemaVersion(
            tenant_id="chang_an",
            schema_id=schema.id,
            version="1",
            definitions=[],
            checksum="e" * 64,
            status="published",
            created_by=1,
        )
        session.add(version)
        await session.flush()
        tagger = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=version.id,
            version="retry-rules",
            engine="rule",
            prompt_content="",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="rules",
            thresholds={},
            config_checksum="f" * 64,
            status="qualified",
            created_by=1,
            qualified_at=now,
        )
        session.add(tagger)
        await session.flush()
        tagger_id = tagger.id

    class FlakyExtractor:
        def __init__(self) -> None:
            self.calls: list[int] = []
            self.failed_once = False

        async def extract_dialogue_unit(self, **kwargs: object) -> None:
            item = int(kwargs["dialogue_unit_id"])
            self.calls.append(item)
            if item == 1 and not self.failed_once:
                self.failed_once = True
                raise RuntimeError("transient")

        async def record_failed_subject(self, **_kwargs: object) -> None:
            return None

    service = TagGovernanceService(factory)
    job = await service.enqueue_job(
        tenant_id="chang_an",
        job_type="extract",
        scope={"dialogue_unit_ids": [1, 2, 3]},
        idempotency_key="failed-subset-retry",
        created_by=1,
        tagger_version_id=tagger_id,
    )
    extractor = FlakyExtractor()
    worker = TagJobWorker(
        factory,
        worker_id="retry-worker",
        extractor=extractor,  # type: ignore[arg-type]
    )
    assert await worker.run_once(now=now)
    failed = await service.get_job(tenant_id="chang_an", job_id=job.id)
    assert failed.status == "failed"
    assert failed.completed_items == 2
    assert failed.failed_items == 1
    assert failed.failed_subset == [1]

    retried = await service.retry_job(
        tenant_id="chang_an",
        job_id=job.id,
        actor_user_id=1,
    )
    assert retried.status == "queued"
    assert retried.completed_items == 2
    assert retried.failed_items == 0
    assert retried.failed_subset == [1]
    assert retried.attempt_count == 0
    assert await worker.run_once(now=datetime.now(UTC))

    completed = await service.get_job(tenant_id="chang_an", job_id=job.id)
    assert completed.status == "completed"
    assert completed.completed_items == 3
    assert completed.failed_items == 0
    assert completed.failed_subset == []
    assert extractor.calls == [1, 2, 3, 1]
    await engine.dispose()
