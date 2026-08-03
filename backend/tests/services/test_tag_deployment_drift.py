"""Distribution-drift tests for candidate/baseline deployment monitoring."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from audio_graphy.models import Base
from audio_graphy.models.reception import DialogueUnit, Reception
from audio_graphy.models.tag_governance import (
    TagAssignmentCurrent,
    TagAssignmentFact,
    TagDeployment,
    TagEvaluationRun,
    TagExtractionJob,
    TagExtractionRun,
    TaggerVersion,
    TagGoldSet,
    TagGoldSetVersion,
    TagHarnessExecution,
    TagSchema,
    TagSchemaVersion,
)


@pytest.fixture
async def drift_factory() -> async_sessionmaker[AsyncSession]:
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


async def _seed_paired_release(
    factory: async_sessionmaker[AsyncSession],
    *,
    window_end: datetime,
    sample_count: int,
    baseline_value: str,
    candidate_value: str,
    baseline_scene_scenario: str = "automotive",
    candidate_scene_scenario: str = "automotive",
) -> dict[str, Any]:
    """Persist real same-input baseline/candidate runs without current pointers."""

    window_start = window_end - timedelta(minutes=5)
    async with factory() as session, session.begin():
        schema = TagSchema(
            tenant_id="tenant-a",
            key=f"drift-{sample_count}-{candidate_value}",
            name="漂移检测标签",
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
                    "evidence_required": False,
                    "subject_types": ["dialogue_unit"],
                    "scenarios": ["automotive"],
                    "critical": False,
                }
            ],
            checksum="a" * 64,
            status="published",
            created_by=1,
            published_by=1,
            published_at=window_start,
        )
        session.add(schema_version)
        await session.flush()
        schema.active_version_id = schema_version.id
        baseline = TaggerVersion(
            tenant_id="tenant-a",
            schema_version_id=schema_version.id,
            version="baseline-v1",
            engine="rule",
            prompt_content="",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="rules-v0",
            thresholds={"intent": 0.7},
            config_checksum="b" * 64,
            status="qualified",
            created_by=1,
            qualified_at=window_start,
        )
        candidate = TaggerVersion(
            tenant_id="tenant-a",
            schema_version_id=schema_version.id,
            version="candidate-v1",
            engine="rule",
            prompt_content="",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="rules-v1",
            thresholds={"intent": 0.7},
            config_checksum="c" * 64,
            status="qualified",
            created_by=1,
            qualified_at=window_start,
        )
        session.add_all([baseline, candidate])
        await session.flush()
        gold_set = TagGoldSet(
            tenant_id="tenant-a",
            key="gold",
            name="黄金集",
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
            checksum="d" * 64,
            item_count=30,
            frozen_by=1,
            frozen_at=window_start,
        )
        session.add(gold_version)
        await session.flush()
        evaluation = TagEvaluationRun(
            tenant_id="tenant-a",
            tagger_version_id=candidate.id,
            baseline_tagger_version_id=baseline.id,
            gold_set_version_id=gold_version.id,
            status="completed",
            metrics={"macro_f1": 0.9, "critical_recall": 0.97},
            baseline_metrics={},
            passed=True,
            started_at=window_start,
            finished_at=window_start,
            created_by=1,
        )
        session.add(evaluation)
        await session.flush()
        deployment = TagDeployment(
            tenant_id="tenant-a",
            tagger_version_id=candidate.id,
            evaluation_run_id=evaluation.id,
            baseline_tagger_version_id=baseline.id,
            status="shadow",
            traffic_percent=0,
            revision=1,
            created_by=1,
        )
        session.add(deployment)
        await session.flush()
        job = TagExtractionJob(
            tenant_id="tenant-a",
            job_type="extract",
            origin="serving",
            status="completed",
            scope={"dialogue_unit_ids": []},
            tagger_version_id=baseline.id,
            idempotency_key=f"drift-{window_end.isoformat()}-{sample_count}",
            total_items=sample_count,
            completed_items=sample_count,
            failed_items=0,
            attempt_count=1,
            max_attempts=3,
            revision=1,
            created_by=1,
            finished_at=window_start + timedelta(minutes=4),
        )
        session.add(job)
        await session.flush()

        candidate_fact_ids: list[int] = []
        reception_ids: list[int] = []
        unit_ids: list[int] = []
        for index in range(sample_count):
            reception = Reception(
                tenant_id="tenant-a",
                scenario="automotive",
                store_id=f"DRIFT-{index}",
                status="ready",
                merge_mode="logical",
                started_at=window_start,
                ended_at=window_start + timedelta(seconds=2),
                version=1,
            )
            session.add(reception)
            await session.flush()
            unit = DialogueUnit(
                tenant_id="tenant-a",
                reception_id=reception.id,
                source_recording_id=None,
                unit_index=0,
                version=1,
                start_sec=0,
                end_sec=2,
                topic="意向",
                business_stage="需求发现",
                segment_refs=[],
                speaker_refs=["customer"],
                edit_status="auto",
            )
            session.add(unit)
            await session.flush()
            stable_snapshot = {
                "dialogue_unit_id": unit.id,
                "dialogue_unit_version": unit.version,
                "reception_id": reception.id,
                "scenario": reception.scenario,
                "segments": [],
                "transcript": f"同一份规范化输入-{index}",
                "schema_version_id": schema_version.id,
                "schema_checksum": schema_version.checksum,
            }
            baseline_run = TagExtractionRun(
                tenant_id="tenant-a",
                job_id=job.id,
                origin="serving",
                subject_type="dialogue_unit",
                subject_id=unit.id,
                tagger_version_id=baseline.id,
                deployment_id=None,
                served_current=True,
                input_hash=f"{index + 1:064x}",
                input_snapshot={
                    **stable_snapshot,
                    "tagger_version_id": baseline.id,
                    "tagger_checksum": baseline.config_checksum,
                    "model_version": baseline.model_version,
                },
                output_snapshot={},
                status="completed",
                started_at=window_start + timedelta(minutes=1),
                finished_at=window_start + timedelta(minutes=2),
            )
            candidate_run = TagExtractionRun(
                tenant_id="tenant-a",
                job_id=job.id,
                origin="serving",
                subject_type="dialogue_unit",
                subject_id=unit.id,
                tagger_version_id=candidate.id,
                deployment_id=deployment.id,
                deployment_stage="shadow",
                deployment_revision=1,
                served_current=False,
                input_hash=f"{index + 10_001:064x}",
                input_snapshot={
                    **stable_snapshot,
                    "tagger_version_id": candidate.id,
                    "tagger_checksum": candidate.config_checksum,
                    "model_version": candidate.model_version,
                },
                output_snapshot={},
                status="completed",
                started_at=window_start + timedelta(minutes=2),
                finished_at=window_start + timedelta(minutes=3),
            )
            session.add_all([baseline_run, candidate_run])
            await session.flush()
            historical_baseline_run = TagExtractionRun(
                tenant_id="tenant-a",
                job_id=job.id,
                origin="serving",
                subject_type="dialogue_unit",
                subject_id=unit.id,
                tagger_version_id=baseline.id,
                deployment_id=None,
                served_current=True,
                input_hash=f"{index + 20_001:064x}",
                input_snapshot={
                    **stable_snapshot,
                    "scenario": baseline_scene_scenario,
                    "transcript": f"历史基线输入-{index}",
                    "tagger_version_id": baseline.id,
                    "tagger_checksum": baseline.config_checksum,
                    "model_version": baseline.model_version,
                },
                output_snapshot={},
                status="completed",
                started_at=window_start - timedelta(hours=1, minutes=2),
                finished_at=window_start - timedelta(hours=1),
            )
            session.add(historical_baseline_run)
            await session.flush()
            profile_template = {
                "subject_type": "dialogue_unit",
                "duration_sec": 2.0,
                "segment_count": 1,
                "speaker_count": 1,
                "average_vad_confidence": 0.9,
                "transcript_char_count": 12,
                "snr": None,
                "overlap_ratio": None,
                "asr_confidence": None,
                "diarization_confidence": None,
            }
            session.add_all(
                [
                    TagHarnessExecution(
                        tenant_id="tenant-a",
                        extraction_run_id=historical_baseline_run.id,
                        tagger_version_id=baseline.id,
                        deployment_id=None,
                        subject_type="dialogue_unit",
                        subject_id=unit.id,
                        input_hash=historical_baseline_run.input_hash,
                        scene_profile={
                            **profile_template,
                            "scenario": baseline_scene_scenario,
                        },
                        resolved_harness_spec={},
                        route="rule_only",
                        status="completed",
                        output_snapshot={},
                        latency_ms=1,
                        token_count=0,
                        cost_units=0,
                        started_at=historical_baseline_run.started_at,
                        finished_at=historical_baseline_run.finished_at,
                    ),
                    TagHarnessExecution(
                        tenant_id="tenant-a",
                        extraction_run_id=candidate_run.id,
                        tagger_version_id=candidate.id,
                        deployment_id=deployment.id,
                        subject_type="dialogue_unit",
                        subject_id=unit.id,
                        input_hash=candidate_run.input_hash,
                        scene_profile={
                            **profile_template,
                            "scenario": candidate_scene_scenario,
                        },
                        resolved_harness_spec={},
                        route="rule_only",
                        status="completed",
                        output_snapshot={},
                        latency_ms=1,
                        token_count=0,
                        cost_units=0,
                        started_at=candidate_run.started_at,
                        finished_at=candidate_run.finished_at,
                    ),
                ]
            )
            baseline_fact = TagAssignmentFact(
                tenant_id="tenant-a",
                subject_type="dialogue_unit",
                subject_id=unit.id,
                reception_id=reception.id,
                dialogue_unit_id=unit.id,
                tag_key="intent",
                tag_value=baseline_value,
                confidence=0.95,
                evidence_refs=[],
                source="rule",
                schema_version_id=schema_version.id,
                tagger_version_id=baseline.id,
                extraction_run_id=baseline_run.id,
                deployment_id=None,
                input_hash=baseline_run.input_hash,
                revision=1,
                tombstone=False,
                actor_user_id=0,
                assigned_at=window_start + timedelta(minutes=2),
            )
            candidate_fact = TagAssignmentFact(
                tenant_id="tenant-a",
                subject_type="dialogue_unit",
                subject_id=unit.id,
                reception_id=reception.id,
                dialogue_unit_id=unit.id,
                tag_key="intent",
                tag_value=candidate_value,
                confidence=0.95,
                evidence_refs=[],
                source="rule",
                schema_version_id=schema_version.id,
                tagger_version_id=candidate.id,
                extraction_run_id=candidate_run.id,
                deployment_id=deployment.id,
                input_hash=candidate_run.input_hash,
                revision=2,
                tombstone=False,
                actor_user_id=0,
                assigned_at=window_start + timedelta(minutes=3),
            )
            session.add_all([baseline_fact, candidate_fact])
            await session.flush()
            baseline_run.output_snapshot = {
                "assignments": [{"tag_key": "intent", "fact_id": baseline_fact.id}]
            }
            candidate_run.output_snapshot = {
                "assignments": [{"tag_key": "intent", "fact_id": candidate_fact.id}]
            }
            candidate_fact_ids.append(candidate_fact.id)
            reception_ids.append(reception.id)
            unit_ids.append(unit.id)

        # Deliberately corrupt tenant lineage to prove the monitor never admits it
        # into tenant-a distributions or review work.
        cross_tenant_fact = TagAssignmentFact(
            tenant_id="tenant-b",
            subject_type="dialogue_unit",
            subject_id=9_999_999,
            reception_id=None,
            dialogue_unit_id=9_999_999,
            tag_key="intent",
            tag_value="browse",
            confidence=0.99,
            evidence_refs=[],
            source="rule",
            schema_version_id=schema_version.id,
            tagger_version_id=candidate.id,
            extraction_run_id=None,
            deployment_id=deployment.id,
            input_hash="f" * 64,
            revision=1,
            tombstone=False,
            actor_user_id=0,
            assigned_at=window_start + timedelta(minutes=3),
        )
        session.add(cross_tenant_fact)
        await session.flush()
        return {
            "deployment_id": deployment.id,
            "candidate_fact_ids": candidate_fact_ids,
            "reception_ids": reception_ids,
            "unit_ids": unit_ids,
        }


def test_jensen_shannon_divergence_is_symmetric_bounded_and_explainable() -> None:
    from audio_graphy.services.tag_deployment_monitor import (
        jensen_shannon_divergence,
        population_stability_index,
    )

    assert jensen_shannon_divergence(Counter({"a": 10}), Counter({"a": 10})) == 0
    assert jensen_shannon_divergence(Counter({"a": 10}), Counter({"b": 10})) == pytest.approx(1)
    left = jensen_shannon_divergence(Counter({"a": 8, "b": 2}), Counter({"a": 3, "b": 7}))
    right = jensen_shannon_divergence(Counter({"a": 3, "b": 7}), Counter({"a": 8, "b": 2}))
    assert 0 < left < 1
    assert left == pytest.approx(right)
    assert population_stability_index(Counter({"a": 10}), Counter({"a": 10})) == 0
    shifted_psi = population_stability_index(
        Counter({"a": 10}),
        Counter({"b": 10}),
    )
    assert shifted_psi > 0.2
    assert shifted_psi == pytest.approx(
        population_stability_index(Counter({"b": 10}), Counter({"a": 10}))
    )


def test_drift_policy_keeps_subject_domains_isolated() -> None:
    from audio_graphy.services.tag_governance import (
        _evaluate_drift_policy,
        _PolicyObservation,
    )

    window_end = datetime(2026, 7, 25, 16, 0, tzinfo=UTC)
    samples = []
    for index in range(24):
        start = window_end - timedelta(hours=2) + timedelta(minutes=5 * index)
        samples.append(
            _PolicyObservation(
                window_start=start,
                window_end=start + timedelta(minutes=5),
                metrics={
                    "drift_by_tag": {
                        "dialogue_unit:intent": {
                            "candidate_distribution": [
                                {
                                    "value": "browse",
                                    "missing": False,
                                    "count": 300,
                                }
                            ],
                            "baseline_distribution": [
                                {
                                    "value": "browse",
                                    "missing": False,
                                    "count": 300,
                                }
                            ],
                        },
                        "reception:intent": {
                            "candidate_distribution": [
                                {
                                    "value": "purchase",
                                    "missing": False,
                                    "count": 30,
                                }
                            ],
                            "baseline_distribution": [
                                {
                                    "value": "browse",
                                    "missing": False,
                                    "count": 30,
                                }
                            ],
                        },
                    },
                    "input_drift_by_feature": {},
                },
            )
        )

    policy = _evaluate_drift_policy(samples, window_end=window_end)

    assert policy["complete"] is True
    assert policy["consecutive_breach"] is True
    assert policy["affected_domains"] == ["reception:intent"]
    assert policy["affected_tags"] == ["reception:intent"]
    assert policy["windows"][0]["domains"]["dialogue_unit:intent"]["breached"] is False
    assert policy["windows"][0]["domains"]["reception:intent"]["breached"] is True


@pytest.mark.asyncio
async def test_drift_requires_minimum_same_input_sample_support(
    drift_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_deployment_monitor import TagDeploymentMonitor

    window_end = datetime(2026, 7, 25, 14, 5, tzinfo=UTC)
    seeded = await _seed_paired_release(
        drift_factory,
        window_end=window_end,
        sample_count=29,
        baseline_value="browse",
        candidate_value="purchase",
    )

    health = await TagDeploymentMonitor(drift_factory).collect_window(
        tenant_id="tenant-a",
        deployment_id=seeded["deployment_id"],
        window_start=window_end - timedelta(minutes=5),
        window_end=window_end,
    )

    assert health is not None
    assert health.metrics["drift_paired_sample_count"] == 29
    assert health.metrics["fact_count"] == 29
    assert health.served_subject_keys == ()
    assert set(health.paired_subject_keys) == {
        ("dialogue_unit", unit_id) for unit_id in seeded["unit_ids"]
    }
    assert health.metrics["drift_max_jsd"] == pytest.approx(1)
    assert health.metrics["output_jsd"] == pytest.approx(1)
    assert health.metrics["drift_by_tag"]["dialogue_unit:intent"]["eligible"] is False
    assert health.metrics["input_psi"] == 0
    assert health.metrics["drift_eligible_tag_count"] == 0
    assert health.metrics["drift_affected_tags"] == []
    assert "drift" not in health.breach_codes


@pytest.mark.asyncio
async def test_input_drift_signal_remains_visible_below_minimum_support(
    drift_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_deployment_monitor import TagDeploymentMonitor

    window_end = datetime(2026, 7, 25, 14, 7, tzinfo=UTC)
    seeded = await _seed_paired_release(
        drift_factory,
        window_end=window_end,
        sample_count=29,
        baseline_value="purchase",
        candidate_value="purchase",
        baseline_scene_scenario="jewelry",
        candidate_scene_scenario="automotive",
    )

    health = await TagDeploymentMonitor(drift_factory).collect_window(
        tenant_id="tenant-a",
        deployment_id=seeded["deployment_id"],
        window_start=window_end - timedelta(minutes=5),
        window_end=window_end,
    )

    assert health is not None
    assert health.metrics["output_jsd"] == 0
    assert health.metrics["input_psi"] > health.metrics["drift_psi_threshold"]
    assert health.metrics["drift_max_psi"] == health.metrics["input_psi"]
    assert health.metrics["input_drift_eligible_feature_count"] == 0
    assert health.metrics["input_drift_affected_domains"] == []
    scenario = health.metrics["input_drift_by_feature"]["dialogue_unit:@input:scenario"]
    assert scenario["candidate_sample_count"] == 29
    assert scenario["reference_sample_count"] == 29
    assert scenario["eligible"] is False
    assert scenario["breached"] is False
    assert "drift" not in health.breach_codes


@pytest.mark.asyncio
async def test_input_drift_reference_below_thirty_is_ineligible(
    drift_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_deployment_monitor import TagDeploymentMonitor

    window_end = datetime(2026, 7, 25, 14, 7, tzinfo=UTC)
    seeded = await _seed_paired_release(
        drift_factory,
        window_end=window_end,
        sample_count=29,
        baseline_value="purchase",
        candidate_value="purchase",
        baseline_scene_scenario="jewelry",
        candidate_scene_scenario="automotive",
    )

    health = await TagDeploymentMonitor(drift_factory).collect_window(
        tenant_id="tenant-a",
        deployment_id=seeded["deployment_id"],
        window_start=window_end - timedelta(minutes=5),
        window_end=window_end,
    )

    assert health is not None
    feature = health.metrics["input_drift_by_feature"]["dialogue_unit:@input:scenario"]
    assert feature["candidate_sample_count"] == 29
    assert feature["reference_sample_count"] == 29
    assert feature["psi"] > health.metrics["drift_psi_threshold"]
    assert feature["eligible"] is False
    assert feature["breached"] is False
    assert health.metrics["input_drift_affected_domains"] == []
    assert "drift" not in health.breach_codes


@pytest.mark.asyncio
async def test_drift_excludes_nearby_runs_when_business_input_differs(
    drift_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_deployment_monitor import TagDeploymentMonitor

    window_end = datetime(2026, 7, 25, 14, 10, tzinfo=UTC)
    seeded = await _seed_paired_release(
        drift_factory,
        window_end=window_end,
        sample_count=30,
        baseline_value="browse",
        candidate_value="purchase",
    )
    async with drift_factory() as session, session.begin():
        deployment = await session.get(TagDeployment, seeded["deployment_id"])
        assert deployment is not None
        baseline_run = (
            await session.execute(
                select(TagExtractionRun)
                .where(
                    TagExtractionRun.tenant_id == "tenant-a",
                    TagExtractionRun.tagger_version_id == deployment.baseline_tagger_version_id,
                )
                .order_by(TagExtractionRun.id)
                .limit(1)
            )
        ).scalar_one()
        baseline_run.input_snapshot = {
            **baseline_run.input_snapshot,
            "transcript": "并非候选版本读取的同一份输入",
        }

    health = await TagDeploymentMonitor(drift_factory).collect_window(
        tenant_id="tenant-a",
        deployment_id=seeded["deployment_id"],
        window_start=window_end - timedelta(minutes=5),
        window_end=window_end,
    )

    assert health is not None
    assert health.metrics["drift_paired_sample_count"] == 29
    assert health.metrics["drift_eligible_tag_count"] == 0
    assert "drift" not in health.breach_codes


@pytest.mark.asyncio
async def test_stable_same_input_distribution_does_not_pause_release(
    drift_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_deployment_monitor import TagDeploymentMonitor

    window_end = datetime(2026, 7, 25, 14, 15, tzinfo=UTC)
    await _seed_paired_release(
        drift_factory,
        window_end=window_end,
        sample_count=30,
        baseline_value="purchase",
        candidate_value="purchase",
    )

    results = await TagDeploymentMonitor(drift_factory).run_once(
        now=window_end + timedelta(seconds=1),
        tenant_id="tenant-a",
    )

    assert len(results) == 1
    assert results[0].observation.metrics["drift_paired_sample_count"] == 30
    assert results[0].observation.metrics["drift_max_jsd"] == 0
    assert results[0].observation.metrics["drift_max_psi"] == 0
    assert results[0].observation.metrics["output_jsd"] == 0
    assert results[0].observation.metrics["input_psi"] == 0
    assert results[0].observation.metrics["drift_affected_tags"] == []
    assert "drift" not in results[0].observation.breach_codes
    assert results[0].observation.action == "observe"
    assert results[0].deployment.promotion_paused is False


@pytest.mark.asyncio
@pytest.mark.parametrize("break_trusted_continuity", [False, True])
async def test_real_drift_pauses_and_reviews_non_current_candidate_facts(
    drift_factory: async_sessionmaker[AsyncSession],
    break_trusted_continuity: bool,
) -> None:
    from audio_graphy.services.tag_deployment_monitor import TagDeploymentMonitor

    window_end = datetime(2026, 7, 25, 15, 0, tzinfo=UTC)
    seeded = await _seed_paired_release(
        drift_factory,
        window_end=window_end,
        sample_count=30,
        baseline_value="browse",
        candidate_value="purchase",
    )
    monitor = TagDeploymentMonitor(drift_factory)
    final_health = await monitor.collect_window(
        tenant_id="tenant-a",
        deployment_id=seeded["deployment_id"],
        window_start=window_end - timedelta(minutes=5),
        window_end=window_end,
    )
    assert final_health is not None
    from audio_graphy.services.tag_governance import TagGovernanceService

    governance = TagGovernanceService(drift_factory)
    policy_start = window_end - timedelta(hours=2)
    for index in range(23):
        start = policy_start + timedelta(minutes=5 * index)
        observation, deployment = await governance.record_deployment_observation(
            tenant_id="tenant-a",
            deployment_id=seeded["deployment_id"],
            sample_reception_ids=[],
            metrics=final_health.metrics,
            breach_codes=["drift"],
            window_start=start,
            window_end=start + timedelta(minutes=5),
            actor_user_id=0,
            source="monitor",
            is_trusted=not (break_trusted_continuity and index == 11),
        )
        assert observation.action == "observe"
        assert deployment.promotion_paused is False

    results = await monitor.run_once(
        now=window_end + timedelta(seconds=1),
        tenant_id="tenant-a",
    )

    assert len(results) == 1
    result = results[0]
    assert result.observation.metrics["drift_paired_sample_count"] == 30
    assert result.observation.metrics["drift_max_jsd"] == pytest.approx(1)
    assert result.observation.metrics["drift_max_psi"] == 0
    assert result.observation.metrics["output_jsd"] == pytest.approx(1)
    assert result.observation.metrics["input_psi"] == 0
    assert result.observation.metrics["drift_affected_tags"] == ["dialogue_unit:intent"]
    assert result.observation.metrics["drift_affected_domains"] == ["dialogue_unit:intent"]
    assert result.observation.metrics["drift_by_tag"]["dialogue_unit:intent"]["sample_count"] == 30
    assert "psi" not in result.observation.metrics["drift_by_tag"]["dialogue_unit:intent"]
    assert result.observation.breach_codes == ["drift"]
    if break_trusted_continuity:
        assert result.observation.metrics["drift_policy"]["complete"] is False
        assert result.observation.metrics["drift_policy"]["consecutive_breach"] is False
        assert result.observation.action == "observe"
        assert result.deployment.promotion_paused is False
        async with drift_factory() as session:
            review_count = int(
                (
                    await session.execute(
                        select(func.count(TagExtractionJob.id)).where(
                            TagExtractionJob.tenant_id == "tenant-a",
                            TagExtractionJob.job_type == "review_batch",
                        )
                    )
                ).scalar_one()
            )
        assert review_count == 0
        return
    assert result.observation.metrics["drift_policy"]["complete"] is True
    assert result.observation.metrics["drift_policy"]["consecutive_breach"] is True
    assert result.observation.action == "pause"
    assert result.deployment.status == "shadow"
    assert result.deployment.promotion_paused is True

    async with drift_factory() as session:
        current_count = (
            await session.execute(select(func.count(TagAssignmentCurrent.id)))
        ).scalar_one()
        review_job = (
            await session.execute(
                select(TagExtractionJob).where(
                    TagExtractionJob.tenant_id == "tenant-a",
                    TagExtractionJob.job_type == "review_batch",
                )
            )
        ).scalar_one()
    assert current_count == 0
    assert review_job.total_items == 30
    assert review_job.scope["selection_policy"] == "drift_audit"
    assert review_job.scope["selection_policy_version"] == "1"
    assert review_job.scope["sampling_probability"] == pytest.approx(1)
    assert review_job.scope["blind_mode"] is True
    reviewed_fact_ids = {int(item["proposed_fact_id"]) for item in review_job.scope["subjects"]}
    assert reviewed_fact_ids == set(seeded["candidate_fact_ids"])
    assert all(item["tag_key"] == "intent" for item in review_job.scope["subjects"])


@pytest.mark.asyncio
async def test_input_shift_with_stable_output_still_pauses_and_reviews_domain(
    drift_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_deployment_monitor import TagDeploymentMonitor
    from audio_graphy.services.tag_governance import TagGovernanceService

    window_end = datetime(2026, 7, 25, 17, 0, tzinfo=UTC)
    seeded = await _seed_paired_release(
        drift_factory,
        window_end=window_end,
        sample_count=30,
        baseline_value="purchase",
        candidate_value="purchase",
        baseline_scene_scenario="jewelry",
        candidate_scene_scenario="automotive",
    )
    monitor = TagDeploymentMonitor(drift_factory)
    health = await monitor.collect_window(
        tenant_id="tenant-a",
        deployment_id=seeded["deployment_id"],
        window_start=window_end - timedelta(minutes=5),
        window_end=window_end,
    )

    assert health is not None
    assert health.metrics["output_jsd"] == 0
    assert health.metrics["input_psi"] > health.metrics["drift_psi_threshold"]
    assert health.metrics["drift_affected_tags"] == []
    assert health.metrics["input_drift_affected_domains"] == ["dialogue_unit:@input:scenario"]
    assert health.metrics["drift_affected_domains"] == ["dialogue_unit:@input:scenario"]
    assert health.breach_codes == ("drift",)

    governance = TagGovernanceService(drift_factory)
    policy_start = window_end - timedelta(hours=2)
    for index in range(23):
        start = policy_start + timedelta(minutes=5 * index)
        observation, deployment = await governance.record_deployment_observation(
            tenant_id="tenant-a",
            deployment_id=seeded["deployment_id"],
            sample_reception_ids=[],
            metrics=health.metrics,
            breach_codes=["drift"],
            window_start=start,
            window_end=start + timedelta(minutes=5),
            actor_user_id=0,
            source="monitor",
            is_trusted=True,
        )
        assert observation.action == "observe"
        assert deployment.promotion_paused is False

    results = await monitor.run_once(
        now=window_end + timedelta(seconds=1),
        tenant_id="tenant-a",
    )

    assert len(results) == 1
    result = results[0]
    assert result.observation.metrics["drift_policy"]["complete"] is True
    assert result.observation.metrics["drift_policy"]["consecutive_breach"] is True
    assert result.observation.metrics["drift_policy"]["affected_domains"] == [
        "dialogue_unit:@input:scenario"
    ]
    assert result.observation.action == "pause"
    assert result.deployment.status == "shadow"
    assert result.deployment.promotion_paused is True
    async with drift_factory() as session:
        review_job = (
            await session.execute(
                select(TagExtractionJob).where(
                    TagExtractionJob.tenant_id == "tenant-a",
                    TagExtractionJob.job_type == "review_batch",
                )
            )
        ).scalar_one()
    assert review_job.total_items == 30
    assert all(item["subject_type"] == "dialogue_unit" for item in review_job.scope["subjects"])
    assert all(item["tag_key"] == "intent" for item in review_job.scope["subjects"])


@pytest.mark.asyncio
async def test_observation_is_discarded_when_deployment_changes_after_collection(
    drift_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.models.tag_governance import TagDeploymentObservation
    from audio_graphy.services.tag_deployment_monitor import TagDeploymentMonitor
    from audio_graphy.services.tag_governance import (
        GovernanceStaleObservationError,
        TagGovernanceService,
    )

    window_end = datetime(2026, 7, 25, 14, 35, tzinfo=UTC)
    seeded = await _seed_paired_release(
        drift_factory,
        window_end=window_end,
        sample_count=1,
        baseline_value="browse",
        candidate_value="purchase",
    )
    health = await TagDeploymentMonitor(drift_factory).collect_window(
        tenant_id="tenant-a",
        deployment_id=seeded["deployment_id"],
        window_start=window_end - timedelta(minutes=5),
        window_end=window_end,
    )
    assert health is not None

    async with drift_factory() as session, session.begin():
        deployment = await session.get(TagDeployment, seeded["deployment_id"])
        assert deployment is not None
        deployment.status = "canary_5"
        deployment.traffic_percent = 5
        deployment.revision += 1

    with pytest.raises(GovernanceStaleObservationError, match="changed"):
        await TagGovernanceService(drift_factory).record_deployment_observation(
            tenant_id="tenant-a",
            deployment_id=seeded["deployment_id"],
            sample_reception_ids=list(health.reception_ids),
            metrics=health.metrics,
            breach_codes=list(health.breach_codes),
            window_start=health.window_start,
            window_end=health.window_end,
            actor_user_id=0,
            review_fact_ids=list(health.review_fact_ids),
            expected_stage=health.observed_stage,
            expected_revision=health.observed_revision,
        )

    async with drift_factory() as session:
        observation_count = (
            await session.execute(select(func.count(TagDeploymentObservation.id)))
        ).scalar_one()
        review_count = (
            await session.execute(
                select(func.count(TagExtractionJob.id)).where(
                    TagExtractionJob.job_type == "review_batch"
                )
            )
        ).scalar_one()
    assert observation_count == 0
    assert review_count == 0
