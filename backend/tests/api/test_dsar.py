"""API tests for DSAR endpoints — PIPL §14.3 (admin-only).

Reuses the in-memory SQLite + TestClient harness from tests/api/conftest.py
and seeds one admin + one inspector user, plus a recording owned by the
admin tenant.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

import pytest

# Re-use the shared API fixtures.
from tests.api.conftest import (  # type: ignore[import-not-found]
    _run_async,
    seed_recording,
    seed_segment,
    seed_tag,
)


async def _seed_admin_recording(factory: Any, tag_suffix: str = "init") -> int:
    """Insert a completed recording owned by tenant chang_an."""
    rec_id = await seed_recording(
        factory,
        tenant_id="chang_an",
        store_id="S001",
        agent_name="agent_ca",
        status="indexed",
        pipeline_state="done",
    )
    await seed_segment(
        factory,
        recording_id=rec_id,
        tenant_id="chang_an",
        transcript="call me 13812345678",
    )
    await seed_tag(
        factory,
        recording_id=rec_id,
        tenant_id="chang_an",
        tag_path=f"quality.{tag_suffix}",
        tag_value="pass",
    )
    return rec_id


def test_non_admin_gets_403(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
) -> None:
    """Inspector / agent / viewer roles are rejected with 403."""
    headers = auth_headers  # type: ignore[assignment]
    for role in ("inspector_t1", "agent_t1", "viewer_t1"):
        resp = test_client.get(
            "/api/v1/dsar/audit",
            headers=headers[role],
        )
        assert resp.status_code == 403, f"{role} should be 403, got {resp.status_code}"


def test_admin_can_list_audit_empty(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
) -> None:
    """Admin can call /dsar/audit; empty list returns total=0."""
    resp = test_client.get(
        "/api/v1/dsar/audit",
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert "total" in body
    assert body["total"] == 0


def test_export_returns_zip_with_expected_files(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
) -> None:
    """POST /dsar/export/{id} streams a ZIP with manifest + transcripts + audit CSV."""
    factory = db_session_factory
    rec_id = _run_async(_seed_admin_recording(factory))

    resp = test_client.post(
        f"/api/v1/dsar/export/{rec_id}",
        json={"reason": "QA inspection"},
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/zip")

    zip_bytes = resp.content
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        assert any("manifest.json" in n for n in names), names
        assert any("transcript/raw.txt" in n for n in names), names
        assert any("audit_history.csv" in n for n in names), names
        # Raw transcript contains the seeded PII.
        raw = next(zf.read(n) for n in names if n.endswith("transcript/raw.txt")).decode("utf-8")
        assert "13812345678" in raw


def test_export_writes_audit_log(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
) -> None:
    """POST /dsar/export writes an audit_log(action='dsar.export') row."""
    factory = db_session_factory
    rec_id = _run_async(_seed_admin_recording(factory, tag_suffix="exp-audit"))

    resp = test_client.post(
        f"/api/v1/dsar/export/{rec_id}",
        json={"reason": "audit test"},
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 200

    # Verify audit_log directly.
    from sqlalchemy import select

    from audio_graphy.models.audit_log import AuditLog

    async def _check() -> bool:
        async with factory() as session:
            rows = list(
                (await session.execute(select(AuditLog).where(AuditLog.action == "dsar.export")))
                .scalars()
                .all()
            )
        return len(rows) >= 1

    assert _run_async(_check())


def test_erase_deletes_recording_and_audits(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
) -> None:
    """POST /dsar/erase removes the recording and writes an audit row."""
    factory = db_session_factory
    rec_id = _run_async(_seed_admin_recording(factory, tag_suffix="erase-audit"))

    resp = test_client.post(
        f"/api/v1/dsar/erase/{rec_id}",
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["recording_id"] == rec_id
    assert body["deleted"] is True

    # Recording row is gone.
    from sqlalchemy import select

    from audio_graphy.models.recording import Recording

    async def _gone() -> bool:
        async with factory() as session:
            row = (
                await session.execute(select(Recording).where(Recording.id == rec_id))
            ).scalar_one_or_none()
        return row is None

    assert _run_async(_gone())

    # Audit row exists.
    from audio_graphy.models.audit_log import AuditLog

    async def _audited() -> int:
        async with factory() as session:
            rows = list(
                (await session.execute(select(AuditLog).where(AuditLog.action == "dsar.erase")))
                .scalars()
                .all()
            )
        return len(rows)

    assert _run_async(_audited()) >= 1


def test_missing_recording_returns_404(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
) -> None:
    """DSAR endpoints return 404 for a non-existent recording id."""
    for method, path in [
        ("post", "/api/v1/dsar/export/99999"),
        ("post", "/api/v1/dsar/erase/99999"),
    ]:
        kwargs: dict[str, Any] = {"headers": auth_headers["admin_t1"]}  # type: ignore[index]
        if method == "post" and "export" in path:
            kwargs["json"] = {"reason": "x"}
        resp = test_client.request(method, path, **kwargs)
        assert resp.status_code == 404, (method, path, resp.status_code, resp.text)


def test_audit_list_pagination(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
) -> None:
    """GET /dsar/audit?limit=...&offset=... paginates results."""
    factory = db_session_factory
    # Seed 3 audit rows by exporting 3 different recordings.
    for i in range(3):
        rec_id = _run_async(_seed_admin_recording(factory, tag_suffix=f"p{i}"))
        resp = test_client.post(
            f"/api/v1/dsar/export/{rec_id}",
            json={"reason": "pagination seed"},
            headers=auth_headers["admin_t1"],  # type: ignore[index]
        )
        assert resp.status_code == 200

    resp = test_client.get(
        "/api/v1/dsar/audit?action=dsar.export&limit=2&offset=0",
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 3
    assert len(body["items"]) <= 2
    assert body["page_size"] == 2
