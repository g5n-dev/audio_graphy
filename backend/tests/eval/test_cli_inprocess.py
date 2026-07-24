"""In-process CLI tests — gap-fill coverage for eval/cli.py + __main__.py.

The existing `test_cli.py` runs subprocesses which coverage.py cannot trace
from the parent pytest process. These tests import `cli.main()` and invoke it
directly so the cli.py code path is measured.

Targeted uncovered branches (per QA verification 2026-07-21):
- argparse build + parse
- gold set not found → exit 2
- --pipeline rag rejection → exit 2
- happy path with judge init failure (covered by missing settings) → falls back
- happy path with --no-judge → exit 0
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

# Ensure consistent working dir / env for in-process Settings().
os.environ.setdefault("WORKING_DIR", "/tmp/m5-qa-cli-wd")
Path(os.environ["WORKING_DIR"]).mkdir(parents=True, exist_ok=True)


def _tiny_gold(path: Path) -> Path:
    path.write_text(
        '- query: "q"\n  gold_answer: "a"\n  gold_context_ids: ["c1"]\n'
        "  gold_entities: []\n  gold_edges: []\n  gold_tags: []\n",
        encoding="utf-8",
    )
    return path


def test_cli_build_parser_has_required_args() -> None:
    """build_parser() exposes --gold-set, --no-judge, --pipeline, --k, --judge-llm."""
    from audio_graphy.eval.cli import build_parser

    parser = build_parser()
    actions = {a.dest for a in parser._actions}
    assert {"gold_set", "report_dir", "no_judge", "pipeline", "k", "judge_llm"} <= actions


def test_cli_missing_gold_set_returns_2(tmp_path: Path) -> None:
    """Missing gold set → exit 2 + stderr message."""
    from audio_graphy.eval.cli import main

    rc = main(
        [
            "--gold-set",
            str(tmp_path / "nonexistent.yaml"),
            "--no-judge",
        ]
    )
    assert rc == 2


def test_cli_pipeline_rag_rejected_returns_2(tmp_path: Path) -> None:
    """--pipeline rag → exit 2 with M5-not-implemented message."""
    from audio_graphy.eval.cli import main

    gold = _tiny_gold(tmp_path / "gold.yaml")
    rc = main(
        [
            "--gold-set",
            str(gold),
            "--no-judge",
            "--pipeline",
            "rag",
        ]
    )
    assert rc == 2


def test_cli_no_judge_exits_zero(tmp_path: Path) -> None:
    """Happy path: --no-judge → exit 0, JSON + Markdown reports created."""
    from audio_graphy.config import get_settings

    get_settings.cache_clear()
    from audio_graphy.eval.cli import main

    gold = _tiny_gold(tmp_path / "gold.yaml")
    out_dir = tmp_path / "out"
    rc = main(
        [
            "--gold-set",
            str(gold),
            "--report-dir",
            str(out_dir),
            "--no-judge",
        ]
    )
    assert rc == 0
    assert list(out_dir.glob("eval-*.json"))
    assert list(out_dir.glob("eval-*.md"))


def test_cli_judge_init_failure_falls_back_to_no_judge(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If build_adapters raises, CLI warns and continues without judge."""
    from audio_graphy.config import get_settings

    get_settings.cache_clear()

    # Force build_adapters to raise by monkey-patching.
    import audio_graphy.config as cfg_mod
    import audio_graphy.eval.cli as cli_mod

    def _boom(_settings: object) -> None:
        raise RuntimeError("simulated adapter build failure")

    monkeypatch.setattr(cfg_mod, "build_adapters", _boom)

    gold = _tiny_gold(tmp_path / "gold.yaml")
    out_dir = tmp_path / "out"
    with caplog.at_level(logging.WARNING, logger="audio_graphy.eval.cli"):
        rc = cli_mod.main(
            [
                "--gold-set",
                str(gold),
                "--report-dir",
                str(out_dir),
                # NOTE: not passing --no-judge — judge path attempted then falls back.
            ]
        )
    assert rc == 0
    assert list(out_dir.glob("eval-*.json"))
    # Warning logged about judge init failure (case-insensitive substring).
    assert any("judge init failed" in r.message.lower() for r in caplog.records)


def test_cli_judge_llm_override_recorded_in_config(tmp_path: Path) -> None:
    """--judge-llm override populates config_snapshot.judge_llm_override."""
    from audio_graphy.config import get_settings

    get_settings.cache_clear()
    from audio_graphy.eval.cli import main

    gold = _tiny_gold(tmp_path / "gold.yaml")
    out_dir = tmp_path / "out"
    rc = main(
        [
            "--gold-set",
            str(gold),
            "--report-dir",
            str(out_dir),
            "--no-judge",
            "--judge-llm",
            "custom-model",
        ]
    )
    assert rc == 0
    # Verify the override was captured by reading the JSON report.
    import json

    reports = list(out_dir.glob("eval-*.json"))
    assert reports
    data = json.loads(reports[0].read_text(encoding="utf-8"))
    assert data["config"]["judge_llm_override"] == "custom-model"


def test_cli_main_module_imports() -> None:
    """``audio_graphy.eval.__main__ imports cleanly (proves wrapper valid).

    The 3-line __main__.py only executes under ``python -m``; here we
    simply assert it imports without error and re-exports ``main``.
    """
    import audio_graphy.eval.__main__ as main_mod

    assert callable(main_mod.main)
