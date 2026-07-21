"""CLI entry point for ``python -m audio_graphy.eval``.

Usage::

    python -m audio_graphy.eval \\
        --gold-set examples/eval/smoke.yaml \\
        --report-dir reports/ \\
        [--no-judge] \\
        [--pipeline mock|rag] \\
        [--judge-llm <model>]

Exit codes:
- 0: success (per-example errors are reflected in the report, not the exit code)
- 2: argparse error or gold set not found
- 70+: uncaught exception during run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

logger = logging.getLogger("audio_graphy.eval.cli")


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser (exported for tests)."""
    parser = argparse.ArgumentParser(
        prog="audiography-eval",
        description="Run AudioGraphy evaluation against a gold set YAML.",
    )
    parser.add_argument(
        "--gold-set",
        type=Path,
        required=True,
        help="Path to gold set YAML (list of examples).",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=Path("reports"),
        help="Output directory for reports (default: reports/).",
    )
    parser.add_argument(
        "--judge-llm",
        type=str,
        default="",
        help=(
            "Override judge LLM model name (default: settings.judge_llm_model_resolved)."
        ),
    )
    parser.add_argument(
        "--no-judge",
        action="store_true",
        help="Skip LLM-dependent metrics (faithfulness/relevance/factual_correctness).",
    )
    parser.add_argument(
        "--pipeline",
        choices=["mock", "rag"],
        default="mock",
        help="Pipeline to evaluate (default: mock; rag not yet implemented in M5).",
    )
    parser.add_argument(
        "--k",
        type=int,
        default=5,
        help="Cutoff k for context_precision_at_k (default: 5).",
    )
    parser.add_argument(
        "--voiceprint-eer",
        action="store_true",
        help=(
            "Enable voiceprint EER metric (M7). Reads 'voiceprint_trials' "
            "metadata from each gold example."
        ),
    )
    parser.add_argument(
        "--diarization-der",
        action="store_true",
        help=(
            "Enable diarization DER metric (M7). Reads reference_rttm / "
            "hypothesis_rttm paths from each gold example's metadata."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argv list (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code (0 on success).
    """
    args = build_parser().parse_args(argv)

    if not args.gold_set.is_file():
        print(
            "error: gold set not found: " + str(args.gold_set), file=sys.stderr
        )
        return 2

    if args.pipeline == "rag":
        print(
            "error: --pipeline rag is not implemented in M5 (lands in M6); "
            "use --pipeline mock (default).",
            file=sys.stderr,
        )
        return 2

    # Lazy imports — keep --help fast.
    from audio_graphy.config import get_settings
    from audio_graphy.eval.reporter import to_json, to_markdown
    from audio_graphy.eval.runner import EvalRunner, MockPipeline

    settings = get_settings()

    pipeline = MockPipeline(precision=1.0)
    print(
        "⚠  Using MockPipeline(precision=1.0) — for real RAG evaluation wait "
        "for M6. Metrics below are baseline upper bounds, not actual scores.",
        file=sys.stderr,
    )

    judge = None
    if not args.no_judge:
        try:
            from audio_graphy.config import build_adapters
            from audio_graphy.eval.judge import LLMJudge

            bundle = build_adapters(settings)
            judge = LLMJudge(llm=bundle.strong_llm)
        except Exception as exc:
            logger.warning("Judge init failed (%s); falling back to --no-judge", exc)
            print(
                "warning: judge init failed (" + str(exc) + "); running without "
                "LLM metrics. Use --no-judge to silence this warning.",
                file=sys.stderr,
            )
            judge = None

    config_snapshot = {
        "pipeline": repr(pipeline),
        "judge": "enabled" if judge is not None else "disabled",
        "k": str(args.k),
        "judge_llm_model_resolved": settings.judge_llm_model_resolved,
    }
    if args.judge_llm:
        config_snapshot["judge_llm_override"] = args.judge_llm

    runner = EvalRunner(
        gold_set_path=args.gold_set,
        pipeline=pipeline,
        judge=judge,
        settings=settings,
        k=args.k,
        config_snapshot=config_snapshot,
        voiceprint_eer_enabled=args.voiceprint_eer,
        diarization_der_enabled=args.diarization_der,
    )

    try:
        run = asyncio.run(runner.run())
    except Exception as exc:
        print("error: evaluation crashed: " + str(exc), file=sys.stderr)
        return 70

    args.report_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.report_dir / f"eval-{run.run_id}.json"
    md_path = args.report_dir / f"eval-{run.run_id}.md"
    to_json(run, json_path)
    to_markdown(run, md_path)

    errors = sum(1 for ex in run.per_example if ex.error is not None)
    print(
        "Eval complete: " + str(len(run.per_example) - errors) + "/" +
        str(len(run.per_example)) + " ok → " + str(md_path),
        file=sys.stderr,
    )
    return 0


__all__ = ["build_parser", "main"]
