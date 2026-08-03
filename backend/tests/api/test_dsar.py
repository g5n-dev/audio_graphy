"""API tests for DSAR endpoints — PIPL §14.3 (admin-only).

Reuses the in-memory SQLite + TestClient harness from tests/api/conftest.py
and seeds one admin + one inspector user, plus a recording owned by the
admin tenant.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
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


def test_export_cannot_bypass_an_active_blind_review(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
) -> None:
    factory = db_session_factory
    rec_id = _run_async(_seed_admin_recording(factory, tag_suffix="blind-isolation"))

    async def _seed_claim() -> None:
        from datetime import UTC, datetime

        from audio_graphy.models.tag_governance import TagReviewTask

        async with factory() as session, session.begin():
            session.add(
                TagReviewTask(
                    tenant_id="chang_an",
                    batch_id="dsar-blind-isolation",
                    selection_policy="random_audit",
                    selection_policy_version="1",
                    blind_mode=True,
                    subject_type="dialogue_unit",
                    subject_id=1,
                    reception_id=None,
                    tag_key="intent",
                    proposed_value="purchase",
                    confidence=0.9,
                    evidence_refs=[],
                    reason="audit",
                    status="claimed",
                    priority=1,
                    claimed_by=1,
                    claimed_at=datetime.now(UTC),
                    created_by=2,
                )
            )

    _run_async(_seed_claim())
    response = test_client.post(
        f"/api/v1/dsar/export/{rec_id}",
        json={"reason": "should be isolated"},
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )

    assert response.status_code == 403


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


def test_erase_commit_failure_never_starts_external_cleanup(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
    tmp_path: Path,
) -> None:
    """The deletion outbox is committed before any file/cache/graph mutation."""
    from sqlalchemy import event, select
    from sqlalchemy.orm import Session

    from audio_graphy.models.erasure_outbox import ErasureOutbox
    from audio_graphy.models.recording import Recording

    factory = db_session_factory
    rec_id = _run_async(_seed_admin_recording(factory, tag_suffix="commit-failure"))
    audio_path = tmp_path / "must-survive.wav"
    audio_path.write_bytes(b"private-audio")

    async def _point_recording_at_fixture() -> None:
        async with factory() as session, session.begin():
            rec = await session.get(Recording, rec_id)
            assert rec is not None
            rec.path = str(audio_path)

    _run_async(_point_recording_at_fixture())

    class _CacheSpy:
        calls = 0

        async def delete_by_provenance(self, *_args: Any, **_kwargs: Any) -> int:
            self.calls += 1
            return 1

    cache = _CacheSpy()
    graph_factory_calls: list[str] = []

    async def _graph_factory(tenant_id: str) -> None:
        graph_factory_calls.append(tenant_id)
        return None

    test_client.app.state.llm_cache = cache
    test_client.app.state.graph_store_factory = _graph_factory

    def _fail_commit(_session: Session) -> None:
        raise RuntimeError("fault-injected dsar commit failure")

    event.listen(Session, "before_commit", _fail_commit)
    try:
        response = test_client.post(
            f"/api/v1/dsar/erase/{rec_id}",
            headers=auth_headers["admin_t1"],  # type: ignore[index]
        )
    finally:
        event.remove(Session, "before_commit", _fail_commit)
        test_client.app.state.llm_cache = None
        test_client.app.state.graph_store_factory = None

    assert response.status_code == 500
    assert audio_path.read_bytes() == b"private-audio"
    assert cache.calls == 0
    assert graph_factory_calls == []

    async def _database_rolled_back() -> tuple[bool, int]:
        async with factory() as session:
            recording_exists = (
                await session.execute(select(Recording.id).where(Recording.id == rec_id))
            ).scalar_one_or_none()
            outbox_count = len(
                (
                    await session.execute(
                        select(ErasureOutbox.id).where(
                            ErasureOutbox.tenant_id == "chang_an",
                            ErasureOutbox.subject_type == "recording",
                            ErasureOutbox.subject_id == str(rec_id),
                        )
                    )
                )
                .scalars()
                .all()
            )
        return recording_exists is not None, outbox_count

    assert _run_async(_database_rolled_back()) == (True, 0)


def test_erase_external_failure_is_retryable_after_database_commit(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
) -> None:
    """A projection failure cannot resurrect DB data and a retry uses the outbox."""
    from sqlalchemy import select

    from audio_graphy.models.erasure_outbox import ErasureOutbox
    from audio_graphy.models.recording import Recording

    factory = db_session_factory
    rec_id = _run_async(_seed_admin_recording(factory, tag_suffix="external-retry"))

    class _FlakyCache:
        fail = True
        calls = 0

        async def delete_by_provenance(self, *_args: Any, **_kwargs: Any) -> int:
            self.calls += 1
            if self.fail:
                raise RuntimeError("fault-injected cache failure")
            return 1

    cache = _FlakyCache()
    test_client.app.state.llm_cache = cache
    try:
        first = test_client.post(
            f"/api/v1/dsar/erase/{rec_id}",
            headers=auth_headers["admin_t1"],  # type: ignore[index]
        )
        assert first.status_code == 200, first.text

        async def _state() -> tuple[bool, str, int]:
            async with factory() as session:
                recording = await session.get(Recording, rec_id)
                outbox = (
                    await session.execute(
                        select(ErasureOutbox).where(
                            ErasureOutbox.tenant_id == "chang_an",
                            ErasureOutbox.subject_type == "recording",
                            ErasureOutbox.subject_id == str(rec_id),
                        )
                    )
                ).scalar_one()
            return recording is None, outbox.status, outbox.attempts

        assert _run_async(_state()) == (True, "failed", 1)

        cache.fail = False
        retried = test_client.post(
            f"/api/v1/dsar/erase/{rec_id}",
            headers=auth_headers["admin_t1"],  # type: ignore[index]
        )
        assert retried.status_code == 200, retried.text
        assert _run_async(_state()) == (True, "succeeded", 2)
        assert cache.calls == 2
    finally:
        test_client.app.state.llm_cache = None


def test_erase_audit_receipt_contains_counts_but_never_storage_paths(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
) -> None:
    from sqlalchemy import select

    from audio_graphy.models.audit_log import AuditLog
    from audio_graphy.models.llm_cache import LLMCacheSourceGuard

    factory = db_session_factory
    rec_id = _run_async(_seed_admin_recording(factory, tag_suffix="pathless-audit"))

    response = test_client.post(
        f"/api/v1/dsar/erase/{rec_id}",
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert response.status_code == 200, response.text

    async def _receipt() -> tuple[AuditLog, LLMCacheSourceGuard]:
        async with factory() as session:
            audit = (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.tenant_id == "chang_an",
                        AuditLog.action == "dsar.erase",
                        AuditLog.target == f"recording:{rec_id}",
                    )
                )
            ).scalar_one()
            guard = (
                await session.execute(
                    select(LLMCacheSourceGuard).where(
                        LLMCacheSourceGuard.tenant_id == "chang_an",
                        LLMCacheSourceGuard.source_type == "recording",
                        LLMCacheSourceGuard.source_id == str(rec_id),
                    )
                )
            ).scalar_one()
            return audit, guard

    receipt, guard = _run_async(_receipt())
    serialized = f"{receipt.before_value!r}{receipt.after_value!r}".lower()
    assert "path" not in serialized
    assert "/tmp/" not in serialized
    assert receipt.before_value == {"recording_id": rec_id}
    assert receipt.after_value is not None
    assert receipt.after_value["recording_deleted"] is True
    assert isinstance(receipt.after_value["database_rows_invalidated"], int)
    assert guard.state == "erased"
    assert guard.erased_at is not None


def test_erase_invalidates_reception_generation_and_canonical_tags(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
) -> None:
    """No active timeline or canonical label may outlive an erased source."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import func, select

    from audio_graphy.models.reception import (
        DialogueStateTransition,
        DialogueTagAssignment,
        DialogueUnit,
        Reception,
        ReceptionRecording,
    )
    from audio_graphy.models.reception_audio import (
        ReceptionAudioArtifact,
        ReceptionAudioOperation,
        ReceptionTimelineRevision,
    )
    from audio_graphy.models.tag_governance import (
        TagAssignmentCurrent,
        TagAssignmentFact,
    )

    factory = db_session_factory
    rec_id = _run_async(_seed_admin_recording(factory, tag_suffix="reception-cascade"))
    working_dir = Path(test_client.app.state.settings.working_dir)
    artifact_path = working_dir / "assembled_audio" / "chang_an" / "dsar-reception.wav"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(b"derived-private-audio")

    async def _seed_reception_generation() -> int:
        now = datetime.now(UTC)
        async with factory() as session, session.begin():
            reception = Reception(
                tenant_id="chang_an",
                scenario="gold",
                store_id="S001",
                status="ready",
                merge_mode="both",
                started_at=now,
                ended_at=now + timedelta(seconds=10),
                merged_audio_path=str(artifact_path),
                version=1,
            )
            session.add(reception)
            await session.flush()
            revision = ReceptionTimelineRevision(
                tenant_id="chang_an",
                reception_id=reception.id,
                revision=1,
                expected_reception_version=1,
                state="ACTIVE",
                plan_signature="a" * 64,
                plan_token_hash="b" * 64,
                source_manifest=[{"recording_id": rec_id}],
                total_duration_ms=10_000,
                physical_eligible=True,
                warnings=[],
                expires_at=now + timedelta(hours=1),
                activated_at=now,
            )
            session.add(revision)
            await session.flush()
            reception.active_timeline_revision_id = revision.id
            session.add(
                ReceptionRecording(
                    tenant_id="chang_an",
                    reception_id=reception.id,
                    recording_id=rec_id,
                    timeline_revision_id=revision.id,
                    sequence_no=0,
                    timeline_start_sec=0,
                    timeline_end_sec=10,
                    source_start_sec=0,
                    source_end_sec=10,
                    source_start_ms=0,
                    source_end_ms=10_000,
                    timeline_start_ms=0,
                    timeline_end_ms=10_000,
                    gap_before_ms=0,
                    gap_before_sec=0,
                    decision_source="manual",
                    merge_reasons={},
                )
            )
            operation = ReceptionAudioOperation(
                tenant_id="chang_an",
                reception_id=reception.id,
                timeline_revision_id=revision.id,
                idempotency_key="dsar-generation",
                mode="both",
                expected_reception_version=1,
                status="succeeded",
                progress=1,
                attempt_count=1,
                finished_at=now,
            )
            session.add(operation)
            await session.flush()
            session.add(
                ReceptionAudioArtifact(
                    tenant_id="chang_an",
                    reception_id=reception.id,
                    timeline_revision_id=revision.id,
                    operation_id=operation.id,
                    state="ATTACHED",
                    path=str(artifact_path),
                    sha256="c" * 64,
                    size_bytes=len(b"derived-private-audio"),
                    duration_ms=10_000,
                    sample_rate=16_000,
                    channels=1,
                    attached_at=now,
                )
            )
            unit = DialogueUnit(
                tenant_id="chang_an",
                reception_id=reception.id,
                source_recording_id=rec_id,
                timeline_revision_id=revision.id,
                unit_index=0,
                start_sec=0,
                end_sec=10,
                boundary_reasons=[],
                segment_refs=[],
                speaker_refs=[],
            )
            session.add(unit)
            await session.flush()
            session.add_all(
                [
                    DialogueStateTransition(
                        tenant_id="chang_an",
                        reception_id=reception.id,
                        dialogue_unit_id=unit.id,
                        timeline_revision_id=revision.id,
                        sequence_no=0,
                        from_state="greeting",
                        to_state="needs",
                        trigger="test",
                        confidence=0.9,
                        evidence_refs=[],
                        algorithm_version="dialogue-hybrid-v2",
                    ),
                    DialogueTagAssignment(
                        tenant_id="chang_an",
                        reception_id=reception.id,
                        dialogue_unit_id=unit.id,
                        timeline_revision_id=revision.id,
                        group_key="intent",
                        group_version="v1",
                        label_key="intent",
                        label_value="purchase",
                        source="manual",
                        evidence_refs=[],
                        assigned_at=now,
                    ),
                ]
            )
            fact = TagAssignmentFact(
                tenant_id="chang_an",
                subject_type="dialogue_unit",
                subject_id=unit.id,
                reception_id=reception.id,
                dialogue_unit_id=unit.id,
                tag_key="intent",
                tag_value="purchase",
                confidence=1,
                evidence_refs=[{"segment_id": 1}],
                source="manual",
                input_hash="d" * 64,
                recipe_hash="e" * 64,
                revision=1,
                tombstone=False,
                actor_user_id=1,
                assigned_at=now,
            )
            session.add(fact)
            await session.flush()
            session.add(
                TagAssignmentCurrent(
                    tenant_id="chang_an",
                    subject_type="dialogue_unit",
                    subject_id=unit.id,
                    tag_key="intent",
                    fact_id=fact.id,
                    revision=1,
                )
            )
            return reception.id

    reception_id = _run_async(_seed_reception_generation())
    response = test_client.post(
        f"/api/v1/dsar/erase/{rec_id}",
        headers=auth_headers["admin_t1"],  # type: ignore[index]
    )
    assert response.status_code == 200, response.text
    assert not artifact_path.exists()

    async def _assert_invalidated() -> None:
        async with factory() as session:
            reception = await session.get(Reception, reception_id)
            assert reception is not None
            assert reception.active_timeline_revision_id is None
            assert reception.merged_audio_path is None
            assert reception.status == "needs_review"
            for model in (
                ReceptionTimelineRevision,
                ReceptionAudioOperation,
                ReceptionAudioArtifact,
                DialogueUnit,
                DialogueStateTransition,
                DialogueTagAssignment,
                TagAssignmentFact,
                TagAssignmentCurrent,
            ):
                count = await session.scalar(
                    select(func.count()).select_from(model).where(model.tenant_id == "chang_an")
                )
                assert count == 0, model.__name__

    _run_async(_assert_invalidated())


def test_erase_invalidates_llm_cache_by_recording_provenance(
    test_client: pytest.fixture,
    auth_headers: pytest.fixture,
    db_session_factory: pytest.fixture,
) -> None:
    factory = db_session_factory
    rec_id = _run_async(_seed_admin_recording(factory, tag_suffix="erase-llm-cache"))

    class _CacheSpy:
        calls: list[tuple[str, list[dict[str, str]]]] = []

        async def delete_by_provenance(
            self,
            tenant_id: str,
            references: list[dict[str, str]],
        ) -> int:
            self.calls.append((tenant_id, references))
            return 1

    cache = _CacheSpy()
    test_client.app.state.llm_cache = cache
    try:
        response = test_client.post(
            f"/api/v1/dsar/erase/{rec_id}",
            headers=auth_headers["admin_t1"],  # type: ignore[index]
        )
    finally:
        test_client.app.state.llm_cache = None

    assert response.status_code == 200, response.text
    assert cache.calls == [
        (
            "chang_an",
            [{"source_type": "recording", "source_id": str(rec_id)}],
        )
    ]


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
