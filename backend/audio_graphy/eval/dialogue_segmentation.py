"""Deterministic release gate for dialogue segmentation gold sets.

The production segmenter stays free of evaluation concerns.  This module
turns a frozen, reviewable gold file into boundary and stage metrics and makes
the default-version decision explicit.  A failed gate never mutates runtime
configuration; callers may publish the consistency fixes while retaining the
previous algorithm as the tenant default.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from audio_graphy.core.dialogue_segmentation import (
    AUTOMOTIVE_STAGE_ORDER,
    GOLD_JEWELRY_STAGE_ORDER,
    DialogueSegment,
    DialogueSegmenter,
    SalesScenario,
)


@dataclass(frozen=True, slots=True)
class DialogueGoldCase:
    """One frozen recording/session with adjudicated boundaries and stages."""

    case_id: str
    scenario: SalesScenario
    segments: tuple[DialogueSegment, ...]
    boundary_after_segment_ids: frozenset[str]
    stage_by_segment_id: Mapping[str, str]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> DialogueGoldCase:
        case_id = str(raw.get("case_id", "")).strip()
        if not case_id:
            raise ValueError("gold case_id must not be empty")
        scenario = SalesScenario(str(raw.get("scenario", "")))
        raw_segments = raw.get("segments")
        if not isinstance(raw_segments, list) or not raw_segments:
            raise ValueError(f"gold case {case_id!r} must contain segments")

        segments: list[DialogueSegment] = []
        for item in raw_segments:
            if not isinstance(item, Mapping):
                raise ValueError(f"gold case {case_id!r} contains an invalid segment")
            embedding = item.get("semantic_embedding")
            segments.append(
                DialogueSegment(
                    segment_id=str(item.get("segment_id", "")),
                    recording_id=case_id,
                    start_sec=float(item["start_sec"]),
                    end_sec=float(item["end_sec"]),
                    transcript=str(item.get("transcript", "")),
                    speaker=(str(item["speaker"]) if item.get("speaker") is not None else None),
                    vad_conf=float(item.get("vad_conf", 1.0)),
                    semantic_embedding=(
                        tuple(float(value) for value in embedding)
                        if isinstance(embedding, list)
                        else None
                    ),
                    topic_hint=(
                        str(item["topic_hint"]) if item.get("topic_hint") is not None else None
                    ),
                    stage_hint=(
                        str(item["stage_hint"]) if item.get("stage_hint") is not None else None
                    ),
                )
            )

        identities = [segment.segment_id for segment in segments]
        if len(identities) != len(set(identities)):
            raise ValueError(f"gold case {case_id!r} contains duplicate segment IDs")
        ordered = sorted(
            segments,
            key=lambda item: (item.start_sec, item.end_sec, item.segment_id),
        )
        if segments != ordered:
            raise ValueError(f"gold case {case_id!r} segments must be chronological")

        raw_boundaries = raw.get("boundary_after_segment_ids", [])
        if not isinstance(raw_boundaries, list):
            raise ValueError(f"gold case {case_id!r} boundaries must be a list")
        boundaries = frozenset(str(value) for value in raw_boundaries)
        legal_boundaries = set(identities[:-1])
        if not boundaries <= legal_boundaries:
            raise ValueError(f"gold case {case_id!r} has an invalid terminal boundary")

        raw_stages = raw.get("stage_by_segment_id")
        if not isinstance(raw_stages, Mapping):
            raise ValueError(f"gold case {case_id!r} must contain stage labels")
        stages = {str(key): str(value) for key, value in raw_stages.items()}
        if set(stages) != set(identities):
            raise ValueError(f"gold case {case_id!r} stage labels must cover every segment")
        legal_stages = _scenario_stages(scenario)
        if any(stage not in legal_stages for stage in stages.values()):
            raise ValueError(f"gold case {case_id!r} contains an out-of-scenario stage")

        return cls(
            case_id=case_id,
            scenario=scenario,
            segments=tuple(segments),
            boundary_after_segment_ids=boundaries,
            stage_by_segment_id=stages,
        )


@dataclass(frozen=True, slots=True)
class ClassificationMetrics:
    precision: float
    recall: float
    f1: float
    true_positive: int
    false_positive: int
    false_negative: int


@dataclass(frozen=True, slots=True)
class DialogueGoldMetrics:
    boundary: ClassificationMetrics
    stage_macro_f1: float
    boundary_f1_by_scenario: Mapping[str, float]
    stage_macro_f1_by_scenario: Mapping[str, float]
    case_count: int
    segment_count: int


@dataclass(frozen=True, slots=True)
class DialogueReleaseDecision:
    """Pure result consumed by release tooling or a feature-flag controller."""

    publish_v2_default: bool
    metrics: DialogueGoldMetrics
    failures: tuple[str, ...]


def load_dialogue_gold(
    path: str | Path,
) -> tuple[tuple[DialogueGoldCase, ...], dict[str, dict[str, float]]]:
    """Load a frozen JSON gold set and its per-scenario v1 baselines."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("dialogue gold root must be an object")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("dialogue gold must contain at least one case")
    cases = tuple(DialogueGoldCase.from_mapping(item) for item in raw_cases)
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("dialogue gold case IDs must be unique")

    raw_baselines = payload.get("v1_baseline_by_scenario")
    if not isinstance(raw_baselines, Mapping):
        raise ValueError("dialogue gold must contain v1 scenario baselines")
    baselines: dict[str, dict[str, float]] = {}
    represented_scenarios = {case.scenario.value for case in cases}
    for scenario in represented_scenarios:
        raw = raw_baselines.get(scenario)
        if not isinstance(raw, Mapping):
            raise ValueError(f"missing v1 baseline for scenario {scenario!r}")
        boundary_f1 = float(raw.get("boundary_f1", -1))
        stage_macro_f1 = float(raw.get("stage_macro_f1", -1))
        if not 0.0 <= boundary_f1 <= 1.0 or not 0.0 <= stage_macro_f1 <= 1.0:
            raise ValueError(f"invalid v1 baseline for scenario {scenario!r}")
        baselines[scenario] = {
            "boundary_f1": boundary_f1,
            "stage_macro_f1": stage_macro_f1,
        }
    return cases, baselines


def evaluate_dialogue_gold(
    cases: Sequence[DialogueGoldCase],
    *,
    segmenter: DialogueSegmenter | None = None,
) -> DialogueGoldMetrics:
    """Evaluate boundaries on adjacencies and stages on source segments."""

    if not cases:
        raise ValueError("at least one dialogue gold case is required")
    active_segmenter = segmenter or DialogueSegmenter()
    boundary_counts = [0, 0, 0]
    boundary_counts_by_scenario: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    gold_stage_pairs: list[tuple[str, str]] = []
    stage_pairs_by_scenario: dict[str, list[tuple[str, str]]] = defaultdict(list)
    segment_count = 0

    for case in cases:
        predicted_units = active_segmenter.segment(
            case.segments,
            scenario=case.scenario,
            recording_id=case.case_id,
        )
        predicted_boundaries = {unit.segment_refs[-1].segment_id for unit in predicted_units[:-1]}
        legal_boundaries = {segment.segment_id for segment in case.segments[:-1]}
        predicted_boundaries &= legal_boundaries
        gold_boundaries = set(case.boundary_after_segment_ids)
        counts = _binary_counts(predicted_boundaries, gold_boundaries)
        for index, value in enumerate(counts):
            boundary_counts[index] += value
            boundary_counts_by_scenario[case.scenario.value][index] += value

        predicted_stage_by_segment: dict[str, str] = {}
        for unit in predicted_units:
            for ref in unit.segment_refs:
                if ref.segment_id in predicted_stage_by_segment:
                    raise RuntimeError(
                        f"segment {ref.segment_id!r} appeared in multiple predicted units"
                    )
                predicted_stage_by_segment[ref.segment_id] = str(unit.stage)
        expected_ids = {segment.segment_id for segment in case.segments}
        if set(predicted_stage_by_segment) != expected_ids:
            raise RuntimeError(
                f"segmenter did not preserve all segments for gold case {case.case_id!r}"
            )
        for segment in case.segments:
            pair = (
                case.stage_by_segment_id[segment.segment_id],
                predicted_stage_by_segment[segment.segment_id],
            )
            gold_stage_pairs.append(pair)
            stage_pairs_by_scenario[case.scenario.value].append(pair)
        segment_count += len(case.segments)

    boundary = _classification_metrics(*boundary_counts)
    return DialogueGoldMetrics(
        boundary=boundary,
        stage_macro_f1=_macro_f1(gold_stage_pairs),
        boundary_f1_by_scenario={
            scenario: _classification_metrics(*counts).f1
            for scenario, counts in sorted(boundary_counts_by_scenario.items())
        },
        stage_macro_f1_by_scenario={
            scenario: _macro_f1(pairs)
            for scenario, pairs in sorted(stage_pairs_by_scenario.items())
        },
        case_count=len(cases),
        segment_count=segment_count,
    )


def evaluate_dialogue_release(
    cases: Sequence[DialogueGoldCase],
    *,
    v1_baseline_by_scenario: Mapping[str, Mapping[str, float]],
    segmenter: DialogueSegmenter | None = None,
    minimum_boundary_f1: float = 0.85,
    minimum_stage_macro_f1: float = 0.80,
) -> DialogueReleaseDecision:
    """Apply global thresholds and the no-regression rule for every scenario."""

    if not 0.0 <= minimum_boundary_f1 <= 1.0:
        raise ValueError("minimum_boundary_f1 must be in [0, 1]")
    if not 0.0 <= minimum_stage_macro_f1 <= 1.0:
        raise ValueError("minimum_stage_macro_f1 must be in [0, 1]")
    metrics = evaluate_dialogue_gold(cases, segmenter=segmenter)
    failures: list[str] = []
    if metrics.boundary.f1 < minimum_boundary_f1:
        failures.append(f"global boundary F1 {metrics.boundary.f1:.4f} < {minimum_boundary_f1:.4f}")
    if metrics.stage_macro_f1 < minimum_stage_macro_f1:
        failures.append(
            f"global stage macro-F1 {metrics.stage_macro_f1:.4f} < {minimum_stage_macro_f1:.4f}"
        )

    for scenario, boundary_f1 in metrics.boundary_f1_by_scenario.items():
        baseline = v1_baseline_by_scenario.get(scenario)
        if baseline is None:
            failures.append(f"missing v1 baseline for scenario {scenario}")
            continue
        baseline_boundary = float(baseline.get("boundary_f1", -1))
        baseline_stage = float(baseline.get("stage_macro_f1", -1))
        if not 0.0 <= baseline_boundary <= 1.0 or not 0.0 <= baseline_stage <= 1.0:
            failures.append(f"invalid v1 baseline for scenario {scenario}")
            continue
        if boundary_f1 < baseline_boundary:
            failures.append(
                f"{scenario} boundary F1 {boundary_f1:.4f} < v1 {baseline_boundary:.4f}"
            )
        stage_f1 = metrics.stage_macro_f1_by_scenario[scenario]
        if stage_f1 < baseline_stage:
            failures.append(f"{scenario} stage macro-F1 {stage_f1:.4f} < v1 {baseline_stage:.4f}")

    return DialogueReleaseDecision(
        publish_v2_default=not failures,
        metrics=metrics,
        failures=tuple(failures),
    )


def _scenario_stages(scenario: SalesScenario) -> frozenset[str]:
    if scenario == SalesScenario.GOLD_JEWELRY:
        return frozenset(GOLD_JEWELRY_STAGE_ORDER)
    if scenario == SalesScenario.AUTOMOTIVE:
        return frozenset(AUTOMOTIVE_STAGE_ORDER)
    raise ValueError("release gold sets must use a scenario-specific state space")


def _binary_counts(
    predicted: set[str],
    expected: set[str],
) -> tuple[int, int, int]:
    return (
        len(predicted & expected),
        len(predicted - expected),
        len(expected - predicted),
    )


def _classification_metrics(
    true_positive: int,
    false_positive: int,
    false_negative: int,
) -> ClassificationMetrics:
    precision_denominator = true_positive + false_positive
    recall_denominator = true_positive + false_negative
    precision = (
        true_positive / precision_denominator
        if precision_denominator
        else 1.0
        if recall_denominator == 0
        else 0.0
    )
    recall = true_positive / recall_denominator if recall_denominator else 1.0
    denominator = precision + recall
    f1 = 2 * precision * recall / denominator if denominator else 0.0
    return ClassificationMetrics(
        precision=round(precision, 6),
        recall=round(recall, 6),
        f1=round(f1, 6),
        true_positive=true_positive,
        false_positive=false_positive,
        false_negative=false_negative,
    )


def _macro_f1(pairs: Sequence[tuple[str, str]]) -> float:
    if not pairs:
        return 0.0
    labels = sorted({label for pair in pairs for label in pair})
    values: list[float] = []
    for label in labels:
        true_positive = sum(1 for expected, predicted in pairs if expected == predicted == label)
        false_positive = sum(
            1 for expected, predicted in pairs if expected != label and predicted == label
        )
        false_negative = sum(
            1 for expected, predicted in pairs if expected == label and predicted != label
        )
        values.append(
            _classification_metrics(
                true_positive,
                false_positive,
                false_negative,
            ).f1
        )
    return round(sum(values) / len(values), 6)


__all__ = [
    "ClassificationMetrics",
    "DialogueGoldCase",
    "DialogueGoldMetrics",
    "DialogueReleaseDecision",
    "evaluate_dialogue_gold",
    "evaluate_dialogue_release",
    "load_dialogue_gold",
]
