"""API contracts for trustworthy tag-Harness evolution."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import _run_async, seed_tag
from tests.api.test_tag_governance import _seed_review_resources


def test_optimization_run_is_admin_only_and_budget_is_bounded(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
) -> None:
    payload = {
        "cohort": {"source": "manual_review"},
        "target_policy": {"policy": "balanced"},
        "search_budget": {
            "max_trials": 32,
            "sealed_holdout_queries": 1,
        },
    }

    forbidden = test_client.post(
        "/api/v1/tag-optimization-runs",
        headers=auth_headers["inspector_t1"],
        json=payload,
    )
    assert forbidden.status_code == 403

    invalid = test_client.post(
        "/api/v1/tag-optimization-runs",
        headers=auth_headers["admin_t1"],
        json={
            **payload,
            "search_budget": {
                "max_trials": 33,
                "sealed_holdout_queries": 1,
            },
        },
    )
    assert invalid.status_code == 422

    client_bound_ids = test_client.post(
        "/api/v1/tag-optimization-runs",
        headers=auth_headers["admin_t1"],
        json={
            **payload,
            "gold_set_version_id": 1,
            "trigger": "feedback_threshold",
        },
    )
    assert client_bound_ids.status_code == 422


def test_client_supplied_error_samples_are_rejected(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
) -> None:
    response = test_client.post(
        "/api/v1/tagger-versions/optimize",
        headers=auth_headers["admin_t1"],
        json={
            "gold_set_version_id": 1,
            "production_tagger_version_id": 1,
            "error_samples": [
                {
                    "gold_label_id": 1,
                    "predicted_value": "injected",
                    "score": 1,
                }
            ],
        },
    )

    assert response.status_code == 422


def test_public_tagger_creation_cannot_forge_optimizer_origin(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
) -> None:
    response = test_client.post(
        "/api/v1/tagger-versions",
        headers=auth_headers["admin_t1"],
        json={
            "schema_version_id": 1,
            "version": "forged-optimizer",
            "engine": "hybrid",
            "prompt_content": "forged optimizer candidate",
            "rule_bundle": {"dsl_version": "1", "rules": []},
            "model_version": "weak",
            "thresholds": {"intent": 0.7},
            "origin": "optimizer",
            "optimization_run_id": 1,
        },
    )
    assert response.status_code == 422


def test_optimization_cancel_and_compare_contracts_are_role_scoped(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
) -> None:
    viewer_compare = test_client.post(
        "/api/v1/tag-optimization-runs/999/compare",
        headers=auth_headers["viewer_t1"],
        json={"left_trial_id": 1, "right_trial_id": 2},
    )
    assert viewer_compare.status_code == 403

    inspector_compare = test_client.post(
        "/api/v1/tag-optimization-runs/999/compare",
        headers=auth_headers["inspector_t1"],
        json={"left_trial_id": 1, "right_trial_id": 2},
    )
    assert inspector_compare.status_code == 404

    inspector_cancel = test_client.post(
        "/api/v1/tag-optimization-runs/999/cancel",
        headers=auth_headers["inspector_t1"],
    )
    assert inspector_cancel.status_code == 403

    admin_cancel = test_client.post(
        "/api/v1/tag-optimization-runs/999/cancel",
        headers=auth_headers["admin_t1"],
    )
    assert admin_cancel.status_code == 404
    inspector_resume = test_client.post(
        "/api/v1/tag-deployments/999/resume",
        headers={**auth_headers["inspector_t1"], "If-Match": "1"},
        json={"reason": "review completed"},
    )
    assert inspector_resume.status_code == 403
    admin_resume = test_client.post(
        "/api/v1/tag-deployments/999/resume",
        headers={**auth_headers["admin_t1"], "If-Match": "1"},
        json={"reason": "review completed"},
    )
    assert admin_resume.status_code == 404

    same_trial = test_client.post(
        "/api/v1/tag-optimization-runs/999/compare",
        headers=auth_headers["inspector_t1"],
        json={"left_trial_id": 1, "right_trial_id": 1},
    )
    assert same_trial.status_code == 422


def test_evolution_read_models_are_inspector_scoped(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
) -> None:
    for path in (
        "/api/v1/tag-evolution/overview",
        "/api/v1/tag-badcases",
        "/api/v1/tag-optimization-runs",
    ):
        allowed = test_client.get(path, headers=auth_headers["inspector_t1"])
        assert allowed.status_code == 200, (path, allowed.text)

        forbidden = test_client.get(path, headers=auth_headers["viewer_t1"])
        assert forbidden.status_code == 403, path


def test_structured_review_decision_persists_feedback_lineage(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    from sqlalchemy import select

    from audio_graphy.models.tag_governance import (
        TagBadcase,
        TagExperienceCase,
        TagFeedbackEvent,
    )

    unit_id, segment_id, schema_version_id = _run_async(_seed_review_resources(db_session_factory))
    batch = test_client.post(
        "/api/v1/tag-reviews/create-batch",
        headers=auth_headers["inspector2_t1"],
        json={
            "reason": "random",
            "review_bundle_id": "audit-2026-07-25",
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
    created_task = batch.json()["items"][0]
    assert created_task["proposed_value"] is None
    assert created_task["confidence"] is None
    assert created_task["evidence_refs"] == []
    assert created_task["tagger_version_id"] is None
    task_id = created_task["id"]
    claimed = test_client.post(
        f"/api/v1/tag-reviews/{task_id}/claim",
        headers=auth_headers["inspector_t1"],
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["proposed_value"] is None
    assert claimed.json()["confidence"] is None
    assert claimed.json()["evidence_refs"] == []

    forged_t3 = test_client.post(
        f"/api/v1/tag-reviews/{task_id}/decide",
        headers=auth_headers["inspector_t1"],
        json={
            "action": "correct",
            "corrected_value": "purchase",
            "truth_state": "present",
            "truth_tier": "t3",
            "annotator_round": 3,
            "primary_failure_stage": "tag_reasoning",
            "reason_code": "forged_release_truth",
            "reviewer_confidence": 1,
            "evidence_refs": [{"segment_id": segment_id, "start_sec": 0, "end_sec": 4}],
        },
    )
    assert forged_t3.status_code == 422

    response = test_client.post(
        f"/api/v1/tag-reviews/{task_id}/decide",
        headers=auth_headers["inspector_t1"],
        json={
            "action": "correct",
            "corrected_value": "purchase",
            "truth_state": "present",
            "primary_failure_stage": "tag_reasoning",
            "reason_code": "model_misjudgment",
            "reason_codes": ["model_misjudgment", "missed_context"],
            "reviewer_confidence": 0.9,
            "review_duration_ms": 12_500,
            "note": "上下文明确为购买",
            "evidence_refs": [{"segment_id": segment_id, "start_sec": 0, "end_sec": 4}],
        },
    )

    assert response.status_code == 200, response.text
    decision = response.json()["decision"]
    assert decision["truth_state"] == "present"
    assert decision["primary_failure_stage"] == "tag_reasoning"
    assert decision["reason_codes"] == ["model_misjudgment", "missed_context"]

    async def _load_feedback() -> tuple[TagFeedbackEvent, TagBadcase, TagExperienceCase]:
        async with db_session_factory() as session:
            return (
                (await session.execute(select(TagFeedbackEvent))).scalar_one(),
                (await session.execute(select(TagBadcase))).scalar_one(),
                (await session.execute(select(TagExperienceCase))).scalar_one(),
            )

    feedback, badcase, experience = _run_async(_load_feedback())
    assert feedback.review_decision_id == decision["id"]
    assert feedback.training_eligible is True
    assert feedback.sampling_probability is None
    assert feedback.error_stage == "tag_reasoning"
    assert badcase.source_feedback_event_id == feedback.id
    assert badcase.failure_stage == "tag_reasoning"
    assert experience.source_feedback_event_id == feedback.id
    assert experience.source_badcase_id == badcase.id
    assert experience.eligible is True


def test_claimed_blind_review_blocks_governance_history_side_channels(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    from sqlalchemy import select

    from audio_graphy.models.recording import Recording

    unit_id, _segment_id, schema_version_id = _run_async(_seed_review_resources(db_session_factory))

    async def _recording_id() -> int:
        async with db_session_factory() as session:
            return int((await session.execute(select(Recording.id))).scalar_one())

    recording_id = _run_async(_recording_id())
    _run_async(seed_tag(db_session_factory, recording_id, "chang_an"))
    batch = test_client.post(
        "/api/v1/tag-reviews/create-batch",
        headers=auth_headers["inspector2_t1"],
        json={
            "reason": "random",
            "review_bundle_id": "blind-history-isolation",
            "subjects": [
                {
                    "subject_type": "dialogue_unit",
                    "subject_id": unit_id,
                    "tag_key": "intent",
                    "schema_version_id": schema_version_id,
                }
            ],
        },
    )
    assert batch.status_code == 201, batch.text
    task_id = int(batch.json()["items"][0]["id"])
    claimed = test_client.post(
        f"/api/v1/tag-reviews/{task_id}/claim",
        headers=auth_headers["inspector_t1"],
    )
    assert claimed.status_code == 200, claimed.text

    for path in (
        "/api/v1/tagger-versions",
        "/api/v1/tag-evolution/overview",
        "/api/v1/tag-badcases",
        "/api/v1/tag-optimization-runs",
        "/api/v1/tag-gold-sets",
        "/api/v1/tag-evaluations",
        "/api/v1/tag-deployments",
        "/api/v1/tag-deployments/999/observations",
        "/api/v1/tag-audit-events",
    ):
        response = test_client.get(path, headers=auth_headers["inspector_t1"])
        assert response.status_code == 403, (path, response.text)
        assert response.json()["error"]["code"] == "BLIND_REVIEW_ISOLATION"

    comparison = test_client.post(
        "/api/v1/tag-optimization-runs/999/compare",
        headers=auth_headers["inspector_t1"],
        json={"left_trial_id": 1, "right_trial_id": 2},
    )
    assert comparison.status_code == 403, comparison.text
    for path in (
        "/api/v1/tags/stats",
        f"/api/v1/recordings/{recording_id}/tags",
    ):
        response = test_client.get(path, headers=auth_headers["inspector_t1"])
        assert response.status_code == 403, (path, response.text)
    neutral_detail = test_client.get(
        f"/api/v1/recordings/{recording_id}",
        headers=auth_headers["inspector_t1"],
    )
    assert neutral_detail.status_code == 200, neutral_detail.text
    assert neutral_detail.json()["current_tags"] == []

    resolved = test_client.post(
        f"/api/v1/tag-reviews/{task_id}/decide",
        headers=auth_headers["inspector_t1"],
        json={
            "action": "reject",
            "truth_state": "absent",
            "primary_failure_stage": "tag_reasoning",
            "reason_code": "verified_absent",
            "reviewer_confidence": 0.95,
            "evidence_refs": [],
        },
    )
    assert resolved.status_code == 200, resolved.text
    assert (
        test_client.get(
            "/api/v1/tag-badcases",
            headers=auth_headers["inspector_t1"],
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "semantic_path",
    [
        "/api/v1/tag-badcases",
        "/api/v1/tags/stats",
        "/api/v1/recordings/{recording_id}/tags",
        "/api/v1/recordings/{recording_id}",
    ],
)
def test_semantic_read_reserves_reviewer_before_blind_claim(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
    semantic_path: str,
) -> None:
    from sqlalchemy import select

    from audio_graphy.models.recording import Recording

    unit_id, _segment_id, schema_version_id = _run_async(_seed_review_resources(db_session_factory))

    async def _recording_id() -> int:
        async with db_session_factory() as session:
            return int((await session.execute(select(Recording.id))).scalar_one())

    path = semantic_path.format(recording_id=_run_async(_recording_id()))
    batch = test_client.post(
        "/api/v1/tag-reviews/create-batch",
        headers=auth_headers["inspector2_t1"],
        json={
            "reason": "random",
            "review_bundle_id": "blind-history-pre-read",
            "subjects": [
                {
                    "subject_type": "dialogue_unit",
                    "subject_id": unit_id,
                    "tag_key": "intent",
                    "schema_version_id": schema_version_id,
                }
            ],
        },
    )
    assert batch.status_code == 201, batch.text
    task_id = int(batch.json()["items"][0]["id"])

    history = test_client.get(path, headers=auth_headers["inspector_t1"])
    assert history.status_code == 200, history.text

    claim = test_client.post(
        f"/api/v1/tag-reviews/{task_id}/claim",
        headers=auth_headers["inspector_t1"],
    )
    assert claim.status_code == 409, claim.text
    assert "previously accessed semantic output" in claim.json()["error"]["message"]


def test_blind_matrix_can_record_absent_without_a_model_fact(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    from sqlalchemy import select

    from audio_graphy.models.tag_governance import TagFeedbackEvent

    unit_id, _segment_id, schema_version_id = _run_async(_seed_review_resources(db_session_factory))
    batch = test_client.post(
        "/api/v1/tag-reviews/create-batch",
        headers=auth_headers["inspector2_t1"],
        json={
            "reason": "random",
            "review_bundle_id": "matrix-negative-2026-07-25",
            "subjects": [
                {
                    "subject_type": "dialogue_unit",
                    "subject_id": unit_id,
                    "tag_key": "intent",
                    "schema_version_id": schema_version_id,
                    "evidence_refs": [],
                }
            ],
        },
    )
    assert batch.status_code == 201, batch.text
    task_id = batch.json()["items"][0]["id"]
    assert (
        test_client.post(
            f"/api/v1/tag-reviews/{task_id}/claim",
            headers=auth_headers["inspector_t1"],
        ).status_code
        == 200
    )
    response = test_client.post(
        f"/api/v1/tag-reviews/{task_id}/decide",
        headers=auth_headers["inspector_t1"],
        json={
            "action": "reject",
            "truth_state": "absent",
            "primary_failure_stage": "tag_reasoning",
            "reason_code": "verified_absent",
            "reviewer_confidence": 0.95,
            "evidence_refs": [],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["fact"] is None
    assert response.json()["decision"]["truth_state"] == "absent"

    async def _feedback() -> TagFeedbackEvent:
        async with db_session_factory() as session:
            return (await session.execute(select(TagFeedbackEvent))).scalar_one()

    feedback = _run_async(_feedback())
    assert feedback.truth_state == "absent"
    assert feedback.training_eligible is True


def test_client_cannot_create_an_adjudication_batch(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    unit_id, _segment_id, schema_version_id = _run_async(_seed_review_resources(db_session_factory))

    response = test_client.post(
        "/api/v1/tag-reviews/create-batch",
        headers=auth_headers["inspector_t1"],
        json={
            "reason": "adjudication",
            "review_bundle_id": "client-forged-adjudication",
            "subjects": [
                {
                    "subject_type": "dialogue_unit",
                    "subject_id": unit_id,
                    "tag_key": "intent",
                    "schema_version_id": schema_version_id,
                }
            ],
        },
    )

    assert response.status_code == 422


def test_client_cannot_self_certify_representative_sampling_or_source_lineage(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    unit_id, _segment_id, schema_version_id = _run_async(_seed_review_resources(db_session_factory))
    base_subject = {
        "subject_type": "dialogue_unit",
        "subject_id": unit_id,
        "tag_key": "intent",
        "schema_version_id": schema_version_id,
    }

    for forged in (
        {
            "selection_policy": "representative_audit",
            "sampling_probability": 0.01,
        },
        {
            "subjects": [
                {
                    **base_subject,
                    "source_deployment_id": 1,
                    "source_extraction_run_id": 1,
                    "source_harness_execution_id": 1,
                }
            ],
        },
    ):
        response = test_client.post(
            "/api/v1/tag-reviews/create-batch",
            headers=auth_headers["inspector_t1"],
            json={
                "reason": "random",
                "subjects": [base_subject],
                **forged,
            },
        )
        assert response.status_code == 422


def test_critical_truth_requires_two_reviewers_then_a_third_adjudicator(
    test_client: TestClient,
    auth_headers: dict[str, dict[str, str]],
    db_session_factory: Any,
) -> None:
    from sqlalchemy import func, select

    from audio_graphy.models.tag_governance import (
        TagAssignmentCurrent,
        TagAssignmentFact,
        TagFeedbackEvent,
        TagReviewTask,
    )
    from audio_graphy.services.tag_governance import TagGovernanceService

    unit_id, segment_id, schema_version_id = _run_async(_seed_review_resources(db_session_factory))
    evidence = [{"segment_id": segment_id, "start_sec": 0, "end_sec": 4}]
    batch = test_client.post(
        "/api/v1/tag-reviews/create-batch",
        headers=auth_headers["inspector2_t1"],
        json={
            "reason": "critical",
            "review_bundle_id": "critical-release-2026-07-25",
            "subjects": [
                {
                    "subject_type": "dialogue_unit",
                    "subject_id": unit_id,
                    "tag_key": "intent",
                    "proposed_value": "browse",
                    "schema_version_id": schema_version_id,
                    "evidence_refs": evidence,
                }
            ],
        },
    )
    assert batch.status_code == 201, batch.text
    tasks = batch.json()["items"]
    assert len(tasks) == 2
    first_id, second_id = (int(tasks[0]["id"]), int(tasks[1]["id"]))
    decision_body = {
        "action": "correct",
        "corrected_value": "purchase",
        "truth_state": "present",
        "primary_failure_stage": "tag_reasoning",
        "reason_code": "independent_label",
        "reviewer_confidence": 0.95,
        "evidence_refs": evidence,
    }

    assert (
        test_client.post(
            f"/api/v1/tag-reviews/{first_id}/claim",
            headers=auth_headers["inspector_t1"],
        ).status_code
        == 200
    )
    first = test_client.post(
        f"/api/v1/tag-reviews/{first_id}/decide",
        headers=auth_headers["inspector_t1"],
        json=decision_body,
    )
    assert first.status_code == 200, first.text
    assert first.json()["fact"] is None

    repeated_reviewer = test_client.post(
        f"/api/v1/tag-reviews/{second_id}/claim",
        headers=auth_headers["inspector_t1"],
    )
    assert repeated_reviewer.status_code == 409
    assert (
        test_client.post(
            f"/api/v1/tag-reviews/{second_id}/claim",
            headers=auth_headers["admin_t1"],
        ).status_code
        == 200
    )
    second = test_client.post(
        f"/api/v1/tag-reviews/{second_id}/decide",
        headers=auth_headers["admin_t1"],
        json=decision_body,
    )
    assert second.status_code == 200, second.text
    assert second.json()["fact"] is None

    async def _adjudicate() -> tuple[int, list[TagFeedbackEvent], int, int]:
        service = TagGovernanceService(db_session_factory)
        async with db_session_factory() as session:
            adjudication_task = (
                await session.execute(
                    select(TagReviewTask).where(
                        TagReviewTask.reason == "adjudication",
                        TagReviewTask.review_bundle_id == "critical-release-2026-07-25",
                    )
                )
            ).scalar_one()
            adjudication_id = adjudication_task.id
        await service.claim_review(
            tenant_id="chang_an",
            task_id=adjudication_id,
            reviewer_user_id=3,
        )
        _task, decision, fact = await service.decide_review(
            tenant_id="chang_an",
            task_id=adjudication_id,
            reviewer_user_id=3,
            action="correct",
            corrected_value="purchase",
            reason_code="third_reviewer_consensus",
            note=None,
            evidence_refs=evidence,
            adjudication=True,
            truth_state="present",
            truth_tier="t3",
            annotator_round=3,
            primary_failure_stage="tag_reasoning",
            reviewer_confidence=0.99,
        )
        assert fact is None
        async with db_session_factory() as session:
            feedback = list(
                (await session.execute(select(TagFeedbackEvent).order_by(TagFeedbackEvent.id)))
                .scalars()
                .all()
            )
            fact_count = int(
                (await session.execute(select(func.count(TagAssignmentFact.id)))).scalar_one()
            )
            current_count = int(
                (await session.execute(select(func.count(TagAssignmentCurrent.id)))).scalar_one()
            )
        return decision.id, feedback, fact_count, current_count

    decision_id, feedback, fact_count, current_count = _run_async(_adjudicate())
    assert decision_id > 0
    assert fact_count == 0
    assert current_count == 0
    assert [item.truth_tier for item in feedback] == ["t2", "t2", "t3"]
    assert [item.training_eligible for item in feedback] == [False, False, False]
