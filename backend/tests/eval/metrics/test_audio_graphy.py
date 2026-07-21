"""Unit tests for AudioGraphy-specific metrics — 12 cases.

Cases:
- entity_f1: perfect / partial / both_empty / normalization
- edge_precision: all_layers_perfect / partial_layer / empty_layer / all_layers_empty
- tag_accuracy: perfect / normalization / empty_gold / path_missing
"""

from __future__ import annotations

import pytest

from audio_graphy.eval.metrics.audio_graphy import (
    edge_precision_by_confidence,
    entity_f1,
    tag_accuracy,
)
from audio_graphy.eval.types import GoldExample, PredictedResult


def _gold(
    entities: tuple[tuple[str, str], ...] = (),
    edges: tuple[tuple[str, str, str, str], ...] = (),
    tags: tuple[dict[str, str], ...] = (),
) -> GoldExample:
    return GoldExample(
        query="q",
        gold_answer="a",
        gold_context_ids=(),
        gold_entities=entities,
        gold_edges=edges,  # type: ignore[arg-type]
        gold_tags=tags,
    )


def _pred(
    entities: tuple[tuple[str, str], ...] = (),
    edges: tuple[tuple[str, str, str, str], ...] = (),
    tags: tuple[dict[str, str], ...] = (),
) -> PredictedResult:
    return PredictedResult(
        query="q",
        answer="a",
        retrieved_context_ids=(),
        entities=entities,
        edges=edges,  # type: ignore[arg-type]
        tags=tags,
    )


# -------- entity_f1 --------


def test_entity_f1_perfect() -> None:
    ents = (("CS75 Plus", "车型"), ("5万", "价格"))
    gold = _gold(entities=ents)
    pred = _pred(entities=ents)
    m = entity_f1(gold, pred)
    assert m.value == pytest.approx(1.0)
    assert m.details["tp"] == 2


def test_entity_f1_partial() -> None:
    """1 of 2 overlap → precision=recall=0.5 → F1=0.5."""
    gold = _gold(entities=(("CS75 Plus", "车型"), ("5万", "价格")))
    pred = _pred(entities=(("CS75 Plus", "车型"), ("UNI-V", "车型")))
    m = entity_f1(gold, pred)
    assert m.value == pytest.approx(0.5)


def test_entity_f1_both_empty() -> None:
    """Both entity sets empty → reason=both_empty, value 1.0."""
    gold = _gold()
    pred = _pred()
    m = entity_f1(gold, pred)
    assert m.value == pytest.approx(1.0)
    assert m.details["reason"] == "both_empty"


def test_entity_f1_normalization() -> None:
    """Fullwidth + case + whitespace variations normalize to same entity."""
    gold = _gold(entities=(("CS75 Plus", "车型"),))
    pred = _pred(entities=(("ｃｓ７５　ＰＬＵＳ", "车型"),))  # fullwidth + uppercase
    m = entity_f1(gold, pred)
    assert m.value == pytest.approx(1.0)


# -------- edge_precision_by_confidence --------


def test_edge_precision_all_layers_perfect() -> None:
    """3 layers each perfect → macro=1.0."""
    edges = (
        ("a", "r1", "b", "EXTRACTED"),
        ("c", "r2", "d", "INFERRED"),
        ("e", "r3", "f", "AMBIGUOUS"),
    )
    gold = _gold(edges=edges)
    pred = _pred(edges=edges)
    m = edge_precision_by_confidence(gold, pred)
    assert m.value == pytest.approx(1.0)
    assert m.details["P_EXTRACTED"] == pytest.approx(1.0)
    assert m.details["P_INFERRED"] == pytest.approx(1.0)
    assert m.details["P_AMBIGUOUS"] == pytest.approx(1.0)


def test_edge_precision_partial_layer() -> None:
    """EXTRACTED 1/2, INFERRED 1/1, AMBIGUOUS empty → macro=(0.5+1.0)/2."""
    gold = _gold(
        edges=(
            ("a", "r", "b", "EXTRACTED"),
            ("c", "r", "d", "INFERRED"),
        )
    )
    pred = _pred(
        edges=(
            ("a", "r", "b", "EXTRACTED"),       # tp
            ("x", "r", "y", "EXTRACTED"),        # fp
            ("c", "r", "d", "INFERRED"),         # tp
        )
    )
    m = edge_precision_by_confidence(gold, pred)
    assert m.value == pytest.approx(0.75)  # (0.5 + 1.0) / 2
    assert m.details["P_EXTRACTED"] == pytest.approx(0.5)
    assert m.details["P_INFERRED"] == pytest.approx(1.0)
    assert m.details["P_AMBIGUOUS"] == pytest.approx(0.0)


def test_edge_precision_empty_layer() -> None:
    """AMBIGUOUS layer empty in pred → not in macro; EXTRACTED perfect."""
    gold = _gold(
        edges=(
            ("a", "r", "b", "EXTRACTED"),
            ("e", "r", "f", "AMBIGUOUS"),
        )
    )
    pred = _pred(edges=(("a", "r", "b", "EXTRACTED"),))
    m = edge_precision_by_confidence(gold, pred)
    # Only EXTRACTED included → macro = 1.0
    assert m.value == pytest.approx(1.0)
    # AMBIGUOUS layer pred empty + gold non-empty → denominator_zero flag
    assert m.details.get("P_AMBIGUOUS_denominator_zero") is True
    assert m.details.get("P_AMBIGUOUS_included") is False


def test_edge_precision_all_layers_empty() -> None:
    """All layers pred empty → macro=0.0, all_layers_empty=True."""
    gold = _gold(
        edges=(
            ("a", "r", "b", "EXTRACTED"),
            ("c", "r", "d", "INFERRED"),
        )
    )
    pred = _pred(edges=())
    m = edge_precision_by_confidence(gold, pred)
    assert m.value == pytest.approx(0.0)
    assert m.details.get("all_layers_empty") is True
    assert m.details.get("denominator_zero") is True


# -------- tag_accuracy --------


def test_tag_accuracy_perfect() -> None:
    tags = (
        {"tag_path": "接待.价格.优惠", "value": "5万"},
        {"tag_path": "接待.金融.免息", "value": "2年"},
    )
    gold = _gold(tags=tags)
    pred = _pred(tags=tags)
    m = tag_accuracy(gold, pred)
    assert m.value == pytest.approx(1.0)
    assert m.details["hits"] == 2


def test_tag_accuracy_normalization() -> None:
    """Value with whitespace + case variant matches."""
    gold = _gold(tags=({"tag_path": "接待.价格", "value": "5万"},))
    pred = _pred(tags=({"tag_path": "接待.价格", "value": " 5万 "},))
    m = tag_accuracy(gold, pred)
    assert m.value == pytest.approx(1.0)


def test_tag_accuracy_empty_gold() -> None:
    """Empty gold_tags → denominator_zero, value 0.0."""
    gold = _gold(tags=())
    pred = _pred(tags=({"tag_path": "x", "value": "y"},))
    m = tag_accuracy(gold, pred)
    assert m.value == 0.0
    assert m.details["denominator_zero"] is True


def test_tag_accuracy_path_missing_in_pred() -> None:
    """A gold tag_path absent from pred does not count as hit."""
    gold = _gold(
        tags=(
            {"tag_path": "接待.价格.优惠", "value": "5万"},
            {"tag_path": "接待.金融.免息", "value": "2年"},
        )
    )
    pred = _pred(
        tags=(
            {"tag_path": "接待.价格.优惠", "value": "5万"},
            # 接待.金融.免息 missing entirely
        )
    )
    m = tag_accuracy(gold, pred)
    assert m.value == pytest.approx(0.5)
