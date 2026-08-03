"""Automatic release-observation tests for the tag-governance loop."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from audio_graphy.models import Base
from audio_graphy.models.reception import DialogueUnit, Reception, ReceptionRecording
from audio_graphy.models.recording import Recording
from audio_graphy.models.segment import Segment
from audio_graphy.models.tag_governance import (
    TagAssignmentCurrent,
    TagAssignmentFact,
    TagDeployment,
    TagDeploymentAuditSubject,
    TagDeploymentObservation,
    TagDeploymentObservationSample,
    TagEvaluationRun,
    TagExtractionJob,
    TagExtractionRun,
    TagFeedbackEvent,
    TaggerVersion,
    TagGoldSet,
    TagGoldSetVersion,
    TagReviewDecision,
    TagReviewTask,
    TagSchema,
    TagSchemaVersion,
)
from audio_graphy.services.tag_governance import review_sampling_manifest_checksum


@pytest.fixture
async def monitor_factory() -> async_sessionmaker[AsyncSession]:
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


async def _add_certified_representative_truth(
    session: AsyncSession,
    *,
    deployment_id: int,
    schema_version_id: int,
    tagger_version_id: int,
    extraction_run_id: int,
    subject_id: int,
    reception_id: int,
    proposed_fact_id: int | None,
    proposed_value: Any,
    truth_value: Any,
    evidence_refs: list[dict[str, Any]],
    sampling_probability: float,
    occurred_at: datetime,
    bundle_id: str,
    reviewer_ids: tuple[int, int, int],
    tag_key: str = "intent",
    subject_type: str = "dialogue_unit",
    truth_state: str = "present",
    error_stage: str | None = None,
) -> None:
    manifest = review_sampling_manifest_checksum(
        deployment_id=deployment_id,
        deployment_stage="canary_5",
        deployment_revision=1,
        extraction_run_id=extraction_run_id,
        subject_type=subject_type,
        subject_id=subject_id,
        tag_key=tag_key,
        selection_policy="representative_audit",
        selection_policy_version="1",
        sampling_probability=sampling_probability,
    )
    for round_number, reviewer_id in enumerate(reviewer_ids[:2], start=1):
        task = TagReviewTask(
            tenant_id="tenant-a",
            batch_id=f"{bundle_id}-round-{round_number}",
            review_bundle_id=bundle_id,
            subject_type=subject_type,
            subject_id=subject_id,
            reception_id=reception_id,
            tag_key=tag_key,
            proposed_value=proposed_value,
            confidence=0.95,
            evidence_refs=evidence_refs,
            proposed_fact_id=proposed_fact_id,
            schema_version_id=schema_version_id,
            tagger_version_id=tagger_version_id,
            selection_policy="representative_audit",
            selection_policy_version="1",
            sampling_probability=sampling_probability,
            blind_mode=True,
            source_deployment_id=deployment_id,
            source_extraction_run_id=extraction_run_id,
            sampled_deployment_stage="canary_5",
            sampled_deployment_revision=1,
            sampling_manifest_checksum=manifest,
            reason="random",
            status="resolved",
            priority=100,
            claimed_by=reviewer_id,
            claimed_at=occurred_at - timedelta(seconds=30),
            resolved_at=occurred_at,
            created_by=0,
        )
        session.add(task)
        await session.flush()
        session.add(
            TagReviewDecision(
                tenant_id="tenant-a",
                task_id=task.id,
                action=("correct" if truth_state == "present" else "reject"),
                corrected_value=(truth_value if truth_state == "present" else None),
                reason_code="independent_label",
                evidence_refs=evidence_refs,
                resulting_fact_id=None,
                reviewer_user_id=reviewer_id,
                adjudication=False,
                truth_state=truth_state,
                truth_tier="t2",
                annotator_round=round_number,
                decided_at=occurred_at,
            )
        )
    adjudicator_id = reviewer_ids[2]
    adjudication_task = TagReviewTask(
        tenant_id="tenant-a",
        batch_id=f"{bundle_id}-adjudication",
        review_bundle_id=bundle_id,
        subject_type=subject_type,
        subject_id=subject_id,
        reception_id=reception_id,
        tag_key=tag_key,
        proposed_value=proposed_value,
        confidence=0.95,
        evidence_refs=evidence_refs,
        proposed_fact_id=proposed_fact_id,
        schema_version_id=schema_version_id,
        tagger_version_id=tagger_version_id,
        selection_policy="representative_audit",
        selection_policy_version="1",
        sampling_probability=sampling_probability,
        blind_mode=True,
        source_deployment_id=deployment_id,
        source_extraction_run_id=extraction_run_id,
        sampled_deployment_stage="canary_5",
        sampled_deployment_revision=1,
        sampling_manifest_checksum=manifest,
        reason="adjudication",
        status="resolved",
        priority=100,
        claimed_by=adjudicator_id,
        claimed_at=occurred_at,
        resolved_at=occurred_at + timedelta(seconds=1),
        created_by=reviewer_ids[1],
    )
    session.add(adjudication_task)
    await session.flush()
    adjudication = TagReviewDecision(
        tenant_id="tenant-a",
        task_id=adjudication_task.id,
        action=("correct" if truth_state == "present" else "reject"),
        corrected_value=(truth_value if truth_state == "present" else None),
        reason_code="adjudicated",
        evidence_refs=evidence_refs,
        resulting_fact_id=None,
        reviewer_user_id=adjudicator_id,
        adjudication=True,
        truth_state=truth_state,
        truth_tier="t3",
        annotator_round=3,
        decided_at=occurred_at + timedelta(seconds=1),
    )
    session.add(adjudication)
    await session.flush()
    session.add(
        TagFeedbackEvent(
            tenant_id="tenant-a",
            review_decision_id=adjudication.id,
            deployment_id=deployment_id,
            source="human",
            truth_tier="t3",
            subject_type=subject_type,
            subject_id=subject_id,
            tag_key=tag_key,
            truth_state=truth_state,
            correction={
                "action": ("correct" if truth_state == "present" else "reject"),
                "corrected_value": (truth_value if truth_state == "present" else None),
            },
            payload={"blind_mode": True, "adjudication": True, "annotator_round": 3},
            error_stage=error_stage,
            training_eligible=True,
            selection_policy="representative_audit",
            sampling_probability=sampling_probability,
            occurred_at=occurred_at + timedelta(seconds=1),
        )
    )


async def _seed_release(
    factory: async_sessionmaker[AsyncSession],
    *,
    window_end: datetime,
    include_review: bool = True,
    review_action: str = "accept",
    corrected_value: Any = None,
    proposed_value: Any = "purchase",
    representative_truth: bool = False,
    representative_error_stage: str | None = None,
    extra_definitions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Seed one active candidate and two receptions in its completed window."""

    window_start = window_end - timedelta(minutes=5)
    async with factory() as session, session.begin():
        schema = TagSchema(
            tenant_id="tenant-a",
            key="sales",
            name="销售标签",
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
                    "evidence_required": True,
                    "subject_types": ["dialogue_unit"],
                    "scenarios": ["automotive"],
                    "critical": False,
                    "critical_values": ["purchase"],
                },
                *(extra_definitions or []),
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
        candidate = TaggerVersion(
            tenant_id="tenant-a",
            schema_version_id=schema_version.id,
            version="candidate-v1",
            engine="rule",
            prompt_content="",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="rules-v1",
            thresholds={"intent": 0.7},
            config_checksum="b" * 64,
            status="qualified",
            created_by=1,
            qualified_at=window_start,
        )
        baseline = TaggerVersion(
            tenant_id="tenant-a",
            schema_version_id=schema_version.id,
            version="baseline-v1",
            engine="rule",
            prompt_content="",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="rules-v0",
            thresholds={"intent": 0.7},
            config_checksum="f" * 64,
            status="qualified",
            created_by=1,
            qualified_at=window_start,
        )
        session.add_all([candidate, baseline])
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
            checksum="c" * 64,
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
            status="canary_5",
            traffic_percent=5,
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
            scope={"reception_ids": []},
            tagger_version_id=candidate.id,
            idempotency_key=f"monitor-seed-{window_end.isoformat()}",
            total_items=2,
            completed_items=1,
            failed_items=1,
            attempt_count=1,
            max_attempts=3,
            revision=1,
            created_by=1,
            finished_at=window_start + timedelta(minutes=3),
        )
        session.add(job)
        await session.flush()

        contexts: list[tuple[Reception, DialogueUnit, Segment]] = []
        for index in range(2):
            recording = Recording(
                tenant_id="tenant-a",
                store_id=f"S{index}",
                agent_name="agent",
                path=f"/tmp/monitor-{index}.wav",
                status="indexed",
                pipeline_state="done",
                recorded_at=window_start,
            )
            session.add(recording)
            await session.flush()
            segment = Segment(
                tenant_id="tenant-a",
                recording_id=recording.id,
                idx=0,
                start_sec=0,
                end_sec=2,
                transcript="客户决定购买",
                text_scrubbed="客户决定购买",
                speaker="customer",
                vad_conf=0.99,
            )
            session.add(segment)
            reception = Reception(
                tenant_id="tenant-a",
                scenario="automotive",
                store_id=f"S{index}",
                status="ready",
                merge_mode="logical",
                started_at=window_start,
                ended_at=window_start + timedelta(seconds=2),
                version=1,
            )
            session.add(reception)
            await session.flush()
            session.add(
                ReceptionRecording(
                    tenant_id="tenant-a",
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
                tenant_id="tenant-a",
                reception_id=reception.id,
                source_recording_id=recording.id,
                unit_index=0,
                version=1,
                start_sec=0,
                end_sec=2,
                topic="成交",
                business_stage="成交意向",
                segment_refs=[{"segment_id": segment.id, "recording_id": recording.id}],
                speaker_refs=["customer"],
                edit_status="auto",
            )
            session.add(unit)
            await session.flush()
            contexts.append((reception, unit, segment))

        completed_run = TagExtractionRun(
            tenant_id="tenant-a",
            job_id=job.id,
            origin="serving",
            subject_type="dialogue_unit",
            subject_id=contexts[0][1].id,
            tagger_version_id=candidate.id,
            deployment_id=deployment.id,
            deployment_stage="canary_5",
            deployment_revision=1,
            served_current=True,
            input_hash="d" * 64,
            input_snapshot={},
            output_snapshot={},
            status="completed",
            started_at=window_start + timedelta(minutes=1),
            finished_at=window_start + timedelta(minutes=2),
        )
        failed_run = TagExtractionRun(
            tenant_id="tenant-a",
            job_id=job.id,
            origin="serving",
            subject_type="dialogue_unit",
            subject_id=contexts[1][1].id,
            tagger_version_id=candidate.id,
            deployment_id=deployment.id,
            deployment_stage="canary_5",
            deployment_revision=1,
            served_current=True,
            input_hash="e" * 64,
            input_snapshot={},
            output_snapshot={},
            status="failed",
            error_code="ModelTimeout",
            error_message="timeout",
            started_at=window_start + timedelta(minutes=1),
            finished_at=window_start + timedelta(minutes=2),
        )
        session.add_all([completed_run, failed_run])
        await session.flush()
        fact = TagAssignmentFact(
            tenant_id="tenant-a",
            subject_type="dialogue_unit",
            subject_id=contexts[0][1].id,
            reception_id=contexts[0][0].id,
            dialogue_unit_id=contexts[0][1].id,
            tag_key="intent",
            tag_value=proposed_value,
            confidence=0.95,
            evidence_refs=[
                {
                    "segment_id": contexts[0][2].id,
                    "start_sec": 0,
                    "end_sec": 2,
                }
            ],
            source="rule",
            schema_version_id=schema_version.id,
            tagger_version_id=candidate.id,
            extraction_run_id=completed_run.id,
            deployment_id=deployment.id,
            input_hash=completed_run.input_hash,
            revision=1,
            tombstone=False,
            actor_user_id=0,
            assigned_at=window_start + timedelta(minutes=2),
        )
        session.add(fact)
        await session.flush()
        session.add(
            TagAssignmentCurrent(
                tenant_id="tenant-a",
                subject_type="dialogue_unit",
                subject_id=contexts[0][1].id,
                tag_key="intent",
                fact_id=fact.id,
                revision=1,
            )
        )
        if include_review and representative_truth:
            truth_value = corrected_value if review_action == "correct" else proposed_value
            await _add_certified_representative_truth(
                session,
                deployment_id=deployment.id,
                schema_version_id=schema_version.id,
                tagger_version_id=candidate.id,
                extraction_run_id=completed_run.id,
                subject_id=contexts[0][1].id,
                reception_id=contexts[0][0].id,
                proposed_fact_id=fact.id,
                proposed_value=proposed_value,
                truth_value=truth_value,
                evidence_refs=list(fact.evidence_refs),
                sampling_probability=0.05,
                occurred_at=window_start + timedelta(minutes=3),
                bundle_id="representative-primary",
                reviewer_ids=(2, 3, 4),
                error_stage=representative_error_stage,
            )
        elif include_review:
            review = TagReviewTask(
                tenant_id="tenant-a",
                batch_id="critical-review",
                subject_type="dialogue_unit",
                subject_id=contexts[0][1].id,
                reception_id=contexts[0][0].id,
                tag_key="intent",
                proposed_value=proposed_value,
                confidence=0.95,
                evidence_refs=fact.evidence_refs,
                proposed_fact_id=fact.id,
                schema_version_id=schema_version.id,
                tagger_version_id=candidate.id,
                selection_policy="critical_positive",
                sampling_probability=None,
                blind_mode=True,
                source_deployment_id=deployment.id,
                source_extraction_run_id=completed_run.id,
                sampled_deployment_stage="canary_5",
                sampled_deployment_revision=1,
                sampling_manifest_checksum=None,
                reason="critical",
                status="resolved",
                priority=100,
                claimed_by=2,
                claimed_at=window_start + timedelta(minutes=2),
                resolved_at=window_start + timedelta(minutes=3),
                created_by=0,
            )
            session.add(review)
            await session.flush()
            decision = TagReviewDecision(
                tenant_id="tenant-a",
                task_id=review.id,
                action=review_action,
                corrected_value=corrected_value,
                reason_code="verified",
                evidence_refs=fact.evidence_refs,
                resulting_fact_id=fact.id,
                reviewer_user_id=2,
                adjudication=False,
                truth_state="present",
                truth_tier="t1",
                decided_at=window_start + timedelta(minutes=3),
            )
            session.add(decision)
        await session.flush()
        return {
            "deployment_id": deployment.id,
            "schema_version_id": schema_version.id,
            "tagger_version_id": candidate.id,
            "completed_run_id": completed_run.id,
            "failed_run_id": failed_run.id,
            "fact_id": fact.id,
            "evidence_refs": list(fact.evidence_refs),
            "subject_evidence_refs": [
                [{"segment_id": context[2].id, "start_sec": 0, "end_sec": 2}]
                for context in contexts
            ],
            "job_id": job.id,
            "unit_ids": [context[1].id for context in contexts],
            "reception_ids": [context[0].id for context in contexts],
        }


def test_completed_window_uses_last_fully_closed_five_minute_bucket() -> None:
    from audio_graphy.services.tag_deployment_monitor import completed_monitor_window

    now = datetime(2026, 7, 25, 10, 7, 42, tzinfo=UTC)
    start, end = completed_monitor_window(now)

    assert start == datetime(2026, 7, 25, 10, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 25, 10, 5, tzinfo=UTC)


@pytest.mark.asyncio
async def test_periodic_monitor_rejects_non_positive_poll_interval(
    monitor_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_deployment_monitor import TagDeploymentMonitor

    with pytest.raises(ValueError, match="positive"):
        await TagDeploymentMonitor(monitor_factory).run_forever(
            stop=asyncio.Event(),
            poll_seconds=0,
        )


@pytest.mark.asyncio
async def test_monitor_aggregates_real_runs_evidence_and_human_truth_idempotently(
    monitor_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_deployment_monitor import TagDeploymentMonitor

    window_end = datetime(2026, 7, 25, 10, 5, tzinfo=UTC)
    seeded = await _seed_release(
        monitor_factory,
        window_end=window_end,
        representative_truth=True,
    )
    monitor = TagDeploymentMonitor(monitor_factory, actor_user_id=0)

    first = await monitor.run_once(now=window_end + timedelta(seconds=10))
    replay = await monitor.run_once(now=window_end + timedelta(minutes=1))

    assert len(first) == 1
    assert len(replay) == 1
    assert first[0].observation.id == replay[0].observation.id
    assert first[0].observation.deployment_id == seeded["deployment_id"]
    assert first[0].observation.sample_count == 2
    assert first[0].observation.source == "monitor"
    assert first[0].observation.is_trusted is True
    assert first[0].observation.served_count == 2
    assert first[0].observation.paired_count == 0
    assert first[0].observation.metrics["run_count"] == 2
    assert first[0].observation.metrics["failed_run_count"] == 1
    assert first[0].observation.metrics["error_rate"] == pytest.approx(0.5)
    assert first[0].observation.metrics["critical_recall"] == pytest.approx(1)
    assert first[0].observation.metrics["review_truth_count"] == 1
    assert first[0].observation.metrics["representative_audit_ipw_population"] == pytest.approx(20)
    assert first[0].observation.metrics[
        "representative_audit_effective_sample_size"
    ] == pytest.approx(1)
    assert first[0].observation.breach_codes == ["error_rate"]

    async with monitor_factory() as session:
        observation_count = (
            await session.execute(
                select(func.count(TagDeploymentObservation.id)).where(
                    TagDeploymentObservation.deployment_id == seeded["deployment_id"]
                )
            )
        ).scalar_one()
    assert observation_count == 1


@pytest.mark.asyncio
async def test_monitor_counts_unbiased_audits_by_distinct_subject(
    monitor_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_deployment_monitor import TagDeploymentMonitor

    window_end = datetime(2026, 7, 25, 10, 15, tzinfo=UTC)
    seeded = await _seed_release(
        monitor_factory,
        window_end=window_end,
        representative_truth=True,
    )
    async with monitor_factory() as session, session.begin():
        await _add_certified_representative_truth(
            session,
            deployment_id=seeded["deployment_id"],
            schema_version_id=seeded["schema_version_id"],
            tagger_version_id=seeded["tagger_version_id"],
            extraction_run_id=seeded["failed_run_id"],
            subject_id=seeded["unit_ids"][1],
            reception_id=seeded["reception_ids"][1],
            proposed_fact_id=None,
            proposed_value="browse",
            truth_value="purchase",
            evidence_refs=seeded["subject_evidence_refs"][1],
            sampling_probability=0.5,
            occurred_at=window_end - timedelta(minutes=1),
            bundle_id="representative-secondary",
            reviewer_ids=(5, 6, 7),
        )
        # A raw row with self-asserted policy metadata has no review lineage and
        # must never contribute to trusted release counters.
        session.add(
            TagFeedbackEvent(
                tenant_id="tenant-a",
                deployment_id=seeded["deployment_id"],
                source="human",
                truth_tier="t3",
                subject_type="dialogue_unit",
                subject_id=999_999,
                tag_key="intent",
                truth_state="present",
                correction="purchase",
                payload={},
                training_eligible=True,
                selection_policy="representative_random",
                sampling_probability=0.05,
                occurred_at=window_end - timedelta(minutes=1),
            )
        )
        session.add(
            TagFeedbackEvent(
                tenant_id="tenant-a",
                deployment_id=seeded["deployment_id"],
                source="human",
                truth_tier="t2",
                subject_type="dialogue_unit",
                subject_id=seeded["unit_ids"][1],
                tag_key="intent",
                truth_state="present",
                correction="purchase",
                payload={},
                training_eligible=True,
                selection_policy="representative_random",
                sampling_probability=0.05,
                occurred_at=window_end - timedelta(minutes=1),
            )
        )

    result = await TagDeploymentMonitor(monitor_factory).run_once(
        now=window_end + timedelta(seconds=1)
    )

    assert result[0].observation.audited_count == 2
    assert result[0].observation.adjudicated_count == 2
    assert result[0].observation.metrics["representative_audit_ipw_population"] == pytest.approx(22)
    assert result[0].observation.metrics[
        "representative_audit_effective_sample_size"
    ] == pytest.approx(484 / 404)
    assert result[0].observation.metrics["critical_recall_ipw"] == pytest.approx(20 / 22)
    assert result[0].observation.metrics["critical_recall_effective_sample_size"] == pytest.approx(
        484 / 404
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("secondary_truth_state", ["absent", "not_applicable"])
async def test_representative_audit_counts_only_after_complete_applicable_matrix(
    monitor_factory: async_sessionmaker[AsyncSession],
    secondary_truth_state: str,
) -> None:
    from audio_graphy.services.tag_deployment_monitor import TagDeploymentMonitor

    first_window_end = datetime(2026, 7, 25, 10, 25, tzinfo=UTC)
    seeded = await _seed_release(
        monitor_factory,
        window_end=first_window_end,
        representative_truth=True,
        extra_definitions=[
            {
                "key": "budget",
                "value_type": "enum",
                "allowed_values": ["known"],
                "evidence_required": False,
                "subject_types": ["dialogue_unit"],
                "scenarios": ["automotive"],
            }
        ],
    )
    monitor = TagDeploymentMonitor(monitor_factory)

    incomplete = await monitor.run_once(now=first_window_end + timedelta(seconds=1))
    assert incomplete[0].observation.audited_count == 0
    assert incomplete[0].observation.metrics["representative_audit_subject_count_by_type"] == {}
    assert "critical_recall" not in incomplete[0].observation.metrics

    second_window_end = first_window_end + timedelta(minutes=5)
    async with monitor_factory() as session, session.begin():
        await _add_certified_representative_truth(
            session,
            deployment_id=seeded["deployment_id"],
            schema_version_id=seeded["schema_version_id"],
            tagger_version_id=seeded["tagger_version_id"],
            extraction_run_id=seeded["completed_run_id"],
            subject_id=seeded["unit_ids"][0],
            reception_id=seeded["reception_ids"][0],
            proposed_fact_id=None,
            proposed_value=None,
            truth_value=None,
            evidence_refs=[],
            sampling_probability=0.05,
            occurred_at=second_window_end - timedelta(minutes=1),
            bundle_id="representative-primary",
            reviewer_ids=(5, 6, 7),
            tag_key="budget",
            truth_state=secondary_truth_state,
        )

    complete = await monitor.run_once(now=second_window_end + timedelta(seconds=1))
    assert complete[0].observation.audited_count == 1
    assert complete[0].observation.metrics["representative_audit_subject_count_by_type"] == {
        "dialogue_unit": 1
    }
    assert complete[0].observation.metrics["critical_recall_by_subject_type"] == {
        "dialogue_unit": pytest.approx(1)
    }


@pytest.mark.asyncio
async def test_upstream_failure_truth_is_excluded_from_harness_release_metrics(
    monitor_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_deployment_monitor import TagDeploymentMonitor

    window_end = datetime(2026, 7, 25, 10, 45, tzinfo=UTC)
    await _seed_release(
        monitor_factory,
        window_end=window_end,
        representative_truth=True,
        representative_error_stage="asr",
    )

    result = await TagDeploymentMonitor(monitor_factory).run_once(
        now=window_end + timedelta(seconds=1)
    )

    assert result[0].observation.audited_count == 0
    assert result[0].observation.metrics["review_truth_count"] == 0
    assert "critical_recall" not in result[0].observation.metrics


@pytest.mark.asyncio
async def test_release_audit_subject_is_counted_once_across_windows_and_retries(
    monitor_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_deployment_monitor import TagDeploymentMonitor

    first_window_end = datetime(2026, 7, 25, 10, 25, tzinfo=UTC)
    seeded = await _seed_release(
        monitor_factory,
        window_end=first_window_end,
        representative_truth=True,
    )
    monitor = TagDeploymentMonitor(monitor_factory)
    first = await monitor.run_once(now=first_window_end + timedelta(seconds=1))
    assert first[0].observation.audited_count == 1

    second_window_end = first_window_end + timedelta(minutes=5)
    async with monitor_factory() as session, session.begin():
        await _add_certified_representative_truth(
            session,
            deployment_id=seeded["deployment_id"],
            schema_version_id=seeded["schema_version_id"],
            tagger_version_id=seeded["tagger_version_id"],
            extraction_run_id=seeded["completed_run_id"],
            subject_id=seeded["unit_ids"][0],
            reception_id=seeded["reception_ids"][0],
            proposed_fact_id=seeded["fact_id"],
            proposed_value="purchase",
            truth_value="purchase",
            evidence_refs=seeded["subject_evidence_refs"][0],
            sampling_probability=0.05,
            occurred_at=second_window_end - timedelta(minutes=1),
            bundle_id="representative-retry",
            reviewer_ids=(8, 9, 10),
        )

    second = await monitor.run_once(now=second_window_end + timedelta(seconds=1))
    replay = await monitor.run_once(now=second_window_end + timedelta(minutes=1))

    assert second[0].observation.audited_count == 0
    assert second[0].observation.adjudicated_count == 0
    assert replay[0].observation.id == second[0].observation.id
    async with monitor_factory() as session:
        persisted_count = (
            await session.execute(
                select(func.count(TagDeploymentAuditSubject.id)).where(
                    TagDeploymentAuditSubject.deployment_id == seeded["deployment_id"],
                    TagDeploymentAuditSubject.count_kind == "audited",
                )
            )
        ).scalar_one()
    assert persisted_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "is_trusted"),
    [
        ("manual", False),
        ("imported", False),
        ("monitor", False),
    ],
)
async def test_only_trusted_monitor_observations_can_control_a_release(
    monitor_factory: async_sessionmaker[AsyncSession],
    source: str,
    is_trusted: bool,
) -> None:
    from audio_graphy.services.tag_governance import TagGovernanceService

    window_end = datetime(2026, 7, 25, 9, 5, tzinfo=UTC)
    seeded = await _seed_release(monitor_factory, window_end=window_end)

    observation, deployment = await TagGovernanceService(
        monitor_factory
    ).record_deployment_observation(
        tenant_id="tenant-a",
        deployment_id=seeded["deployment_id"],
        sample_reception_ids=[],
        metrics={
            "run_count": 100,
            "failed_run_count": 100,
            "error_rate": 1,
            "drift_affected_tags": ["intent"],
            "provider_budget": {
                "source": "client_claim",
                "exhausted_job_ids": [seeded["job_id"]],
                "near_exhaustion_job_ids": [seeded["job_id"]],
            },
        },
        breach_codes=[
            "schema_inconsistent",
            "error_rate",
            "drift",
            "budget_exhausted",
            "budget_near_exhaustion",
        ],
        window_start=window_end,
        window_end=window_end + timedelta(minutes=5),
        actor_user_id=7,
        source=source,
        is_trusted=is_trusted,
    )

    assert observation.action == "observe"
    assert "provider_budget" not in observation.metrics
    assert "budget_exhausted" not in observation.breach_codes
    assert "budget_near_exhaustion" not in observation.breach_codes
    assert deployment.status == "canary_5"
    assert deployment.promotion_paused is False
    async with monitor_factory() as session:
        review_job_count = int(
            (
                await session.execute(
                    select(func.count(TagExtractionJob.id)).where(
                        TagExtractionJob.job_type == "review_batch"
                    )
                )
            ).scalar_one()
        )
    assert review_job_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("break_continuity", [False, True])
async def test_error_gate_requires_two_complete_trusted_fifteen_minute_windows(
    monitor_factory: async_sessionmaker[AsyncSession],
    break_continuity: bool,
) -> None:
    from audio_graphy.services.tag_governance import TagGovernanceService

    seeded = await _seed_release(
        monitor_factory,
        window_end=datetime(2026, 7, 25, 9, 55, tzinfo=UTC),
    )
    service = TagGovernanceService(monitor_factory)
    policy_start = datetime(2026, 7, 25, 10, 0, tzinfo=UTC)
    last_observation = None
    deployment = None
    for index in range(6):
        start = policy_start + timedelta(minutes=5 * index)
        last_observation, deployment = await service.record_deployment_observation(
            tenant_id="tenant-a",
            deployment_id=seeded["deployment_id"],
            sample_reception_ids=[],
            metrics={
                "run_count": 100,
                "failed_run_count": 1,
                "error_rate": 0.01,
            },
            breach_codes=["error_rate"],
            window_start=start,
            window_end=start + timedelta(minutes=5),
            actor_user_id=0,
            source="monitor",
            is_trusted=not (break_continuity and index == 2),
        )
        if index < 5:
            assert deployment.status == "canary_5"

    assert last_observation is not None
    assert deployment is not None
    assert last_observation.metrics["error_policy"]["complete"] is (not break_continuity)
    assert last_observation.action == ("observe" if break_continuity else "rollback")
    assert deployment.status == ("canary_5" if break_continuity else "rolled_back")


@pytest.mark.asyncio
async def test_zero_sample_trusted_window_never_advances_release(
    monitor_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_deployment_monitor import TagDeploymentMonitor

    seeded_window_end = datetime(2026, 7, 25, 10, 25, tzinfo=UTC)
    await _seed_release(monitor_factory, window_end=seeded_window_end)

    result = await TagDeploymentMonitor(monitor_factory).run_once(
        now=seeded_window_end + timedelta(minutes=10, seconds=1)
    )

    assert len(result) == 1
    assert result[0].observation.is_trusted is True
    assert result[0].observation.sample_count == 0
    assert result[0].observation.served_count == 0
    assert result[0].observation.paired_count == 0
    assert result[0].observation.audited_count == 0
    assert result[0].observation.adjudicated_count == 0
    assert result[0].deployment.status == "canary_5"
    assert set(result[0].observation.metrics["promotion_readiness"]["unmet"]) >= {
        "served_count",
        "audited_count",
    }


@pytest.mark.asyncio
async def test_monitor_samples_do_not_promote_without_duration_and_unbiased_audits(
    monitor_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_deployment_monitor import TagDeploymentMonitor

    window_end = datetime(2026, 7, 25, 10, 35, tzinfo=UTC)
    window_start = window_end - timedelta(minutes=5)
    seeded = await _seed_release(monitor_factory, window_end=window_end)
    async with monitor_factory() as session, session.begin():
        job_id = (await session.execute(select(TagExtractionJob.id).limit(1))).scalar_one()
        receptions = [
            Reception(
                tenant_id="tenant-a",
                scenario="automotive",
                store_id=f"PROMOTE-{index}",
                status="ready",
                merge_mode="logical",
                started_at=window_start,
                ended_at=window_start + timedelta(seconds=1),
                version=1,
            )
            for index in range(198)
        ]
        session.add_all(receptions)
        await session.flush()
        units = [
            DialogueUnit(
                tenant_id="tenant-a",
                reception_id=reception.id,
                source_recording_id=None,
                unit_index=0,
                version=1,
                start_sec=0,
                end_sec=1,
                topic="接待",
                business_stage="需求发现",
                segment_refs=[],
                speaker_refs=[],
                edit_status="auto",
            )
            for reception in receptions
        ]
        session.add_all(units)
        await session.flush()
        session.add_all(
            [
                TagExtractionRun(
                    tenant_id="tenant-a",
                    job_id=job_id,
                    origin="serving",
                    subject_type="dialogue_unit",
                    subject_id=unit.id,
                    tagger_version_id=seeded["tagger_version_id"],
                    deployment_id=seeded["deployment_id"],
                    deployment_stage="canary_5",
                    deployment_revision=1,
                    served_current=True,
                    input_hash=f"{index + 100:064x}",
                    input_snapshot={"reception_id": unit.reception_id},
                    output_snapshot={"assignments": []},
                    status="completed",
                    started_at=window_start + timedelta(minutes=1),
                    finished_at=window_start + timedelta(minutes=2),
                )
                for index, unit in enumerate(units)
            ]
        )

    result = await TagDeploymentMonitor(monitor_factory).run_once(
        now=window_end + timedelta(seconds=1)
    )

    assert result[0].observation.sample_count == 200
    assert result[0].observation.source == "monitor"
    assert result[0].observation.is_trusted is True
    assert result[0].observation.served_count == 200
    assert result[0].observation.audited_count == 0
    assert result[0].observation.metrics["error_rate"] == pytest.approx(0.005)
    assert result[0].observation.breach_codes == []
    assert result[0].deployment.status == "canary_5"
    assert result[0].deployment.traffic_percent == 5


@pytest.mark.asyncio
async def test_stage_gate_counts_each_reception_only_once_across_windows(
    monitor_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_deployment_monitor import TagDeploymentMonitor

    first_end = datetime(2026, 7, 25, 10, 55, tzinfo=UTC)
    seeded = await _seed_release(monitor_factory, window_end=first_end)
    monitor = TagDeploymentMonitor(monitor_factory)
    first = await monitor.run_once(now=first_end + timedelta(seconds=1))
    second_end = first_end + timedelta(minutes=5)
    async with monitor_factory() as session, session.begin():
        session.add_all(
            [
                TagExtractionRun(
                    tenant_id="tenant-a",
                    job_id=seeded["job_id"],
                    origin="serving",
                    subject_type="dialogue_unit",
                    subject_id=unit_id,
                    tagger_version_id=seeded["tagger_version_id"],
                    deployment_id=seeded["deployment_id"],
                    deployment_stage="canary_5",
                    deployment_revision=1,
                    served_current=True,
                    input_hash=f"{index + 900:064x}",
                    input_snapshot={"reception_id": reception_id},
                    output_snapshot={"assignments": []},
                    status="completed",
                    started_at=first_end + timedelta(minutes=1),
                    finished_at=first_end + timedelta(minutes=2),
                )
                for index, (unit_id, reception_id) in enumerate(
                    zip(
                        seeded["unit_ids"],
                        seeded["reception_ids"],
                        strict=True,
                    )
                )
            ]
        )

    second = await monitor.run_once(now=second_end + timedelta(seconds=1))

    assert first[0].observation.sample_count == 2
    assert first[0].observation.metrics["window_reception_count"] == 2
    assert second[0].observation.sample_count == 0
    assert second[0].observation.served_count == 0
    assert second[0].observation.metrics["window_reception_count"] == 2
    async with monitor_factory() as session:
        samples = list(
            (
                await session.execute(
                    select(TagDeploymentObservationSample).where(
                        TagDeploymentObservationSample.deployment_id == seeded["deployment_id"],
                        TagDeploymentObservationSample.stage == "canary_5",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert {sample.reception_id for sample in samples} == set(seeded["reception_ids"])


@pytest.mark.asyncio
async def test_monitor_does_not_report_critical_recall_without_human_truth(
    monitor_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_deployment_monitor import TagDeploymentMonitor

    window_end = datetime(2026, 7, 25, 11, 5, tzinfo=UTC)
    await _seed_release(monitor_factory, window_end=window_end, include_review=False)

    result = await TagDeploymentMonitor(monitor_factory).run_once(
        now=window_end + timedelta(seconds=1)
    )

    assert len(result) == 1
    assert "critical_recall" not in result[0].observation.metrics
    assert result[0].observation.metrics["review_truth_count"] == 0
    assert "critical_recall" not in result[0].observation.breach_codes


@pytest.mark.asyncio
async def test_monitor_does_not_call_selective_critical_positive_review_recall(
    monitor_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_deployment_monitor import TagDeploymentMonitor

    window_end = datetime(2026, 7, 25, 11, 15, tzinfo=UTC)
    await _seed_release(
        monitor_factory,
        window_end=window_end,
        include_review=True,
        representative_truth=False,
    )

    result = await TagDeploymentMonitor(monitor_factory).run_once(
        now=window_end + timedelta(seconds=1)
    )

    assert result[0].observation.metrics["review_truth_count"] == 0
    assert "critical_recall" not in result[0].observation.metrics
    assert "critical_recall" not in result[0].observation.breach_codes


@pytest.mark.asyncio
async def test_monitor_rolls_back_on_schema_and_required_evidence_breaches(
    monitor_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_deployment_monitor import TagDeploymentMonitor

    window_end = datetime(2026, 7, 25, 12, 5, tzinfo=UTC)
    seeded = await _seed_release(monitor_factory, window_end=window_end)
    async with monitor_factory() as session, session.begin():
        fact = await session.get(TagAssignmentFact, seeded["fact_id"])
        assert fact is not None
        # Direct corruption seed: normal writes reject both violations.
        # Disable the ORM append-only guard only for this fixture by issuing SQL.
        await session.execute(
            TagAssignmentFact.__table__.update()
            .where(TagAssignmentFact.id == fact.id)
            .values(tag_value="not-in-schema", evidence_refs=[])
        )
        session.expunge(fact)

    result = await TagDeploymentMonitor(monitor_factory).run_once(
        now=window_end + timedelta(seconds=1)
    )

    assert len(result) == 1
    assert set(result[0].observation.breach_codes) >= {
        "error_rate",
        "schema_inconsistent",
        "evidence_inconsistent",
    }
    assert result[0].observation.metrics["schema_violation_count"] == 1
    assert result[0].observation.metrics["evidence_violation_count"] == 1
    assert result[0].deployment.status == "rolled_back"


@pytest.mark.asyncio
async def test_monitor_rolls_back_when_server_linked_job_budget_is_exhausted(
    monitor_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_deployment_monitor import TagDeploymentMonitor

    window_end = datetime(2026, 7, 25, 12, 15, tzinfo=UTC)
    seeded = await _seed_release(monitor_factory, window_end=window_end)
    async with monitor_factory() as session, session.begin():
        job = await session.get(TagExtractionJob, seeded["job_id"])
        assert job is not None
        job.budget_source = "explicit"
        job.budget_max_provider_tokens = 100
        job.budget_consumed_provider_tokens = 80
        job.budget_reserved_provider_tokens = 20
        job.status = "failed"

    result = await TagDeploymentMonitor(monitor_factory).run_once(
        now=window_end + timedelta(seconds=1)
    )

    assert len(result) == 1
    assert result[0].observation.action == "rollback"
    assert result[0].deployment.status == "rolled_back"
    assert "budget_exhausted" in result[0].observation.breach_codes
    budget = result[0].observation.metrics["provider_budget"]
    assert budget["source"] == "server_linked_jobs"
    assert budget["hard_budget_job_count"] == 1
    assert budget["exhausted_job_ids"] == [seeded["job_id"]]
    assert budget["max_completion_by_dimension"]["provider_tokens"] == pytest.approx(1)


@pytest.mark.asyncio
async def test_monitor_only_observes_successfully_completed_job_at_budget_limit(
    monitor_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_deployment_monitor import TagDeploymentMonitor

    window_end = datetime(2026, 7, 25, 12, 20, tzinfo=UTC)
    seeded = await _seed_release(monitor_factory, window_end=window_end)
    async with monitor_factory() as session, session.begin():
        job = await session.get(TagExtractionJob, seeded["job_id"])
        assert job is not None
        assert job.status == "completed"
        job.budget_source = "explicit"
        job.budget_max_provider_tokens = 100
        job.budget_consumed_provider_tokens = 100

    result = await TagDeploymentMonitor(monitor_factory).run_once(
        now=window_end + timedelta(seconds=1)
    )

    assert len(result) == 1
    assert result[0].deployment.status == "canary_5"
    assert result[0].deployment.promotion_paused is False
    assert "budget_exhausted" not in result[0].observation.breach_codes
    assert "budget_near_exhaustion" not in result[0].observation.breach_codes
    budget = result[0].observation.metrics["provider_budget"]
    assert budget["max_completion_ratio"] == pytest.approx(1)
    assert budget["exhausted_job_ids"] == []
    assert budget["near_exhaustion_job_ids"] == []
    assert budget["jobs"][0]["successfully_completed"] is True


@pytest.mark.asyncio
async def test_monitor_pauses_near_budget_and_reuses_human_review_job(
    monitor_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_deployment_monitor import TagDeploymentMonitor
    from audio_graphy.services.tag_governance import TagGovernanceService

    window_end = datetime(2026, 7, 25, 12, 25, tzinfo=UTC)
    seeded = await _seed_release(monitor_factory, window_end=window_end)
    async with monitor_factory() as session, session.begin():
        job = await session.get(TagExtractionJob, seeded["job_id"])
        assert job is not None
        job.budget_source = "explicit"
        job.budget_max_provider_tokens = 100
        job.budget_consumed_provider_tokens = 85
        job.budget_reserved_provider_tokens = 5
        job.status = "running"
        job.total_items = 3
        job.finished_at = None
        await session.execute(
            TagAssignmentCurrent.__table__.delete().where(
                TagAssignmentCurrent.fact_id == seeded["fact_id"]
            )
        )
        await session.execute(
            TagAssignmentFact.__table__.delete().where(TagAssignmentFact.id == seeded["fact_id"])
        )

    result = await TagDeploymentMonitor(monitor_factory).run_once(
        now=window_end + timedelta(seconds=1)
    )

    assert len(result) == 1
    first = result[0]
    assert first.observation.action == "pause"
    assert first.deployment.status == "canary_5"
    assert first.deployment.promotion_paused is True
    assert "budget_near_exhaustion" in first.observation.breach_codes
    budget = first.observation.metrics["provider_budget"]
    assert budget["near_exhaustion_job_ids"] == [seeded["job_id"]]
    assert budget["review_status"] == "queued_or_in_progress"
    review_job_id = int(budget["review_job_id"])

    second_observation, second_deployment = await TagGovernanceService(
        monitor_factory
    ).record_deployment_observation(
        tenant_id="tenant-a",
        deployment_id=seeded["deployment_id"],
        sample_reception_ids=[],
        metrics={
            "run_count": 1,
            "failed_run_count": 0,
            "error_rate": 0,
            "provider_budget": {
                "source": "server_linked_jobs",
                "near_exhaustion_job_ids": [seeded["job_id"]],
            },
        },
        breach_codes=["budget_near_exhaustion"],
        window_start=window_end,
        window_end=window_end + timedelta(minutes=5),
        actor_user_id=0,
        source="monitor",
        is_trusted=True,
    )

    assert second_observation.action == "pause"
    assert second_deployment.revision == first.deployment.revision
    assert second_observation.metrics["provider_budget"]["review_job_id"] == review_job_id
    async with monitor_factory() as session:
        review_jobs = list(
            (
                await session.execute(
                    select(TagExtractionJob).where(
                        TagExtractionJob.tenant_id == "tenant-a",
                        TagExtractionJob.job_type == "review_batch",
                        TagExtractionJob.origin == "monitor",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(review_jobs) == 1
    assert review_jobs[0].id == review_job_id
    assert review_jobs[0].scope["selection_policy"] == "budget_guard"
    assert review_jobs[0].total_items == 2
    assert all("proposed_fact_id" not in subject for subject in review_jobs[0].scope["subjects"])
    assert review_jobs[0].scope["trusted_observation_id"] == first.observation.id
    assert review_jobs[0].scope["linked_observation_ids"] == [
        first.observation.id,
        second_observation.id,
    ]


@pytest.mark.asyncio
async def test_monitor_reports_truth_backed_critical_recall_breach(
    monitor_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_deployment_monitor import TagDeploymentMonitor

    window_end = datetime(2026, 7, 25, 13, 5, tzinfo=UTC)
    await _seed_release(
        monitor_factory,
        window_end=window_end,
        review_action="correct",
        corrected_value="purchase",
        proposed_value="browse",
        representative_truth=True,
    )

    result = await TagDeploymentMonitor(monitor_factory).run_once(
        now=window_end + timedelta(seconds=1)
    )

    assert result[0].observation.metrics["review_truth_count"] == 1
    assert result[0].observation.metrics["critical_recall"] == 0
    assert "critical_recall" in result[0].observation.breach_codes
    assert result[0].deployment.status == "rolled_back"


def test_duplicate_current_counter_detects_logical_corruption() -> None:
    from audio_graphy.services.tag_deployment_monitor import count_duplicate_current_keys

    keys = [
        ("dialogue_unit", 1, "intent"),
        ("dialogue_unit", 1, "intent"),
        ("dialogue_unit", 1, "budget"),
    ]

    assert count_duplicate_current_keys(keys) == 1
