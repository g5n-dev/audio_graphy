"""Deterministic multi-group dialogue-tag comparison and insight engine."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations

from audio_graphy.schemas.tag_insights import (
    MAX_DIMENSION_ITEMS,
    MAX_DISTRIBUTION_ITEMS,
    MAX_OUTPUT_EVIDENCE_REFS,
    MAX_OUTPUT_EVIDENCE_TEXT_BYTES,
    MAX_TREND_ITEMS,
    AnalyzeTagInsightsRequest,
    AnalyzeTagInsightsResponse,
    ConfidenceInsight,
    CoOccurrenceInsight,
    CoverageInsight,
    DimensionComparison,
    DimensionName,
    DistributionInsight,
    EvidenceRef,
    EvidenceRefSummary,
    InsightOutputBudget,
    InsightOverview,
    MatrixCell,
    MatrixRow,
    MergedTagResult,
    MergeStrategy,
    PairwiseComparison,
    PairwiseDifference,
    TagAssignment,
    TagGroup,
    TimeWindow,
    TrendGranularity,
    TrendInsight,
)

CellKey = tuple[str, int, int, str]
WindowKey = tuple[str, int, int]

_MERGED_GROUP_KEY = "__merged__"
_CONFIDENCE_BUCKETS = (
    "0.0-0.2",
    "0.2-0.4",
    "0.4-0.6",
    "0.6-0.8",
    "0.8-1.0",
)
_MAX_EVIDENCE_EXCERPT_BYTES = 256


def _truncate_utf8(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore")


@dataclass(slots=True)
class _EvidenceOutputBudget:
    """Count every serialized evidence occurrence, including summaries."""

    ref_limit: int = MAX_OUTPUT_EVIDENCE_REFS
    text_byte_limit: int = MAX_OUTPUT_EVIDENCE_TEXT_BYTES
    ref_count: int = 0
    text_bytes: int = 0
    truncated: bool = False

    def take(self, evidence: EvidenceRef) -> EvidenceRef | None:
        if self.ref_count >= self.ref_limit:
            self.truncated = True
            return None
        excerpt = evidence.text_excerpt
        if excerpt is not None:
            remaining = self.text_byte_limit - self.text_bytes
            bounded = _truncate_utf8(
                excerpt,
                min(_MAX_EVIDENCE_EXCERPT_BYTES, max(remaining, 0)),
            )
            if not bounded:
                self.truncated = True
                return None
            if bounded != excerpt:
                self.truncated = True
            excerpt = bounded
            self.text_bytes += len(excerpt.encode("utf-8"))
        self.ref_count += 1
        return evidence.model_copy(update={"text_excerpt": excerpt})

    def take_many(self, evidence_refs: Iterable[EvidenceRef]) -> list[EvidenceRef]:
        result: list[EvidenceRef] = []
        for evidence in evidence_refs:
            selected = self.take(evidence)
            if selected is not None:
                result.append(selected)
        return result

    def take_summary(self, evidence: EvidenceRef) -> EvidenceRefSummary | None:
        if self.ref_count >= self.ref_limit:
            self.truncated = True
            return None
        self.ref_count += 1
        return EvidenceRefSummary(
            ref_id=evidence.ref_id,
            kind=evidence.kind,
            recording_id=evidence.recording_id,
            start_ms=evidence.start_ms,
            end_ms=evidence.end_ms,
        )


def _group_id(group: TagGroup) -> str:
    return group.group_id or group.group_key


def _assignment_group_id(assignment: TagAssignment) -> str:
    return assignment.group_id or assignment.group_key


def _cell_key(assignment: TagAssignment) -> CellKey:
    return (
        assignment.target_id,
        assignment.window.start_ms,
        assignment.window.end_ms,
        assignment.label_key,
    )


def _window_key(assignment: TagAssignment) -> WindowKey:
    return (
        assignment.target_id,
        assignment.window.start_ms,
        assignment.window.end_ms,
    )


def _mean_confidence(assignments: Iterable[TagAssignment]) -> float | None:
    values = [
        assignment.confidence for assignment in assignments if assignment.confidence is not None
    ]
    return round(sum(values) / len(values), 6) if values else None


def _deduplicate_evidence(assignments: Iterable[TagAssignment]) -> list[EvidenceRef]:
    result: list[EvidenceRef] = []
    seen: set[str] = set()
    for assignment in assignments:
        for evidence in assignment.evidence_refs:
            fingerprint = evidence.model_dump_json()
            if fingerprint not in seen:
                result.append(evidence)
                seen.add(fingerprint)
    return result


def _highest_priority(
    candidates: list[tuple[int, TagGroup, TagAssignment]],
) -> tuple[int, TagGroup, TagAssignment]:
    # ``max`` is stable, so an equal-priority tie follows caller-declared group order.
    return max(candidates, key=lambda item: item[1].priority)


def _merge_cell(
    *,
    strategy: MergeStrategy,
    groups: list[TagGroup],
    by_group: dict[str, TagAssignment],
) -> MergedTagResult:
    candidates = [
        (index, group, by_group[_group_id(group)])
        for index, group in enumerate(groups)
        if _group_id(group) in by_group
    ]

    selected: list[tuple[int, TagGroup, TagAssignment]]
    values: list[str]
    if strategy == "union":
        selected = candidates
        values = sorted({item[2].value for item in selected})
    elif strategy == "intersection":
        present_values = {item[2].value for item in candidates}
        if len(candidates) == len(groups) and len(present_values) == 1:
            selected = candidates
            values = sorted(present_values)
        else:
            selected = []
            values = []
    else:
        pool = candidates
        if strategy == "manual_wins":
            manual = [
                item
                for item in candidates
                if item[2].is_manual or item[1].source.casefold() == "manual"
            ]
            if manual:
                pool = manual
        selected = [_highest_priority(pool)] if pool else []
        values = [selected[0][2].value] if selected else []

    selected_assignments = [item[2] for item in selected]
    return MergedTagResult(
        strategy=strategy,
        values=values,
        selected_group_keys=[_group_id(item[1]) for item in selected],
        confidence=_mean_confidence(selected_assignments),
        evidence_refs=_deduplicate_evidence(selected_assignments),
    )


def _build_matrix(
    request: AnalyzeTagInsightsRequest,
) -> tuple[list[MatrixRow], dict[CellKey, dict[str, TagAssignment]]]:
    indexed: dict[CellKey, dict[str, TagAssignment]] = defaultdict(dict)
    for assignment in request.assignments:
        indexed[_cell_key(assignment)][_assignment_group_id(assignment)] = assignment

    rows: list[MatrixRow] = []
    for target_id, start_ms, end_ms, label_key in sorted(indexed):
        by_group = indexed[(target_id, start_ms, end_ms, label_key)]
        cells = [
            MatrixCell(
                group=group,
                assignments=[by_group[_group_id(group)]] if _group_id(group) in by_group else [],
                missing=_group_id(group) not in by_group,
            )
            for group in request.groups
        ]
        merged = _merge_cell(
            strategy=request.merge_strategy,
            groups=request.groups,
            by_group=by_group,
        )
        values = {assignment.value for assignment in by_group.values()}
        rows.append(
            MatrixRow(
                target_id=target_id,
                window=TimeWindow(start_ms=start_ms, end_ms=end_ms),
                label_key=label_key,
                store_ids=sorted(
                    {
                        assignment.store_id
                        for assignment in by_group.values()
                        if assignment.store_id is not None
                    }
                ),
                agent_ids=sorted(
                    {
                        assignment.agent_id
                        for assignment in by_group.values()
                        if assignment.agent_id is not None
                    }
                ),
                cells=cells,
                merged=merged,
                conflict=len(values) > 1,
                missing_group_keys=[
                    _group_id(group) for group in request.groups if _group_id(group) not in by_group
                ],
            )
        )
    return rows, indexed


def _bounded_matrix(
    rows: list[MatrixRow],
    *,
    limit: int,
    evidence_budget: _EvidenceOutputBudget,
) -> list[MatrixRow]:
    """Copy only visible rows and summarize evidence under one global budget."""
    result: list[MatrixRow] = []
    for row in rows[:limit]:
        bounded = row.model_copy(deep=True)
        for cell in bounded.cells:
            for assignment in cell.assignments:
                assignment.evidence_refs = evidence_budget.take_many(assignment.evidence_refs)
        bounded.merged.evidence_refs = evidence_budget.take_many(bounded.merged.evidence_refs)
        result.append(bounded)
    return result


def _coverage(
    groups: list[TagGroup],
    indexed: dict[CellKey, dict[str, TagAssignment]],
) -> list[CoverageInsight]:
    total = len(indexed)
    result: list[CoverageInsight] = []
    for group in groups:
        group_id = _group_id(group)
        assigned = sum(group_id in by_group for by_group in indexed.values())
        result.append(
            CoverageInsight(
                group_key=group_id,
                assigned_cells=assigned,
                missing_cells=total - assigned,
                coverage_rate=round(assigned / total, 6) if total else 0.0,
            )
        )
    return result


def _pairwise(
    groups: list[TagGroup],
    indexed: dict[CellKey, dict[str, TagAssignment]],
    *,
    difference_limit: int,
    evidence_budget: _EvidenceOutputBudget,
) -> tuple[list[PairwiseComparison], int, int]:
    result: list[PairwiseComparison] = []
    total_differences = 0
    returned_differences = 0
    for left_group, right_group in combinations(groups, 2):
        comparable = agreements = differences = left_only = right_only = 0
        difference_items: list[PairwiseDifference] = []
        for (target_id, start_ms, end_ms, label_key), by_group in sorted(indexed.items()):
            left = by_group.get(_group_id(left_group))
            right = by_group.get(_group_id(right_group))
            if left is not None and right is not None:
                comparable += 1
                if left.value == right.value:
                    agreements += 1
                else:
                    differences += 1
                    total_differences += 1
                    if returned_differences < difference_limit:
                        left_summary = (
                            evidence_budget.take_summary(left.evidence_refs[0])
                            if left.evidence_refs
                            else None
                        )
                        right_summary = (
                            evidence_budget.take_summary(right.evidence_refs[0])
                            if right.evidence_refs
                            else None
                        )
                        difference_items.append(
                            PairwiseDifference(
                                target_id=target_id,
                                window=TimeWindow(
                                    start_ms=start_ms,
                                    end_ms=end_ms,
                                ),
                                label_key=label_key,
                                left_value=left.value,
                                right_value=right.value,
                                left_evidence_count=len(left.evidence_refs),
                                right_evidence_count=len(right.evidence_refs),
                                left_evidence_refs=(
                                    [left_summary] if left_summary is not None else []
                                ),
                                right_evidence_refs=(
                                    [right_summary] if right_summary is not None else []
                                ),
                            )
                        )
                        returned_differences += 1
            elif left is not None:
                left_only += 1
            elif right is not None:
                right_only += 1

        union_count = comparable + left_only + right_only
        result.append(
            PairwiseComparison(
                left_group_key=_group_id(left_group),
                right_group_key=_group_id(right_group),
                comparable_cells=comparable,
                agreements=agreements,
                differences=differences,
                agreement_rate=round(agreements / comparable, 6) if comparable else None,
                left_only_cells=left_only,
                right_only_cells=right_only,
                overlap_rate=round(comparable / union_count, 6) if union_count else 0.0,
                difference_items=difference_items,
                difference_items_truncated=differences > len(difference_items),
            )
        )
    return result, total_differences, returned_differences


def _iter_merged_assignments(rows: list[MatrixRow]) -> Iterable[tuple[str, str, str]]:
    for row in rows:
        for value in row.merged.values:
            yield row.label_key, value, row.target_id


def _distributions(
    assignments: list[TagAssignment],
    rows: list[MatrixRow],
) -> list[DistributionInsight]:
    counts: Counter[tuple[str, str, str]] = Counter(
        (
            _assignment_group_id(assignment),
            assignment.label_key,
            assignment.value,
        )
        for assignment in assignments
    )
    counts.update(
        (_MERGED_GROUP_KEY, label_key, value)
        for label_key, value, _target_id in _iter_merged_assignments(rows)
    )
    totals: Counter[tuple[str, str]] = Counter()
    for (group_key, label_key, _value), count in counts.items():
        totals[(group_key, label_key)] += count

    return [
        DistributionInsight(
            group_key=group_key,
            label_key=label_key,
            value=value,
            count=count,
            proportion=round(count / totals[(group_key, label_key)], 6),
        )
        for (group_key, label_key, value), count in sorted(counts.items())
    ]


def _trend_bucket(
    occurred_at: datetime | None,
    target_id: str,
    granularity: TrendGranularity,
) -> str:
    if occurred_at is None:
        return f"target:{target_id}"
    if granularity == "month":
        return occurred_at.strftime("%Y-%m")
    if granularity == "week":
        iso_year, iso_week, _weekday = occurred_at.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    return occurred_at.strftime("%Y-%m-%d")


def _trends(
    assignments: list[TagAssignment],
    rows: list[MatrixRow],
    indexed: dict[CellKey, dict[str, TagAssignment]],
    granularity: TrendGranularity,
) -> list[TrendInsight]:
    counts: Counter[tuple[str, str, str, str]] = Counter(
        (
            _trend_bucket(
                assignment.occurred_at,
                assignment.target_id,
                granularity,
            ),
            _assignment_group_id(assignment),
            assignment.label_key,
            assignment.value,
        )
        for assignment in assignments
    )
    for row in rows:
        cell_key = (
            row.target_id,
            row.window.start_ms,
            row.window.end_ms,
            row.label_key,
        )
        selected = [
            indexed[cell_key][group_key]
            for group_key in row.merged.selected_group_keys
            if group_key in indexed[cell_key]
        ]
        occurred_at = next(
            (
                assignment.occurred_at
                for assignment in selected
                if assignment.occurred_at is not None
            ),
            None,
        )
        bucket = _trend_bucket(occurred_at, row.target_id, granularity)
        counts.update(
            (bucket, _MERGED_GROUP_KEY, row.label_key, value) for value in row.merged.values
        )
    return [
        TrendInsight(
            bucket_key=bucket,
            group_key=group_key,
            label_key=label_key,
            value=value,
            count=count,
        )
        for (bucket, group_key, label_key, value), count in sorted(counts.items())
    ]


def _co_occurrences(
    assignments: list[TagAssignment],
    rows: list[MatrixRow],
    top_n: int,
) -> list[CoOccurrenceInsight]:
    tokens_by_window: dict[tuple[str, WindowKey], set[str]] = defaultdict(set)
    for assignment in assignments:
        tokens_by_window[(_assignment_group_id(assignment), _window_key(assignment))].add(
            f"{assignment.label_key}={assignment.value}"
        )
    for row in rows:
        window_key = (row.target_id, row.window.start_ms, row.window.end_ms)
        for value in row.merged.values:
            tokens_by_window[(_MERGED_GROUP_KEY, window_key)].add(f"{row.label_key}={value}")

    counts: Counter[tuple[str, str, str]] = Counter()
    for (group_key, _window), tokens in tokens_by_window.items():
        for left, right in combinations(sorted(tokens), 2):
            counts[(group_key, left, right)] += 1

    ranked = sorted(
        counts.items(),
        key=lambda item: (-item[1], item[0][0], item[0][1], item[0][2]),
    )[:top_n]
    return [
        CoOccurrenceInsight(
            group_key=group_key,
            left_label=left,
            right_label=right,
            count=count,
        )
        for (group_key, left, right), count in ranked
    ]


def _confidence_bucket(value: float | None) -> str:
    if value is None:
        return "unknown"
    index = min(int(value * 5), len(_CONFIDENCE_BUCKETS) - 1)
    return _CONFIDENCE_BUCKETS[index]


def _confidence(assignments: list[TagAssignment]) -> list[ConfidenceInsight]:
    grouped: dict[tuple[str, str], list[float]] = defaultdict(list)
    unknown_counts: Counter[tuple[str, str]] = Counter()
    for assignment in assignments:
        bucket = _confidence_bucket(assignment.confidence)
        key = (_assignment_group_id(assignment), bucket)
        if assignment.confidence is None:
            unknown_counts[key] += 1
        else:
            grouped[key].append(assignment.confidence)

    keys = sorted(set(grouped) | set(unknown_counts))
    return [
        ConfidenceInsight(
            group_key=group_key,
            bucket=bucket,
            count=len(grouped[(group_key, bucket)]) + unknown_counts[(group_key, bucket)],
            average_confidence=(
                round(
                    sum(grouped[(group_key, bucket)]) / len(grouped[(group_key, bucket)]),
                    6,
                )
                if grouped[(group_key, bucket)]
                else None
            ),
        )
        for group_key, bucket in keys
    ]


def _dimension_comparisons(
    assignments: list[TagAssignment],
    conflict_keys: set[CellKey],
    groups: list[TagGroup],
) -> list[DimensionComparison]:
    grouped: dict[
        tuple[DimensionName, str, str],
        list[TagAssignment],
    ] = defaultdict(list)
    dimension_cells: dict[tuple[DimensionName, str], set[CellKey]] = defaultdict(set)
    for assignment in assignments:
        if assignment.store_id is not None:
            grouped[("store", assignment.store_id, _assignment_group_id(assignment))].append(
                assignment
            )
            dimension_cells[("store", assignment.store_id)].add(_cell_key(assignment))
        if assignment.agent_id is not None:
            grouped[("agent", assignment.agent_id, _assignment_group_id(assignment))].append(
                assignment
            )
            dimension_cells[("agent", assignment.agent_id)].add(_cell_key(assignment))

    result: list[DimensionComparison] = []
    dimension_group_keys = [
        (dimension, value, _group_id(group))
        for dimension, value in sorted(dimension_cells)
        for group in groups
    ]
    for dimension, value, group_key in dimension_group_keys:
        items = grouped[(dimension, value, group_key)]
        total_cells = len(dimension_cells[(dimension, value)])
        assignment_count = len(items)
        conflict_assignments = sum(_cell_key(item) in conflict_keys for item in items)
        result.append(
            DimensionComparison(
                dimension=dimension,
                dimension_value=value,
                group_key=group_key,
                total_cells=total_cells,
                assignment_count=assignment_count,
                missing_cells=total_cells - assignment_count,
                coverage_rate=round(assignment_count / total_cells, 6),
                unique_targets=len({item.target_id for item in items}),
                average_confidence=_mean_confidence(items),
                conflict_assignments=conflict_assignments,
                conflict_rate=(
                    round(conflict_assignments / assignment_count, 6) if assignment_count else 0.0
                ),
            )
        )
    return result


def analyze_tag_insights(
    request: AnalyzeTagInsightsRequest,
    *,
    tenant_id: str,
) -> AnalyzeTagInsightsResponse:
    """Analyze bounded tag snapshots without reading or writing persistent state."""
    complete_matrix, indexed = _build_matrix(request)
    conflict_keys = {
        (
            row.target_id,
            row.window.start_ms,
            row.window.end_ms,
            row.label_key,
        )
        for row in complete_matrix
        if row.conflict
    }
    incomplete = sum(bool(row.missing_group_keys) for row in complete_matrix)
    conflicts = len(conflict_keys)
    total = len(complete_matrix)

    evidence_budget = _EvidenceOutputBudget()
    matrix = _bounded_matrix(
        complete_matrix,
        limit=request.matrix_limit,
        evidence_budget=evidence_budget,
    )
    pairwise, difference_total, difference_returned = _pairwise(
        request.groups,
        indexed,
        difference_limit=request.difference_limit,
        evidence_budget=evidence_budget,
    )
    all_distributions = _distributions(request.assignments, complete_matrix)
    distributions = all_distributions[:MAX_DISTRIBUTION_ITEMS]
    all_trends = _trends(
        request.assignments,
        complete_matrix,
        indexed,
        request.trend_granularity,
    )
    trends = all_trends[:MAX_TREND_ITEMS]
    all_dimensions = _dimension_comparisons(
        request.assignments,
        conflict_keys,
        request.groups,
    )
    dimensions = all_dimensions[:MAX_DIMENSION_ITEMS]

    matrix_truncated = total > len(matrix)
    difference_truncated = difference_total > difference_returned
    list_truncated = (
        len(all_distributions) > len(distributions)
        or len(all_trends) > len(trends)
        or len(all_dimensions) > len(dimensions)
    )
    truncated = (
        matrix_truncated or difference_truncated or evidence_budget.truncated or list_truncated
    )

    return AnalyzeTagInsightsResponse(
        tenant_id=tenant_id,
        merge_strategy=request.merge_strategy,
        groups=request.groups,
        truncated=truncated,
        matrix_truncated=matrix_truncated,
        difference_truncated=difference_truncated,
        evidence_truncated=evidence_budget.truncated,
        output_budget=InsightOutputBudget(
            matrix_limit=request.matrix_limit,
            matrix_total_rows=total,
            matrix_returned_rows=len(matrix),
            difference_limit=request.difference_limit,
            difference_total_items=difference_total,
            difference_returned_items=difference_returned,
            distribution_limit=MAX_DISTRIBUTION_ITEMS,
            distribution_total_items=len(all_distributions),
            distribution_returned_items=len(distributions),
            trend_limit=MAX_TREND_ITEMS,
            trend_total_items=len(all_trends),
            trend_returned_items=len(trends),
            dimension_limit=MAX_DIMENSION_ITEMS,
            dimension_total_items=len(all_dimensions),
            dimension_returned_items=len(dimensions),
            evidence_ref_limit=MAX_OUTPUT_EVIDENCE_REFS,
            evidence_ref_count=evidence_budget.ref_count,
            evidence_text_byte_limit=MAX_OUTPUT_EVIDENCE_TEXT_BYTES,
            evidence_text_bytes=evidence_budget.text_bytes,
        ),
        overview=InsightOverview(
            group_count=len(request.groups),
            assignment_count=len(request.assignments),
            total_cells=total,
            complete_cells=total - incomplete,
            incomplete_cells=incomplete,
            conflict_cells=conflicts,
            conflict_rate=round(conflicts / total, 6) if total else 0.0,
        ),
        matrix=matrix,
        coverage=_coverage(request.groups, indexed),
        pairwise=pairwise,
        distributions=distributions,
        trends=trends,
        co_occurrences=_co_occurrences(
            request.assignments,
            complete_matrix,
            request.top_n_co_occurrences,
        ),
        confidence=_confidence(request.assignments),
        dimension_comparisons=dimensions,
    )
