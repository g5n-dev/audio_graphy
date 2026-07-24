"""API tests for ``/api/v1/eval/runs`` endpoints (M6 WS-2).

Reuses the in-memory SQLite + TestClient harness from ``tests/api/conftest.py``.

The background scheduler is NOT registered — tests use ``EvalRunState``
directly to drive runs to completion / failure, simulating what the
real ``run_eval_job`` would persist.

Cases:
    1. POST /runs returns 202 + ``run_id``.
    2. GET /runs/{run_id} returns the EvalRunOut shape (after create).
    3. GET /runs/{run_id}/report before completion → 404.
    4. GET /runs/{run_id}/report after completion → 200 + file content.
    5. GET /runs paginated.
    6. Tenant isolation: cross-tenant GET → 404.
    7. Inspector role allowed (not just admin).
    8. Failed run captures ``error_message``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.api.conftest import _run_async  # type: ignore[import-not-found]


def _create_run(
    test_client: Any,
    headers: dict[str, str],
    *,
    gold_set_path: str = "/tmp/gold.yaml",
    pipeline: str = "mock",
) -> str:
    """POST /eval/runs and return the new run_id."""
    resp = test_client.post(
        "/api/v1/eval/runs",
        json={
            "gold_set_path": gold_set_path,
            "pipeline": pipeline,
            "judge_enabled": False,
            "k": 5,
            "position_debias": False,
        },
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert "run_id" in body
    return body["run_id"]


def _complete_run(
    factory: Any,
    run_id: str,
    tenant_id: str,
    *,
    metrics: dict[str, float] | None = None,
    report_dir: Path | None = None,
) -> None:
    """Drive a run to 'completed' via EvalRunState (simulating scheduler)."""
    from audio_graphy.eval.state import EvalRunState

    state = EvalRunState(factory)
    metrics = metrics or {"context_recall": 0.9, "tag_accuracy": 0.8}
    md_path = None
    json_path = None
    if report_dir is not None:
        report_dir.mkdir(parents=True, exist_ok=True)
        md_path = report_dir / f"eval_run_{run_id}.md"
        md_path.write_text(f"# Eval Report {run_id}\n\nmock content\n", encoding="utf-8")
        json_path = report_dir / f"eval_run_{run_id}.json"
        json_path.write_text(f'{{"run_id": "{run_id}"}}', encoding="utf-8")

    async def _go() -> None:
        await state.transition_to(run_id, "running")
        await state.transition_to(
            run_id,
            "completed",
            aggregate_metrics=metrics,
            report_markdown_path=str(md_path) if md_path else None,
            report_json_path=str(json_path) if json_path else None,
        )

    _run_async(_go())


def _fail_run(factory: Any, run_id: str, msg: str = "boom") -> None:
    """Drive a run to 'failed' via EvalRunState (simulating scheduler error)."""
    from audio_graphy.eval.state import EvalRunState

    state = EvalRunState(factory)

    async def _go() -> None:
        await state.transition_to(run_id, "failed", error_message=msg)

    _run_async(_go())


# ============================================================
# Tests
# ============================================================


def test_post_returns_202_with_run_id(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
) -> None:
    """POST /eval/runs returns 202 + run_id."""
    run_id = _create_run(test_client, auth_headers["admin_t1"])  # type: ignore[index]
    assert isinstance(run_id, str)
    assert len(run_id) == 32  # uuid4 hex


def test_get_returns_run_shape(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
) -> None:
    """GET /eval/runs/{id} returns the EvalRunOut shape with status=pending."""
    run_id = _create_run(test_client, auth_headers["admin_t1"])  # type: ignore[index]
    resp = test_client.get(
        f"/api/v1/eval/runs/{run_id}",
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == run_id
    assert body["status"] == "pending"
    assert body["pipeline"] == "mock"
    assert body["tenant_id"] == "chang_an"
    assert body["judge_enabled"] is False
    assert body["k_value"] == 5
    assert "config" in body
    assert "started_at" in body


def test_get_report_before_completion_returns_404(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
) -> None:
    """GET /report on a pending run returns 404 (not ready)."""
    run_id = _create_run(test_client, auth_headers["admin_t1"])  # type: ignore[index]
    resp = test_client.get(
        f"/api/v1/eval/runs/{run_id}/report?format=markdown",
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 404, resp.text


def test_get_report_after_completion_returns_file(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
    tmp_path: Path,
) -> None:
    """GET /report on a completed run streams the Markdown file."""
    run_id = _create_run(test_client, auth_headers["admin_t1"])  # type: ignore[index]
    _complete_run(db_session_factory, run_id, "chang_an", report_dir=tmp_path)

    resp = test_client.get(
        f"/api/v1/eval/runs/{run_id}/report?format=markdown",
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 200, resp.text
    assert "mock content" in resp.text
    assert resp.headers["content-type"].startswith("text/markdown")


def test_list_runs_paginated(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
) -> None:
    """GET /eval/runs returns paginated results (total + items)."""
    # Create 3 runs.
    for _ in range(3):
        _create_run(test_client, auth_headers["admin_t1"])  # type: ignore[index]

    resp = test_client.get(
        "/api/v1/eval/runs?limit=2&offset=0",
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert len(body["items"]) == 2


def test_tenant_isolation_cross_tenant_get_returns_404(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
) -> None:
    """A run created by tenant A is not visible to tenant B."""
    # Create as chang_an admin.
    run_id = _create_run(test_client, auth_headers["admin_t1"])  # type: ignore[index]
    # Query as byd admin → 404.
    resp = test_client.get(
        f"/api/v1/eval/runs/{run_id}",
        headers=auth_headers["admin_t2"],  # type: ignore[index]
    )
    assert resp.status_code == 404, resp.text


def test_inspector_role_allowed(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
) -> None:
    """Inspector role can POST /runs (not just admin)."""
    run_id = _create_run(test_client, auth_headers["inspector_t1"])  # type: ignore[index]
    assert isinstance(run_id, str) and len(run_id) == 32
    # And can GET.
    resp = test_client.get(
        f"/api/v1/eval/runs/{run_id}",
        headers=auth_headers["inspector_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 200


def test_failed_run_has_error_message(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
) -> None:
    """A run transitioned to 'failed' surfaces error_message on GET."""
    run_id = _create_run(test_client, auth_headers["admin_t1"])  # type: ignore[index]
    _fail_run(db_session_factory, run_id, msg="eval blew up")

    resp = test_client.get(
        f"/api/v1/eval/runs/{run_id}",
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    assert body["error_message"] is not None
    assert "blew up" in body["error_message"]
