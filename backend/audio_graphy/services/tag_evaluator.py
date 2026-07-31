"""Holdout-only tag evaluation executed by the durable tag worker."""

from __future__ import annotations

import inspect
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.models.tag_governance import (
    TagEvaluationItem,
    TagEvaluationMetric,
    TagEvaluationRun,
    TagExtractionJob,
    TagGateResult,
    TaggerVersion,
    TagGoldLabel,
    TagGoldSet,
    TagGoldSetVersion,
    TagOptimizationRun,
    TagSchemaVersion,
)
from audio_graphy.services.llm_gateway import LLMUsageContext
from audio_graphy.services.tag_evaluation_policy import (
    CRITICAL_RECALL_LCB_THRESHOLD,
    critical_enum_values,
    wilson_lower_bound,
)
from audio_graphy.services.tag_governance import (
    Gate,
    GovernanceConflictError,
    GovernanceNotFoundError,
    compute_gold_dataset_snapshot_hash,
    enforce_sealed_holdout_access,
    evaluate_quality_gates,
    schema_subject_tag_pairs,
)


class PredictionResult(Protocol):
    @property
    def assignments(self) -> Sequence[Mapping[str, Any]]: ...


class TagPredictor(Protocol):
    async def predict_dialogue_unit(
        self,
        *,
        tenant_id: str,
        dialogue_unit_id: int,
        tagger_version_id: int,
    ) -> PredictionResult: ...


@dataclass(frozen=True, slots=True)
class EvaluationSummary:
    metrics: dict[str, float]
    label_metrics: dict[str, dict[str, float | int]]
    value_metrics: dict[str, dict[str, dict[str, float | int]]]
    critical_value_metrics: dict[str, dict[str, dict[str, float | int]]]
    confusion: dict[str, dict[str, dict[str, int]]]
    insufficient_labels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PairedComparison:
    support: int
    candidate_wins: int
    baseline_wins: int
    ties: int
    delta: float
    lower_bound: float
    upper_bound: float


def _safe_ratio(
    numerator: int | float,
    denominator: int | float,
    *,
    empty: float = 0.0,
) -> float:
    return numerator / denominator if denominator else empty


def _truth_state(gold: Mapping[str, Any]) -> str:
    state = gold.get("truth_state")
    if state is None:
        # Backward compatibility for gold frozen before explicit truth states.
        return "absent" if gold.get("tag_value") is None else "present"
    normalized = str(state).strip().lower()
    if normalized in {"unknown", "uncertain"}:
        return "uncertain"
    if normalized in {"n/a", "na", "not-applicable", "not_applicable"}:
        return "not_applicable"
    if normalized not in {"present", "absent"}:
        raise ValueError(f"unsupported truth_state: {state!r}")
    return normalized


def _prediction_index(
    predictions: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]],
) -> dict[tuple[str, int, str], Mapping[str, Any]]:
    indexed: dict[tuple[str, int, str], Mapping[str, Any]] = {}
    for (subject_type, subject_id), assignments in predictions.items():
        for assignment in assignments:
            tag_key = str(assignment.get("tag_key", ""))
            if tag_key:
                indexed[(subject_type, subject_id, tag_key)] = assignment
    return indexed


def _is_correct_prediction(
    gold: Mapping[str, Any],
    prediction: Mapping[str, Any] | None,
) -> bool | None:
    state = _truth_state(gold)
    if state in {"uncertain", "not_applicable"}:
        return None
    if state == "absent":
        return prediction is None
    return prediction is not None and prediction.get("tag_value") == gold.get("tag_value")


def _valid_evidence_ref(value: Any) -> bool:
    if not isinstance(value, Mapping) or value.get("segment_id") is None:
        return False
    start = value.get("start_sec")
    end = value.get("end_sec")
    if start is None and end is None:
        return True
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return False
    return float(end) > float(start)


def _evidence_iou(
    gold_refs: Sequence[Any],
    predicted_refs: Sequence[Any],
) -> float:
    """Return mean best-match temporal IoU for the frozen gold evidence."""

    valid_gold = [item for item in gold_refs if _valid_evidence_ref(item)]
    valid_predicted = [item for item in predicted_refs if _valid_evidence_ref(item)]
    if not valid_gold:
        return 1.0
    if not valid_predicted:
        return 0.0
    scores: list[float] = []
    for gold in valid_gold:
        best = 0.0
        for predicted in valid_predicted:
            if str(predicted.get("segment_id")) != str(gold.get("segment_id")):
                continue
            gold_start = gold.get("start_sec")
            gold_end = gold.get("end_sec")
            predicted_start = predicted.get("start_sec")
            predicted_end = predicted.get("end_sec")
            if (
                gold_start is None
                and gold_end is None
                and predicted_start is None
                and predicted_end is None
            ):
                best = 1.0
                break
            if not all(
                isinstance(value, (int, float))
                for value in (gold_start, gold_end, predicted_start, predicted_end)
            ):
                continue
            intersection = max(
                0.0,
                min(float(gold_end), float(predicted_end))
                - max(float(gold_start), float(predicted_start)),
            )
            union = max(float(gold_end), float(predicted_end)) - min(
                float(gold_start), float(predicted_start)
            )
            best = max(best, _safe_ratio(intersection, union))
        scores.append(best)
    return _safe_ratio(sum(scores), len(scores))


def _calibration_metrics(
    observations: Sequence[tuple[float, bool]],
) -> tuple[float, float]:
    if not observations:
        return 0.0, 0.0
    brier = sum((confidence - float(correct)) ** 2 for confidence, correct in observations) / len(
        observations
    )
    bins: dict[int, list[tuple[float, bool]]] = defaultdict(list)
    for confidence, correct in observations:
        bins[min(9, int(confidence * 10))].append((confidence, correct))
    ece = 0.0
    for values in bins.values():
        average_confidence = sum(item[0] for item in values) / len(values)
        accuracy = sum(float(item[1]) for item in values) / len(values)
        ece += len(values) / len(observations) * abs(accuracy - average_confidence)
    return brier, ece


def _prediction_integrity_counts(
    *,
    predictions: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]],
    definitions: Mapping[str, Mapping[str, Any]],
) -> tuple[int, int]:
    schema_violations = 0
    evidence_violations = 0
    for (subject_type, _subject_id), assignments in predictions.items():
        seen_keys: set[str] = set()
        for assignment in assignments:
            tag_key = str(assignment.get("tag_key", ""))
            definition = definitions.get(tag_key)
            if not tag_key or definition is None:
                schema_violations += 1
                continue
            if tag_key in seen_keys:
                schema_violations += 1
            seen_keys.add(tag_key)
            allowed_subject_types = definition.get("subject_types") or []
            if allowed_subject_types and subject_type not in allowed_subject_types:
                schema_violations += 1
            allowed_values = definition.get("allowed_values") or []
            if allowed_values and assignment.get("tag_value") not in allowed_values:
                schema_violations += 1
            confidence = assignment.get("confidence")
            if confidence is not None and (
                not isinstance(confidence, (int, float)) or not 0.0 <= float(confidence) <= 1.0
            ):
                schema_violations += 1
            raw_refs = assignment.get("evidence_refs") or []
            refs_are_valid = isinstance(raw_refs, Sequence) and not isinstance(
                raw_refs, (str, bytes)
            )
            refs = list(raw_refs) if refs_are_valid else []
            refs_are_valid = refs_are_valid and all(_valid_evidence_ref(item) for item in refs)
            if (bool(definition.get("evidence_required")) and not refs) or (
                refs and not refs_are_valid
            ):
                evidence_violations += 1
    return schema_violations, evidence_violations


def validate_reception_lane_isolation(labels: Sequence[TagGoldLabel]) -> None:
    """Reject any frozen dataset that places one Reception in multiple lanes."""

    splits_by_reception: dict[int, set[str]] = defaultdict(set)
    for item in labels:
        reception_id = getattr(item, "reception_id", None)
        if reception_id is not None:
            splits_by_reception[int(reception_id)].add(str(item.split))
    leaked = sorted(
        reception_id for reception_id, splits in splits_by_reception.items() if len(splits) > 1
    )
    if leaked:
        raise GovernanceConflictError(f"Reception leakage across evaluation lanes: {leaked[:10]}")


def compute_paired_comparison(
    *,
    gold_labels: Sequence[Mapping[str, Any]],
    candidate_predictions: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]],
    baseline_predictions: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]],
) -> PairedComparison:
    """Compare candidate and baseline correctness on the exact same explicit truths."""

    candidate_index = _prediction_index(candidate_predictions)
    baseline_index = _prediction_index(baseline_predictions)
    differences: list[int] = []
    candidate_wins = 0
    baseline_wins = 0
    for gold in gold_labels:
        identity = (
            str(gold["subject_type"]),
            int(gold["subject_id"]),
            str(gold["tag_key"]),
        )
        candidate_correct = _is_correct_prediction(gold, candidate_index.get(identity))
        baseline_correct = _is_correct_prediction(gold, baseline_index.get(identity))
        if candidate_correct is None or baseline_correct is None:
            continue
        difference = int(candidate_correct) - int(baseline_correct)
        differences.append(difference)
        candidate_wins += int(difference > 0)
        baseline_wins += int(difference < 0)

    support = len(differences)
    delta = _safe_ratio(sum(differences), support)
    if support <= 1:
        margin = 0.0 if support == 0 else 1.0
    else:
        variance = sum((difference - delta) ** 2 for difference in differences) / (support - 1)
        margin = 1.959963984540054 * math.sqrt(variance / support)
    return PairedComparison(
        support=support,
        candidate_wins=candidate_wins,
        baseline_wins=baseline_wins,
        ties=support - candidate_wins - baseline_wins,
        delta=delta,
        lower_bound=max(-1.0, delta - margin),
        upper_bound=min(1.0, delta + margin),
    )


def compute_evaluation_summary(
    *,
    gold_labels: Sequence[Mapping[str, Any]],
    predictions: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]],
    definitions: Mapping[str, Mapping[str, Any]],
    extraction_errors: int,
    subject_count: int,
    lineage_violation_count: int = 0,
) -> EvaluationSummary:
    """Compute metrics from explicit truth rows without inventing sparse negatives."""

    by_label: dict[str, dict[str, int]] = defaultdict(
        lambda: {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "support": 0}
    )
    confusion: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(int))
    )
    required_total = 0
    required_with_evidence = 0
    evidence_iou_scores: list[float] = []
    calibration_observations: list[tuple[float, bool]] = []
    prediction_index = _prediction_index(predictions)
    evaluable_rows: list[tuple[str, Any, Mapping[str, Any] | None, str]] = []

    for gold in gold_labels:
        subject_type = str(gold["subject_type"])
        subject_id = int(gold["subject_id"])
        tag_key = str(gold["tag_key"])
        expected_value = gold.get("tag_value")
        truth_state = _truth_state(gold)
        if truth_state in {"uncertain", "not_applicable"}:
            continue
        if truth_state == "present" and expected_value is None:
            raise ValueError(f"present truth for {tag_key!r} requires tag_value")
        expected = "__absent__" if truth_state == "absent" else str(expected_value)
        identity = (subject_type, subject_id, tag_key)
        counts = by_label[tag_key]
        counts["support"] += 1
        predicted = prediction_index.get(identity)
        predicted_value = "__missing__" if predicted is None else str(predicted.get("tag_value"))
        confusion[tag_key][expected][predicted_value] += 1
        evaluable_rows.append((tag_key, expected_value, predicted, truth_state))
        if predicted is not None and isinstance(predicted.get("confidence"), (int, float)):
            confidence = min(1.0, max(0.0, float(predicted["confidence"])))
            correct = _is_correct_prediction(gold, predicted)
            if correct is not None:
                calibration_observations.append((confidence, correct))
        if truth_state == "absent":
            if predicted is None:
                counts["tn"] += 1
            else:
                counts["fp"] += 1
            continue
        if predicted is not None and predicted_value == expected:
            counts["tp"] += 1
        else:
            counts["fn"] += 1
            if predicted is not None:
                counts["fp"] += 1

        if bool(definitions.get(tag_key, {}).get("evidence_required")):
            required_total += 1
            if (
                predicted is not None
                and predicted_value == expected
                and bool(predicted.get("evidence_refs"))
            ):
                required_with_evidence += 1
        if (
            truth_state == "present"
            and predicted is not None
            and predicted_value == expected
            and gold.get("evidence_refs")
        ):
            evidence_iou_scores.append(
                _evidence_iou(
                    list(gold.get("evidence_refs") or []),
                    list(predicted.get("evidence_refs") or []),
                )
            )

    label_metrics: dict[str, dict[str, float | int]] = {}
    for tag_key, counts in sorted(by_label.items()):
        precision = _safe_ratio(counts["tp"], counts["tp"] + counts["fp"])
        recall = _safe_ratio(counts["tp"], counts["tp"] + counts["fn"])
        f1 = _safe_ratio(
            2 * precision * recall,
            precision + recall,
        )
        label_metrics[tag_key] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "true_negatives": counts["tn"],
            "support": counts["support"],
        }

    values_by_label: dict[str, set[str]] = defaultdict(set)
    for tag_key, definition in definitions.items():
        allowed_values = definition.get("allowed_values", [])
        if isinstance(allowed_values, Sequence) and not isinstance(allowed_values, (str, bytes)):
            values_by_label[tag_key].update(str(value) for value in allowed_values)
        configured_critical_values = definition.get("critical_values", [])
        if isinstance(configured_critical_values, Sequence) and not isinstance(
            configured_critical_values,
            (str, bytes),
        ):
            # A configured critical value must remain visible to the release
            # gate even when the frozen lane contains zero positive examples.
            values_by_label[tag_key].update(str(value) for value in configured_critical_values)
    for tag_key, expected_value, predicted, truth_state in evaluable_rows:
        if truth_state == "present":
            values_by_label[tag_key].add(str(expected_value))
        if predicted is not None and predicted.get("tag_value") is not None:
            values_by_label[tag_key].add(str(predicted.get("tag_value")))

    raw_value_counts: dict[str, dict[str, dict[str, int]]] = defaultdict(dict)
    for tag_key, allowed_tag_values in values_by_label.items():
        for value in sorted(allowed_tag_values):
            raw_value_counts[tag_key][value] = {
                "tp": 0,
                "fp": 0,
                "fn": 0,
                "tn": 0,
                "support": 0,
                "evaluated_support": 0,
            }
    for tag_key, expected_value, predicted, truth_state in evaluable_rows:
        predicted_tag_value: Any = None if predicted is None else predicted.get("tag_value")
        for value, counts in raw_value_counts[tag_key].items():
            expected_positive = truth_state == "present" and str(expected_value) == value
            predicted_positive = predicted is not None and str(predicted_tag_value) == value
            counts["support"] += int(expected_positive)
            counts["evaluated_support"] += 1
            if expected_positive and predicted_positive:
                counts["tp"] += 1
            elif expected_positive:
                counts["fn"] += 1
            elif predicted_positive:
                counts["fp"] += 1
            else:
                counts["tn"] += 1

    value_metrics: dict[str, dict[str, dict[str, float | int]]] = {}
    macro_values: list[float] = []
    for tag_key, counts_by_value in sorted(raw_value_counts.items()):
        value_metrics[tag_key] = {}
        definition = definitions.get(tag_key, {})
        negative_values = {str(value) for value in definition.get("negative_values", [])}
        for value, counts in sorted(counts_by_value.items()):
            precision = _safe_ratio(counts["tp"], counts["tp"] + counts["fp"])
            recall = _safe_ratio(counts["tp"], counts["tp"] + counts["fn"])
            f1 = _safe_ratio(2 * precision * recall, precision + recall)
            f2 = _safe_ratio(5 * precision * recall, 4 * precision + recall)
            value_metrics[tag_key][value] = {
                "precision": precision,
                "recall": recall,
                "recall_lcb": wilson_lower_bound(counts["tp"], counts["tp"] + counts["fn"]),
                "f1": f1,
                "f2": f2,
                "true_positives": counts["tp"],
                "false_positives": counts["fp"],
                "false_negatives": counts["fn"],
                "true_negatives": counts["tn"],
                "support": counts["support"],
                "evaluated_support": counts["evaluated_support"],
            }
            if value not in negative_values and (counts["support"] > 0 or counts["fp"] > 0):
                macro_values.append(f1)

    macro_f1 = _safe_ratio(sum(macro_values), len(macro_values))
    critical_pairs: set[tuple[str, str]] = set()
    for tag_key, definition in definitions.items():
        critical_pairs.update(
            (tag_key, value)
            for value in critical_enum_values(
                definition,
                observed_values=tuple(value_metrics.get(tag_key, {})),
            )
        )
    has_critical_definition = bool(critical_pairs)
    critical_tp = sum(
        int(value_metrics[tag_key][value]["true_positives"])
        for tag_key, value in critical_pairs
        if value in value_metrics.get(tag_key, {})
    )
    critical_fn = sum(
        int(value_metrics[tag_key][value]["false_negatives"])
        for tag_key, value in critical_pairs
        if value in value_metrics.get(tag_key, {})
    )
    critical_positive_support = critical_tp + critical_fn
    critical_recall = _safe_ratio(
        critical_tp,
        critical_positive_support,
        empty=0.0 if has_critical_definition else 1.0,
    )
    critical_value_metrics: dict[str, dict[str, dict[str, float | int]]] = defaultdict(dict)
    critical_lcbs: list[float] = []
    for tag_key, value in sorted(critical_pairs):
        metric = value_metrics.get(tag_key, {}).get(value)
        if metric is None:
            critical_lcbs.append(0.0)
            continue
        critical_value_metrics[tag_key][value] = dict(metric)
        critical_lcbs.append(float(metric["recall_lcb"]) if int(metric["support"]) > 0 else 0.0)
    critical_recall_lcb = min(
        critical_lcbs,
        default=(0.0 if has_critical_definition else 1.0),
    )
    evidence_coverage = _safe_ratio(
        required_with_evidence,
        required_total,
        empty=1.0,
    )
    evidence_iou = _safe_ratio(
        sum(evidence_iou_scores),
        len(evidence_iou_scores),
        empty=1.0,
    )
    brier_score, ece = _calibration_metrics(calibration_observations)
    schema_violations, evidence_violations = _prediction_integrity_counts(
        predictions=predictions,
        definitions=definitions,
    )
    error_rate = _safe_ratio(extraction_errors, subject_count, empty=1.0)
    insufficient_labels = tuple(
        key for key, item in sorted(label_metrics.items()) if int(item["support"]) < 30
    )
    return EvaluationSummary(
        metrics={
            "macro_f1": macro_f1,
            "critical_recall": critical_recall,
            "critical_recall_lcb": critical_recall_lcb,
            "critical_lcb_enforced": float(has_critical_definition),
            "critical_positive_support": float(critical_positive_support),
            "evidence_coverage": evidence_coverage,
            "evidence_iou": evidence_iou,
            "brier_score": brier_score,
            "ece": ece,
            "calibration_support": float(len(calibration_observations)),
            "schema_violation_count": float(schema_violations),
            "evidence_violation_count": float(evidence_violations),
            "lineage_violation_count": float(max(0, lineage_violation_count)),
            "error_rate": error_rate,
        },
        label_metrics=label_metrics,
        value_metrics=value_metrics,
        critical_value_metrics={
            tag_key: dict(sorted(values.items()))
            for tag_key, values in sorted(critical_value_metrics.items())
        },
        confusion={
            label_key: {
                expected: dict(sorted(predicted.items()))
                for expected, predicted in sorted(expected_values.items())
            }
            for label_key, expected_values in sorted(confusion.items())
        },
        insufficient_labels=insufficient_labels,
    )


def compute_evaluation_summaries_by_subject_type(
    *,
    gold_labels: Sequence[Mapping[str, Any]],
    predictions: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]],
    definitions: Mapping[str, Mapping[str, Any]],
    extraction_errors_by_subject_type: Mapping[str, int] | None = None,
) -> dict[str, EvaluationSummary]:
    """Evaluate each subject domain independently so one cohort cannot mask another."""

    labels_by_subject_type: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for gold in gold_labels:
        labels_by_subject_type[str(gold["subject_type"])].append(gold)
    summaries: dict[str, EvaluationSummary] = {}
    error_counts = extraction_errors_by_subject_type or {}
    for subject_type, subject_labels in sorted(labels_by_subject_type.items()):
        subject_predictions = {
            identity: assignments
            for identity, assignments in predictions.items()
            if identity[0] == subject_type
        }
        subject_definitions = {
            tag_key: definition
            for tag_key, definition in definitions.items()
            if not definition.get("subject_types")
            or subject_type in definition.get("subject_types", [])
        }
        subject_count = len(
            {(str(item["subject_type"]), int(item["subject_id"])) for item in subject_labels}
        )
        summaries[subject_type] = compute_evaluation_summary(
            gold_labels=subject_labels,
            predictions=subject_predictions,
            definitions=subject_definitions,
            extraction_errors=max(0, int(error_counts.get(subject_type, 0))),
            subject_count=subject_count,
        )
    return summaries


def compute_evaluation_summaries_by_slice(
    *,
    gold_labels: Sequence[Mapping[str, Any]],
    predictions: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]],
    definitions: Mapping[str, Mapping[str, Any]],
) -> dict[str, EvaluationSummary]:
    """Compute stable cohort/scenario/store slices without mixing subject domains."""

    labels_by_slice: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for gold in gold_labels:
        subject_type = str(gold["subject_type"])
        for dimension in ("cohort", "scenario", "store_id"):
            value = gold.get(dimension)
            if value not in (None, ""):
                labels_by_slice[f"{subject_type}|{dimension}={value}"].append(gold)
    summaries: dict[str, EvaluationSummary] = {}
    for slice_key, slice_labels in sorted(labels_by_slice.items()):
        identities = {(str(item["subject_type"]), int(item["subject_id"])) for item in slice_labels}
        slice_predictions = {
            identity: assignments
            for identity, assignments in predictions.items()
            if identity in identities
        }
        subject_type = str(slice_labels[0]["subject_type"])
        slice_definitions = {
            tag_key: definition
            for tag_key, definition in definitions.items()
            if not definition.get("subject_types")
            or subject_type in definition.get("subject_types", [])
        }
        summaries[slice_key] = compute_evaluation_summary(
            gold_labels=slice_labels,
            predictions=slice_predictions,
            definitions=slice_definitions,
            extraction_errors=0,
            subject_count=len(identities),
        )
    return summaries


def _summary_payload(summary: EvaluationSummary) -> dict[str, Any]:
    return {
        **summary.metrics,
        "confusion": summary.confusion,
        "critical_value_metrics": summary.critical_value_metrics,
        "insufficient_labels": list(summary.insufficient_labels),
        "label_metrics": summary.label_metrics,
        "value_metrics": summary.value_metrics,
    }


def _paired_payload(paired: PairedComparison) -> dict[str, int | float]:
    return {
        "baseline_wins": paired.baseline_wins,
        "candidate_wins": paired.candidate_wins,
        "delta": paired.delta,
        "lower_bound": paired.lower_bound,
        "support": paired.support,
        "ties": paired.ties,
        "upper_bound": paired.upper_bound,
    }


class TagEvaluationService:
    """Create and execute a durable evaluation against hidden holdout only."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        predictor: TagPredictor | None = None,
    ) -> None:
        self._factory = session_factory
        self._predictor = predictor

    def _get_predictor(self) -> TagPredictor:
        if self._predictor is not None:
            return self._predictor
        from audio_graphy.services.tag_extractor import TagExtractor

        extractor = TagExtractor(self._factory)
        method = getattr(extractor, "predict_dialogue_unit", None)
        if method is None:
            raise RuntimeError("TagExtractor does not expose pure prediction")
        return cast(TagPredictor, extractor)

    async def enqueue(
        self,
        *,
        tenant_id: str,
        tagger_version_id: int,
        gold_set_version_id: int,
        baseline_tagger_version_id: int,
        idempotency_key: str,
        actor_user_id: int,
        evaluation_lane: str = "challenge",
        release_service: bool = False,
        trusted_optimization_binding: bool = False,
    ) -> tuple[TagEvaluationRun, TagExtractionJob]:
        if evaluation_lane not in {"challenge", "holdout"}:
            raise GovernanceConflictError("unsupported evaluation lane")
        if evaluation_lane == "holdout" and not release_service:
            raise GovernanceConflictError(
                "sealed holdout can only be queried by the release service"
            )
        async with self._factory() as session, session.begin():
            candidate_optimization_run_id = (
                await session.execute(
                    select(TaggerVersion.optimization_run_id).where(
                        TaggerVersion.id == tagger_version_id,
                        TaggerVersion.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            optimization_run: TagOptimizationRun | None = None
            if candidate_optimization_run_id is not None:
                # Optimization cancellation locks the run before any related
                # job/run/tagger rows.  Taking the same lock first makes
                # holdout consumption atomic with cancellation.
                optimization_run = (
                    await session.execute(
                        select(TagOptimizationRun)
                        .where(
                            TagOptimizationRun.id == candidate_optimization_run_id,
                            TagOptimizationRun.tenant_id == tenant_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if optimization_run is None:
                    raise GovernanceConflictError("candidate references a missing optimization run")

            tagger = (
                await session.execute(
                    select(TaggerVersion)
                    .where(
                        TaggerVersion.id == tagger_version_id,
                        TaggerVersion.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if tagger is None:
                raise GovernanceNotFoundError("tagger version not found")

            optimization_run_id = getattr(tagger, "optimization_run_id", None)
            if optimization_run_id != candidate_optimization_run_id:
                raise GovernanceConflictError(
                    "candidate optimization binding changed during enqueue"
                )
            if trusted_optimization_binding and optimization_run_id is None:
                raise GovernanceConflictError(
                    "trusted optimization evaluation requires an optimization-bound candidate"
                )
            if optimization_run_id is not None:
                if not trusted_optimization_binding:
                    raise GovernanceConflictError(
                        "optimization-bound candidates can only be evaluated "
                        "by the optimizer service"
                    )
                if evaluation_lane != "holdout" or not release_service:
                    raise GovernanceConflictError(
                        "optimizer service must use the sealed holdout lane"
                    )
                if optimization_run is None:
                    raise GovernanceConflictError("candidate references a missing optimization run")
                if optimization_run.status == "cancelled":
                    raise GovernanceConflictError("optimization run is cancelled")
                if optimization_run.status != "running" or optimization_run.phase not in {
                    "validation",
                    "holdout",
                }:
                    raise GovernanceConflictError(
                        "optimization run is not ready for sealed holdout"
                    )
                if optimization_run.candidate_tagger_version_id != tagger_version_id:
                    raise GovernanceConflictError(
                        "optimization run is not bound to the requested candidate"
                    )
                if optimization_run.gold_set_version_id != gold_set_version_id:
                    raise GovernanceConflictError(
                        "optimization candidate must use its bound gold set"
                    )
                if optimization_run.baseline_tagger_version_id != baseline_tagger_version_id:
                    raise GovernanceConflictError(
                        "optimization candidate must use its bound baseline"
                    )
                expected_idempotency_key = f"optimization-run:{optimization_run.id}:sealed-holdout"
                if idempotency_key != expected_idempotency_key:
                    raise GovernanceConflictError(
                        "optimization sealed holdout idempotency key does not match its run"
                    )

            existing_job = (
                await session.execute(
                    select(TagExtractionJob)
                    .where(
                        TagExtractionJob.tenant_id == tenant_id,
                        TagExtractionJob.idempotency_key == idempotency_key,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if existing_job is not None:
                if existing_job.job_type != "evaluate":
                    raise GovernanceConflictError("idempotency key was used for another operation")
                if (
                    int(existing_job.scope.get("tagger_version_id", 0)) != tagger_version_id
                    or int(existing_job.scope.get("gold_set_version_id", 0)) != gold_set_version_id
                    or existing_job.scope.get("baseline_tagger_version_id")
                    != baseline_tagger_version_id
                    or existing_job.scope.get("evaluation_lane", "holdout") != evaluation_lane
                ):
                    raise GovernanceConflictError(
                        "idempotency key was used for a different evaluation"
                    )
                run_id = int(existing_job.scope["evaluation_run_id"])
                existing_run = (
                    await session.execute(
                        select(TagEvaluationRun).where(
                            TagEvaluationRun.id == run_id,
                            TagEvaluationRun.tenant_id == tenant_id,
                        )
                    )
                ).scalar_one()
                return existing_run, existing_job

            gold_version = (
                await session.execute(
                    select(TagGoldSetVersion, TagGoldSet)
                    .join(TagGoldSet, TagGoldSet.id == TagGoldSetVersion.gold_set_id)
                    .where(
                        TagGoldSetVersion.id == gold_set_version_id,
                        TagGoldSetVersion.tenant_id == tenant_id,
                        TagGoldSet.tenant_id == tenant_id,
                    )
                )
            ).one_or_none()
            if gold_version is None:
                raise GovernanceNotFoundError("gold set version not found")
            version, gold_set = gold_version
            if version.status != "frozen":
                raise GovernanceConflictError("evaluation requires a frozen gold set")
            completeness_manifest = version.completeness_manifest
            if evaluation_lane == "holdout" and not (
                isinstance(completeness_manifest, Mapping)
                and completeness_manifest.get("complete") is True
                and completeness_manifest.get("legacy_sparse") is not True
            ):
                raise GovernanceConflictError(
                    "sealed holdout requires a complete, non-legacy gold matrix"
                )
            if tagger.schema_version_id != gold_set.schema_version_id:
                raise GovernanceConflictError("tagger and gold set schema versions differ")
            if baseline_tagger_version_id == tagger_version_id:
                raise GovernanceConflictError("baseline and candidate taggers must differ")
            baseline = (
                await session.execute(
                    select(TaggerVersion).where(
                        TaggerVersion.id == baseline_tagger_version_id,
                        TaggerVersion.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if baseline is None:
                raise GovernanceNotFoundError("baseline tagger version not found")
            if baseline.schema_version_id != tagger.schema_version_id:
                raise GovernanceConflictError(
                    "baseline and candidate tagger schema versions differ"
                )
            if baseline.status != "qualified":
                raise GovernanceConflictError("baseline tagger version must be qualified")
            lane_truth_tiers = ["t3"] if evaluation_lane == "holdout" else ["t2", "t3"]
            lane_count = int(
                (
                    await session.execute(
                        select(func.count(TagGoldLabel.id)).where(
                            TagGoldLabel.tenant_id == tenant_id,
                            TagGoldLabel.gold_set_version_id == gold_set_version_id,
                            TagGoldLabel.split == evaluation_lane,
                            TagGoldLabel.truth_tier.in_(lane_truth_tiers),
                            TagGoldLabel.truth_state.in_(["present", "absent"]),
                        )
                    )
                ).scalar_one()
            )
            if lane_count == 0:
                raise GovernanceConflictError(f"gold set has no eligible {evaluation_lane} labels")
            if tagger.status not in {"draft", "validating", "evaluating"}:
                raise GovernanceConflictError("only draft/validating taggers can enter evaluation")
            active_evaluation = (
                await session.execute(
                    select(TagEvaluationRun.id)
                    .where(
                        TagEvaluationRun.tenant_id == tenant_id,
                        TagEvaluationRun.tagger_version_id == tagger_version_id,
                        TagEvaluationRun.status.in_(("queued", "running")),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if active_evaluation is not None:
                raise GovernanceConflictError("candidate already has an active evaluation")

            if optimization_run_id is not None and evaluation_lane == "holdout":
                if optimization_run is None:
                    raise GovernanceConflictError(
                        "candidate optimization binding changed during enqueue"
                    )
                if optimization_run.id != optimization_run_id:
                    raise GovernanceConflictError(
                        "candidate optimization binding changed during enqueue"
                    )
                if optimization_run.gold_set_version_id != gold_set_version_id:
                    raise GovernanceConflictError(
                        "optimization candidate must use its bound gold set"
                    )
                enforce_sealed_holdout_access(
                    requested_candidate_id=tagger_version_id,
                    requested_baseline_id=baseline_tagger_version_id,
                    consumed_candidate_id=(
                        optimization_run.candidate_tagger_version_id
                        or optimization_run.winner_tagger_version_id
                    ),
                    bound_baseline_id=optimization_run.baseline_tagger_version_id,
                )
                previous_query = (
                    await session.execute(
                        select(TagEvaluationRun.id)
                        .join(
                            TaggerVersion,
                            TaggerVersion.id == TagEvaluationRun.tagger_version_id,
                        )
                        .where(
                            TagEvaluationRun.tenant_id == tenant_id,
                            TagEvaluationRun.gold_set_version_id == gold_set_version_id,
                            TaggerVersion.tenant_id == tenant_id,
                            TaggerVersion.optimization_run_id == optimization_run.id,
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if previous_query is not None:
                    raise GovernanceConflictError(
                        "sealed holdout query was already consumed for this optimization run"
                    )
                optimization_run.candidate_tagger_version_id = tagger.id
                optimization_run.phase = "holdout"

            now = datetime.now(UTC)
            run = TagEvaluationRun(
                tenant_id=tenant_id,
                tagger_version_id=tagger_version_id,
                baseline_tagger_version_id=baseline_tagger_version_id,
                gold_set_version_id=gold_set_version_id,
                evaluator_version="tag-evaluator-v2",
                dataset_snapshot_hash=str(
                    version.dataset_snapshot_hash or version.checksum or "legacy-unfrozen"
                ),
                status="queued",
                metrics={},
                baseline_metrics={},
                passed=False,
                started_at=now,
                created_by=actor_user_id,
            )
            session.add(run)
            await session.flush()
            scope = {
                "baseline_tagger_version_id": baseline_tagger_version_id,
                "evaluation_run_id": run.id,
                "gold_set_version_id": gold_set_version_id,
                "evaluation_lane": evaluation_lane,
                "holdout_only": evaluation_lane == "holdout",
                "release_service": release_service,
                "optimization_run_id": optimization_run_id,
                "sealed_holdout_query": (
                    evaluation_lane == "holdout" and optimization_run_id is not None
                ),
                "tagger_version_id": tagger_version_id,
            }
            job = TagExtractionJob(
                tenant_id=tenant_id,
                job_type="evaluate",
                origin="system",
                status="queued",
                scope=scope,
                tagger_version_id=tagger_version_id,
                idempotency_key=idempotency_key,
                total_items=lane_count,
                completed_items=0,
                failed_items=0,
                attempt_count=0,
                max_attempts=3,
                revision=1,
                created_by=actor_user_id,
            )
            session.add(job)
            tagger.status = "validating"
            await session.flush()
            return run, job

    async def _load_evaluation_inputs(
        self,
        *,
        tenant_id: str,
        evaluation_run_id: int,
    ) -> tuple[
        TagEvaluationRun,
        TagExtractionJob,
        TaggerVersion,
        dict[str, dict[str, Any]],
        list[TagGoldLabel],
        int | None,
    ]:
        async with self._factory() as session:
            run = (
                await session.execute(
                    select(TagEvaluationRun).where(
                        TagEvaluationRun.id == evaluation_run_id,
                        TagEvaluationRun.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if run is None:
                raise GovernanceNotFoundError("tag evaluation not found")
            jobs = list(
                (
                    await session.execute(
                        select(TagExtractionJob).where(
                            TagExtractionJob.tenant_id == tenant_id,
                            TagExtractionJob.job_type == "evaluate",
                        )
                    )
                )
                .scalars()
                .all()
            )
            job = next(
                (
                    candidate
                    for candidate in jobs
                    if int(candidate.scope.get("evaluation_run_id", 0)) == run.id
                ),
                None,
            )
            if job is None:
                raise GovernanceNotFoundError("evaluation job not found")
            evaluation_lane = str(job.scope.get("evaluation_lane", "holdout"))
            if evaluation_lane not in {"challenge", "holdout"}:
                raise GovernanceConflictError("evaluation job has an invalid lane binding")
            lane_truth_tiers = {"t3"} if evaluation_lane == "holdout" else {"t2", "t3"}
            tagger = (
                await session.execute(
                    select(TaggerVersion).where(
                        TaggerVersion.id == run.tagger_version_id,
                        TaggerVersion.tenant_id == tenant_id,
                    )
                )
            ).scalar_one()
            schema = (
                await session.execute(
                    select(TagSchemaVersion).where(
                        TagSchemaVersion.id == tagger.schema_version_id,
                        TagSchemaVersion.tenant_id == tenant_id,
                        TagSchemaVersion.status.in_(["published", "deprecated"]),
                    )
                )
            ).scalar_one_or_none()
            if schema is None:
                raise GovernanceConflictError(
                    "evaluation schema is not an immutable published version"
                )
            all_labels = list(
                (
                    await session.execute(
                        select(TagGoldLabel)
                        .where(
                            TagGoldLabel.tenant_id == tenant_id,
                            TagGoldLabel.gold_set_version_id == run.gold_set_version_id,
                        )
                        .order_by(
                            TagGoldLabel.subject_type,
                            TagGoldLabel.subject_id,
                            TagGoldLabel.tag_key,
                        )
                    )
                )
                .scalars()
                .all()
            )
            validate_reception_lane_isolation(all_labels)
            labels = [
                item
                for item in all_labels
                if item.split == evaluation_lane
                and item.truth_tier in lane_truth_tiers
                and item.truth_state in {"present", "absent"}
            ]
            if not labels:
                raise GovernanceConflictError(
                    f"evaluation requires eligible {evaluation_lane} labels"
                )
            gold_snapshot = (
                await session.execute(
                    select(TagGoldSetVersion).where(
                        TagGoldSetVersion.id == run.gold_set_version_id,
                        TagGoldSetVersion.tenant_id == tenant_id,
                    )
                )
            ).scalar_one()
            label_snapshots = [
                {
                    "review_decision_id": item.review_decision_id,
                    "reception_id": item.reception_id,
                    "subject_type": item.subject_type,
                    "subject_id": item.subject_id,
                    "tag_key": item.tag_key,
                    "tag_value": item.tag_value,
                    "truth_state": getattr(
                        item,
                        "truth_state",
                        "absent" if item.tag_value is None else "present",
                    ),
                    "truth_tier": getattr(item, "truth_tier", "t1"),
                    "evidence_refs": item.evidence_refs,
                    "input_hash": getattr(item, "input_hash", None),
                    "input_snapshot": getattr(item, "input_snapshot", None),
                    "annotation_quality": getattr(item, "annotation_quality", None),
                    "cohort": getattr(item, "cohort", None),
                    "completeness_manifest": getattr(item, "completeness_manifest", None),
                    "split": item.split,
                }
                for item in all_labels
            ]
            computed_snapshot_hash = compute_gold_dataset_snapshot_hash(label_snapshots)
            if (
                gold_snapshot.dataset_snapshot_hash
                and computed_snapshot_hash != gold_snapshot.dataset_snapshot_hash
            ):
                raise GovernanceConflictError(
                    "frozen evaluation input no longer matches its dataset snapshot hash"
                )
            if (
                gold_snapshot.dataset_snapshot_hash
                and run.dataset_snapshot_hash != gold_snapshot.dataset_snapshot_hash
            ):
                raise GovernanceConflictError(
                    "evaluation run is bound to a different dataset snapshot"
                )
            manifest = gold_snapshot.completeness_manifest
            if (
                isinstance(manifest, Mapping)
                and (
                    manifest.get("complete")
                    or manifest.get("matrix_complete")
                    or manifest.get("status") in {"complete", "qualified"}
                )
                and any(
                    not getattr(item, "input_hash", None)
                    or not getattr(item, "input_snapshot", None)
                    for item in labels
                )
            ):
                raise GovernanceConflictError(
                    "complete release gold must include frozen input snapshots"
                )
            definitions = {
                str(item["key"]): item
                for item in schema.definitions
                if isinstance(item, dict) and item.get("key")
            }
            baseline_id = job.scope.get("baseline_tagger_version_id")
            if baseline_id is None or int(baseline_id) != int(run.baseline_tagger_version_id):
                raise GovernanceConflictError(
                    "evaluation job baseline binding differs from its immutable run"
                )
            return (
                run,
                job,
                tagger,
                definitions,
                labels,
                int(run.baseline_tagger_version_id),
            )

    async def _predict(
        self,
        *,
        tenant_id: str,
        tagger_version_id: int,
        labels: Sequence[TagGoldLabel],
        evaluation_run_id: int,
    ) -> tuple[dict[tuple[str, int], Sequence[Mapping[str, Any]]], int]:
        predictor = self._get_predictor()
        usage_context = LLMUsageContext(
            evaluation_run_id=evaluation_run_id,
            require_durable_ledger=True,
        )

        async def invoke(method: Any, **kwargs: Any) -> PredictionResult:
            try:
                signature = inspect.signature(method)
            except (TypeError, ValueError):
                signature = None
            if signature is not None and (
                "usage_context" in signature.parameters
                or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in signature.parameters.values()
                )
            ):
                kwargs["usage_context"] = usage_context
            return cast(PredictionResult, await method(**kwargs))

        labels_by_subject: dict[tuple[str, int], list[TagGoldLabel]] = defaultdict(list)
        for item in labels:
            labels_by_subject[(item.subject_type, item.subject_id)].append(item)
        output: dict[tuple[str, int], Sequence[Mapping[str, Any]]] = {}
        errors = 0
        for (subject_type, subject_id), subject_labels in sorted(labels_by_subject.items()):
            try:
                snapshots = [
                    snapshot
                    for item in subject_labels
                    if isinstance((snapshot := getattr(item, "input_snapshot", None)), Mapping)
                    and snapshot
                ]
                input_hashes = {
                    str(input_hash)
                    for item in subject_labels
                    if (input_hash := getattr(item, "input_hash", None))
                }
                if len(input_hashes) > 1 or any(snapshot != snapshots[0] for snapshot in snapshots):
                    raise GovernanceConflictError(
                        "frozen gold labels disagree on their subject input snapshot"
                    )
                frozen_method = getattr(predictor, "predict_frozen_input", None)
                if snapshots and frozen_method is not None:
                    result = await invoke(
                        frozen_method,
                        tenant_id=tenant_id,
                        subject_type=subject_type,
                        subject_id=subject_id,
                        input_snapshot=dict(snapshots[0]),
                        tagger_version_id=tagger_version_id,
                    )
                elif subject_type == "dialogue_unit":
                    result = await invoke(
                        predictor.predict_dialogue_unit,
                        tenant_id=tenant_id,
                        dialogue_unit_id=subject_id,
                        tagger_version_id=tagger_version_id,
                    )
                elif subject_type == "reception":
                    reception_method = getattr(predictor, "predict_reception", None)
                    if reception_method is None:
                        raise RuntimeError("predictor does not support reception subjects")
                    result = await invoke(
                        reception_method,
                        tenant_id=tenant_id,
                        reception_id=subject_id,
                        tagger_version_id=tagger_version_id,
                    )
                else:
                    raise RuntimeError(f"unsupported evaluation subject type: {subject_type}")
            except Exception:
                errors += 1
                continue
            output[(subject_type, subject_id)] = result.assignments
        return output, errors

    async def execute(
        self,
        *,
        tenant_id: str,
        evaluation_run_id: int,
        worker_id: str,
        manage_job: bool = True,
    ) -> TagEvaluationRun:
        run, job, tagger, definitions, labels, baseline_id = await self._load_evaluation_inputs(
            tenant_id=tenant_id,
            evaluation_run_id=evaluation_run_id,
        )
        evaluation_lane = str(job.scope.get("evaluation_lane", "holdout"))
        sealed_release = evaluation_lane == "holdout" and bool(job.scope.get("release_service"))
        raw_optimization_run_id = job.scope.get("optimization_run_id")
        optimization_run_id = (
            int(raw_optimization_run_id)
            if isinstance(raw_optimization_run_id, int)
            and not isinstance(raw_optimization_run_id, bool)
            else None
        )
        optimizer_holdout = evaluation_lane == "holdout" and optimization_run_id is not None
        if run.status == "completed":
            return run
        async with self._factory() as session, session.begin():
            locked_optimization_run: TagOptimizationRun | None = None
            if optimizer_holdout:
                locked_optimization_run = (
                    await session.execute(
                        select(TagOptimizationRun)
                        .where(
                            TagOptimizationRun.id == optimization_run_id,
                            TagOptimizationRun.tenant_id == tenant_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if locked_optimization_run is None:
                    raise GovernanceConflictError(
                        "evaluation references a missing optimization run"
                    )
                if locked_optimization_run.status == "cancelled":
                    raise GovernanceConflictError("optimization run is cancelled")
            locked_job = (
                await session.execute(
                    select(TagExtractionJob)
                    .where(
                        TagExtractionJob.id == job.id,
                        TagExtractionJob.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one()
            locked_run = (
                await session.execute(
                    select(TagEvaluationRun)
                    .where(
                        TagEvaluationRun.id == run.id,
                        TagEvaluationRun.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one()
            if locked_run.status == "completed":
                return locked_run
            locked_tagger = (
                await session.execute(
                    select(TaggerVersion)
                    .where(
                        TaggerVersion.id == tagger.id,
                        TaggerVersion.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one()
            if locked_optimization_run is not None and (
                locked_optimization_run.status != "running"
                or locked_optimization_run.phase != "holdout"
            ):
                raise GovernanceConflictError("optimization run is not active for sealed holdout")
            if locked_job.status == "cancelled":
                raise GovernanceConflictError("evaluation job is cancelled")
            if locked_run.status not in {"queued", "running"}:
                raise GovernanceConflictError("evaluation run is not active")
            if locked_tagger.status not in {"draft", "validating", "evaluating"}:
                raise GovernanceConflictError("evaluation candidate is not active")
            if manage_job:
                if locked_job.status not in {"queued", "running"}:
                    raise GovernanceConflictError("evaluation job is not active")
                if locked_job.status == "running" and locked_job.lease_owner not in {
                    None,
                    worker_id,
                }:
                    raise GovernanceConflictError("evaluation job is leased by another worker")
                locked_job.status = "running"
                locked_job.lease_owner = worker_id
                locked_job.attempt_count += int(locked_job.attempt_count == 0)
            elif locked_job.status != "running" or locked_job.lease_owner != worker_id:
                raise GovernanceConflictError("evaluation job was cancelled or its lease was lost")
            locked_run.status = "running"
            locked_tagger.status = "evaluating"

        predictions, errors = await self._predict(
            tenant_id=tenant_id,
            tagger_version_id=tagger.id,
            labels=labels,
            evaluation_run_id=run.id,
        )
        subject_count = len({(item.subject_type, item.subject_id) for item in labels})
        gold_payload: list[dict[str, Any]] = []
        for frozen_label in labels:
            frozen_input = (
                frozen_label.input_snapshot
                if isinstance(frozen_label.input_snapshot, Mapping)
                else {}
            )
            gold_payload.append(
                {
                    "subject_type": frozen_label.subject_type,
                    "subject_id": frozen_label.subject_id,
                    "tag_key": frozen_label.tag_key,
                    "tag_value": frozen_label.tag_value,
                    "truth_state": getattr(
                        frozen_label,
                        "truth_state",
                        "absent" if frozen_label.tag_value is None else "present",
                    ),
                    "evidence_refs": list(frozen_label.evidence_refs or []),
                    "cohort": getattr(frozen_label, "cohort", None),
                    "scenario": frozen_input.get("scenario"),
                    "store_id": frozen_input.get("store_id"),
                }
            )
        required_schema_pairs = schema_subject_tag_pairs(definitions)
        required_schema_pair_set = set(required_schema_pairs)
        gold_subjects_by_schema_pair: dict[tuple[str, str], set[int]] = defaultdict(set)
        for item in gold_payload:
            pair = (str(item["subject_type"]), str(item["tag_key"]))
            if pair in required_schema_pair_set:
                gold_subjects_by_schema_pair[pair].add(int(item["subject_id"]))
        schema_support_by_subject_tag = {
            f"{subject_type}:{tag_key}": len(gold_subjects_by_schema_pair[(subject_type, tag_key)])
            for subject_type, tag_key in required_schema_pairs
        }
        summary = compute_evaluation_summary(
            gold_labels=gold_payload,
            predictions=predictions,
            definitions=definitions,
            extraction_errors=errors,
            subject_count=subject_count,
        )
        candidate_error_counts = {
            subject_type: len(
                {int(item.subject_id) for item in labels if item.subject_type == subject_type}
                - {
                    int(subject_id)
                    for prediction_subject_type, subject_id in predictions
                    if prediction_subject_type == subject_type
                }
            )
            for subject_type in {item.subject_type for item in labels}
        }
        summaries_by_subject_type = compute_evaluation_summaries_by_subject_type(
            gold_labels=gold_payload,
            predictions=predictions,
            definitions=definitions,
            extraction_errors_by_subject_type=candidate_error_counts,
        )
        if baseline_id is None:
            baseline_summary = summary
            baseline_predictions = predictions
            baseline_summaries_by_subject_type = summaries_by_subject_type
            paired = PairedComparison(
                support=(
                    summary.label_metrics
                    and sum(int(item["support"]) for item in summary.label_metrics.values())
                )
                or 0,
                candidate_wins=0,
                baseline_wins=0,
                ties=(
                    summary.label_metrics
                    and sum(int(item["support"]) for item in summary.label_metrics.values())
                )
                or 0,
                delta=0.0,
                lower_bound=0.0,
                upper_bound=0.0,
            )
        else:
            baseline_predictions, baseline_errors = await self._predict(
                tenant_id=tenant_id,
                tagger_version_id=baseline_id,
                labels=labels,
                evaluation_run_id=run.id,
            )
            baseline_summary = compute_evaluation_summary(
                gold_labels=gold_payload,
                predictions=baseline_predictions,
                definitions=definitions,
                extraction_errors=baseline_errors,
                subject_count=subject_count,
            )
            baseline_error_counts = {
                subject_type: len(
                    {int(item.subject_id) for item in labels if item.subject_type == subject_type}
                    - {
                        int(subject_id)
                        for prediction_subject_type, subject_id in baseline_predictions
                        if prediction_subject_type == subject_type
                    }
                )
                for subject_type in {item.subject_type for item in labels}
            }
            baseline_summaries_by_subject_type = compute_evaluation_summaries_by_subject_type(
                gold_labels=gold_payload,
                predictions=baseline_predictions,
                definitions=definitions,
                extraction_errors_by_subject_type=baseline_error_counts,
            )
            paired = compute_paired_comparison(
                gold_labels=gold_payload,
                candidate_predictions=predictions,
                baseline_predictions=baseline_predictions,
            )
        slice_summaries = compute_evaluation_summaries_by_slice(
            gold_labels=gold_payload,
            predictions=predictions,
            definitions=definitions,
        )
        baseline_slice_summaries = compute_evaluation_summaries_by_slice(
            gold_labels=gold_payload,
            predictions=baseline_predictions,
            definitions=definitions,
        )
        paired_by_subject_type = {
            subject_type: compute_paired_comparison(
                gold_labels=[item for item in gold_payload if item["subject_type"] == subject_type],
                candidate_predictions=predictions,
                baseline_predictions=baseline_predictions,
            )
            for subject_type in summaries_by_subject_type
        }
        subject_type_gates: list[Gate] = []
        for subject_type, subject_summary in sorted(summaries_by_subject_type.items()):
            baseline_subject_summary = baseline_summaries_by_subject_type[subject_type]
            subject_quality = evaluate_quality_gates(
                metrics={
                    **subject_summary.metrics,
                    "paired_accuracy_delta": paired_by_subject_type[subject_type].delta,
                    "paired_accuracy_delta_lcb": (paired_by_subject_type[subject_type].lower_bound),
                },
                baseline=baseline_subject_summary.metrics,
                supported_label_f1={
                    key: float(value["f1"])
                    for key, value in subject_summary.label_metrics.items()
                    if int(value["support"]) >= 30
                },
                baseline_label_f1={
                    key: float(value["f1"])
                    for key, value in baseline_subject_summary.label_metrics.items()
                    if int(value["support"]) >= 30
                },
            )
            subject_type_gates.extend(
                Gate(
                    code=f"subject_type:{subject_type}:{gate.code}",
                    passed=gate.passed,
                    actual=gate.actual,
                    threshold=gate.threshold,
                    message=f"{subject_type}: {gate.message}",
                )
                for gate in subject_quality.gates
            )
            for tag_key, values in sorted(subject_summary.critical_value_metrics.items()):
                for value, metric in sorted(values.items()):
                    positive_support = int(metric["support"])
                    recall_lcb = float(metric["recall_lcb"])
                    subject_type_gates.append(
                        Gate(
                            code=(f"subject_type:{subject_type}:critical_value:{tag_key}:{value}"),
                            passed=(
                                positive_support > 0
                                and recall_lcb >= CRITICAL_RECALL_LCB_THRESHOLD
                            ),
                            actual=recall_lcb,
                            threshold=CRITICAL_RECALL_LCB_THRESHOLD,
                            message=(
                                f"{subject_type}/{tag_key}={value}: critical-value "
                                "recall Wilson lower bound must be at least 95% "
                                f"(positive support={positive_support})"
                            ),
                        )
                    )
            subject_supports = [
                int(value["support"]) for value in subject_summary.label_metrics.values()
            ]
            subject_type_gates.append(
                Gate(
                    code=f"subject_type:{subject_type}:minimum_support",
                    passed=bool(subject_supports) and not subject_summary.insufficient_labels,
                    actual=float(min(subject_supports, default=0)),
                    threshold=30.0,
                    message=(
                        f"{subject_type}: every evaluated label requires at least "
                        f"30 {evaluation_lane} samples"
                    ),
                )
            )
        schema_support_gates: list[Gate] = []
        if evaluation_lane == "holdout":
            schema_support_gates.extend(
                Gate(
                    code=f"subject_type:{subject_type}:tag_support:{tag_key}",
                    passed=(schema_support_by_subject_tag[f"{subject_type}:{tag_key}"] >= 30),
                    actual=float(schema_support_by_subject_tag[f"{subject_type}:{tag_key}"]),
                    threshold=30.0,
                    message=(
                        f"{subject_type}/{tag_key}: sealed holdout requires at least "
                        "30 definitive T3 subjects"
                    ),
                )
                for subject_type, tag_key in required_schema_pairs
            )
            if not required_schema_pairs:
                schema_support_gates.append(
                    Gate(
                        code="schema:applicable_subject_tag_support",
                        passed=False,
                        actual=0.0,
                        threshold=1.0,
                        message=(
                            "sealed holdout requires at least one applicable "
                            "Schema subject/tag pair"
                        ),
                    )
                )
        critical_tag_keys = {
            tag_key
            for tag_key, definition in definitions.items()
            if definition.get("critical") or definition.get("critical_values")
        }
        slice_gates: list[Gate] = []
        supported_slice_f1: list[tuple[str, str, float]] = []
        for slice_key, slice_summary in sorted(slice_summaries.items()):
            baseline_slice_summary = baseline_slice_summaries[slice_key]
            for tag_key, label_metric in sorted(slice_summary.label_metrics.items()):
                support = int(label_metric["support"])
                if support < 30 or tag_key not in critical_tag_keys:
                    continue
                baseline_metric = baseline_slice_summary.label_metrics.get(tag_key)
                if baseline_metric is None:
                    continue
                actual = float(label_metric["f1"])
                threshold = float(baseline_metric["f1"]) - 0.01
                supported_slice_f1.append((slice_key, tag_key, actual))
                slice_gates.append(
                    Gate(
                        code=f"slice:{slice_key}:{tag_key}:f1",
                        passed=actual >= threshold,
                        actual=actual,
                        threshold=threshold,
                        message=(
                            f"{slice_key}/{tag_key}: supported critical slice F1 "
                            "must not regress by more than one percentage point"
                        ),
                    )
                )
        # The aggregate summary remains visible, but never shares a release denominator
        # across dialogue_unit and reception.
        gates = (*schema_support_gates, *subject_type_gates, *slice_gates)
        passed = all(item.passed for item in gates)
        worst_slice_item = min(
            supported_slice_f1,
            key=lambda value: (value[2], value[0], value[1]),
            default=None,
        )
        worst_slice = (
            {
                "slice": worst_slice_item[0],
                "tag_key": worst_slice_item[1],
                "f1": worst_slice_item[2],
            }
            if worst_slice_item is not None
            else None
        )
        metrics_payload: dict[str, Any] = {
            **summary.metrics,
            "confusion": summary.confusion,
            "critical_value_metrics": summary.critical_value_metrics,
            "evaluation_lane": evaluation_lane,
            "holdout_only": evaluation_lane == "holdout",
            "sealed_release": sealed_release,
            "insufficient_labels": list(summary.insufficient_labels),
            "label_metrics": summary.label_metrics,
            "paired_accuracy": _paired_payload(paired),
            "schema_support_by_subject_tag": schema_support_by_subject_tag,
            "subject_count": subject_count,
            "mixed_denominator_release_gate": False,
            "by_subject_type": {
                subject_type: {
                    **_summary_payload(subject_summary),
                    "paired_accuracy": _paired_payload(paired_by_subject_type[subject_type]),
                }
                for subject_type, subject_summary in sorted(summaries_by_subject_type.items())
            },
            "slice_metrics": {
                slice_key: _summary_payload(slice_summary)
                for slice_key, slice_summary in sorted(slice_summaries.items())
            },
            "worst_supported_critical_slice": worst_slice,
            "value_metrics": summary.value_metrics,
        }
        baseline_payload: dict[str, Any] = {
            **baseline_summary.metrics,
            "by_subject_type": {
                subject_type: _summary_payload(subject_summary)
                for subject_type, subject_summary in sorted(
                    baseline_summaries_by_subject_type.items()
                )
            },
            "label_metrics": baseline_summary.label_metrics,
            "slice_metrics": {
                slice_key: _summary_payload(slice_summary)
                for slice_key, slice_summary in sorted(baseline_slice_summaries.items())
            },
            "value_metrics": baseline_summary.value_metrics,
        }
        candidate_index = _prediction_index(predictions)
        baseline_index = (
            candidate_index if baseline_id is None else _prediction_index(baseline_predictions)
        )
        evaluation_items: list[dict[str, Any]] = []
        for label in labels:
            identity = (label.subject_type, label.subject_id, label.tag_key)
            candidate_prediction = candidate_index.get(identity)
            baseline_prediction = baseline_index.get(identity)
            truth_mapping = {
                "subject_type": label.subject_type,
                "subject_id": label.subject_id,
                "tag_key": label.tag_key,
                "tag_value": label.tag_value,
                "truth_state": getattr(
                    label,
                    "truth_state",
                    "absent" if label.tag_value is None else "present",
                ),
            }
            candidate_correct = _is_correct_prediction(truth_mapping, candidate_prediction)
            baseline_correct = _is_correct_prediction(truth_mapping, baseline_prediction)
            error_taxonomy: list[str] = []
            if candidate_correct is False:
                if truth_mapping["truth_state"] == "absent":
                    error_taxonomy.append("false_positive")
                elif candidate_prediction is None:
                    error_taxonomy.append("false_negative")
                else:
                    error_taxonomy.append("wrong_value")
            evaluation_items.append(
                {
                    "gold_label": label,
                    "truth_state": truth_mapping["truth_state"],
                    "candidate_prediction": candidate_prediction,
                    "baseline_prediction": baseline_prediction,
                    "candidate_correct": candidate_correct,
                    "baseline_correct": baseline_correct,
                    "error_taxonomy": error_taxonomy,
                }
            )
        now = datetime.now(UTC)
        async with self._factory() as session, session.begin():
            locked_optimization_run = None
            if optimizer_holdout:
                locked_optimization_run = (
                    await session.execute(
                        select(TagOptimizationRun)
                        .where(
                            TagOptimizationRun.id == optimization_run_id,
                            TagOptimizationRun.tenant_id == tenant_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if locked_optimization_run is None:
                    raise GovernanceConflictError(
                        "evaluation references a missing optimization run"
                    )
                if locked_optimization_run.status == "cancelled":
                    raise GovernanceConflictError("optimization run is cancelled")
            locked_job = (
                await session.execute(
                    select(TagExtractionJob)
                    .where(
                        TagExtractionJob.id == job.id,
                        TagExtractionJob.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one()
            locked_run = (
                await session.execute(
                    select(TagEvaluationRun)
                    .where(
                        TagEvaluationRun.id == run.id,
                        TagEvaluationRun.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one()
            if locked_run.status == "completed":
                return locked_run
            locked_tagger = (
                await session.execute(
                    select(TaggerVersion)
                    .where(
                        TaggerVersion.id == tagger.id,
                        TaggerVersion.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one()
            if locked_optimization_run is not None and (
                locked_optimization_run.status != "running"
                or locked_optimization_run.phase != "holdout"
            ):
                raise GovernanceConflictError("optimization run is not active for sealed holdout")
            if locked_job.status == "cancelled":
                raise GovernanceConflictError("evaluation job is cancelled")
            if locked_job.status != "running" or locked_job.lease_owner != worker_id:
                raise GovernanceConflictError("evaluation job was cancelled or its lease was lost")
            if locked_run.status != "running":
                raise GovernanceConflictError("evaluation run is not active")
            if locked_tagger.status != "evaluating":
                raise GovernanceConflictError("evaluation candidate is not active")
            await session.execute(
                delete(TagEvaluationMetric).where(
                    TagEvaluationMetric.tenant_id == tenant_id,
                    TagEvaluationMetric.evaluation_run_id == run.id,
                )
            )
            await session.execute(
                delete(TagGateResult).where(
                    TagGateResult.tenant_id == tenant_id,
                    TagGateResult.evaluation_run_id == run.id,
                )
            )
            await session.execute(
                delete(TagEvaluationItem).where(
                    TagEvaluationItem.tenant_id == tenant_id,
                    TagEvaluationItem.evaluation_run_id == run.id,
                )
            )
            for item in evaluation_items:
                label = cast(TagGoldLabel, item["gold_label"])
                candidate_prediction = cast(Mapping[str, Any] | None, item["candidate_prediction"])
                baseline_prediction = cast(Mapping[str, Any] | None, item["baseline_prediction"])
                candidate_correct = cast(bool | None, item["candidate_correct"])
                baseline_correct = cast(bool | None, item["baseline_correct"])
                session.add(
                    TagEvaluationItem(
                        tenant_id=tenant_id,
                        evaluation_run_id=run.id,
                        gold_label_id=label.id,
                        subject_type=label.subject_type,
                        subject_id=label.subject_id,
                        tag_key=label.tag_key,
                        truth_state=str(item["truth_state"]),
                        candidate_prediction=(
                            candidate_prediction.get("tag_value")
                            if candidate_prediction is not None
                            else None
                        ),
                        baseline_prediction=(
                            baseline_prediction.get("tag_value")
                            if baseline_prediction is not None
                            else None
                        ),
                        candidate_score=(
                            float(candidate_prediction["confidence"])
                            if candidate_prediction is not None
                            and candidate_prediction.get("confidence") is not None
                            else None
                        ),
                        baseline_score=(
                            float(baseline_prediction["confidence"])
                            if baseline_prediction is not None
                            and baseline_prediction.get("confidence") is not None
                            else None
                        ),
                        candidate_evidence_refs=(
                            list(candidate_prediction.get("evidence_refs") or [])
                            if candidate_prediction is not None
                            else []
                        ),
                        baseline_evidence_refs=(
                            list(baseline_prediction.get("evidence_refs") or [])
                            if baseline_prediction is not None
                            else []
                        ),
                        error_taxonomy=list(item["error_taxonomy"]),
                        slice_snapshot={
                            "cohort": getattr(label, "cohort", None),
                            "reception_id": label.reception_id,
                            "subject_type": label.subject_type,
                        },
                        paired_delta={
                            "baseline_correct": baseline_correct,
                            "candidate_correct": candidate_correct,
                            "difference": (
                                int(candidate_correct) - int(baseline_correct)
                                if candidate_correct is not None and baseline_correct is not None
                                else None
                            ),
                        },
                    )
                )
            for metric_key in (
                "macro_f1",
                "critical_recall",
                "critical_recall_lcb",
                "evidence_coverage",
                "evidence_iou",
                "brier_score",
                "ece",
                "schema_violation_count",
                "evidence_violation_count",
                "lineage_violation_count",
                "error_rate",
            ):
                session.add(
                    TagEvaluationMetric(
                        tenant_id=tenant_id,
                        evaluation_run_id=run.id,
                        metric_key=metric_key,
                        label_key=None,
                        value=float(summary.metrics[metric_key]),
                        support=subject_count,
                    )
                )
            for label_key, label_values in summary.label_metrics.items():
                for metric_key in ("precision", "recall", "f1"):
                    session.add(
                        TagEvaluationMetric(
                            tenant_id=tenant_id,
                            evaluation_run_id=run.id,
                            metric_key=metric_key,
                            label_key=label_key,
                            value=float(label_values[metric_key]),
                            support=int(label_values["support"]),
                        )
                    )
            for gate in gates:
                session.add(
                    TagGateResult(
                        tenant_id=tenant_id,
                        evaluation_run_id=run.id,
                        code=gate.code,
                        passed=gate.passed,
                        actual=gate.actual,
                        threshold=gate.threshold,
                        message=gate.message,
                    )
                )
            # Child metrics/items become immutable as soon as the parent run is
            # certified, so persist them before taking the terminal transition.
            await session.flush()
            locked_run.status = "completed"
            locked_run.metrics = metrics_payload
            locked_run.baseline_metrics = baseline_payload
            locked_run.passed = passed
            locked_run.finished_at = now
            release_passed = passed and sealed_release
            locked_tagger.status = (
                "qualified" if release_passed else ("validating" if passed else "rejected")
            )
            locked_tagger.qualified_at = now if release_passed else None
            candidate_optimization_run_id = getattr(
                locked_tagger,
                "optimization_run_id",
                None,
            )
            if candidate_optimization_run_id is not None and evaluation_lane == "holdout":
                if (
                    locked_optimization_run is None
                    or locked_optimization_run.id != candidate_optimization_run_id
                ):
                    raise GovernanceConflictError(
                        "evaluation optimization binding changed during execution"
                    )
                optimization_run = locked_optimization_run
                optimization_run.winner_tagger_version_id = locked_tagger.id if passed else None
                optimization_run.status = "completed"
                optimization_run.phase = "completed"
                optimization_run.finished_at = now
                optimization_run.summary = {
                    **dict(optimization_run.summary),
                    "evaluation_run_id": locked_run.id,
                    "holdout_passed": passed,
                    "sealed_holdout_queries_used": 1,
                }
                optimization_run.next_actions = (
                    ["start_shadow_deployment"]
                    if passed
                    else ["inspect_regressions", "create_new_optimization_run"]
                )
            if manage_job:
                locked_job.status = "completed"
                locked_job.completed_items = subject_count - errors
                locked_job.failed_items = errors
                locked_job.lease_owner = None
                locked_job.lease_token = None
                locked_job.lease_expires_at = None
                locked_job.finished_at = now
                locked_job.revision += 1
            await session.flush()
            return locked_run


__all__ = [
    "EvaluationSummary",
    "PairedComparison",
    "TagEvaluationService",
    "compute_evaluation_summaries_by_subject_type",
    "compute_evaluation_summary",
    "compute_paired_comparison",
    "wilson_lower_bound",
]
