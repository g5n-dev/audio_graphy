"""Regression tests for rollout/baseline serialization."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from audio_graphy.models import Base
from audio_graphy.models.tag_governance import (
    TagDeployment,
    TagEvaluationRun,
    TaggerVersion,
    TagGoldSet,
    TagGoldSetVersion,
    TagSchema,
    TagSchemaVersion,
)
from audio_graphy.services.tag_governance import (
    GovernanceConflictError,
    TagGovernanceService,
)


@pytest.fixture
async def baseline_factory() -> async_sessionmaker[AsyncSession]:
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
async def test_rolling_back_production_atomically_invalidates_bound_child_release(
    baseline_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with baseline_factory() as session, session.begin():
        schema = TagSchema(
            tenant_id="tenant-a",
            key="baseline-race",
            name="baseline-race",
            status="published",
            created_by=1,
        )
        session.add(schema)
        await session.flush()
        schema_version = TagSchemaVersion(
            tenant_id="tenant-a",
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
            checksum="1" * 64,
            status="published",
            created_by=1,
            published_by=1,
            published_at=now,
        )
        session.add(schema_version)
        await session.flush()
        schema.active_version_id = schema_version.id
        taggers = [
            TaggerVersion(
                tenant_id="tenant-a",
                schema_version_id=schema_version.id,
                version=version,
                engine="rule",
                prompt_content="",
                rule_bundle={"dsl_version": "1", "rules": []},
                model_version=version,
                thresholds={"intent": 0.7},
                config_checksum=checksum * 64,
                status="qualified",
                created_by=1,
                qualified_at=now,
            )
            for version, checksum in (("baseline-b", "2"), ("production-a", "3"), ("child-c", "4"))
        ]
        session.add_all(taggers)
        await session.flush()
        baseline_b, production_a, child_c = taggers
        gold_set = TagGoldSet(
            tenant_id="tenant-a",
            key="release",
            name="release",
            schema_version_id=schema_version.id,
            created_by=1,
        )
        session.add(gold_set)
        await session.flush()
        gold_version = TagGoldSetVersion(
            tenant_id="tenant-a",
            gold_set_id=gold_set.id,
            version="1",
            status="frozen",
            checksum="5" * 64,
            dataset_snapshot_hash="5" * 64,
            item_count=30,
            frozen_by=1,
            frozen_at=now,
        )
        session.add(gold_version)
        await session.flush()

        def evaluation(candidate_id: int, baseline_id: int) -> TagEvaluationRun:
            return TagEvaluationRun(
                tenant_id="tenant-a",
                tagger_version_id=candidate_id,
                baseline_tagger_version_id=baseline_id,
                gold_set_version_id=gold_version.id,
                evaluator_version="tag-evaluator-v2",
                dataset_snapshot_hash="5" * 64,
                status="completed",
                metrics={"evaluation_lane": "holdout", "sealed_release": True},
                baseline_metrics={},
                passed=True,
                started_at=now,
                finished_at=now,
                created_by=1,
            )

        evaluation_b = evaluation(baseline_b.id, baseline_b.id)
        evaluation_a = evaluation(production_a.id, baseline_b.id)
        evaluation_c = evaluation(child_c.id, production_a.id)
        session.add_all([evaluation_b, evaluation_a, evaluation_c])
        await session.flush()
        deployment_b = TagDeployment(
            tenant_id="tenant-a",
            tagger_version_id=baseline_b.id,
            evaluation_run_id=evaluation_b.id,
            baseline_tagger_version_id=baseline_b.id,
            status="retired",
            traffic_percent=0,
            revision=2,
            created_by=1,
        )
        deployment_a = TagDeployment(
            tenant_id="tenant-a",
            tagger_version_id=production_a.id,
            evaluation_run_id=evaluation_a.id,
            baseline_tagger_version_id=baseline_b.id,
            status="production",
            traffic_percent=100,
            revision=3,
            created_by=1,
        )
        deployment_c = TagDeployment(
            tenant_id="tenant-a",
            tagger_version_id=child_c.id,
            evaluation_run_id=evaluation_c.id,
            baseline_tagger_version_id=production_a.id,
            status="awaiting_admin",
            traffic_percent=25,
            revision=4,
            created_by=1,
        )
        session.add_all([deployment_b, deployment_a, deployment_c])
        await session.flush()
        deployment_a_id = int(deployment_a.id)
        deployment_c_id = int(deployment_c.id)

    service = TagGovernanceService(baseline_factory)
    rolled_back = await service.transition_deployment(
        tenant_id="tenant-a",
        deployment_id=deployment_a_id,
        action="rollback",
        actor_user_id=9,
        expected_revision=3,
        reason="production health gate",
    )
    assert rolled_back.status == "rolled_back"

    with pytest.raises(GovernanceConflictError):
        await service.transition_deployment(
            tenant_id="tenant-a",
            deployment_id=deployment_c_id,
            action="approve",
            actor_user_id=9,
            expected_revision=4,
        )

    async with baseline_factory() as session:
        deployments = list(
            (await session.execute(select(TagDeployment).order_by(TagDeployment.id)))
            .scalars()
            .all()
        )
        production_count = int(
            (
                await session.execute(
                    select(func.count(TagDeployment.id)).where(TagDeployment.status == "production")
                )
            ).scalar_one()
        )
    by_id = {int(item.id): item for item in deployments}
    assert by_id[deployment_c_id].status == "rolled_back"
    assert by_id[deployment_c_id].rollback_reason == f"stale_baseline:{deployment_a_id}"
    assert production_count == 1
    assert next(item for item in deployments if item.status == "production").tagger_version_id == (
        by_id[deployment_a_id].baseline_tagger_version_id
    )
