"""API integration tests for the tag-governance closed loop."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import _run_async

SCHEMA_DEFINITIONS = [
    {
        "key": "intent",
        "name": "客户意图",
        "category": "sales",
        "value_type": "enum",
        "allowed_values": ["browse", "test_drive", "purchase"],
        "subject_types": ["dialogue_unit"],
        "scenarios": ["automotive"],
        "evidence_required": True,
        "critical": False,
        "threshold": 0.7,
    },
    {
        "key": "compliance_risk",
        "name": "合规风险",
        "category": "risk",
        "value_type": "enum",
        "allowed_values": ["none", "personal_transfer"],
        "subject_types": ["dialogue_unit"],
        "scenarios": ["automotive", "gold"],
        "evidence_required": True,
        "critical": True,
        "threshold": 0.95,
    },
]


def _create_schema(
    client: TestClient,
    headers: dict[str, str],
    *,
    key: str = "sales-dialogue",
) -> dict:
    response = client.post(
        "/api/v1/tag-schemas",
        headers=headers,
        json={"key": key, "name": "销售对话标签", "description": "闭环标签体系"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _seed_evaluation_resources(factory: Any) -> tuple[int, int, int]:
    from audio_graphy.models.tag_governance import (
        TagDeployment,
        TagEvaluationRun,
        TaggerVersion,
        TagGoldLabel,
        TagGoldSet,
        TagGoldSetVersion,
        TagSchema,
        TagSchemaVersion,
    )

    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        schema = TagSchema(
            tenant_id="chang_an",
            key="api-evaluation",
            name="API评估体系",
            status="published",
            created_by=1,
        )
        session.add(schema)
        await session.flush()
        schema_version = TagSchemaVersion(
            tenant_id="chang_an",
            schema_id=schema.id,
            version="1",
            definitions=SCHEMA_DEFINITIONS,
            checksum="7" * 64,
            status="published",
            created_by=1,
            published_by=1,
            published_at=now,
        )
        session.add(schema_version)
        await session.flush()
        tagger = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=schema_version.id,
            version="api-candidate",
            engine="rule",
            prompt_content="",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="rules-local",
            thresholds={"intent": 0.7},
            config_checksum="8" * 64,
            status="draft",
            created_by=1,
        )
        baseline = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=schema_version.id,
            version="api-baseline",
            engine="rule",
            prompt_content="",
            rule_bundle={
                "dsl_version": "1",
                "rules": [
                    {
                        "tag_key": "intent",
                        "value": "browse",
                        "contains_any": ["看看"],
                        "confidence": 0.9,
                    }
                ],
            },
            model_version="rules-baseline",
            thresholds={"intent": 0.7},
            config_checksum="6" * 64,
            status="qualified",
            created_by=1,
            qualified_at=now,
        )
        session.add_all([tagger, baseline])
        gold_set = TagGoldSet(
            tenant_id="chang_an",
            key="api-holdout",
            name="API隐藏集",
            schema_version_id=schema_version.id,
            created_by=1,
        )
        session.add(gold_set)
        await session.flush()
        version = TagGoldSetVersion(
            tenant_id="chang_an",
            gold_set_id=gold_set.id,
            version="1",
            status="frozen",
            checksum="9" * 64,
            item_count=30,
            frozen_by=1,
            frozen_at=now,
        )
        session.add(version)
        await session.flush()
        baseline_evaluation = TagEvaluationRun(
            tenant_id="chang_an",
            tagger_version_id=baseline.id,
            baseline_tagger_version_id=baseline.id,
            gold_set_version_id=version.id,
            dataset_snapshot_hash=version.dataset_snapshot_hash or version.checksum or "",
            status="completed",
            metrics={"holdout_only": True, "sealed_release": True},
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
        session.add_all(
            [
                TagGoldLabel(
                    tenant_id="chang_an",
                    gold_set_version_id=version.id,
                    review_decision_id=2_000 + subject_id,
                    reception_id=3_000 + subject_id,
                    subject_type="dialogue_unit",
                    subject_id=subject_id,
                    tag_key="intent",
                    tag_value="purchase",
                    evidence_refs=[{"segment_id": subject_id}],
                    truth_state="present",
                    truth_tier="t2",
                    split="challenge",
                )
                for subject_id in range(1, 31)
            ]
        )
        return tagger.id, version.id, baseline.id


async def _bind_api_optimization_candidate(
    factory: Any,
    *,
    tagger_id: int,
    gold_version_id: int,
    baseline_id: int,
) -> int:
    from audio_graphy.models.tag_governance import (
        TagExtractionJob,
        TaggerVersion,
        TagGoldSetVersion,
        TagOptimizationRun,
    )

    async with factory() as session, session.begin():
        candidate = await session.get(TaggerVersion, tagger_id)
        gold = await session.get(TagGoldSetVersion, gold_version_id)
        assert candidate is not None and gold is not None
        job = TagExtractionJob(
            tenant_id="chang_an",
            job_type="optimize",
            origin="system",
            status="running",
            scope={"optimization_run_id": 0},
            tagger_version_id=baseline_id,
            idempotency_key=f"api-optimization:{tagger_id}",
            total_items=1,
            completed_items=0,
            failed_items=0,
            failed_subset=[],
            attempt_count=1,
            max_attempts=3,
            revision=1,
            lease_owner="api-optimizer-test",
            created_by=1,
        )
        session.add(job)
        await session.flush()
        run = TagOptimizationRun(
            tenant_id="chang_an",
            baseline_tagger_version_id=baseline_id,
            gold_set_version_id=gold_version_id,
            job_id=job.id,
            dataset_snapshot_hash=str(gold.dataset_snapshot_hash or gold.checksum),
            trigger="manual",
            status="running",
            phase="validation",
            cohort={"source": "api-boundary-test"},
            objective={"policy": "balanced"},
            search_budget={"max_trials": 1, "sealed_holdout_queries": 1},
            candidate_tagger_version_id=tagger_id,
            summary={},
            next_actions=["enqueue_sealed_holdout_evaluation"],
            artifacts=[],
            created_by=1,
        )
        session.add(run)
        await session.flush()
        job.scope = {"optimization_run_id": run.id}
        candidate.origin = "optimizer"
        candidate.parent_version_id = baseline_id
        candidate.optimization_run_id = run.id
        return int(run.id)


async def _seed_monitor_subjects(
    factory: Any,
    *,
    reception_count: int,
    dialogue_count: int,
) -> tuple[list[int], list[int]]:
    from audio_graphy.models.reception import DialogueUnit, Reception

    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        receptions = [
            Reception(
                tenant_id="chang_an",
                scenario="automotive",
                store_id=f"MONITOR-{index}",
                status="ready",
                merge_mode="logical",
                started_at=now,
                ended_at=now + timedelta(seconds=1),
                version=1,
            )
            for index in range(reception_count)
        ]
        session.add_all(receptions)
        await session.flush()
        dialogue_units = [
            DialogueUnit(
                tenant_id="chang_an",
                reception_id=receptions[index % reception_count].id,
                unit_index=index // reception_count,
                version=1,
                start_sec=float(index),
                end_sec=float(index + 1),
                topic="promotion audit",
                boundary_reasons=[],
                segment_refs=[],
                speaker_refs=[],
                edit_status="auto",
            )
            for index in range(dialogue_count)
        ]
        session.add_all(dialogue_units)
        await session.flush()
        return (
            [int(reception.id) for reception in receptions],
            [int(unit.id) for unit in dialogue_units],
        )


async def _seed_trusted_stage_history(
    factory: Any,
    *,
    deployment_id: int,
    deployment_revision: int,
    stage: str,
    window_start: datetime,
    window_count: int,
) -> None:
    from audio_graphy.models.tag_governance import TagDeploymentObservation

    async with factory() as session, session.begin():
        session.add_all(
            [
                TagDeploymentObservation(
                    tenant_id="chang_an",
                    deployment_id=deployment_id,
                    deployment_revision=deployment_revision,
                    stage=stage,
                    window_start=window_start + timedelta(minutes=5 * index),
                    window_end=window_start + timedelta(minutes=5 * (index + 1)),
                    sample_count=0,
                    source="monitor",
                    provenance={"collector": "api-test-monitor-history"},
                    is_trusted=True,
                    served_count=0,
                    paired_count=0,
                    audited_count=0,
                    adjudicated_count=0,
                    metrics={"error_rate": 0.001},
                    breach_codes=[],
                    action="observe",
                )
                for index in range(window_count)
            ]
        )


async def _seed_job_tagger(factory: Any) -> tuple[int, int]:
    from audio_graphy.models.tag_governance import (
        TaggerVersion,
        TagSchema,
        TagSchemaVersion,
    )

    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        schema = TagSchema(
            tenant_id="chang_an",
            key="api-job-route",
            name="API任务路由体系",
            status="published",
            created_by=1,
        )
        session.add(schema)
        await session.flush()
        schema_version = TagSchemaVersion(
            tenant_id="chang_an",
            schema_id=schema.id,
            version="1",
            definitions=SCHEMA_DEFINITIONS,
            checksum="3" * 64,
            status="published",
            created_by=1,
            published_by=1,
            published_at=now,
        )
        session.add(schema_version)
        await session.flush()
        tagger = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=schema_version.id,
            version="api-job-qualified",
            engine="rule",
            prompt_content="",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="rules-local",
            thresholds={"intent": 0.7},
            config_checksum="2" * 64,
            status="qualified",
            created_by=1,
            qualified_at=now,
        )
        session.add(tagger)
        await session.flush()
        return int(tagger.id), int(schema_version.id)


async def _seed_optimizer_owned_api_job(factory: Any, *, job_type: str) -> int:
    from audio_graphy.models.tag_governance import TagExtractionJob

    async with factory() as session, session.begin():
        job = TagExtractionJob(
            tenant_id="chang_an",
            job_type=job_type,
            origin="system",
            status="running",
            scope={"optimization_run_id": 73},
            idempotency_key=f"api-optimizer-owned-{job_type}",
            total_items=1,
            completed_items=0,
            failed_items=0,
            attempt_count=1,
            max_attempts=3,
            revision=1,
            lease_owner="api-optimizer-worker",
            created_by=1,
        )
        session.add(job)
        await session.flush()
        return int(job.id)


async def _seed_review_resources(factory: Any) -> tuple[int, int, int]:
    from datetime import timedelta

    from audio_graphy.models.reception import (
        DialogueUnit,
        Reception,
        ReceptionRecording,
    )
    from audio_graphy.models.recording import Recording
    from audio_graphy.models.segment import Segment
    from audio_graphy.models.tag_governance import TagSchema, TagSchemaVersion

    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        recording = Recording(
            tenant_id="chang_an",
            store_id="S001",
            agent_name="agent_ca",
            agent_user_id=3,
            path="/tmp/api-review.wav",
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
            transcript="客户明确今天购买",
            text_scrubbed="客户明确今天购买",
            speaker="customer",
            vad_conf=0.99,
        )
        session.add(segment)
        reception = Reception(
            tenant_id="chang_an",
            scenario="automotive",
            store_id="S001",
            agent_name="agent_ca",
            agent_user_id=3,
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
            topic="购买",
            business_stage="成交意向",
            segment_refs=[{"segment_id": segment.id, "recording_id": recording.id}],
            speaker_refs=["customer"],
            edit_status="auto",
        )
        session.add(unit)
        schema = TagSchema(
            tenant_id="chang_an",
            key="api-review",
            name="API复核体系",
            status="published",
            created_by=1,
        )
        session.add(schema)
        await session.flush()
        version = TagSchemaVersion(
            tenant_id="chang_an",
            schema_id=schema.id,
            version="1",
            definitions=SCHEMA_DEFINITIONS,
            checksum="a" * 64,
            status="published",
            created_by=1,
            published_by=1,
            published_at=now,
        )
        session.add(version)
        await session.flush()
        return unit.id, segment.id, version.id


async def _seed_lineage_fact(factory: Any) -> int:
    from audio_graphy.models.tag_governance import (
        TagExtractionJob,
        TagExtractionRun,
        TaggerVersion,
    )
    from audio_graphy.services.tag_governance import TagGovernanceService

    unit_id, segment_id, schema_version_id = await _seed_review_resources(factory)
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        tagger = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=schema_version_id,
            version="lineage-secret-tagger",
            engine="hybrid",
            prompt_content="SECRET SYSTEM PROMPT",
            rule_bundle={
                "dsl_version": "1",
                "rules": [
                    {
                        "tag_key": "intent",
                        "value": "purchase",
                        "contains_any": ["购买"],
                        "confidence": 0.95,
                    }
                ],
            },
            model_version="private-model",
            thresholds={"intent": 0.7},
            config_checksum="5" * 64,
            status="qualified",
            created_by=1,
            qualified_at=now,
        )
        session.add(tagger)
        await session.flush()
        job = TagExtractionJob(
            tenant_id="chang_an",
            job_type="extract",
            status="completed",
            scope={
                "dialogue_unit_ids": [unit_id],
                "internal_instruction": "SECRET JOB SCOPE",
            },
            tagger_version_id=tagger.id,
            idempotency_key="lineage-secret-job",
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
        run = TagExtractionRun(
            tenant_id="chang_an",
            job_id=job.id,
            subject_type="dialogue_unit",
            subject_id=unit_id,
            tagger_version_id=tagger.id,
            input_hash="4" * 64,
            input_snapshot={"dialogue_unit_id": unit_id},
            output_snapshot={},
            status="completed",
            started_at=now,
            finished_at=now,
        )
        session.add(run)
        await session.flush()
        tagger_id = tagger.id
        run_id = run.id
    fact = await TagGovernanceService(factory).append_assignment(
        tenant_id="chang_an",
        subject_type="dialogue_unit",
        subject_id=unit_id,
        tag_key="intent",
        tag_value="purchase",
        confidence=0.95,
        evidence_refs=[{"segment_id": segment_id, "start_sec": 0, "end_sec": 4}],
        source="rule",
        schema_version_id=schema_version_id,
        tagger_version_id=tagger_id,
        extraction_run_id=run_id,
        deployment_id=None,
        input_hash="4" * 64,
        actor_user_id=1,
    )
    return fact.id


def test_schema_versions_are_tenant_scoped_and_publish_is_admin_only(
    test_client: TestClient,
    auth_headers: dict,
) -> None:
    schema = _create_schema(test_client, auth_headers["admin_t1"])
    version_response = test_client.post(
        f"/api/v1/tag-schemas/{schema['id']}/versions",
        headers=auth_headers["admin_t1"],
        json={"version": "1.0.0", "definitions": SCHEMA_DEFINITIONS},
    )
    assert version_response.status_code == 201, version_response.text
    version = version_response.json()
    assert len(version["checksum"]) == 64
    assert version["status"] == "draft"

    forbidden = test_client.post(
        f"/api/v1/tag-schemas/{schema['id']}/versions/{version['id']}/publish",
        headers=auth_headers["inspector_t1"],
    )
    assert forbidden.status_code == 403

    published = test_client.post(
        f"/api/v1/tag-schemas/{schema['id']}/versions/{version['id']}/publish",
        headers=auth_headers["admin_t1"],
    )
    assert published.status_code == 200
    assert published.json()["status"] == "published"

    cross_tenant = test_client.get(
        f"/api/v1/tag-schemas/{schema['id']}",
        headers=auth_headers["admin_t2"],
    )
    assert cross_tenant.status_code == 404


def test_job_api_is_idempotent_scoped_and_returns_202(
    test_client: TestClient,
    auth_headers: dict,
) -> None:
    payload = {
        "job_type": "extract",
        "scope": {"dialogue_unit_ids": [1001, 1002]},
    }
    headers = {
        **auth_headers["inspector_t1"],
        "Idempotency-Key": "batch-2026-07-25-a",
    }
    first = test_client.post("/api/v1/tag-jobs", headers=headers, json=payload)
    second = test_client.post("/api/v1/tag-jobs", headers=headers, json=payload)

    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["status"] == "queued"

    cross_tenant = test_client.get(
        f"/api/v1/tag-jobs/{first.json()['id']}",
        headers=auth_headers["admin_t2"],
    )
    assert cross_tenant.status_code == 404


def test_job_api_without_header_uses_stable_normalized_scope_key(
    test_client: TestClient,
    auth_headers: dict,
) -> None:
    first = test_client.post(
        "/api/v1/tag-jobs",
        headers=auth_headers["inspector_t1"],
        json={
            "job_type": "extract",
            "scope": {
                "dialogue_unit_ids": [1002, 1001, 1002],
                "target_tag_keys": ["stage", "intent", "stage"],
            },
        },
    )
    replay = test_client.post(
        "/api/v1/tag-jobs",
        headers=auth_headers["inspector_t1"],
        json={
            "job_type": "extract",
            "scope": {
                "target_tag_keys": ["intent", "stage"],
                "dialogue_unit_ids": [1001, 1002],
            },
        },
    )

    assert first.status_code == 202, first.text
    assert replay.status_code == 202, replay.text
    assert first.json()["id"] == replay.json()["id"]
    assert first.json()["scope"]["dialogue_unit_ids"] == [1001, 1002]
    assert first.json()["scope"]["target_tag_keys"] == ["intent", "stage"]


def test_job_api_default_key_tracks_the_server_resolved_tagger_version(
    test_client: TestClient,
    auth_headers: dict,
    db_session_factory: Any,
) -> None:
    first_tagger_id, schema_version_id = _run_async(_seed_job_tagger(db_session_factory))
    payload = {
        "job_type": "extract",
        "scope": {"dialogue_unit_ids": [1001]},
    }
    first = test_client.post(
        "/api/v1/tag-jobs",
        headers=auth_headers["inspector_t1"],
        json=payload,
    )

    async def replace_default_tagger() -> int:
        from audio_graphy.models.tag_governance import TaggerVersion

        async with db_session_factory() as session, session.begin():
            replacement = TaggerVersion(
                tenant_id="chang_an",
                schema_version_id=schema_version_id,
                version="api-job-qualified-v2",
                engine="rule",
                prompt_content="",
                rule_bundle={"dsl_version": "1", "rules": []},
                model_version="rules-local-v2",
                thresholds={"intent": 0.7},
                config_checksum="7" * 64,
                status="qualified",
                created_by=1,
                qualified_at=datetime.now(UTC) + timedelta(seconds=1),
            )
            session.add(replacement)
            await session.flush()
            return int(replacement.id)

    replacement_id = _run_async(replace_default_tagger())
    second = test_client.post(
        "/api/v1/tag-jobs",
        headers=auth_headers["inspector_t1"],
        json=payload,
    )

    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert first.json()["tagger_version_id"] == first_tagger_id
    assert second.json()["tagger_version_id"] == replacement_id
    assert first.json()["id"] != second.json()["id"]


@pytest.mark.parametrize("job_type", ["optimize", "evaluate"])
def test_generic_job_cancel_rejects_optimizer_owned_jobs(
    test_client: TestClient,
    auth_headers: dict,
    db_session_factory: Any,
    job_type: str,
) -> None:
    job_id = _run_async(
        _seed_optimizer_owned_api_job(
            db_session_factory,
            job_type=job_type,
        )
    )

    response = test_client.post(
        f"/api/v1/tag-jobs/{job_id}/cancel",
        headers=auth_headers["admin_t1"],
    )
    persisted = test_client.get(
        f"/api/v1/tag-jobs/{job_id}",
        headers=auth_headers["admin_t1"],
    )

    assert response.status_code == 409, response.text
    assert "optimization-run cancel" in response.json()["error"]["message"]
    assert persisted.status_code == 200, persisted.text
    assert persisted.json()["status"] == "running"
    assert persisted.json()["revision"] == 1


def test_job_api_rejects_client_controlled_routing_and_internal_jobs(
    test_client: TestClient,
    auth_headers: dict,
) -> None:
    poisoned_requests = [
        {
            "job_type": "extract",
            "scope": {"dialogue_unit_ids": [1001]},
            "tagger_version_id": 7,
        },
        {
            "job_type": "extract",
            "scope": {"dialogue_unit_ids": [1001]},
            "origin": "serving",
        },
        {
            "job_type": "review_batch",
            "scope": {"subjects": []},
        },
        {
            "job_type": "optimize",
            "scope": {"optimization_run_id": 1},
        },
        {
            "job_type": "extract",
            "scope": {
                "dialogue_unit_ids": [1001],
                "deployment_id": 1,
            },
        },
    ]

    for index, payload in enumerate(poisoned_requests):
        response = test_client.post(
            "/api/v1/tag-jobs",
            headers={
                **auth_headers["inspector_t1"],
                "Idempotency-Key": f"poisoned-route-{index}",
            },
            json=payload,
        )
        assert response.status_code == 422, (payload, response.text)


def test_optimizer_candidate_creation_is_admin_only(
    test_client: TestClient,
    auth_headers: dict,
) -> None:
    forbidden = test_client.post(
        "/api/v1/tagger-versions/optimize",
        headers=auth_headers["inspector_t1"],
        json={
            "gold_set_version_id": 1,
            "production_tagger_version_id": 1,
        },
    )
    assert forbidden.status_code == 403


def test_lineage_honors_data_permissions_and_redacts_governance_secrets(
    test_client: TestClient,
    auth_headers: dict,
    db_session_factory: Any,
) -> None:
    fact_id = _run_async(_seed_lineage_fact(db_session_factory))
    own = test_client.get(
        f"/api/v1/tag-facts/{fact_id}/lineage",
        headers=auth_headers["agent_t1"],
    )
    assert own.status_code == 200, own.text
    body = own.json()
    assert body["fact"]["id"] == fact_id
    assert body["tagger_version"]["model_version"] == "private-model"
    assert "prompt_content" not in body["tagger_version"]
    assert "rule_bundle" not in body["tagger_version"]
    assert "thresholds" not in body["tagger_version"]
    assert "scope" not in body["job"]

    cross_tenant = test_client.get(
        f"/api/v1/tag-facts/{fact_id}/lineage",
        headers=auth_headers["viewer_t2"],
    )
    assert cross_tenant.status_code == 404

    viewer_forbidden = test_client.get(
        "/api/v1/tagger-versions",
        headers=auth_headers["viewer_t1"],
    )
    assert viewer_forbidden.status_code == 403


def test_review_decision_appends_manual_fact_and_resolves_task(
    test_client: TestClient,
    auth_headers: dict,
    db_session_factory: Any,
) -> None:
    unit_id, segment_id, schema_version_id = _run_async(_seed_review_resources(db_session_factory))
    batch = test_client.post(
        "/api/v1/tag-reviews/create-batch",
        headers=auth_headers["inspector_t1"],
        json={
            "reason": "low_confidence",
            "subjects": [
                {
                    "subject_type": "dialogue_unit",
                    "subject_id": unit_id,
                    "tag_key": "intent",
                    "proposed_value": "browse",
                    "schema_version_id": schema_version_id,
                    "evidence_refs": [
                        {
                            "segment_id": segment_id,
                            "start_sec": 0,
                            "end_sec": 4,
                        }
                    ],
                }
            ],
        },
    )
    assert batch.status_code == 201, batch.text
    task_id = batch.json()["items"][0]["id"]

    unclaimed = test_client.post(
        f"/api/v1/tag-reviews/{task_id}/decide",
        headers=auth_headers["inspector_t1"],
        json={
            "action": "correct",
            "corrected_value": "purchase",
            "reason_code": "evidence_confirmed",
            "evidence_refs": [{"segment_id": segment_id, "start_sec": 0, "end_sec": 4}],
        },
    )
    assert unclaimed.status_code == 409

    claimed = test_client.post(
        f"/api/v1/tag-reviews/{task_id}/claim",
        headers=auth_headers["inspector_t1"],
    )
    assert claimed.status_code == 200
    assert claimed.json()["status"] == "claimed"

    decision = test_client.post(
        f"/api/v1/tag-reviews/{task_id}/decide",
        headers=auth_headers["inspector_t1"],
        json={
            "action": "correct",
            "corrected_value": "purchase",
            "reason_code": "evidence_confirmed",
            "note": "客户明确表示今天签约",
            "evidence_refs": [{"segment_id": segment_id, "start_sec": 0, "end_sec": 4}],
        },
    )
    assert decision.status_code == 200, decision.text
    body = decision.json()
    assert body["task"]["status"] == "resolved"
    assert body["fact"]["source"] == "manual"
    assert body["fact"]["tag_value"] == "purchase"


def test_blind_review_masks_model_semantics_and_rejects_creator_claim(
    test_client: TestClient,
    auth_headers: dict,
    db_session_factory: Any,
) -> None:
    unit_id, segment_id, schema_version_id = _run_async(_seed_review_resources(db_session_factory))
    batch = test_client.post(
        "/api/v1/tag-reviews/create-batch",
        headers=auth_headers["inspector_t1"],
        json={
            "reason": "critical",
            "subjects": [
                {
                    "subject_type": "dialogue_unit",
                    "subject_id": unit_id,
                    "tag_key": "intent",
                    "proposed_value": "browse",
                    "schema_version_id": schema_version_id,
                    "confidence": 0.98,
                    "evidence_refs": [
                        {
                            "segment_id": segment_id,
                            "start_sec": 0,
                            "end_sec": 4,
                        }
                    ],
                }
            ],
        },
    )
    assert batch.status_code == 201, batch.text
    pending = batch.json()["items"][0]
    assert pending["blind_mode"] is True
    assert pending["status"] == "pending"
    assert pending["subject_id"] is None
    assert pending["reception_id"] is None
    assert pending["schema_version_id"] is None
    assert pending["proposed_value"] is None
    assert pending["confidence"] is None
    assert pending["evidence_refs"] == []
    assert pending["created_by"] is None

    creator_claim = test_client.post(
        f"/api/v1/tag-reviews/{pending['id']}/claim",
        headers=auth_headers["inspector_t1"],
    )
    assert creator_claim.status_code == 409
    assert creator_claim.json()["error"]["code"] == "TAG_GOVERNANCE_CONFLICT"

    independent_claim = test_client.post(
        f"/api/v1/tag-reviews/{pending['id']}/claim",
        headers=auth_headers["admin_t1"],
    )
    assert independent_claim.status_code == 200, independent_claim.text
    claimed = independent_claim.json()
    assert claimed["subject_id"] == unit_id
    assert claimed["schema_version_id"] == schema_version_id
    assert claimed["proposed_value"] is None
    assert claimed["confidence"] is None
    assert claimed["evidence_refs"] == []


def test_active_review_queue_masks_every_pending_hint_and_excludes_history(
    test_client: TestClient,
    auth_headers: dict,
    db_session_factory: Any,
) -> None:
    unit_id, segment_id, schema_version_id = _run_async(_seed_review_resources(db_session_factory))
    nonblind = test_client.post(
        "/api/v1/tag-reviews/create-batch",
        headers=auth_headers["inspector2_t1"],
        json={
            "reason": "low_confidence",
            "review_bundle_id": "active-nonblind-pending",
            "subjects": [
                {
                    "subject_type": "dialogue_unit",
                    "subject_id": unit_id,
                    "tag_key": "intent",
                    "proposed_value": "browse",
                    "schema_version_id": schema_version_id,
                    "confidence": 0.81,
                    "evidence_refs": [
                        {
                            "segment_id": segment_id,
                            "start_sec": 0,
                            "end_sec": 4,
                        }
                    ],
                }
            ],
        },
    )
    assert nonblind.status_code == 201, nonblind.text
    nonblind_id = int(nonblind.json()["items"][0]["id"])
    blind = test_client.post(
        "/api/v1/tag-reviews/create-batch",
        headers=auth_headers["inspector2_t1"],
        json={
            "reason": "random",
            "review_bundle_id": "active-blind-pending",
            "subjects": [
                {
                    "subject_type": "dialogue_unit",
                    "subject_id": unit_id,
                    "tag_key": "intent",
                    "proposed_value": "purchase",
                    "schema_version_id": schema_version_id,
                    "confidence": 0.99,
                    "evidence_refs": [
                        {
                            "segment_id": segment_id,
                            "start_sec": 0,
                            "end_sec": 4,
                        }
                    ],
                }
            ],
        },
    )
    assert blind.status_code == 201, blind.text

    active = test_client.get(
        "/api/v1/tag-reviews?status=active",
        headers=auth_headers["inspector_t1"],
    )
    assert active.status_code == 200, active.text
    assert {item["status"] for item in active.json()["items"]} == {"pending"}
    assert nonblind_id not in {int(item["id"]) for item in active.json()["items"]}, (
        "a non-blind candidate for the same cell must not enter the blind-safe pool"
    )
    for item in active.json()["items"]:
        assert item["proposed_value"] is None
        assert item["confidence"] is None
        assert item["evidence_refs"] == []
        assert item["proposed_fact_id"] is None
        assert item["tagger_version_id"] is None

    claimed = test_client.post(
        f"/api/v1/tag-reviews/{nonblind_id}/claim",
        headers=auth_headers["inspector_t1"],
    )
    assert claimed.status_code == 200, claimed.text
    refreshed = test_client.get(
        "/api/v1/tag-reviews?status=active",
        headers=auth_headers["inspector_t1"],
    )
    assert refreshed.status_code == 200, refreshed.text
    claimed_rows = [item for item in refreshed.json()["items"] if item["status"] == "claimed"]
    assert [int(item["id"]) for item in claimed_rows] == [nonblind_id]
    assert claimed_rows[0]["proposed_value"] == "browse"


def test_review_adjudication_requires_explicit_endpoint_and_claimed_state(
    test_client: TestClient,
    auth_headers: dict,
    db_session_factory: Any,
) -> None:
    unit_id, segment_id, schema_version_id = _run_async(_seed_review_resources(db_session_factory))
    batch = test_client.post(
        "/api/v1/tag-reviews/create-batch",
        headers=auth_headers["inspector_t1"],
        json={
            "reason": "conflict",
            "subjects": [
                {
                    "subject_type": "dialogue_unit",
                    "subject_id": unit_id,
                    "tag_key": "intent",
                    "proposed_value": "browse",
                    "schema_version_id": schema_version_id,
                    "evidence_refs": [{"segment_id": segment_id, "start_sec": 0, "end_sec": 4}],
                }
            ],
        },
    )
    assert batch.status_code == 201, batch.text
    task_id = batch.json()["items"][0]["id"]
    body = {
        "action": "correct",
        "corrected_value": "purchase",
        "reason_code": "arbitrated",
        "evidence_refs": [{"segment_id": segment_id, "start_sec": 0, "end_sec": 4}],
    }

    pending_adjudication = test_client.post(
        f"/api/v1/tag-reviews/{task_id}/adjudicate",
        headers=auth_headers["admin_t1"],
        json=body,
    )
    assert pending_adjudication.status_code == 409

    claimed = test_client.post(
        f"/api/v1/tag-reviews/{task_id}/claim",
        headers=auth_headers["inspector_t1"],
    )
    assert claimed.status_code == 200

    claimant_bypass = test_client.post(
        f"/api/v1/tag-reviews/{task_id}/decide",
        headers=auth_headers["admin_t1"],
        json={**body, "adjudication": True},
    )
    assert claimant_bypass.status_code == 422

    wrong_claimant = test_client.post(
        f"/api/v1/tag-reviews/{task_id}/decide",
        headers=auth_headers["admin_t1"],
        json=body,
    )
    assert wrong_claimant.status_code == 409

    direct_adjudication = test_client.post(
        f"/api/v1/tag-reviews/{task_id}/adjudicate",
        headers=auth_headers["admin_t1"],
        json=body,
    )
    assert direct_adjudication.status_code == 409


def test_public_evaluation_cannot_target_an_optimizer_bound_candidate(
    test_client: TestClient,
    auth_headers: dict,
    db_session_factory: Any,
) -> None:
    tagger_id, gold_version_id, baseline_tagger_id = _run_async(
        _seed_evaluation_resources(db_session_factory)
    )
    _run_async(
        _bind_api_optimization_candidate(
            db_session_factory,
            tagger_id=tagger_id,
            gold_version_id=gold_version_id,
            baseline_id=baseline_tagger_id,
        )
    )
    payload = {
        "tagger_version_id": tagger_id,
        "gold_set_version_id": gold_version_id,
        "baseline_tagger_version_id": baseline_tagger_id,
    }

    response = test_client.post(
        "/api/v1/tag-evaluations",
        headers={
            **auth_headers["inspector_t1"],
            "Idempotency-Key": "public-optimizer-evaluation",
        },
        json=payload,
    )
    forged = test_client.post(
        "/api/v1/tag-evaluations",
        headers={
            **auth_headers["inspector_t1"],
            "Idempotency-Key": "forged-optimizer-evaluation",
        },
        json={**payload, "trusted_optimization_binding": True},
    )

    assert response.status_code == 409, response.text
    assert "optimizer service" in response.json()["error"]["message"]
    assert forged.status_code == 422, forged.text


def test_evaluation_and_deployment_require_gate_then_admin_approval(
    test_client: TestClient,
    auth_headers: dict,
    db_session_factory: Any,
) -> None:
    tagger_id, gold_version_id, baseline_tagger_id = _run_async(
        _seed_evaluation_resources(db_session_factory)
    )
    evaluation = test_client.post(
        "/api/v1/tag-evaluations",
        headers={
            **auth_headers["inspector_t1"],
            "Idempotency-Key": "api-evaluation-candidate",
        },
        json={
            "tagger_version_id": tagger_id,
            "gold_set_version_id": gold_version_id,
            "baseline_tagger_version_id": baseline_tagger_id,
        },
    )
    replay = test_client.post(
        "/api/v1/tag-evaluations",
        headers={
            **auth_headers["inspector_t1"],
            "Idempotency-Key": "api-evaluation-candidate",
        },
        json={
            "tagger_version_id": tagger_id,
            "gold_set_version_id": gold_version_id,
            "baseline_tagger_version_id": baseline_tagger_id,
        },
    )
    assert evaluation.status_code == 202, evaluation.text
    assert replay.status_code == 202, replay.text
    assert evaluation.json()["job_id"] == replay.json()["job_id"]
    assert evaluation.json()["evaluation"]["status"] == "queued"

    evaluation_id = evaluation.json()["evaluation"]["id"]

    async def qualify_evaluation() -> None:
        from audio_graphy.models.tag_governance import (
            TagEvaluationRun,
            TaggerVersion,
        )

        async with db_session_factory() as session, session.begin():
            persisted = await session.get(TagEvaluationRun, evaluation_id)
            tagger = await session.get(TaggerVersion, tagger_id)
            assert persisted is not None and tagger is not None
            persisted.status = "completed"
            persisted.metrics = {
                "macro_f1": 0.88,
                "critical_recall": 0.97,
                "evidence_coverage": 0.99,
                "error_rate": 0.002,
                "holdout_only": True,
                "evaluation_lane": "holdout",
                "sealed_release": True,
            }
            persisted.baseline_metrics = {
                "macro_f1": 0.87,
                "critical_recall": 0.96,
            }
            persisted.passed = True
            persisted.finished_at = datetime.now(UTC)
            tagger.status = "qualified"
            tagger.qualified_at = datetime.now(UTC)

    _run_async(qualify_evaluation())

    rejected_override = test_client.post(
        "/api/v1/tag-deployments",
        headers=auth_headers["admin_t1"],
        json={
            "tagger_version_id": tagger_id,
            "evaluation_run_id": evaluation_id,
            "baseline_tagger_version_id": baseline_tagger_id,
            "override_reason": "客户端不得覆盖发布硬门禁",
        },
    )
    assert rejected_override.status_code == 422

    deployment = test_client.post(
        "/api/v1/tag-deployments",
        headers=auth_headers["admin_t1"],
        json={
            "tagger_version_id": tagger_id,
            "evaluation_run_id": evaluation_id,
            "baseline_tagger_version_id": baseline_tagger_id,
        },
    )
    assert deployment.status_code == 201, deployment.text
    deployment_id = deployment.json()["id"]
    assert deployment.json()["status"] == "shadow"

    from audio_graphy.services.tag_governance import TagGovernanceService

    service = TagGovernanceService(db_session_factory)
    observation_now = datetime.now(UTC)
    shadow_end = observation_now.replace(
        minute=(observation_now.minute // 5) * 5,
        second=0,
        microsecond=0,
    )
    stage_start = shadow_end - timedelta(hours=24)

    async def backdate_shadow_start() -> None:
        from audio_graphy.models.tag_governance import TagDeployment

        async with db_session_factory() as session, session.begin():
            persisted = await session.get(TagDeployment, deployment_id)
            assert persisted is not None
            persisted.created_at = stage_start - timedelta(hours=1)

    _run_async(backdate_shadow_start())
    monitor_reception_ids, monitor_dialogue_ids = _run_async(
        _seed_monitor_subjects(
            db_session_factory,
            reception_count=1_000,
            dialogue_count=5_000,
        )
    )
    observed_deployment = None
    for index, (
        stage,
        sample_count,
        served_count,
        paired_count,
        audited_count,
        duration_hours,
        expected,
    ) in enumerate(
        (
            ("shadow", 500, 0, 500, 100, 24, "canary_5"),
            ("canary_5", 1_000, 1_000, 0, 200, 24, "canary_25"),
            ("canary_25", 1_000, 5_000, 0, 500, 48, "awaiting_admin"),
        )
    ):
        history_window_count = duration_hours * 12 - 1
        _run_async(
            _seed_trusted_stage_history(
                db_session_factory,
                deployment_id=deployment_id,
                deployment_revision=index + 1,
                stage=stage,
                window_start=stage_start,
                window_count=history_window_count,
            )
        )
        window_end = stage_start + timedelta(hours=duration_hours)
        window_start = window_end - timedelta(minutes=5)
        _observation, observed_deployment = _run_async(
            service.record_deployment_observation(
                tenant_id="chang_an",
                deployment_id=deployment_id,
                sample_reception_ids=monitor_reception_ids[:sample_count],
                metrics={"error_rate": 0.001},
                breach_codes=[],
                window_start=window_start,
                window_end=window_end,
                actor_user_id=0,
                source="monitor",
                provenance={"collector": "api-test-monitor"},
                is_trusted=True,
                served_count=served_count,
                paired_count=paired_count,
                audited_count=audited_count,
                served_subject_keys=[
                    ("dialogue_unit", subject_id)
                    for subject_id in monitor_dialogue_ids[:served_count]
                ],
                paired_subject_keys=[
                    ("dialogue_unit", subject_id)
                    for subject_id in monitor_dialogue_ids[:paired_count]
                ],
                audited_subject_keys=[
                    ("dialogue_unit", subject_id)
                    for subject_id in monitor_dialogue_ids[:audited_count]
                ],
            )
        )
        assert observed_deployment.status == expected
        stage_start = window_end

    timeline = test_client.get(
        f"/api/v1/tag-deployments/{deployment_id}/observations?limit=2",
        headers=auth_headers["inspector_t1"],
    )
    assert timeline.status_code == 200, timeline.text
    assert timeline.json()["total"] == 2
    assert len(timeline.json()["items"]) == 2
    assert timeline.json()["items"][0]["window_end"] >= timeline.json()["items"][1]["window_end"]
    viewer_forbidden = test_client.get(
        f"/api/v1/tag-deployments/{deployment_id}/observations",
        headers=auth_headers["viewer_t1"],
    )
    assert viewer_forbidden.status_code == 403
    cross_tenant = test_client.get(
        f"/api/v1/tag-deployments/{deployment_id}/observations",
        headers=auth_headers["admin_t2"],
    )
    assert cross_tenant.status_code == 404

    assert observed_deployment is not None
    revision = observed_deployment.revision
    forbidden = test_client.post(
        f"/api/v1/tag-deployments/{deployment_id}/approve",
        headers={**auth_headers["inspector_t1"], "If-Match": str(revision)},
    )
    assert forbidden.status_code == 403
    stale = test_client.post(
        f"/api/v1/tag-deployments/{deployment_id}/approve",
        headers={**auth_headers["admin_t1"], "If-Match": str(revision - 1)},
    )
    assert stale.status_code == 409
    approved = test_client.post(
        f"/api/v1/tag-deployments/{deployment_id}/approve",
        headers={**auth_headers["admin_t1"], "If-Match": str(revision)},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "production"


def test_resume_requires_a_substantive_explicit_admin_reason(
    test_client: TestClient,
    auth_headers: dict,
) -> None:
    missing = test_client.post(
        "/api/v1/tag-deployments/999999/resume",
        headers={**auth_headers["admin_t1"], "If-Match": "1"},
        json={},
    )
    too_short = test_client.post(
        "/api/v1/tag-deployments/999999/resume",
        headers={**auth_headers["admin_t1"], "If-Match": "1"},
        json={"reason": "确认"},
    )

    assert missing.status_code == 422
    assert too_short.status_code == 422


def test_only_admin_can_force_release_a_review_claim(
    test_client: TestClient,
    auth_headers: dict,
) -> None:
    forbidden = test_client.post(
        "/api/v1/tag-reviews/999999/release",
        headers=auth_headers["inspector_t1"],
        json={"force": True},
    )

    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "TAG_REVIEW_FORCE_RELEASE_FORBIDDEN"
