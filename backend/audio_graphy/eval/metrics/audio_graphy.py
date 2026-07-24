"""AudioGraphy-specific metrics — Entity F1, Edge Precision×Confidence, Tag Accuracy.

All three are pure functions with no LLM dependency; normalization handles
common Chinese/case/fullwidth-halfwidth variants.

Formulas (PRD §5.3.2):
- Entity F1: F1 over ``(entity_text, entity_type)`` set after NFKC normalize.
  Supports both ``strict`` (exact set match) and ``fuzzy`` (rapidfuzz WRatio
  ≥ threshold) modes — EvalRunner calls it twice and reports both.
- Edge P/C: per-layer (EXTRACTED / INFERRED / AMBIGUOUS) precision; reported as
  macro-mean of non-empty pred layers.
- Tag Accuracy: (# tags where path+value match) / len(gold_tags).

Edge cases (PRD §5.3.3):
- Entity F1 both empty → 1.0, reason="both_empty".
- Denominator 0 → 0.0 with ``details["denominator_zero"]=True``.
"""

from __future__ import annotations

import unicodedata
from typing import Any

from audio_graphy.eval.types import (
    EdgeConfidence,
    GoldExample,
    MetricResult,
    PredictedResult,
)

_LAYERS: tuple[EdgeConfidence, ...] = ("EXTRACTED", "INFERRED", "AMBIGUOUS")


def _norm(s: str) -> str:
    """Normalize a string: NFKC + strip + lowercase.

    NFKC collapses fullwidth/halfwidth variants (｢ＣＳ７５｣ → ``CS75``) and is
    idempotent. Lowercase + strip handle case/whitespace noise.
    """
    return unicodedata.normalize("NFKC", s).strip().lower()


# ============================================================
# Entity F1 (strict + fuzzy dual mode)
# ============================================================


def entity_f1(
    gold: GoldExample,
    pred: PredictedResult,
    *,
    fuzzy_threshold: float = 1.0,
) -> MetricResult:
    """Entity F1 over ``(normalized_entity_text, entity_type)`` sets.

    Per architecture A.2: strict set equality after NFKC + lowercase, no
    character-level Jaccard. ``both_empty`` returns 1.0 (PRD §5.3.3).

    Args:
        gold: Gold example.
        pred: Predicted result.
        fuzzy_threshold: ``1.0`` (default) → strict exact-match mode.
            ``< 1.0`` → fuzzy mode: a (gold, pred) pair counts as a true
            positive when their entity types match AND
            ``rapidfuzz.fuzz.WRatio(gold_text, pred_text) >= threshold*100``.
            The metric name is suffixed with ``"_fuzzy"`` in fuzzy mode
            so callers can disambiguate in aggregate reports.
    """
    fuzzy_mode = fuzzy_threshold < 1.0
    metric_name = "entity_f1_fuzzy" if fuzzy_mode else "entity_f1"

    gold_set = {(_norm(text), etype) for text, etype in gold.gold_entities}
    pred_set = {(_norm(text), etype) for text, etype in pred.entities}

    if not gold_set and not pred_set:
        return MetricResult(
            name=metric_name,
            value=1.0,
            denominator=1,
            details={
                "reason": "both_empty",
                "gold_count": 0,
                "pred_count": 0,
                "tp": 0,
                "fuzzy_threshold": fuzzy_threshold,
            },
        )

    if fuzzy_mode:
        tp = _fuzzy_tp_count(gold_set, pred_set, fuzzy_threshold)
        matched_gold: set[Any] = set()  # populated by _fuzzy_tp_count via aliasing
        # Re-compute matched sets for precision/recall denominators.
        matched_gold = _fuzzy_matched_gold(gold_set, pred_set, fuzzy_threshold)
        matched_pred_count = tp
    else:
        matched_gold = gold_set & pred_set
        tp = len(matched_gold)
        matched_pred_count = tp

    precision = matched_pred_count / len(pred_set) if pred_set else 0.0
    recall = len(matched_gold) / len(gold_set) if gold_set else 0.0
    value = 0.0 if precision + recall <= 0.0 else 2 * precision * recall / (precision + recall)

    return MetricResult(
        name=metric_name,
        value=value,
        denominator=1,
        details={
            "gold_count": len(gold_set),
            "pred_count": len(pred_set),
            "tp": tp,
            "precision": precision,
            "recall": recall,
            "fuzzy_threshold": fuzzy_threshold,
        },
    )


def _fuzzy_tp_count(
    gold_set: set[tuple[str, str]],
    pred_set: set[tuple[str, str]],
    threshold: float,
) -> int:
    """Count fuzzy true positives: gold entity matched by some pred entity."""
    from rapidfuzz import fuzz

    cutoff = threshold * 100
    count = 0
    for g_text, g_type in gold_set:
        for p_text, p_type in pred_set:
            if g_type != p_type:
                continue
            if fuzz.WRatio(g_text, p_text) >= cutoff:
                count += 1
                break
    return count


def _fuzzy_matched_gold(
    gold_set: set[tuple[str, str]],
    pred_set: set[tuple[str, str]],
    threshold: float,
) -> set[tuple[str, str]]:
    """Return the subset of ``gold_set`` that has a fuzzy match in ``pred_set``."""
    from rapidfuzz import fuzz

    cutoff = threshold * 100
    matched: set[tuple[str, str]] = set()
    for g_text, g_type in gold_set:
        for p_text, p_type in pred_set:
            if g_type != p_type:
                continue
            if fuzz.WRatio(g_text, p_text) >= cutoff:
                matched.add((g_text, g_type))
                break
    return matched


# ============================================================
# Edge Precision × Confidence (per-layer + macro)
# ============================================================


def edge_precision_by_confidence(gold: GoldExample, pred: PredictedResult) -> MetricResult:
    """Per-layer precision + macro mean.

    Each edge key: ``(norm(src), rel, norm(dst), confidence)``.
    For each layer L:
        tp_L   = |gold_L ∩ pred_L|
        prec_L = tp_L / len(pred_L) if pred_L non-empty else 0.0 (excluded from macro)
    macro = mean(prec_L for L in layers where pred_L non-empty)
    """

    def _key(e: tuple[str, str, str, EdgeConfidence]) -> tuple[str, str, str, EdgeConfidence]:
        src, rel, dst, conf = e
        return (_norm(src), rel, _norm(dst), conf)

    gold_keys = {_key(e) for e in gold.gold_edges}
    pred_keys = {_key(e) for e in pred.edges}

    per_layer: dict[str, float] = {}
    included_flags: dict[str, bool] = {}
    denom_zero_flags: dict[str, bool] = {}
    included_precisions: list[float] = []

    for layer in _LAYERS:
        gold_layer = {k for k in gold_keys if k[3] == layer}
        pred_layer = {k for k in pred_keys if k[3] == layer}
        if not pred_layer:
            per_layer[f"P_{layer}"] = 0.0
            included_flags[f"P_{layer}_included"] = False
            if gold_layer:
                # Empty pred on a non-empty gold layer — denominator_zero flag
                # for this layer (helps triage).
                denom_zero_flags[f"P_{layer}_denominator_zero"] = True
            continue
        tp = len(gold_layer & pred_layer)
        prec = tp / len(pred_layer)
        per_layer[f"P_{layer}"] = prec
        included_flags[f"P_{layer}_included"] = True
        included_precisions.append(prec)

    macro = sum(included_precisions) / len(included_precisions) if included_precisions else 0.0

    details: dict[str, float | int | str] = {
        **{k: float(v) for k, v in per_layer.items()},
        **included_flags,
        **denom_zero_flags,
        "macro": macro,
    }
    if not included_precisions:
        details["all_layers_empty"] = True
        details["denominator_zero"] = True

    return MetricResult(
        name="edge_precision_by_confidence",
        value=macro,
        denominator=len(included_precisions),
        details=details,
    )


# ============================================================
# Tag Accuracy
# ============================================================


def tag_accuracy(gold: GoldExample, pred: PredictedResult) -> MetricResult:
    """Tag accuracy = (# path+value matches) / len(gold_tags).

    Value match uses ``_norm`` (NFKC + lowercase + trim) so ``"A"`` matches
    ``"ａ"`` and ``" 5万 "`` matches ``"5万"``.
    """
    if not gold.gold_tags:
        return MetricResult(
            name="tag_accuracy",
            value=0.0,
            denominator=0,
            details={"gold_count": 0, "hits": 0, "denominator_zero": True},
        )

    pred_index: dict[str, str] = {
        _norm(t.get("tag_path", "")): _norm(t.get("value", "")) for t in pred.tags
    }

    hits = 0
    for g in gold.gold_tags:
        gpath = _norm(g.get("tag_path", ""))
        gval = _norm(g.get("value", ""))
        if pred_index.get(gpath) == gval:
            hits += 1

    denom = len(gold.gold_tags)
    value = hits / denom

    return MetricResult(
        name="tag_accuracy",
        value=value,
        denominator=denom,
        details={"gold_count": denom, "hits": hits},
    )


__all__ = ["edge_precision_by_confidence", "entity_f1", "tag_accuracy"]
