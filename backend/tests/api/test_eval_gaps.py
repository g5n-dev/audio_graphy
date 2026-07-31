"""Coverage gap-fill tests for /api/v1/eval endpoints.

Targets uncovered branches:
- format validation rejecting unknown format values (400 path)
- non-inspector role rejection
- json format report download
- report path missing for a completed run (404)
- report file missing on disk (404)
- status filter on GET /runs
- scheduler attached: _schedule_eval_job hits the configured branch
- audit writer attached: _write_audit path is exercised
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.api.conftest import _run_async  # type: ignore[import-not-found]


def _create_run(test_client: Any, headers: dict[str, str]) -> str:
    resp = test_client.post(
        "/api/v1/eval/runs",
        json={
            "gold_set_path": "/tmp/gold.yaml",
            "pipeline": "mock",
            "judge_enabled": False,
            "k": 5,
            "position_debias": False,
        },
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    return resp.json()["run_id"]


def _complete(
    factory: Any,
    run_id: str,
    tenant_id: str,
    *,
    metrics: dict[str, float] | None = None,
    report_dir: Path | None = None,
    markdown_path: str | None = None,
    json_path: str | None = None,
) -> None:
    from audio_graphy.eval.state import EvalRunState

    state = EvalRunState(factory)
    md = markdown_path
    jp = json_path
    if report_dir is not None:
        report_dir.mkdir(parents=True, exist_ok=True)
        md_path = report_dir / f"{run_id}.md"
        md_path.write_text(f"# Report {run_id}\n", encoding="utf-8")
        jp_path = report_dir / f"{run_id}.json"
        jp_path.write_text('{"r": 1}', encoding="utf-8")
        md = str(md_path)
        jp = str(jp_path)

    async def _go() -> None:
        await state.transition_to(run_id, "running")
        await state.transition_to(
            run_id,
            "completed",
            aggregate_metrics=metrics or {"x": 1.0},
            report_markdown_path=md,
            report_json_path=jp,
        )

    _run_async(_go())


def test_invalid_format_returns_400(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
    tmp_path: Path,
) -> None:
    """GET /report?format=yaml returns 400 (only markdown|json allowed)."""
    run_id = _create_run(test_client, auth_headers["admin_t1"])  # type: ignore[index]
    _complete(db_session_factory, run_id, "chang_an", report_dir=tmp_path)
    resp = test_client.get(
        f"/api/v1/eval/runs/{run_id}/report?format=yaml",
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    # FastAPI validates the Query pattern before our handler runs.
    assert resp.status_code == 422


def test_agent_role_rejected(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
) -> None:
    """Agent role (below inspector) cannot POST /runs."""
    resp = test_client.post(
        "/api/v1/eval/runs",
        json={
            "gold_set_path": "/tmp/gold.yaml",
            "pipeline": "mock",
            "judge_enabled": False,
            "k": 5,
            "position_debias": False,
        },
        headers=auth_headers["agent_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 403


def test_viewer_role_rejected(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
) -> None:
    """Viewer role cannot GET /runs (inspector+ only)."""
    resp = test_client.get(
        "/api/v1/eval/runs",
        headers=auth_headers["viewer_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 403


def test_json_format_report(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
    tmp_path: Path,
) -> None:
    """GET /report?format=json streams the JSON report file."""
    run_id = _create_run(test_client, auth_headers["admin_t1"])  # type: ignore[index]
    _complete(db_session_factory, run_id, "chang_an", report_dir=tmp_path)

    resp = test_client.get(
        f"/api/v1/eval/runs/{run_id}/report?format=json",
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/json")


def test_report_path_missing_returns_404(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
) -> None:
    """A completed run without a report path returns 404."""
    run_id = _create_run(test_client, auth_headers["admin_t1"])  # type: ignore[index]
    # Complete with no report paths.
    _complete(db_session_factory, run_id, "chang_an")

    resp = test_client.get(
        f"/api/v1/eval/runs/{run_id}/report?format=markdown",
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 404


def test_report_file_missing_on_disk_returns_404(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
) -> None:
    """A completed run whose report path is set but file is gone → 404."""
    run_id = _create_run(test_client, auth_headers["admin_t1"])  # type: ignore[index]
    _complete(
        db_session_factory,
        run_id,
        "chang_an",
        markdown_path="/tmp/eval_does_not_exist_xxx.md",
    )

    resp = test_client.get(
        f"/api/v1/eval/runs/{run_id}/report?format=markdown",
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 404


def test_status_filter_on_list(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
) -> None:
    """GET /runs?status=completed filters by status."""
    # Create 2 runs.
    r1 = _create_run(test_client, auth_headers["admin_t1"])  # type: ignore[index]
    _create_run(test_client, auth_headers["admin_t1"])  # type: ignore[index]
    # Complete only r1.
    _complete(db_session_factory, r1, "chang_an")

    resp = test_client.get(
        "/api/v1/eval/runs?status=completed",
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    for item in body["items"]:
        assert item["status"] == "completed"


def test_post_run_with_scheduler_attached_logs(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When eval_scheduler is set on app.state, _schedule_eval_job registers a job."""

    class _FakeScheduler:
        def __init__(self) -> None:
            self.added: list[tuple[str, dict[str, str]]] = []

        def add_job(
            self, func: str, *, kwargs: dict[str, str], id: str, replace_existing: bool
        ) -> None:
            self.added.append((id, kwargs))

    sched = _FakeScheduler()
    test_client.app.state.eval_scheduler = sched

    try:
        run_id = _create_run(test_client, auth_headers["admin_t1"])  # type: ignore[index]
        # The scheduler captured the job.
        assert len(sched.added) == 1
        job_id, kwargs = sched.added[0]
        assert job_id == f"eval_run_{run_id}"
        assert kwargs["run_id"] == run_id
        assert kwargs["tenant_id"] == "chang_an"
    finally:
        del test_client.app.state.eval_scheduler


def test_post_run_scheduler_add_job_failure_logged(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If scheduler.add_job raises, _schedule_eval_job logs a warning (no crash)."""

    class _BrokenScheduler:
        def add_job(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("simulated scheduler down")

    test_client.app.state.eval_scheduler = _BrokenScheduler()

    try:
        with caplog.at_level("WARNING"):
            run_id = _create_run(test_client, auth_headers["admin_t1"])  # type: ignore[index]
        assert isinstance(run_id, str)
        # The failure was logged but the request still returned 202.
        assert any("Failed to schedule eval job" in r.message for r in caplog.records)
    finally:
        del test_client.app.state.eval_scheduler


def test_post_run_no_scheduler_logs_noop(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Without a scheduler, _schedule_eval_job logs an info message (polling fallback)."""
    with caplog.at_level("INFO"):
        run_id = _create_run(test_client, auth_headers["admin_t1"])  # type: ignore[index]
    assert isinstance(run_id, str)
    assert any("No eval_scheduler" in r.message for r in caplog.records)


def test_post_run_writes_audit_when_writer_attached(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
) -> None:
    """When audit_writer is attached on app.state, _write_audit records 'eval.run.created'."""
    from audio_graphy.core.audit import AuditWriter

    writer = AuditWriter(db_session_factory, flush_batch_size=10, flush_interval_sec=10.0)
    test_client.app.state.audit_writer = writer

    try:
        run_id = _create_run(test_client, auth_headers["admin_t1"])  # type: ignore[index]
        assert isinstance(run_id, str)

        async def _drain() -> int:
            return await writer.flush()

        _run_async(_drain())

        # Audit row written.
        from sqlalchemy import select

        from audio_graphy.models.audit_log import AuditLog

        async def _check() -> int:
            async with db_session_factory() as session:
                rows = list(
                    (
                        await session.execute(
                            select(AuditLog).where(AuditLog.action == "eval.run.created")
                        )
                    )
                    .scalars()
                    .all()
                )
            return len(rows)

        assert _run_async(_check()) >= 1
    finally:
        test_client.app.state.audit_writer = None
