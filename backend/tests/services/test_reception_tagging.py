"""Unit tests for evidence-bound reception dialogue tag derivation."""

from __future__ import annotations

from audio_graphy.services.reception_tagging import (
    ReceptionRuleTagger,
    SegmentEvidence,
    UnitEvidence,
)


def _segment(
    segment_id: int,
    text: str,
    *,
    start_sec: float = 0.0,
) -> SegmentEvidence:
    return SegmentEvidence(
        segment_id=segment_id,
        recording_id=10,
        source_start_sec=start_sec,
        source_end_sec=start_sec + 5.0,
        text=text,
    )


def test_automotive_rules_emit_only_requested_evidence_backed_labels() -> None:
    unit = UnitEvidence(
        unit_id=7,
        unit_index=0,
        start_sec=0.0,
        end_sec=10.0,
        business_stage="需求了解",
        segments=(
            _segment(101, "客户说价格太高，想先试驾再决定。"),
            _segment(102, "销售要求把定金转到个人账户。", start_sec=5.0),
        ),
    )

    result = ReceptionRuleTagger().derive(
        scenario="automotive",
        unit=unit,
        target_labels=frozenset({"objection", "next_step", "compliance_risk"}),
    )

    assert [(item.label_key, item.label_value) for item in result] == [
        ("objection", "price"),
        ("next_step", "test_drive"),
        ("compliance_risk", "off_book_payment"),
    ]
    assert {ref.segment_id for item in result for ref in item.evidence} == {101, 102}
    assert all(item.confidence > 0 for item in result)
    assert all(item.evidence for item in result)


def test_gold_rule_does_not_create_a_tag_without_matching_segment_evidence() -> None:
    unit = UnitEvidence(
        unit_id=8,
        unit_index=1,
        start_sec=0.0,
        end_sec=5.0,
        business_stage=None,
        segments=(_segment(201, "今天天气不错。"),),
    )

    result = ReceptionRuleTagger().derive(
        scenario="gold",
        unit=unit,
        target_labels=frozenset({"stage", "intent", "objection", "next_step", "compliance_risk"}),
    )

    assert result == []


def test_stage_uses_persisted_unit_state_and_real_segment_references() -> None:
    unit = UnitEvidence(
        unit_id=9,
        unit_index=2,
        start_sec=10.0,
        end_sec=15.0,
        business_stage="产品介绍",
        segments=(_segment(301, "", start_sec=10.0),),
    )

    result = ReceptionRuleTagger().derive(
        scenario="gold",
        unit=unit,
        target_labels=frozenset({"stage"}),
    )

    assert len(result) == 1
    assert result[0].label_key == "stage"
    assert result[0].label_value == "产品介绍"
    assert result[0].evidence[0].segment_id == 301


def test_evidence_reference_exposes_source_and_timeline_coordinates() -> None:
    evidence = SegmentEvidence(
        segment_id=401,
        recording_id=40,
        source_start_sec=5.0,
        source_end_sec=8.0,
        timeline_start_sec=35.0,
        timeline_end_sec=38.0,
        text="安排试驾",
    )

    assert evidence.to_reference() == {
        "ref_id": "segment:401",
        "kind": "audio",
        "segment_id": 401,
        "recording_id": 40,
        "coordinate_space": "reception_timeline",
        "source_start_sec": 5.0,
        "source_end_sec": 8.0,
        "timeline_start_sec": 35.0,
        "timeline_end_sec": 38.0,
        "start_ms": 35_000,
        "end_ms": 38_000,
    }

    source_only = _segment(402, "先看看").to_reference()
    assert source_only["coordinate_space"] == "source"
    assert "timeline_start_sec" not in source_only
