"""Tests for multi-group dialogue-tag merge, comparison, and insights."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from audio_graphy.analytics.tag_insights import analyze_tag_insights
from audio_graphy.schemas.tag_insights import (
    MAX_ASSIGNMENTS,
    MAX_DIFFERENCE_ITEMS,
    MAX_DIMENSION_ITEMS,
    MAX_DISTRIBUTION_ITEMS,
    MAX_GROUPS,
    MAX_LABELS_PER_GROUP_WINDOW,
    MAX_MATRIX_ROWS,
    MAX_OUTPUT_EVIDENCE_REFS,
    MAX_OUTPUT_EVIDENCE_TEXT_BYTES,
    MAX_TREND_ITEMS,
    AnalyzeTagInsightsRequest,
    EvidenceRef,
    TagAssignment,
    TagGroup,
    TimeWindow,
)


def _groups() -> list[TagGroup]:
    return [
        TagGroup(group_key="rules", version="v1", source="rule", priority=10),
        TagGroup(group_key="model", version="v2", source="llm", priority=20),
        TagGroup(group_key="review", version="v3", source="manual", priority=5),
    ]


def _evidence(ref_id: str, kind: str = "audio") -> EvidenceRef:
    return EvidenceRef(
        ref_id=ref_id,
        kind=kind,
        recording_id="rec-1",
        start_ms=0,
        end_ms=1_000,
        text_excerpt="对应原文" if kind == "text" else None,
    )


def _assignments() -> list[TagAssignment]:
    occurred_at = datetime(2026, 7, 23, 10, 30, tzinfo=UTC)
    window = TimeWindow(start_ms=0, end_ms=1_000)
    return [
        TagAssignment(
            group_key="rules",
            target_id="reception-1",
            window=window,
            label_key="stage.greeting",
            value="pass",
            confidence=0.70,
            evidence_refs=[_evidence("audio-rules")],
            store_id="store-a",
            agent_id="agent-1",
            occurred_at=occurred_at,
        ),
        TagAssignment(
            group_key="model",
            target_id="reception-1",
            window=window,
            label_key="stage.greeting",
            value="fail",
            confidence=0.85,
            evidence_refs=[_evidence("audio-model")],
            store_id="store-a",
            agent_id="agent-1",
            occurred_at=occurred_at,
        ),
        TagAssignment(
            group_key="review",
            target_id="reception-1",
            window=window,
            label_key="stage.greeting",
            value="pass",
            confidence=1.0,
            evidence_refs=[_evidence("text-review", "text")],
            is_manual=True,
            store_id="store-a",
            agent_id="agent-1",
            occurred_at=occurred_at,
        ),
        TagAssignment(
            group_key="rules",
            target_id="reception-1",
            window=window,
            label_key="need.budget",
            value="identified",
            confidence=0.75,
            evidence_refs=[_evidence("budget-rules")],
            store_id="store-a",
            agent_id="agent-1",
            occurred_at=occurred_at,
        ),
        TagAssignment(
            group_key="model",
            target_id="reception-1",
            window=window,
            label_key="need.budget",
            value="identified",
            confidence=0.90,
            evidence_refs=[_evidence("budget-model")],
            store_id="store-a",
            agent_id="agent-1",
            occurred_at=occurred_at,
        ),
    ]


def _request(strategy: str = "manual_wins") -> AnalyzeTagInsightsRequest:
    return AnalyzeTagInsightsRequest(
        tenant_id="tenant-a",
        merge_strategy=strategy,
        groups=_groups(),
        assignments=_assignments(),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("strategy", "expected"),
    [
        ("union", ["fail", "pass"]),
        ("intersection", []),
        ("priority", ["fail"]),
        ("manual_wins", ["pass"]),
    ],
)
def test_merge_strategies_are_deterministic(strategy: str, expected: list[str]) -> None:
    response = analyze_tag_insights(_request(strategy), tenant_id="tenant-a")

    greeting = next(row for row in response.matrix if row.label_key == "stage.greeting")
    assert greeting.merged.values == expected
    assert greeting.conflict is True
    assert [cell.group.version for cell in greeting.cells] == ["v1", "v2", "v3"]


@pytest.mark.unit
def test_manual_override_preserves_selected_and_source_evidence() -> None:
    response = analyze_tag_insights(_request(), tenant_id="tenant-a")

    greeting = next(row for row in response.matrix if row.label_key == "stage.greeting")
    assert greeting.merged.selected_group_keys == ["review"]
    assert [item.ref_id for item in greeting.merged.evidence_refs] == ["text-review"]
    assert {
        item.ref_id
        for cell in greeting.cells
        for assignment in cell.assignments
        for item in assignment.evidence_refs
    } == {"audio-rules", "audio-model", "text-review"}


@pytest.mark.unit
def test_pairwise_coverage_missing_conflict_and_insights() -> None:
    response = analyze_tag_insights(_request(), tenant_id="tenant-a")

    assert response.overview.total_cells == 2
    assert response.overview.conflict_cells == 1
    assert response.overview.incomplete_cells == 1

    review = next(item for item in response.coverage if item.group_key == "review")
    assert review.assigned_cells == 1
    assert review.missing_cells == 1
    assert review.coverage_rate == 0.5

    rules_model = next(
        item
        for item in response.pairwise
        if {item.left_group_key, item.right_group_key} == {"rules", "model"}
    )
    assert rules_model.comparable_cells == 2
    assert rules_model.agreements == 1
    assert rules_model.differences == 1
    assert rules_model.agreement_rate == 0.5
    assert rules_model.difference_items[0].label_key == "stage.greeting"
    assert rules_model.difference_items[0].left_evidence_count == 1
    assert rules_model.difference_items[0].right_evidence_count == 1
    assert rules_model.difference_items[0].left_evidence_refs
    assert rules_model.difference_items[0].right_evidence_refs
    assert rules_model.difference_items_truncated is False

    assert any(
        item.group_key == "model" and item.label_key == "stage.greeting" and item.value == "fail"
        for item in response.distributions
    )
    assert any(item.bucket_key == "2026-07-23" for item in response.trends)
    assert any(
        item.group_key == "__merged__" and item.bucket_key == "2026-07-23"
        for item in response.trends
    )
    assert any(
        item.left_label == "need.budget=identified" and item.right_label == "stage.greeting=pass"
        for item in response.co_occurrences
    )
    assert any(
        item.group_key == "model" and item.bucket == "0.8-1.0" for item in response.confidence
    )
    store_rules = next(
        item
        for item in response.dimension_comparisons
        if item.dimension == "store"
        and item.dimension_value == "store-a"
        and item.group_key == "rules"
    )
    assert store_rules.total_cells == 2
    assert store_rules.missing_cells == 0
    assert store_rules.coverage_rate == 1.0
    assert store_rules.conflict_rate == 0.5
    assert any(
        item.dimension == "agent"
        and item.dimension_value == "agent-1"
        and item.group_key == "model"
        for item in response.dimension_comparisons
    )


@pytest.mark.unit
def test_dimension_comparison_includes_declared_group_with_zero_coverage() -> None:
    request = _request()
    request.groups.append(TagGroup(group_key="shadow", version="v1", source="llm"))

    response = analyze_tag_insights(request, tenant_id="tenant-a")

    store_shadow = next(
        item
        for item in response.dimension_comparisons
        if item.dimension == "store"
        and item.dimension_value == "store-a"
        and item.group_key == "shadow"
    )
    assert store_shadow.total_cells == 2
    assert store_shadow.assignment_count == 0
    assert store_shadow.missing_cells == 2
    assert store_shadow.coverage_rate == 0.0
    assert store_shadow.average_confidence is None


@pytest.mark.unit
def test_same_group_versions_are_distinct_comparison_columns() -> None:
    window = TimeWindow(start_ms=0, end_ms=1_000)
    request = AnalyzeTagInsightsRequest(
        tenant_id="tenant-a",
        groups=[
            TagGroup(group_key="stage", version="v1", source="llm"),
            TagGroup(group_key="stage", version="v2", source="llm"),
        ],
        assignments=[
            TagAssignment(
                group_key="stage",
                group_version="v1",
                target_id="reception-1",
                window=window,
                label_key="greeting",
                value="fail",
            ),
            TagAssignment(
                group_key="stage",
                group_version="v2",
                target_id="reception-1",
                window=window,
                label_key="greeting",
                value="pass",
            ),
        ],
    )

    response = analyze_tag_insights(request, tenant_id="tenant-a")

    assert [group.group_id for group in response.groups] == [
        "stage@v1",
        "stage@v2",
    ]
    assert [cell.group.group_id for cell in response.matrix[0].cells] == [
        "stage@v1",
        "stage@v2",
    ]
    assert response.matrix[0].conflict is True
    assert {
        response.pairwise[0].left_group_key,
        response.pairwise[0].right_group_key,
    } == {"stage@v1", "stage@v2"}


@pytest.mark.unit
def test_same_group_version_assignment_must_be_unambiguous() -> None:
    with pytest.raises(ValidationError, match="ambiguous"):
        AnalyzeTagInsightsRequest(
            tenant_id="tenant-a",
            groups=[
                TagGroup(group_key="stage", version="v1", source="llm"),
                TagGroup(group_key="stage", version="v2", source="llm"),
            ],
            assignments=[
                TagAssignment(
                    group_key="stage",
                    target_id="reception-1",
                    window=TimeWindow(start_ms=0, end_ms=1_000),
                    label_key="greeting",
                    value="pass",
                )
            ],
        )


@pytest.mark.unit
def test_intersection_keeps_unanimous_value_and_all_evidence() -> None:
    request = _request("intersection")
    request.assignments.append(
        TagAssignment(
            group_key="review",
            target_id="reception-1",
            window=TimeWindow(start_ms=0, end_ms=1_000),
            label_key="need.budget",
            value="identified",
            confidence=1.0,
            evidence_refs=[_evidence("budget-review")],
            is_manual=True,
        )
    )

    response = analyze_tag_insights(request, tenant_id="tenant-a")
    budget = next(row for row in response.matrix if row.label_key == "need.budget")
    assert budget.merged.values == ["identified"]
    assert {item.ref_id for item in budget.merged.evidence_refs} == {
        "budget-rules",
        "budget-model",
        "budget-review",
    }


@pytest.mark.unit
def test_empty_input_and_limits_are_rejected() -> None:
    with pytest.raises(ValidationError):
        AnalyzeTagInsightsRequest(
            tenant_id="tenant-a",
            groups=[],
            assignments=[],
        )

    with pytest.raises(ValidationError):
        AnalyzeTagInsightsRequest(
            tenant_id="tenant-a",
            groups=[
                TagGroup(group_key=f"group-{index}", version="v1", source="llm")
                for index in range(MAX_GROUPS + 1)
            ],
            assignments=[_assignments()[0]],
        )

    group = TagGroup(group_key="rules", version="v1", source="rule")
    assignment = _assignments()[0]
    with pytest.raises(ValidationError):
        AnalyzeTagInsightsRequest(
            tenant_id="tenant-a",
            groups=[group],
            assignments=[assignment.model_copy() for _ in range(MAX_ASSIGNMENTS + 1)],
        )

    with pytest.raises(ValidationError):
        AnalyzeTagInsightsRequest(
            tenant_id="tenant-a",
            groups=[group],
            assignments=[assignment],
            matrix_limit=MAX_MATRIX_ROWS + 1,
        )

    with pytest.raises(ValidationError):
        AnalyzeTagInsightsRequest(
            tenant_id="tenant-a",
            groups=[group],
            assignments=[assignment],
            difference_limit=MAX_DIFFERENCE_ITEMS + 1,
        )


@pytest.mark.unit
def test_maximum_legal_analysis_response_is_hard_bounded() -> None:
    groups = [
        TagGroup(
            group_key=f"group-{group_index}",
            version="v1",
            source="llm",
            priority=group_index,
        )
        for group_index in range(MAX_GROUPS)
    ]
    assignments: list[TagAssignment] = []
    cells_per_group = MAX_ASSIGNMENTS // MAX_GROUPS
    for group_index, group in enumerate(groups):
        for cell_index in range(cells_per_group):
            assignments.append(
                TagAssignment(
                    group_key=group.group_key,
                    target_id=f"reception-{cell_index}",
                    window=TimeWindow(start_ms=0, end_ms=1_000),
                    label_key=f"label-{cell_index}",
                    value=f"group-{group_index}-{'值' * 490}",
                    confidence=0.9,
                    evidence_refs=[
                        EvidenceRef(
                            ref_id=f"evidence-{group_index}-{cell_index}",
                            kind="text",
                            recording_id=f"recording-{cell_index}",
                            start_ms=0,
                            end_ms=1_000,
                            text_excerpt="证" * 500,
                        )
                    ],
                    store_id=f"store-{cell_index}",
                    agent_id=f"agent-{cell_index}",
                )
            )
    assert len(assignments) == MAX_ASSIGNMENTS

    response = analyze_tag_insights(
        AnalyzeTagInsightsRequest(
            tenant_id="tenant-a",
            groups=groups,
            assignments=assignments,
            matrix_limit=MAX_MATRIX_ROWS,
            difference_limit=MAX_DIFFERENCE_ITEMS,
        ),
        tenant_id="tenant-a",
    )
    payload = response.model_dump(mode="json")
    serialized = response.model_dump_json().encode("utf-8")
    serialized_evidence: list[dict[str, object]] = []

    def collect_evidence(value: object) -> None:
        if isinstance(value, dict):
            if {"ref_id", "kind", "recording_id"} <= value.keys():
                serialized_evidence.append(value)
            for nested in value.values():
                collect_evidence(nested)
        elif isinstance(value, list):
            for nested in value:
                collect_evidence(nested)

    collect_evidence(payload)
    evidence_text_bytes = sum(
        len(text.encode("utf-8"))
        for item in serialized_evidence
        if isinstance((text := item.get("text_excerpt")), str)
    )

    assert response.overview.assignment_count == MAX_ASSIGNMENTS
    assert response.overview.total_cells == cells_per_group
    assert len(response.matrix) == MAX_MATRIX_ROWS
    assert sum(len(item.difference_items) for item in response.pairwise) <= (MAX_DIFFERENCE_ITEMS)
    assert len(response.distributions) <= MAX_DISTRIBUTION_ITEMS
    assert len(response.trends) <= MAX_TREND_ITEMS
    assert len(response.dimension_comparisons) <= MAX_DIMENSION_ITEMS
    assert response.truncated is True
    assert response.matrix_truncated is True
    assert response.difference_truncated is True
    assert response.output_budget.evidence_ref_count <= MAX_OUTPUT_EVIDENCE_REFS
    assert len(serialized_evidence) == response.output_budget.evidence_ref_count
    assert response.output_budget.evidence_text_bytes <= MAX_OUTPUT_EVIDENCE_TEXT_BYTES
    assert evidence_text_bytes == response.output_budget.evidence_text_bytes
    assert len(serialized) < 5 * 1024 * 1024


@pytest.mark.unit
def test_undeclared_group_and_duplicate_cell_are_rejected() -> None:
    assignment = _assignments()[0]
    with pytest.raises(ValidationError, match="undeclared"):
        AnalyzeTagInsightsRequest(
            tenant_id="tenant-a",
            groups=[TagGroup(group_key="other", version="v1", source="llm")],
            assignments=[assignment],
        )

    with pytest.raises(ValidationError, match="duplicate"):
        AnalyzeTagInsightsRequest(
            tenant_id="tenant-a",
            groups=[_groups()[0]],
            assignments=[assignment, assignment.model_copy()],
        )


@pytest.mark.unit
def test_per_window_complexity_is_bounded() -> None:
    assignment = _assignments()[0]
    assignments = [
        assignment.model_copy(update={"label_key": f"label-{index}"})
        for index in range(MAX_LABELS_PER_GROUP_WINDOW + 1)
    ]

    with pytest.raises(ValidationError, match="too many labels"):
        AnalyzeTagInsightsRequest(
            tenant_id="tenant-a",
            groups=[_groups()[0]],
            assignments=assignments,
        )
