"""Reporter — JSON + Markdown writers for EvalRun.

报告器：把 ``EvalRun`` 写成 JSON（dataclass asdict）+ Markdown（PRD §5.5 模板）。

JSON format:
    ``json.dumps(asdict(eval_run), ensure_ascii=False, indent=2)`` — fully
    round-trippable; tuples become lists, dicts preserved.

Markdown sections (PRD §5.5):
1. Header (Run ID / Started / Finished / Examples / Errors / Config)
2. MockPipeline warning banner (if detected in config["pipeline"])
3. Aggregate Metrics table
4. Per-Example Highlights (top 5 worst faithfulness OR worst context_precision
   if no judge was attached)
5. Errors section (if any)
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from audio_graphy.eval.types import EvalExampleResult, EvalRun, MetricResult


def to_json(eval_run: EvalRun, path: Path) -> None:
    """Write ``eval_run`` as a JSON file at ``path`` (parents auto-created)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dataclasses.asdict(eval_run)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def to_markdown(eval_run: EvalRun, path: Path) -> None:
    """Write ``eval_run`` as a Markdown report at ``path`` (PRD §5.5 template)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_markdown(eval_run), encoding="utf-8")


# ============================================================
# Internals
# ============================================================

_WORST_HIGHLIGHT_COUNT = 5


def _render_markdown(run: EvalRun) -> str:
    lines: list[str] = []
    errors = [ex for ex in run.per_example if ex.error is not None]
    ok_examples = [ex for ex in run.per_example if ex.error is None]

    # --- Header ---
    lines.append(f"# Eval Report — Run `{run.run_id}`")
    lines.append("")
    lines.append(f"- **Gold set**: `{run.gold_set_path}`")
    lines.append(f"- **Started**: {run.started_at}")
    lines.append(f"- **Finished**: {run.finished_at}")
    lines.append(
        f"- **Examples**: {len(run.per_example)} total / {len(ok_examples)} ok / {len(errors)} errors"
    )
    if run.config:
        lines.append("- **Config**:")
        for key in sorted(run.config):
            lines.append(f"  - `{key}`: {run.config[key]}")
    lines.append("")

    # --- MockPipeline warning banner ---
    pipeline_str = run.config.get("pipeline", "")
    if "MockPipeline" in pipeline_str:
        lines.append(
            "> ⚠ **MockPipeline detected** — metrics reflect a pipeline that "
            "echoes gold. These are baseline upper-bound scores, not real "
            "evaluation results. Use a real EvalPipeline for genuine metrics."
        )
        lines.append("")

    # --- Aggregate metrics ---
    lines.append("## Aggregate Metrics")
    lines.append("")
    if run.aggregate_metrics:
        lines.append("| Metric | Value |")
        lines.append("|---|---|")
        for name in sorted(run.aggregate_metrics):
            value = run.aggregate_metrics[name]
            lines.append(f"| `{name}` | {value:.3f} |")
    else:
        lines.append("_(none — all examples errored or no metrics computed)_")
    lines.append("")

    # --- Per-example highlights ---
    lines.append("## Per-Example Highlights")
    lines.append("")
    worst = _worst_examples(ok_examples, _WORST_HIGHLIGHT_COUNT)
    if worst:
        lines.append("| Example | Faithfulness | Context Precision | Tag Accuracy |")
        lines.append("|---|---|---|---|")
        for ex in worst:
            faith = _find_metric(ex.metrics, "faithfulness")
            ctx = _find_metric(ex.metrics, "context_precision_at_5") or _find_metric(
                ex.metrics, "context_precision_at_k"
            )
            tag = _find_metric(ex.metrics, "tag_accuracy")
            faith_v = f"{faith.value:.3f}" if faith else "—"
            ctx_v = f"{ctx.value:.3f}" if ctx else "—"
            tag_v = f"{tag.value:.3f}" if tag else "—"
            lines.append(f"| `{ex.example_id}` | {faith_v} | {ctx_v} | {tag_v} |")
    else:
        lines.append("_(no per-example results)_")
    lines.append("")

    # --- M7 Phase 2 metrics (voiceprint EER + diarization DER) ---
    phase2 = _collect_phase2_metrics(ok_examples)
    if phase2:
        lines.append("## M7 Phase 2 Metrics")
        lines.append("")
        lines.append("| Example | Voiceprint EER | Diarization DER |")
        lines.append("|---|---|---|")
        for ex_id, eer, der in phase2:
            eer_v = f"{eer:.3f}" if eer is not None else "—"
            der_v = f"{der:.3f}" if der is not None else "—"
            lines.append(f"| `{ex_id}` | {eer_v} | {der_v} |")
        lines.append("")

    # --- Errors ---
    if errors:
        lines.append("## Errors")
        lines.append("")
        lines.append("| Example | Error |")
        lines.append("|---|---|")
        for ex in errors:
            err = (ex.error or "").replace("|", "\\|")
            if len(err) > 120:
                err = err[:117] + "..."
            lines.append(f"| `{ex.example_id}` | {err} |")
        lines.append("")

    return "\n".join(lines)


def _find_metric(metrics: tuple[MetricResult, ...], name: str) -> MetricResult | None:
    for m in metrics:
        if m.name == name:
            return m
    return None


def _worst_examples(examples: list[EvalExampleResult], count: int) -> list[EvalExampleResult]:
    """Return up to ``count`` worst examples sorted by primary metric ascending.

    If faithfulness metric is present (judge attached), sort by it; else sort
    by context_precision_at_5; else by tag_accuracy; else unsorted.
    """

    def key(ex: EvalExampleResult) -> float:
        for name in ("faithfulness", "context_precision_at_5", "tag_accuracy"):
            m = _find_metric(ex.metrics, name)
            if m is not None and not m.details.get("skipped"):
                return m.value
        return 1.0

    return sorted(examples, key=key)[:count]


def _collect_phase2_metrics(
    examples: list[EvalExampleResult],
) -> list[tuple[str, float | None, float | None]]:
    """Extract (example_id, voiceprint_eer, diarization_der) for M7 reporting.

    Only includes examples where at least one of the two metrics is present
    and not skipped.
    """
    out: list[tuple[str, float | None, float | None]] = []
    for ex in examples:
        eer = _find_metric(ex.metrics, "voiceprint_eer")
        der = _find_metric(ex.metrics, "diarization_der")
        eer_v = float(eer.value) if eer and not eer.details.get("skipped") else None
        der_v = float(der.value) if der and not der.details.get("skipped") else None
        if eer_v is not None or der_v is not None:
            out.append((ex.example_id, eer_v, der_v))
    return out


__all__ = ["to_json", "to_markdown"]
