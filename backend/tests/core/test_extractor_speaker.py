"""Unit tests for ``build_speaker_entities_from_segments`` (M7 WS-2)."""

from __future__ import annotations

from audio_graphy.core.extractor import build_speaker_entities_from_segments


class TestEmpty:
    def test_empty_input_returns_empty(self) -> None:
        assert build_speaker_entities_from_segments([]) == []

    def test_all_none_speakers_returns_empty(self) -> None:
        out = build_speaker_entities_from_segments([(0, None, 1), (1, None, 1), (2, None, 1)])
        assert out == []


class TestBasicEmission:
    def test_single_speaker(self) -> None:
        out = build_speaker_entities_from_segments([(0, "spk_0", 1)])
        assert len(out) == 1
        name, type_, desc = out[0]
        assert name == "spk_0"
        assert type_ == "说话人"
        assert "speaker label=spk_0" in desc

    def test_two_distinct_speakers(self) -> None:
        out = build_speaker_entities_from_segments(
            [(0, "spk_0", 1), (1, "spk_1", 1), (2, "spk_0", 1)]
        )
        # Distinct count = 2 (spk_0 deduped).
        assert len(out) == 2
        names = {n for n, _, _ in out}
        assert names == {"spk_0", "spk_1"}

    def test_dedup_preserves_first_seen_order(self) -> None:
        out = build_speaker_entities_from_segments(
            [(0, "spk_2", 1), (1, "spk_0", 1), (2, "spk_1", 1)]
        )
        assert [n for n, _, _ in out] == ["spk_2", "spk_0", "spk_1"]


class TestAmbiguityTag:
    def test_no_ambiguity_map_emits_plain_description(self) -> None:
        out = build_speaker_entities_from_segments([(0, "spk_0", 1)])
        assert "AMBIGUOUS" not in out[0][2]
        assert "PENDING_REVIEW" not in out[0][2]

    def test_ambiguous_speaker_has_tag_in_description(self) -> None:
        out = build_speaker_entities_from_segments(
            [(0, "spk_0", 1), (1, "spk_1", 1)],
            ambiguity_map={"spk_0": "AMBIGUOUS"},
        )
        descriptions = {n: d for n, _, d in out}
        assert "AMBIGUOUS" in descriptions["spk_0"]
        assert "AMBIGUOUS" not in descriptions["spk_1"]

    def test_pending_review_speaker_has_tag_in_description(self) -> None:
        out = build_speaker_entities_from_segments(
            [(0, "spk_0", 1)],
            ambiguity_map={"spk_0": "PENDING_REVIEW"},
        )
        assert "PENDING_REVIEW" in out[0][2]

    def test_ambiguity_map_none_value_treated_as_no_tag(self) -> None:
        """If map explicitly has speaker → None, no tag is appended."""
        out = build_speaker_entities_from_segments(
            [(0, "spk_0", 1)],
            ambiguity_map={"spk_0": None},
        )
        assert "AMBIGUOUS" not in out[0][2]
        assert "PENDING_REVIEW" not in out[0][2]


class TestEntityTypeDef:
    def test_type_is_locked_to_speaker(self) -> None:
        out = build_speaker_entities_from_segments([(0, "spk_0", 1)])
        assert out[0][1] == "说话人"

    def test_type_remains_locked_even_with_ambiguity(self) -> None:
        out = build_speaker_entities_from_segments(
            [(0, "spk_0", 1)],
            ambiguity_map={"spk_0": "AMBIGUOUS"},
        )
        assert out[0][1] == "说话人"
