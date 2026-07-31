"""Real, holdout-only tag evaluation and durable evaluate-job contracts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from audio_graphy.models import Base
from audio_graphy.models.tag_governance import (
    TagEvaluationRun,
    TagExtractionJob,
    TagGateResult,
    TaggerVersion,
    TagGoldLabel,
    TagGoldSet,
    TagGoldSetVersion,
    TagOptimizationRun,
    TagOptimizationTrial,
    TagSchema,
    TagSchemaVersion,
)


@pytest.fixture
async def evaluator_factory() -> async_sessionmaker[AsyncSession]:
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


def test_metric_summary_penalizes_wrong_values_missing_evidence_and_small_support() -> None:
    from audio_graphy.services.tag_evaluator import compute_evaluation_summary

    gold = [
        {
            "subject_type": "dialogue_unit",
            "subject_id": subject_id,
            "tag_key": "intent",
            "tag_value": "purchase",
        }
        for subject_id in range(1, 4)
    ]
    predictions = {
        ("dialogue_unit", 1): [
            {
                "tag_key": "intent",
                "tag_value": "purchase",
                "evidence_refs": [{"segment_id": 1}],
            }
        ],
        ("dialogue_unit", 2): [
            {
                "tag_key": "intent",
                "tag_value": "browse",
                "evidence_refs": [{"segment_id": 2}],
            }
        ],
        ("dialogue_unit", 3): [
            {
                "tag_key": "intent",
                "tag_value": "purchase",
                "evidence_refs": [],
            }
        ],
    }

    summary = compute_evaluation_summary(
        gold_labels=gold,
        predictions=predictions,
        definitions={
            "intent": {
                "critical": True,
                "evidence_required": True,
            }
        },
        extraction_errors=0,
        subject_count=3,
    )

    # One-vs-rest macro-F1 includes the false-positive "browse" value.
    assert summary.metrics["macro_f1"] == pytest.approx(0.4)
    assert summary.metrics["critical_recall"] == pytest.approx(2 / 3)
    assert summary.metrics["evidence_coverage"] == pytest.approx(1 / 3)
    assert summary.label_metrics["intent"]["support"] == 3
    assert summary.insufficient_labels == ("intent",)
    assert summary.confusion["intent"]["purchase"]["browse"] == 1


def test_metric_summary_reports_calibration_evidence_iou_and_integrity_violations() -> None:
    from audio_graphy.services.tag_evaluator import compute_evaluation_summary

    summary = compute_evaluation_summary(
        gold_labels=[
            {
                "subject_type": "dialogue_unit",
                "subject_id": 1,
                "tag_key": "intent",
                "tag_value": "purchase",
                "truth_state": "present",
                "evidence_refs": [{"segment_id": 11, "start_sec": 0.0, "end_sec": 4.0}],
            },
            {
                "subject_type": "dialogue_unit",
                "subject_id": 2,
                "tag_key": "intent",
                "tag_value": None,
                "truth_state": "absent",
                "evidence_refs": [],
            },
        ],
        predictions={
            ("dialogue_unit", 1): [
                {
                    "tag_key": "intent",
                    "tag_value": "purchase",
                    "confidence": 0.8,
                    "evidence_refs": [{"segment_id": 11, "start_sec": 2.0, "end_sec": 6.0}],
                }
            ],
            ("dialogue_unit", 2): [
                {
                    "tag_key": "intent",
                    "tag_value": "purchase",
                    "confidence": 0.9,
                    "evidence_refs": [],
                },
                {
                    "tag_key": "unregistered",
                    "tag_value": "anything",
                    "confidence": 0.7,
                    "evidence_refs": [],
                },
            ],
        },
        definitions={
            "intent": {
                "allowed_values": ["browse", "purchase"],
                "subject_types": ["dialogue_unit"],
                "evidence_required": True,
            }
        },
        extraction_errors=0,
        subject_count=2,
    )

    assert summary.metrics["evidence_iou"] == pytest.approx(1 / 3)
    assert summary.metrics["brier_score"] == pytest.approx((0.2**2 + 0.9**2) / 2)
    assert summary.metrics["ece"] == pytest.approx((0.2 + 0.9) / 2)
    assert summary.metrics["schema_violation_count"] == 1
    assert summary.metrics["evidence_violation_count"] == 1
    assert summary.metrics["lineage_violation_count"] == 0
    assert summary.value_metrics["intent"]["purchase"]["f2"] == pytest.approx(5 / 6)


def test_metric_summary_treats_null_gold_as_an_expected_absence() -> None:
    from audio_graphy.services.tag_evaluator import compute_evaluation_summary

    summary = compute_evaluation_summary(
        gold_labels=[
            {
                "subject_type": "dialogue_unit",
                "subject_id": 1,
                "tag_key": "intent",
                "tag_value": None,
            },
            {
                "subject_type": "dialogue_unit",
                "subject_id": 2,
                "tag_key": "intent",
                "tag_value": None,
            },
        ],
        predictions={
            ("dialogue_unit", 2): [
                {
                    "tag_key": "intent",
                    "tag_value": "purchase",
                    "evidence_refs": [{"segment_id": 2}],
                }
            ]
        },
        definitions={"intent": {"critical": True, "evidence_required": True}},
        extraction_errors=0,
        subject_count=2,
    )

    assert summary.label_metrics["intent"]["true_negatives"] == 1
    assert summary.label_metrics["intent"]["support"] == 2
    assert summary.confusion["intent"]["__absent__"]["__missing__"] == 1
    assert summary.confusion["intent"]["__absent__"]["purchase"] == 1
    assert summary.metrics["critical_recall"] == 0
    assert summary.metrics["critical_recall_lcb"] == 0
    assert summary.critical_value_metrics["intent"]["purchase"]["support"] == 0
    assert summary.metrics["evidence_coverage"] == 1


def test_metric_summary_ignores_sparse_unknown_and_non_applicable_predictions() -> None:
    from audio_graphy.services.tag_evaluator import compute_evaluation_summary

    summary = compute_evaluation_summary(
        gold_labels=[
            {
                "subject_type": "dialogue_unit",
                "subject_id": 1,
                "tag_key": "intent",
                "tag_value": "purchase",
                "truth_state": "present",
            },
            {
                "subject_type": "dialogue_unit",
                "subject_id": 2,
                "tag_key": "intent",
                "tag_value": None,
                "truth_state": "uncertain",
            },
            {
                "subject_type": "dialogue_unit",
                "subject_id": 3,
                "tag_key": "intent",
                "tag_value": None,
                "truth_state": "not_applicable",
            },
            {
                "subject_type": "dialogue_unit",
                "subject_id": 4,
                "tag_key": "intent",
                "tag_value": None,
                "truth_state": "unknown",
            },
            {
                "subject_type": "dialogue_unit",
                "subject_id": 5,
                "tag_key": "intent",
                "tag_value": None,
                "truth_state": "NA",
            },
        ],
        predictions={
            ("dialogue_unit", 1): [
                {"tag_key": "intent", "tag_value": "purchase", "evidence_refs": []}
            ],
            ("dialogue_unit", 2): [
                {"tag_key": "intent", "tag_value": "browse", "evidence_refs": []}
            ],
            ("dialogue_unit", 3): [
                {"tag_key": "intent", "tag_value": "browse", "evidence_refs": []}
            ],
            ("dialogue_unit", 4): [
                {"tag_key": "intent", "tag_value": "browse", "evidence_refs": []}
            ],
            ("dialogue_unit", 5): [
                {"tag_key": "intent", "tag_value": "browse", "evidence_refs": []}
            ],
            # No gold row means unknown, not an implicit negative.
            ("dialogue_unit", 999): [
                {"tag_key": "intent", "tag_value": "browse", "evidence_refs": []}
            ],
        },
        definitions={
            "intent": {
                "allowed_values": ["purchase", "browse", "none"],
                "negative_values": ["none"],
            }
        },
        extraction_errors=0,
        subject_count=6,
    )

    assert summary.label_metrics["intent"]["support"] == 1
    assert summary.label_metrics["intent"]["precision"] == 1
    assert summary.value_metrics["intent"]["purchase"]["false_positives"] == 0
    assert summary.value_metrics["intent"]["browse"]["false_positives"] == 0
    assert summary.confusion["intent"] == {"purchase": {"purchase": 1}}


def test_explicit_critical_value_without_positive_truth_cannot_pass_wilson_gate() -> None:
    from audio_graphy.services.tag_evaluator import compute_evaluation_summary

    summary = compute_evaluation_summary(
        gold_labels=[
            {
                "subject_type": "dialogue_unit",
                "subject_id": subject_id,
                "tag_key": "risk",
                "tag_value": "none",
                "truth_state": "present",
            }
            for subject_id in range(1, 31)
        ],
        predictions={
            ("dialogue_unit", subject_id): [
                {"tag_key": "risk", "tag_value": "none", "evidence_refs": []}
            ]
            for subject_id in range(1, 31)
        },
        definitions={
            "risk": {
                "allowed_values": ["none", "high"],
                "critical_values": ["high"],
                "negative_values": ["none"],
            }
        },
        extraction_errors=0,
        subject_count=30,
    )

    assert summary.metrics["critical_recall"] == 0
    assert summary.metrics["critical_recall_lcb"] == 0
    assert summary.metrics["critical_positive_support"] == 0
    assert summary.critical_value_metrics["risk"]["high"]["support"] == 0


@pytest.mark.parametrize(
    ("positive_support", "expected_to_pass"),
    [
        (0, False),
        (1, False),
        (100, True),
    ],
)
def test_critical_flag_enforces_wilson_gate_for_every_registered_positive_value(
    positive_support: int,
    expected_to_pass: bool,
) -> None:
    from audio_graphy.services.tag_evaluator import (
        compute_evaluation_summary,
        wilson_lower_bound,
    )
    from audio_graphy.services.tag_governance import evaluate_quality_gates

    gold = [
        {
            "subject_type": "dialogue_unit",
            "subject_id": subject_id,
            "tag_key": "risk",
            "tag_value": "high" if subject_id <= positive_support else "none",
            "truth_state": "present",
        }
        for subject_id in range(1, max(positive_support, 30) + 1)
    ]
    predictions = {
        ("dialogue_unit", subject_id): [
            {
                "tag_key": "risk",
                "tag_value": "high" if subject_id <= positive_support else "none",
                "evidence_refs": [],
            }
        ]
        for subject_id in range(1, max(positive_support, 30) + 1)
    }
    summary = compute_evaluation_summary(
        gold_labels=gold,
        predictions=predictions,
        definitions={
            "risk": {
                "allowed_values": ["none", "high"],
                "negative_values": ["none"],
                "critical": True,
            }
        },
        extraction_errors=0,
        subject_count=len(gold),
    )

    high = summary.critical_value_metrics["risk"]["high"]
    expected_lcb = wilson_lower_bound(positive_support, positive_support)
    assert summary.metrics["critical_lcb_enforced"] == 1
    assert high["support"] == positive_support
    assert high["recall_lcb"] == pytest.approx(expected_lcb)
    assert summary.metrics["critical_recall_lcb"] == pytest.approx(expected_lcb)

    evaluation = evaluate_quality_gates(
        metrics=summary.metrics,
        baseline=summary.metrics,
        supported_label_f1={"risk": float(summary.label_metrics["risk"]["f1"])},
        baseline_label_f1={"risk": float(summary.label_metrics["risk"]["f1"])},
    )
    critical_gate = next(gate for gate in evaluation.gates if gate.code == "critical_recall")
    assert critical_gate.passed is expected_to_pass


def test_evaluation_summaries_keep_dialogue_unit_and_reception_cohorts_separate() -> None:
    from audio_graphy.services.tag_evaluator import (
        compute_evaluation_summaries_by_subject_type,
    )

    gold = [
        {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "tag_key": "intent",
            "tag_value": "purchase",
            "truth_state": "present",
        }
        for subject_type in ("dialogue_unit", "reception")
        for subject_id in range(1, 31)
    ]
    predictions = {
        ("dialogue_unit", subject_id): [
            {"tag_key": "intent", "tag_value": "purchase", "evidence_refs": []}
        ]
        for subject_id in range(1, 31)
    }

    summaries = compute_evaluation_summaries_by_subject_type(
        gold_labels=gold,
        predictions=predictions,
        definitions={
            "intent": {
                "allowed_values": ["browse", "purchase"],
                "subject_types": ["dialogue_unit", "reception"],
            }
        },
        extraction_errors_by_subject_type={
            "dialogue_unit": 0,
            "reception": 0,
        },
    )

    assert summaries["dialogue_unit"].metrics["macro_f1"] == 1
    assert summaries["dialogue_unit"].metrics["error_rate"] == 0
    assert summaries["reception"].metrics["macro_f1"] == 0
    assert summaries["reception"].label_metrics["intent"]["recall"] == 0
    assert summaries["dialogue_unit"].label_metrics["intent"]["support"] == 30
    assert summaries["reception"].label_metrics["intent"]["support"] == 30


def test_evaluation_slice_summaries_keep_scene_and_store_dimensions_explicit() -> None:
    from audio_graphy.services.tag_evaluator import compute_evaluation_summaries_by_slice

    gold = [
        {
            "subject_type": "reception",
            "subject_id": subject_id,
            "tag_key": "intent",
            "tag_value": "purchase",
            "truth_state": "present",
            "scenario": "sales",
            "store_id": "north" if subject_id <= 30 else "south",
        }
        for subject_id in range(1, 61)
    ]
    predictions = {
        ("reception", subject_id): [
            {"tag_key": "intent", "tag_value": "purchase", "evidence_refs": []}
        ]
        for subject_id in range(1, 31)
    }

    summaries = compute_evaluation_summaries_by_slice(
        gold_labels=gold,
        predictions=predictions,
        definitions={"intent": {"allowed_values": ["purchase"]}},
    )

    assert summaries["reception|scenario=sales"].label_metrics["intent"]["support"] == 60
    assert summaries["reception|store_id=north"].metrics["macro_f1"] == 1
    assert summaries["reception|store_id=south"].metrics["macro_f1"] == 0


def test_metric_summary_computes_one_vs_rest_values_and_critical_wilson_lcb() -> None:
    from audio_graphy.services.tag_evaluator import (
        compute_evaluation_summary,
        wilson_lower_bound,
    )

    gold = [
        {
            "subject_type": "dialogue_unit",
            "subject_id": subject_id,
            "tag_key": "intent",
            "tag_value": "purchase",
            "truth_state": "present",
        }
        for subject_id in range(1, 101)
    ]
    gold.extend(
        [
            {
                "subject_type": "dialogue_unit",
                "subject_id": 101,
                "tag_key": "intent",
                "tag_value": "browse",
                "truth_state": "present",
            },
            {
                "subject_type": "dialogue_unit",
                "subject_id": 102,
                "tag_key": "intent",
                "tag_value": None,
                "truth_state": "absent",
            },
        ]
    )
    predictions = {
        ("dialogue_unit", subject_id): [
            {"tag_key": "intent", "tag_value": "purchase", "evidence_refs": []}
        ]
        for subject_id in range(1, 101)
    }
    predictions[("dialogue_unit", 101)] = [
        {"tag_key": "intent", "tag_value": "purchase", "evidence_refs": []}
    ]
    predictions[("dialogue_unit", 102)] = [
        {"tag_key": "intent", "tag_value": "browse", "evidence_refs": []}
    ]

    summary = compute_evaluation_summary(
        gold_labels=gold,
        predictions=predictions,
        definitions={
            "intent": {
                "allowed_values": ["purchase", "browse", "none"],
                "critical_values": ["purchase"],
                "negative_values": ["none"],
            }
        },
        extraction_errors=0,
        subject_count=102,
    )

    purchase = summary.value_metrics["intent"]["purchase"]
    browse = summary.value_metrics["intent"]["browse"]
    assert purchase["true_positives"] == 100
    assert purchase["false_positives"] == 1
    assert purchase["false_negatives"] == 0
    assert browse["true_positives"] == 0
    assert browse["false_positives"] == 1
    assert browse["false_negatives"] == 1
    assert summary.metrics["critical_recall"] == 1
    assert summary.metrics["critical_recall_lcb"] == pytest.approx(wilson_lower_bound(100, 100))
    assert summary.metrics["critical_recall_lcb"] > 0.95
    assert summary.critical_value_metrics["intent"]["purchase"]["recall_lcb"] > 0.95


def test_paired_comparison_uses_only_explicit_truth_rows() -> None:
    from audio_graphy.services.tag_evaluator import compute_paired_comparison

    gold = [
        {
            "subject_type": "dialogue_unit",
            "subject_id": subject_id,
            "tag_key": "intent",
            "tag_value": "purchase",
            "truth_state": "present",
        }
        for subject_id in range(1, 4)
    ]
    gold.append(
        {
            "subject_type": "dialogue_unit",
            "subject_id": 4,
            "tag_key": "intent",
            "tag_value": None,
            "truth_state": "uncertain",
        }
    )
    candidate = {
        ("dialogue_unit", subject_id): [{"tag_key": "intent", "tag_value": "purchase"}]
        for subject_id in range(1, 5)
    }
    baseline = {
        ("dialogue_unit", 1): [{"tag_key": "intent", "tag_value": "purchase"}],
        ("dialogue_unit", 2): [{"tag_key": "intent", "tag_value": "purchase"}],
        ("dialogue_unit", 3): [{"tag_key": "intent", "tag_value": "browse"}],
        ("dialogue_unit", 4): [{"tag_key": "intent", "tag_value": "browse"}],
    }

    comparison = compute_paired_comparison(
        gold_labels=gold,
        candidate_predictions=candidate,
        baseline_predictions=baseline,
    )

    assert comparison.support == 3
    assert comparison.candidate_wins == 1
    assert comparison.baseline_wins == 0
    assert comparison.delta == pytest.approx(1 / 3)
    assert comparison.lower_bound <= comparison.delta <= comparison.upper_bound


def test_reception_lane_isolation_rejects_cross_split_leakage() -> None:
    from audio_graphy.services.tag_evaluator import validate_reception_lane_isolation
    from audio_graphy.services.tag_governance import GovernanceConflictError

    labels = [
        SimpleNamespace(reception_id=42, split="challenge"),
        SimpleNamespace(reception_id=42, split="holdout"),
    ]

    with pytest.raises(GovernanceConflictError, match="Reception leakage"):
        validate_reception_lane_isolation(labels)  # type: ignore[arg-type]


@dataclass(frozen=True)
class _Prediction:
    assignments: tuple[dict[str, Any], ...]


class _PerfectPredictor:
    async def predict_dialogue_unit(
        self,
        *,
        tenant_id: str,
        dialogue_unit_id: int,
        tagger_version_id: int,
    ) -> _Prediction:
        assert tenant_id == "chang_an"
        assert tagger_version_id > 0
        return _Prediction(
            assignments=(
                {
                    "tag_key": "intent",
                    "tag_value": "purchase",
                    "confidence": 0.99,
                    "evidence_refs": [{"segment_id": dialogue_unit_id}],
                    "source": "rule",
                },
            )
        )


class _FrozenSnapshotPredictor:
    def __init__(self) -> None:
        self.snapshots: list[dict[str, Any]] = []

    async def predict_dialogue_unit(self, **_: Any) -> _Prediction:
        raise AssertionError("live input must not be read when a frozen snapshot exists")

    async def predict_frozen_input(
        self,
        *,
        tenant_id: str,
        subject_type: str,
        subject_id: int,
        input_snapshot: dict[str, Any],
        tagger_version_id: int,
    ) -> _Prediction:
        assert tenant_id == "chang_an"
        assert subject_type == "dialogue_unit"
        assert subject_id == 7
        assert tagger_version_id == 11
        self.snapshots.append(input_snapshot)
        return _Prediction(assignments=())


@pytest.mark.asyncio
async def test_predict_replays_frozen_input_snapshot_instead_of_live_subject() -> None:
    from audio_graphy.services.tag_evaluator import TagEvaluationService

    predictor = _FrozenSnapshotPredictor()
    service = TagEvaluationService(
        None,  # type: ignore[arg-type]
        predictor=predictor,
    )
    labels = [
        SimpleNamespace(
            subject_type="dialogue_unit",
            subject_id=7,
            input_snapshot={"transcript": "冻结输入", "segments": [{"id": 1}]},
            input_hash="a" * 64,
        ),
        SimpleNamespace(
            subject_type="dialogue_unit",
            subject_id=7,
            input_snapshot={"transcript": "冻结输入", "segments": [{"id": 1}]},
            input_hash="a" * 64,
        ),
    ]

    output, errors = await service._predict(
        tenant_id="chang_an",
        tagger_version_id=11,
        labels=labels,  # type: ignore[arg-type]
        evaluation_run_id=17,
    )

    assert errors == 0
    assert output == {("dialogue_unit", 7): ()}
    assert predictor.snapshots == [{"transcript": "冻结输入", "segments": [{"id": 1}]}]


async def _seed_frozen_holdout(
    factory: async_sessionmaker[AsyncSession],
    *,
    schema_subject_types: tuple[str, ...] = ("dialogue_unit",),
) -> tuple[int, int, int]:
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        schema = TagSchema(
            tenant_id="chang_an",
            key="evaluation",
            name="评估标签体系",
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
                    "negative_values": ["browse"],
                    "subject_types": list(schema_subject_types),
                    "evidence_required": True,
                    "critical": True,
                }
            ],
            checksum="a" * 64,
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
            version="candidate-1",
            engine="rule",
            prompt_content="",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="rules-local",
            thresholds={"intent": 0.7},
            config_checksum="b" * 64,
            status="draft",
            created_by=1,
        )
        baseline = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=schema_version.id,
            version="baseline-1",
            engine="rule",
            prompt_content="",
            rule_bundle={"dsl_version": "1", "rules": []},
            model_version="rules-baseline",
            thresholds={"intent": 0.7},
            config_checksum="d" * 64,
            status="qualified",
            created_by=1,
            qualified_at=now,
        )
        session.add_all([tagger, baseline])
        gold_set = TagGoldSet(
            tenant_id="chang_an",
            key="gold",
            name="冻结黄金集",
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
            checksum="c" * 64,
            completeness_manifest={
                "complete": True,
                "legacy_sparse": False,
                "missing_label_count": 0,
                "missing_input_snapshot_count": 0,
                "weak_truth_count": 0,
            },
            item_count=100,
            frozen_by=1,
            frozen_at=now,
        )
        session.add(version)
        await session.flush()
        labels = [
            TagGoldLabel(
                tenant_id="chang_an",
                gold_set_version_id=version.id,
                review_decision_id=1_000 + subject_id,
                reception_id=10_000 + subject_id,
                subject_type="dialogue_unit",
                subject_id=subject_id,
                tag_key="intent",
                tag_value="purchase",
                evidence_refs=[{"segment_id": subject_id}],
                truth_state="present",
                truth_tier="t3",
                input_hash=f"{subject_id:064x}",
                input_snapshot={
                    "subject_type": "dialogue_unit",
                    "subject_id": subject_id,
                    "scenario": "sales",
                    "store_id": "north",
                    "segments": [{"id": subject_id}],
                },
                completeness_manifest=version.completeness_manifest,
                split="holdout",
            )
            for subject_id in range(1, 101)
        ]
        labels.append(
            TagGoldLabel(
                tenant_id="chang_an",
                gold_set_version_id=version.id,
                review_decision_id=2_001,
                reception_id=20_001,
                subject_type="dialogue_unit",
                subject_id=2_001,
                tag_key="intent",
                tag_value="browse",
                evidence_refs=[{"segment_id": 2_001}],
                truth_state="present",
                truth_tier="t2",
                input_hash=f"{2_001:064x}",
                input_snapshot={
                    "subject_type": "dialogue_unit",
                    "subject_id": 2_001,
                    "scenario": "sales",
                    "store_id": "south",
                    "segments": [{"id": 2_001}],
                },
                completeness_manifest=version.completeness_manifest,
                split="challenge",
            )
        )
        session.add_all(labels)
        await session.flush()
        from audio_graphy.services.tag_governance import (
            compute_gold_dataset_snapshot_hash,
        )

        version.dataset_snapshot_hash = compute_gold_dataset_snapshot_hash(
            [
                {
                    "review_decision_id": item.review_decision_id,
                    "reception_id": item.reception_id,
                    "subject_type": item.subject_type,
                    "subject_id": item.subject_id,
                    "tag_key": item.tag_key,
                    "tag_value": item.tag_value,
                    "truth_state": item.truth_state,
                    "truth_tier": item.truth_tier,
                    "evidence_refs": item.evidence_refs,
                    "input_hash": item.input_hash,
                    "input_snapshot": item.input_snapshot,
                    "annotation_quality": item.annotation_quality,
                    "cohort": item.cohort,
                    "completeness_manifest": item.completeness_manifest,
                    "split": item.split,
                }
                for item in labels
            ]
        )
        version.checksum = version.dataset_snapshot_hash
        version.item_count = len(labels)
        return tagger.id, version.id, baseline.id


async def _bind_optimizer_run(
    factory: async_sessionmaker[AsyncSession],
    *,
    tagger_id: int,
    gold_version_id: int,
    baseline_id: int,
) -> tuple[int, int]:
    harness_spec = {
        "context": {"neighbor_units": 0},
        "tools": {"primary_model": "weak"},
        "generation": {"temperature": 0},
        "orchestration": {"route": "rule_only"},
        "memory": {"policy": "none"},
        "output": {"review_threshold": 0.7},
    }
    async with factory() as session, session.begin():
        gold_version = await session.get(TagGoldSetVersion, gold_version_id)
        candidate = await session.get(TaggerVersion, tagger_id)
        assert gold_version is not None
        assert candidate is not None
        gold_version.completeness_manifest = {
            "complete": True,
            "legacy_sparse": False,
        }
        candidate.harness_spec = harness_spec
        candidate.parent_version_id = baseline_id
        candidate.origin = "optimizer"
        job = TagExtractionJob(
            tenant_id="chang_an",
            job_type="optimize",
            status="queued",
            scope={"optimization_run_id": 0},
            tagger_version_id=baseline_id,
            idempotency_key=f"optimization-for-{tagger_id}",
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
        run = TagOptimizationRun(
            tenant_id="chang_an",
            baseline_tagger_version_id=baseline_id,
            gold_set_version_id=gold_version_id,
            job_id=job.id,
            dataset_snapshot_hash=str(gold_version.dataset_snapshot_hash),
            trigger="manual",
            status="queued",
            phase="prepare",
            cohort={"source": "eligible_feedback"},
            objective={"policy": "balanced"},
            search_budget={"max_trials": 1, "sealed_holdout_queries": 1},
            summary={},
            next_actions=["execute_bounded_search"],
            artifacts=[],
            created_by=1,
        )
        session.add(run)
        await session.flush()
        job.scope = {"optimization_run_id": run.id}
        candidate.optimization_run_id = run.id
        session.add(
            TagOptimizationTrial(
                tenant_id="chang_an",
                optimization_run_id=run.id,
                ordinal=1,
                mutation={"description": "baseline"},
                harness_spec=harness_spec,
                status="pending",
                phase="train",
                reward_vector={},
                metrics={},
                gate_results={},
                summary={},
                next_actions=[],
                artifacts=[],
            )
        )
        return run.id, job.id


def _optimizer_result_metadata() -> dict[str, Any]:
    return {
        "bounded_search": {
            "winner": {"index": 0},
            "trials": [
                {
                    "index": 0,
                    "mutation": "baseline",
                    "reward": {
                        "feasible": True,
                        "quality_delta": 0.0,
                        "review_rate_delta": 0.0,
                        "p95_latency_delta": 0.0,
                        "cost_delta": 0.0,
                    },
                    "metrics": {"macro_f1": 1.0},
                }
            ],
        }
    }


@pytest.mark.asyncio
async def test_optimizer_candidate_rejects_public_and_unbound_evaluation_enqueue(
    evaluator_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_evaluator import TagEvaluationService
    from audio_graphy.services.tag_governance import GovernanceConflictError

    tagger_id, gold_version_id, baseline_id = await _seed_frozen_holdout(evaluator_factory)
    optimization_run_id, _job_id = await _bind_optimizer_run(
        evaluator_factory,
        tagger_id=tagger_id,
        gold_version_id=gold_version_id,
        baseline_id=baseline_id,
    )
    async with evaluator_factory() as session, session.begin():
        run = await session.get(TagOptimizationRun, optimization_run_id)
        assert run is not None
        run.status = "running"
        run.phase = "validation"
        run.candidate_tagger_version_id = tagger_id

    evaluator = TagEvaluationService(evaluator_factory, predictor=_PerfectPredictor())
    with pytest.raises(
        GovernanceConflictError,
        match="optimizer service",
    ):
        await evaluator.enqueue(
            tenant_id="chang_an",
            tagger_version_id=tagger_id,
            gold_set_version_id=gold_version_id,
            baseline_tagger_version_id=baseline_id,
            idempotency_key="public-optimizer-challenge",
            actor_user_id=9,
        )
    with pytest.raises(
        GovernanceConflictError,
        match="sealed holdout lane",
    ):
        await evaluator.enqueue(
            tenant_id="chang_an",
            tagger_version_id=tagger_id,
            gold_set_version_id=gold_version_id,
            baseline_tagger_version_id=baseline_id,
            idempotency_key="trusted-optimizer-challenge",
            actor_user_id=9,
            trusted_optimization_binding=True,
        )
    with pytest.raises(
        GovernanceConflictError,
        match="idempotency key",
    ):
        await evaluator.enqueue(
            tenant_id="chang_an",
            tagger_version_id=tagger_id,
            gold_set_version_id=gold_version_id,
            baseline_tagger_version_id=baseline_id,
            idempotency_key="wrong-internal-key",
            actor_user_id=9,
            evaluation_lane="holdout",
            release_service=True,
            trusted_optimization_binding=True,
        )
    with pytest.raises(
        GovernanceConflictError,
        match="bound gold set",
    ):
        await evaluator.enqueue(
            tenant_id="chang_an",
            tagger_version_id=tagger_id,
            gold_set_version_id=gold_version_id + 10_000,
            baseline_tagger_version_id=baseline_id,
            idempotency_key=f"optimization-run:{optimization_run_id}:sealed-holdout",
            actor_user_id=9,
            evaluation_lane="holdout",
            release_service=True,
            trusted_optimization_binding=True,
        )
    with pytest.raises(
        GovernanceConflictError,
        match="bound baseline",
    ):
        await evaluator.enqueue(
            tenant_id="chang_an",
            tagger_version_id=tagger_id,
            gold_set_version_id=gold_version_id,
            baseline_tagger_version_id=baseline_id + 10_000,
            idempotency_key=f"optimization-run:{optimization_run_id}:sealed-holdout",
            actor_user_id=9,
            evaluation_lane="holdout",
            release_service=True,
            trusted_optimization_binding=True,
        )

    async with evaluator_factory() as session:
        assert (
            int((await session.execute(select(func.count(TagEvaluationRun.id)))).scalar_one()) == 0
        )
        assert (
            int(
                (
                    await session.execute(
                        select(func.count(TagExtractionJob.id)).where(
                            TagExtractionJob.job_type == "evaluate"
                        )
                    )
                ).scalar_one()
            )
            == 0
        )
        candidate = await session.get(TaggerVersion, tagger_id)
    assert candidate is not None and candidate.status == "draft"


@pytest.mark.asyncio
async def test_cancel_between_search_and_holdout_enqueue_cannot_consume_holdout(
    evaluator_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from audio_graphy.services.tag_evaluator import TagEvaluationService
    from audio_graphy.services.tag_governance import (
        GovernanceConflictError,
        TagGovernanceService,
    )

    tagger_id, gold_version_id, baseline_id = await _seed_frozen_holdout(evaluator_factory)
    optimization_run_id, _job_id = await _bind_optimizer_run(
        evaluator_factory,
        tagger_id=tagger_id,
        gold_version_id=gold_version_id,
        baseline_id=baseline_id,
    )
    governance = TagGovernanceService(evaluator_factory)

    async def reuse_candidate(**_kwargs: Any) -> tuple[TaggerVersion, dict[str, Any]]:
        async with evaluator_factory() as session:
            candidate = await session.get(TaggerVersion, tagger_id)
            assert candidate is not None
            return candidate, _optimizer_result_metadata()

    monkeypatch.setattr(governance, "create_optimization_candidate", reuse_candidate)
    enqueue_entered = asyncio.Event()
    allow_enqueue = asyncio.Event()
    original_enqueue = TagEvaluationService.enqueue

    async def enqueue_after_barrier(
        evaluator: TagEvaluationService,
        **kwargs: Any,
    ) -> tuple[TagEvaluationRun, TagExtractionJob]:
        enqueue_entered.set()
        await allow_enqueue.wait()
        return await original_enqueue(evaluator, **kwargs)

    monkeypatch.setattr(TagEvaluationService, "enqueue", enqueue_after_barrier)
    execution = asyncio.create_task(
        governance.execute_optimization_run(
            tenant_id="chang_an",
            optimization_run_id=optimization_run_id,
            actor_user_id=1,
        )
    )
    await asyncio.wait_for(enqueue_entered.wait(), timeout=2)
    try:
        cancelled = await governance.cancel_optimization_run(
            tenant_id="chang_an",
            optimization_run_id=optimization_run_id,
            actor_user_id=2,
        )
    finally:
        allow_enqueue.set()

    assert cancelled.status == "cancelled"
    with pytest.raises(GovernanceConflictError, match="cancelled"):
        await execution
    async with evaluator_factory() as session:
        persisted_run = await session.get(TagOptimizationRun, optimization_run_id)
        candidate = await session.get(TaggerVersion, tagger_id)
        evaluation_count = int(
            (await session.execute(select(func.count(TagEvaluationRun.id)))).scalar_one()
        )
        evaluate_job_count = int(
            (
                await session.execute(
                    select(func.count(TagExtractionJob.id)).where(
                        TagExtractionJob.job_type == "evaluate"
                    )
                )
            ).scalar_one()
        )
    assert persisted_run is not None and persisted_run.status == "cancelled"
    assert candidate is not None and candidate.status == "rejected"
    assert evaluation_count == 0
    assert evaluate_job_count == 0


@pytest.mark.asyncio
async def test_cancel_during_holdout_evaluation_cannot_resurrect_optimization(
    evaluator_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_evaluator import TagEvaluationService
    from audio_graphy.services.tag_governance import (
        GovernanceConflictError,
        TagGovernanceService,
    )

    tagger_id, gold_version_id, baseline_id = await _seed_frozen_holdout(evaluator_factory)
    optimization_run_id, _job_id = await _bind_optimizer_run(
        evaluator_factory,
        tagger_id=tagger_id,
        gold_version_id=gold_version_id,
        baseline_id=baseline_id,
    )
    async with evaluator_factory() as session, session.begin():
        optimization_run = await session.get(TagOptimizationRun, optimization_run_id)
        assert optimization_run is not None
        optimization_run.status = "running"
        optimization_run.phase = "validation"
        optimization_run.candidate_tagger_version_id = tagger_id

    prediction_started = asyncio.Event()
    allow_prediction = asyncio.Event()

    class BlockingPredictor(_PerfectPredictor):
        async def predict_dialogue_unit(
            self,
            *,
            tenant_id: str,
            dialogue_unit_id: int,
            tagger_version_id: int,
        ) -> _Prediction:
            if tagger_version_id == tagger_id and not prediction_started.is_set():
                prediction_started.set()
                await allow_prediction.wait()
            return await super().predict_dialogue_unit(
                tenant_id=tenant_id,
                dialogue_unit_id=dialogue_unit_id,
                tagger_version_id=tagger_version_id,
            )

    evaluator = TagEvaluationService(
        evaluator_factory,
        predictor=BlockingPredictor(),
    )
    evaluation_run, evaluation_job = await evaluator.enqueue(
        tenant_id="chang_an",
        tagger_version_id=tagger_id,
        gold_set_version_id=gold_version_id,
        baseline_tagger_version_id=baseline_id,
        idempotency_key=f"optimization-run:{optimization_run_id}:sealed-holdout",
        actor_user_id=1,
        evaluation_lane="holdout",
        release_service=True,
        trusted_optimization_binding=True,
    )
    evaluation = asyncio.create_task(
        evaluator.execute(
            tenant_id="chang_an",
            evaluation_run_id=evaluation_run.id,
            worker_id="evaluation-race-worker",
        )
    )
    await asyncio.wait_for(prediction_started.wait(), timeout=2)
    governance = TagGovernanceService(evaluator_factory)
    try:
        cancelled = await governance.cancel_optimization_run(
            tenant_id="chang_an",
            optimization_run_id=optimization_run_id,
            actor_user_id=2,
        )
    finally:
        allow_prediction.set()

    assert cancelled.status == "cancelled"
    with pytest.raises(GovernanceConflictError, match="cancelled"):
        await evaluation
    async with evaluator_factory() as session:
        persisted_optimization = await session.get(
            TagOptimizationRun,
            optimization_run_id,
        )
        persisted_evaluation = await session.get(TagEvaluationRun, evaluation_run.id)
        persisted_job = await session.get(TagExtractionJob, evaluation_job.id)
        persisted_candidate = await session.get(TaggerVersion, tagger_id)
    assert persisted_optimization is not None
    assert persisted_optimization.status == "cancelled"
    assert persisted_optimization.winner_tagger_version_id is None
    assert persisted_evaluation is not None and persisted_evaluation.status == "failed"
    assert persisted_job is not None and persisted_job.status == "cancelled"
    assert persisted_candidate is not None and persisted_candidate.status == "rejected"


@pytest.mark.asyncio
async def test_sealed_holdout_requires_a_complete_non_legacy_gold_matrix(
    evaluator_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_evaluator import TagEvaluationService
    from audio_graphy.services.tag_governance import GovernanceConflictError

    tagger_id, gold_version_id, baseline_id = await _seed_frozen_holdout(evaluator_factory)
    async with evaluator_factory() as session, session.begin():
        version = await session.get(TagGoldSetVersion, gold_version_id)
        assert version is not None
        version.completeness_manifest = {"complete": False, "legacy_sparse": True}

    service = TagEvaluationService(evaluator_factory, predictor=_PerfectPredictor())
    with pytest.raises(GovernanceConflictError, match="complete, non-legacy"):
        await service.enqueue(
            tenant_id="chang_an",
            tagger_version_id=tagger_id,
            gold_set_version_id=gold_version_id,
            baseline_tagger_version_id=baseline_id,
            idempotency_key="incomplete-sealed-holdout",
            actor_user_id=1,
            evaluation_lane="holdout",
            release_service=True,
        )


@pytest.mark.asyncio
async def test_sealed_holdout_rejects_non_definitive_t3_labels(
    evaluator_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_evaluator import TagEvaluationService
    from audio_graphy.services.tag_governance import GovernanceConflictError

    tagger_id, gold_version_id, baseline_id = await _seed_frozen_holdout(evaluator_factory)
    async with evaluator_factory() as session, session.begin():
        labels = list(
            (
                await session.execute(
                    select(TagGoldLabel).where(
                        TagGoldLabel.gold_set_version_id == gold_version_id,
                        TagGoldLabel.split == "holdout",
                    )
                )
            )
            .scalars()
            .all()
        )
        for label in labels:
            label.truth_state = "uncertain"

    service = TagEvaluationService(evaluator_factory, predictor=_PerfectPredictor())
    with pytest.raises(GovernanceConflictError, match="no eligible holdout"):
        await service.enqueue(
            tenant_id="chang_an",
            tagger_version_id=tagger_id,
            gold_set_version_id=gold_version_id,
            baseline_tagger_version_id=baseline_id,
            idempotency_key="non-definitive-sealed-holdout",
            actor_user_id=1,
            evaluation_lane="holdout",
            release_service=True,
        )


@pytest.mark.asyncio
async def test_evaluation_job_is_idempotent_and_executes_frozen_holdout(
    evaluator_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_evaluator import TagEvaluationService
    from audio_graphy.services.tag_governance import GovernanceConflictError

    tagger_id, gold_version_id, baseline_id = await _seed_frozen_holdout(evaluator_factory)
    service = TagEvaluationService(
        evaluator_factory,
        predictor=_PerfectPredictor(),
    )
    first_run, first_job = await service.enqueue(
        tenant_id="chang_an",
        tagger_version_id=tagger_id,
        gold_set_version_id=gold_version_id,
        baseline_tagger_version_id=baseline_id,
        idempotency_key="evaluation-candidate-1",
        actor_user_id=1,
        evaluation_lane="holdout",
        release_service=True,
    )
    replay_run, replay_job = await service.enqueue(
        tenant_id="chang_an",
        tagger_version_id=tagger_id,
        gold_set_version_id=gold_version_id,
        baseline_tagger_version_id=baseline_id,
        idempotency_key="evaluation-candidate-1",
        actor_user_id=1,
        evaluation_lane="holdout",
        release_service=True,
    )

    assert first_run.id == replay_run.id
    assert first_job.id == replay_job.id
    assert first_job.status == "queued"
    with pytest.raises(GovernanceConflictError, match="different evaluation"):
        await service.enqueue(
            tenant_id="chang_an",
            tagger_version_id=tagger_id,
            gold_set_version_id=gold_version_id + 1,
            baseline_tagger_version_id=baseline_id,
            idempotency_key="evaluation-candidate-1",
            actor_user_id=1,
            evaluation_lane="holdout",
            release_service=True,
        )

    completed = await service.execute(
        tenant_id="chang_an",
        evaluation_run_id=first_run.id,
        worker_id="test-worker",
    )

    async with evaluator_factory() as session:
        tagger = await session.get(TaggerVersion, tagger_id)
        job = await session.get(TagExtractionJob, first_job.id)
        gate_count = int(
            (
                await session.execute(
                    select(func.count(TagGateResult.id)).where(
                        TagGateResult.evaluation_run_id == completed.id
                    )
                )
            ).scalar_one()
        )
        gate_codes = set(
            (
                await session.execute(
                    select(TagGateResult.code).where(
                        TagGateResult.evaluation_run_id == completed.id
                    )
                )
            ).scalars()
        )
        persisted_run = await session.get(TagEvaluationRun, completed.id)

    assert persisted_run is not None
    assert persisted_run.status == "completed"
    assert persisted_run.passed is True
    assert persisted_run.baseline_tagger_version_id == baseline_id
    assert persisted_run.metrics["macro_f1"] == 1
    assert persisted_run.metrics["holdout_only"] is True
    assert persisted_run.metrics["paired_accuracy"]["support"] == 100
    assert persisted_run.metrics["paired_accuracy"]["delta"] == 0
    assert persisted_run.metrics["by_subject_type"]["dialogue_unit"]["macro_f1"] == 1
    assert (
        persisted_run.metrics["by_subject_type"]["dialogue_unit"]["paired_accuracy"]["support"]
        == 100
    )
    assert (
        persisted_run.metrics["by_subject_type"]["dialogue_unit"]["paired_accuracy"]["lower_bound"]
        == 0
    )
    assert tagger is not None and tagger.status == "qualified"
    assert job is not None and job.status == "completed"
    assert job.completed_items == 100
    assert gate_count >= 5
    assert "subject_type:dialogue_unit:critical_value:intent:purchase" in gate_codes


@pytest.mark.asyncio
async def test_holdout_gate_fails_when_schema_supported_subject_domain_is_missing(
    evaluator_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_evaluator import TagEvaluationService

    tagger_id, gold_version_id, baseline_id = await _seed_frozen_holdout(
        evaluator_factory,
        schema_subject_types=("dialogue_unit", "reception"),
    )
    service = TagEvaluationService(
        evaluator_factory,
        predictor=_PerfectPredictor(),
    )
    run, _job = await service.enqueue(
        tenant_id="chang_an",
        tagger_version_id=tagger_id,
        gold_set_version_id=gold_version_id,
        baseline_tagger_version_id=baseline_id,
        idempotency_key="evaluation-missing-reception-domain",
        actor_user_id=1,
        evaluation_lane="holdout",
        release_service=True,
    )

    completed = await service.execute(
        tenant_id="chang_an",
        evaluation_run_id=run.id,
        worker_id="missing-domain-worker",
    )

    async with evaluator_factory() as session:
        missing_domain_gate = (
            await session.execute(
                select(TagGateResult).where(
                    TagGateResult.evaluation_run_id == completed.id,
                    TagGateResult.code == "subject_type:reception:tag_support:intent",
                )
            )
        ).scalar_one()
        tagger = await session.get(TaggerVersion, tagger_id)

    assert completed.passed is False
    assert missing_domain_gate.passed is False
    assert missing_domain_gate.actual == 0
    assert missing_domain_gate.threshold == 30
    assert tagger is not None and tagger.status == "rejected"


@pytest.mark.asyncio
async def test_evaluation_rejects_job_scope_baseline_that_differs_from_bound_run(
    evaluator_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_evaluator import TagEvaluationService
    from audio_graphy.services.tag_governance import GovernanceConflictError

    tagger_id, gold_version_id, baseline_id = await _seed_frozen_holdout(evaluator_factory)
    service = TagEvaluationService(
        evaluator_factory,
        predictor=_PerfectPredictor(),
    )
    run, job = await service.enqueue(
        tenant_id="chang_an",
        tagger_version_id=tagger_id,
        gold_set_version_id=gold_version_id,
        baseline_tagger_version_id=baseline_id,
        idempotency_key="evaluation-baseline-binding",
        actor_user_id=1,
        evaluation_lane="holdout",
        release_service=True,
    )
    async with evaluator_factory() as session, session.begin():
        persisted_job = await session.get(TagExtractionJob, job.id)
        assert persisted_job is not None
        persisted_job.scope = {
            **persisted_job.scope,
            "baseline_tagger_version_id": baseline_id + 10_000,
        }

    with pytest.raises(GovernanceConflictError, match="baseline binding"):
        await service.execute(
            tenant_id="chang_an",
            evaluation_run_id=run.id,
            worker_id="test-worker",
        )


@pytest.mark.asyncio
async def test_evaluation_job_cancel_and_final_failure_sync_candidate_state(
    evaluator_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_evaluator import TagEvaluationService
    from audio_graphy.services.tag_governance import (
        GovernanceConflictError,
        TagGovernanceService,
    )

    tagger_id, gold_version_id, baseline_id = await _seed_frozen_holdout(evaluator_factory)
    evaluator = TagEvaluationService(
        evaluator_factory,
        predictor=_PerfectPredictor(),
    )
    cancelled_run, cancelled_job = await evaluator.enqueue(
        tenant_id="chang_an",
        tagger_version_id=tagger_id,
        gold_set_version_id=gold_version_id,
        baseline_tagger_version_id=baseline_id,
        idempotency_key="evaluation-cancelled",
        actor_user_id=1,
        evaluation_lane="holdout",
        release_service=True,
    )
    governance = TagGovernanceService(evaluator_factory)
    await governance.cancel_job(
        tenant_id="chang_an",
        job_id=cancelled_job.id,
        actor_user_id=1,
    )
    async with evaluator_factory() as session:
        persisted_cancelled = await session.get(TagEvaluationRun, cancelled_run.id)
        cancelled_tagger = await session.get(TaggerVersion, tagger_id)
    assert persisted_cancelled is not None
    assert persisted_cancelled.status == "failed"
    assert persisted_cancelled.metrics["terminal_reason"] == "cancelled"
    assert cancelled_tagger is not None and cancelled_tagger.status == "rejected"

    async with evaluator_factory() as session, session.begin():
        cancelled_tagger = await session.get(TaggerVersion, tagger_id)
        assert cancelled_tagger is not None
        cancelled_tagger.status = "draft"
    failed_run, failed_job = await evaluator.enqueue(
        tenant_id="chang_an",
        tagger_version_id=tagger_id,
        gold_set_version_id=gold_version_id,
        baseline_tagger_version_id=baseline_id,
        idempotency_key="evaluation-final-failure",
        actor_user_id=1,
        evaluation_lane="holdout",
        release_service=True,
    )
    now = datetime.now(UTC)
    for attempt in range(3):
        claimed = await governance.claim_next_job(
            worker_id="failing-evaluator",
            now=now + timedelta(minutes=attempt),
            lease_for=timedelta(seconds=30),
        )
        assert claimed is not None
        assert claimed.id == failed_job.id
        assert await governance.defer_job_failure(
            tenant_id="chang_an",
            job_id=claimed.id,
            worker_id="failing-evaluator",
            expected_revision=claimed.revision,
            error_code="EvaluationError",
            error_message="candidate evaluation failed",
            now=now + timedelta(minutes=attempt, seconds=1),
        )
    async with evaluator_factory() as session:
        persisted_failed = await session.get(TagEvaluationRun, failed_run.id)
        failed_tagger = await session.get(TaggerVersion, tagger_id)
        persisted_job = await session.get(TagExtractionJob, failed_job.id)
    assert persisted_failed is not None and persisted_failed.status == "failed"
    assert persisted_failed.metrics["terminal_reason"] == "failed"
    assert failed_tagger is not None and failed_tagger.status == "rejected"
    assert persisted_job is not None and persisted_job.status == "failed"

    with pytest.raises(
        GovernanceConflictError,
        match="rejected candidates cannot be retried",
    ):
        await governance.retry_job(
            tenant_id="chang_an",
            job_id=failed_job.id,
            actor_user_id=1,
        )
    async with evaluator_factory() as session:
        terminal_run = await session.get(TagEvaluationRun, failed_run.id)
        terminal_tagger = await session.get(TaggerVersion, tagger_id)
        terminal_job = await session.get(TagExtractionJob, failed_job.id)
    assert terminal_run is not None and terminal_run.status == "failed"
    assert terminal_tagger is not None and terminal_tagger.status == "rejected"
    assert terminal_job is not None and terminal_job.status == "failed"
