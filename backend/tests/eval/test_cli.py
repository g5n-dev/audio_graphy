"""Integration tests for ``python -m audio_graphy.eval`` CLI.

Cases (per arch §7.3.8):
- ``--help`` exits 0
- Smoke run: ``--gold-set examples/eval/smoke.yaml --no-judge --report-dir /tmp/...``
  exits 0 and produces both JSON and Markdown reports.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Path to the project's examples directory (one level up from backend/).
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_EXAMPLES_GOLD = _PROJECT_ROOT / "examples" / "eval" / "smoke.yaml"


def test_cli_help_exits_zero() -> None:
    """``python -m audio_graphy.eval --help`` exits 0."""
    proc = subprocess.run(
        [sys.executable, "-m", "audio_graphy.eval", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "audiography-eval" in proc.stdout
    assert "--gold-set" in proc.stdout
    assert "--no-judge" in proc.stdout


@pytest.mark.skipif(
    not _EXAMPLES_GOLD.is_file(),
    reason="examples/eval/smoke.yaml not found (skipping CLI integration)",
)
def test_cli_smoke_no_judge(tmp_path: Path) -> None:
    """End-to-end run with --no-judge → exits 0 + produces JSON + Markdown.

    Uses the project-root examples/eval/smoke.yaml as the gold set.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "audio_graphy.eval",
            "--gold-set",
            str(_EXAMPLES_GOLD),
            "--report-dir",
            str(tmp_path),
            "--no-judge",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    # Both report files exist (eval-<run_id>.json / .md).
    json_files = list(tmp_path.glob("eval-*.json"))
    md_files = list(tmp_path.glob("eval-*.md"))
    assert json_files, f"no JSON report in {tmp_path}"
    assert md_files, f"no Markdown report in {tmp_path}"
    # Markdown contains the MockPipeline banner.
    md = md_files[0].read_text(encoding="utf-8")
    assert "MockPipeline detected" in md
    assert "## Aggregate Metrics" in md


def test_cli_missing_gold_set_returns_2(tmp_path: Path) -> None:
    """Missing gold set file → exit code 2 + stderr message."""
    bogus = tmp_path / "nonexistent.yaml"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "audio_graphy.eval",
            "--gold-set",
            str(bogus),
            "--no-judge",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2
    assert "not found" in proc.stderr


def test_cli_pipeline_rag_rejected_in_m5(tmp_path: Path) -> None:
    """``--pipeline rag`` → exit code 2 with M5-not-implemented message."""
    gold_set = tmp_path / "stub.yaml"
    gold_set.write_text(
        '- query: "q1"\n  gold_answer: "a1"\n  gold_context_ids: []\n'
        "  gold_entities: []\n  gold_edges: []\n  gold_tags: []\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "audio_graphy.eval",
            "--gold-set",
            str(gold_set),
            "--no-judge",
            "--pipeline",
            "rag",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2
    assert "M6" in proc.stderr


def test_cli_smoke_with_local_gold_set(tmp_path: Path) -> None:
    """Local tiny gold set also works (decouples from examples/ layout)."""
    if shutil.which(sys.executable) is None and not Path(sys.executable).is_file():
        pytest.skip("sys.executable not directly runnable")
    gold = tmp_path / "tiny.yaml"
    gold.write_text(
        '- query: "q"\n  gold_answer: "a"\n  gold_context_ids: ["c"]\n'
        "  gold_entities: []\n  gold_edges: []\n  gold_tags: []\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "audio_graphy.eval",
            "--gold-set",
            str(gold),
            "--report-dir",
            str(out_dir),
            "--no-judge",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert list(out_dir.glob("eval-*.json"))
    assert list(out_dir.glob("eval-*.md"))
