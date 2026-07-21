"""Tests for ``audio_graphy.eval.reporter`` — JSON roundtrip + Markdown rendering.

Cases (per arch §7.3.7):
- to_json roundtrip preserves all fields
- to_markdown contains expected section headers
- empty per_example doesn't crash
- MockPipeline banner appears in markdown when config["pipeline"] mentions it
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from audio_graphy.eval.reporter import to_json, to_markdown
from audio_graphy.eval.types import (
    EvalExampleResult,
    EvalRun,
    MetricResult,
)


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def sample_run() -> EvalRun:
    return EvalRun(
        run_id="abc123def456",
        gold_set_path="/tmp/gold.yaml",
        started_at="2026-07-21T00:00:00+00:00",
        finished_at="2026-07-21T00:01:00+00:00",
        config={"pipeline": "MockPipeline(precision=1.0)", "k": "5"},
        aggregate_metrics={
            "context_precision_at_5": 1.0,
            "context_recall": 0.833,
            "entity_f1": 1.0,
        },
        per_example=(
            EvalExampleResult(
                example_id="ex-001",
                metrics=(
                    MetricResult(
                        name="context_precision_at_5",
                        value=1.0,
                        denominator=2,
                        details={"k": 5, "hits": 2},
                    ),
                    MetricResult(
                        name="faithfulness",
                        value=0.0,
                        denominator=0,
                        details={"skipped": True},
                    ),
                ),
            ),
            EvalExampleResult(
                example_id="ex-002",
                metrics=(),
                error="RuntimeError('pipeline crashed')",
            ),
        ),
    )


# ============================================================
# Tests
# ============================================================


def test_to_json_roundtrip(tmp_path: Path, sample_run: EvalRun) -> None:
    """Write → read JSON → all top-level fields preserved."""
    out = tmp_path / "report.json"
    to_json(sample_run, out)
    assert out.is_file()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["run_id"] == sample_run.run_id
    assert payload["gold_set_path"] == sample_run.gold_set_path
    assert payload["started_at"] == sample_run.started_at
    assert payload["finished_at"] == sample_run.finished_at
    assert payload["config"]["pipeline"] == "MockPipeline(precision=1.0)"
    assert payload["aggregate_metrics"]["context_recall"] == pytest.approx(0.833)
    assert len(payload["per_example"]) == 2
    assert payload["per_example"][1]["error"] == "RuntimeError('pipeline crashed')"


def test_to_json_creates_parent_dirs(tmp_path: Path, sample_run: EvalRun) -> None:
    """Reporter auto-creates missing parent dirs."""
    out = tmp_path / "deep" / "nested" / "report.json"
    to_json(sample_run, out)
    assert out.is_file()


def test_to_markdown_contains_expected_sections(
    tmp_path: Path, sample_run: EvalRun
) -> None:
    """Markdown report has Header + Aggregate + Highlights + Errors sections."""
    out = tmp_path / "report.md"
    to_markdown(sample_run, out)
    assert out.is_file()
    md = out.read_text(encoding="utf-8")
    assert "# Eval Report" in md
    assert "## Aggregate Metrics" in md
    assert "## Per-Example Highlights" in md
    assert "## Errors" in md
    assert sample_run.run_id in md
    # Aggregate table includes context_recall value.
    assert "0.833" in md


def test_to_markdown_empty_per_example(tmp_path: Path) -> None:
    """Empty per_example doesn't crash; should print 'no per-example' note."""
    run = EvalRun(
        run_id="empty12345",
        gold_set_path="/tmp/empty.yaml",
        started_at="2026-07-21T00:00:00+00:00",
        finished_at="2026-07-21T00:00:01+00:00",
        config={},
        aggregate_metrics={},
        per_example=(),
    )
    out = tmp_path / "report.md"
    to_markdown(run, out)
    md = out.read_text(encoding="utf-8")
    assert "no per-example results" in md
    assert "## Errors" not in md  # no errors section when empty


def test_to_markdown_mock_pipeline_banner(
    tmp_path: Path, sample_run: EvalRun
) -> None:
    """When config['pipeline'] contains 'MockPipeline', the warning banner appears."""
    out = tmp_path / "report.md"
    to_markdown(sample_run, out)
    md = out.read_text(encoding="utf-8")
    assert "MockPipeline detected" in md


def test_to_markdown_no_banner_for_real_pipeline(tmp_path: Path) -> None:
    """No banner when config['pipeline'] is not MockPipeline."""
    run = EvalRun(
        run_id="real1234567",
        gold_set_path="/tmp/x.yaml",
        started_at="2026-07-21T00:00:00+00:00",
        finished_at="2026-07-21T00:00:01+00:00",
        config={"pipeline": "RAGPipeline"},
        aggregate_metrics={},
        per_example=(),
    )
    out = tmp_path / "report.md"
    to_markdown(run, out)
    md = out.read_text(encoding="utf-8")
    assert "MockPipeline detected" not in md


def test_to_markdown_truncates_long_errors(tmp_path: Path) -> None:
    """Errors longer than 120 chars get truncated with ellipsis."""
    long_err = "x" * 300
    run = EvalRun(
        run_id="trunc1234567",
        gold_set_path="/tmp/x.yaml",
        started_at="2026-07-21T00:00:00+00:00",
        finished_at="2026-07-21T00:00:01+00:00",
        config={},
        aggregate_metrics={},
        per_example=(
            EvalExampleResult(example_id="ex-001", metrics=(), error=long_err),
        ),
    )
    out = tmp_path / "report.md"
    to_markdown(run, out)
    md = out.read_text(encoding="utf-8")
    # Truncated to 117 chars + "..."
    assert "..." in md
    assert long_err not in md
