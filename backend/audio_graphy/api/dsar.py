"""DSAR (Data Subject Access Request) router — PIPL §14.3 admin endpoints.

Three endpoints, ALL admin-only:

    POST /api/v1/dsar/export/{recording_id}
        Returns a StreamingResponse ZIP containing:
        - decrypted audio (if encrypted_path is set)
        - raw transcript + scrubbed transcript
        - all tag_facts
        - segments + chunks metadata
        - audit history for this recording as CSV
        Writes audit_log(action="dsar.export").

    POST /api/v1/dsar/erase/{recording_id}
        Hard-deletes the recording (audio file + DB rows + GraphML refs).
        Writes audit_log(action="dsar.erase").

    GET /api/v1/dsar/audit
        Paginated audit_logs query with optional filters
        (recording_id, user_id, action).

See: docs/m6-architecture.md §3.5, docs/m6-prd.md §4.4.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from audio_graphy.api.deps import get_current_user, get_db, get_session_factory
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.core.audit import AuditWriter
from audio_graphy.core.crypto import AudioCrypto
from audio_graphy.errors import ForbiddenError, RecordingNotFoundError
from audio_graphy.models.audit_log import AuditLog
from audio_graphy.models.chunk import Chunk
from audio_graphy.models.recording import Recording
from audio_graphy.models.segment import Segment
from audio_graphy.models.tag_fact import TagFact
from audio_graphy.schemas.dsar import (
    AuditLogList,
    AuditLogOut,
    DSAREraseResponse,
    DSARExportRequest,
    DSARExportResponse,
)

router = APIRouter(prefix="/dsar", tags=["DSAR (PIPL §14.3)"])


def _require_admin(user: AuthUser) -> None:
    """Raise 403 if user is not admin. Used by all DSAR endpoints."""
    if user.role != "admin":
        raise ForbiddenError(
            message="DSAR endpoints require admin role",
            detail={"required_role": "admin", "actual_role": user.role},
        )


def _get_audio_crypto(request: Request) -> AudioCrypto | None:
    """Fetch the AudioCrypto from app.state (None if not configured)."""
    return getattr(request.app.state, "audio_crypto", None)


def _get_audit_writer(request: Request) -> AuditWriter | None:
    """Fetch the AuditWriter from app.state (None if not configured)."""
    return getattr(request.app.state, "audit_writer", None)


async def _write_audit(
    request: Request,
    *,
    tenant_id: str,
    user_id: int,
    action: str,
    target: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    """Write one audit row via AuditWriter if configured, else direct insert.

    The fallback exists for non-lifespan test setups and for deployments
    that disable the AuditWriter. The direct insert re-uses the request's
    session so it commits atomically with the endpoint's work.
    """
    writer = _get_audit_writer(request)
    if writer is not None:
        await writer.record(
            tenant_id=tenant_id,
            user_id=user_id,
            action=action,
            target=target,
            before=before,
            after=after,
        )
        await writer.flush()
        return

    # Direct insert fallback.
    factory = get_session_factory(request)
    from datetime import UTC, datetime

    async with factory() as session:
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                user_id=user_id,
                action=action,
                target=target,
                before_value=before,
                after_value=after,
                occurred_at=datetime.now(UTC),
            )
        )
        await session.commit()


async def _fetch_recording(
    session: AsyncSession, recording_id: int, tenant_id: str
) -> Recording:
    """Async-safe fetch + 404 on miss."""
    result = await session.execute(
        select(Recording).where(
            Recording.id == recording_id,
            Recording.tenant_id == tenant_id,
        )
    )
    rec = result.scalar_one_or_none()
    if rec is None:
        raise RecordingNotFoundError(detail={"recording_id": recording_id})
    return rec


# ------------------------------------------------------------------
# POST /dsar/export/{recording_id}
# ------------------------------------------------------------------


@router.post(
    "/export/{recording_id}",
    response_model=DSARExportResponse,
    summary="Export one recording's PII bundle as ZIP (admin only)",
)
async def export_recording(
    recording_id: int,
    body: DSARExportRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Stream a ZIP containing the recording's PII data.

    Bundle layout::

        audiography_export_{id}/
            manifest.json
            audio/recording.wav           # decrypted, if encrypted_path was set
            transcript/raw.txt
            transcript/scrubbed.txt
            segments.json
            chunks.json
            tags.json
            audit_history.csv
    """
    _require_admin(user)
    factory = get_session_factory(request)
    crypto = _get_audio_crypto(request)

    rec = await _fetch_recording(session, recording_id, user.tenant_id)

    bundle = await _build_export_bundle(factory, rec, crypto)

    # Audit before streaming (so the row is committed even if client disconnects).
    requested_at = datetime.now(UTC)
    await _write_audit(
        request,
        tenant_id=user.tenant_id,
        user_id=user.id,
        action="dsar.export",
        target=f"recording:{recording_id}",
        before={"reason": body.reason},
        after={
            "audio_included": bundle["audio_bytes"] is not None,
            "segment_count": len(bundle["segments"]),
        },
    )

    zip_bytes = _render_export_zip(
        recording_id=recording_id,
        reason=body.reason,
        user_id=user.id,
        requested_at=requested_at,
        bundle=bundle,
    )

    filename = f"audiography_export_{recording_id}_{requested_at.strftime('%Y%m%d%H%M%S')}.zip"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(
        iter([zip_bytes]),
        media_type="application/zip",
        headers=headers,
    )


# ------------------------------------------------------------------
# POST /dsar/erase/{recording_id}
# ------------------------------------------------------------------


@router.post(
    "/erase/{recording_id}",
    response_model=DSAREraseResponse,
    status_code=status.HTTP_200_OK,
    summary="Hard-delete a recording (admin only)",
)
async def erase_recording(
    recording_id: int,
    request: Request,
    user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> DSAREraseResponse:
    """Erase a recording permanently: audio + DB rows + GraphML refs.

    Writes audit_log(action="dsar.erase") BEFORE deleting so the audit row
    survives even if the deletion partially fails.
    """
    _require_admin(user)
    factory = get_session_factory(request)

    rec = await _fetch_recording(session, recording_id, user.tenant_id)

    # Audit FIRST (target row still exists).
    await _write_audit(
        request,
        tenant_id=user.tenant_id,
        user_id=user.id,
        action="dsar.erase",
        target=f"recording:{recording_id}",
        before={
            "path": str(rec.path),
            "audio_encrypted_path": rec.audio_encrypted_path,
        },
        after={},
    )

    # Audio file (encrypted if present, else raw path).
    audio_target = (
        rec.audio_encrypted_path if rec.audio_encrypted_path else str(rec.path)
    )
    if audio_target:
        audio_path = Path(audio_target)
        if audio_path.exists():
            # Audit already records intent; surface nothing to client.
            with contextlib.suppress(OSError):
                audio_path.unlink()

    # DB rows — explicit deletes (mirror RetentionEnforcer strategy).
    from sqlalchemy import delete

    async with factory() as s:
        await s.execute(delete(TagFact).where(TagFact.recording_id == recording_id))
        await s.execute(delete(Chunk).where(Chunk.recording_id == recording_id))
        await s.execute(delete(Segment).where(Segment.recording_id == recording_id))
        await s.execute(delete(Recording).where(Recording.id == recording_id))
        await s.commit()

    # GraphML — best-effort.
    graph_stores: dict[str, Any] | None = getattr(request.app.state, "graph_stores", None)
    if graph_stores is not None:
        gs = graph_stores.get(user.tenant_id)
        if gs is not None:
            try:
                from audio_graphy.core.retention import RetentionEnforcer

                RetentionEnforcer._remove_graph_refs(gs, recording_id)
            except Exception:
                pass

    return DSAREraseResponse(recording_id=recording_id, deleted=True)


# ------------------------------------------------------------------
# GET /dsar/audit
# ------------------------------------------------------------------


@router.get(
    "/audit",
    response_model=AuditLogList,
    summary="Paginated audit log query (admin only)",
)
async def list_audit_logs(
    request: Request,
    recording_id: int | None = Query(None, description="Filter by target recording id"),
    user_id: int | None = Query(None, description="Filter by user_id"),
    action: str | None = Query(None, description="Filter by action code"),
    limit: int = Query(20, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> AuditLogList:
    """Paginated audit log query with optional filters (admin only)."""
    _require_admin(user)

    stmt = select(AuditLog).where(AuditLog.tenant_id == user.tenant_id)
    count_stmt = (
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.tenant_id == user.tenant_id)
    )
    if recording_id is not None:
        like = f"recording:{recording_id}"
        stmt = stmt.where(AuditLog.target == like)
        count_stmt = count_stmt.where(AuditLog.target == like)
    if user_id is not None:
        stmt = stmt.where(AuditLog.user_id == user_id)
        count_stmt = count_stmt.where(AuditLog.user_id == user_id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
        count_stmt = count_stmt.where(AuditLog.action == action)

    total = (await session.execute(count_stmt)).scalar_one()
    stmt = stmt.order_by(AuditLog.occurred_at.desc()).limit(limit).offset(offset)
    rows = list((await session.execute(stmt)).scalars().all())

    return AuditLogList(
        items=[
            AuditLogOut(
                id=r.id,
                tenant_id=str(r.tenant_id),
                user_id=r.user_id,
                action=str(r.action),
                target=str(r.target),
                before_value=r.before_value,
                after_value=r.after_value,
                occurred_at=r.occurred_at,
            )
            for r in rows
        ],
        total=int(total),
        page=offset // limit + 1 if limit else 1,
        page_size=limit,
    )


# ------------------------------------------------------------------
# ZIP helpers
# ------------------------------------------------------------------


async def _build_export_bundle(
    factory: Any,
    rec: Recording,
    crypto: AudioCrypto | None,
) -> dict[str, Any]:
    """Collect all rows + audio bytes for one recording."""
    async with factory() as s:
        seg_result = await s.execute(
            select(Segment).where(Segment.recording_id == rec.id)
        )
        segments = list(seg_result.scalars().all())
        chunk_result = await s.execute(
            select(Chunk).where(Chunk.recording_id == rec.id)
        )
        chunks = list(chunk_result.scalars().all())
        tag_result = await s.execute(
            select(TagFact).where(TagFact.recording_id == rec.id)
        )
        tags = list(tag_result.scalars().all())
        audit_result = await s.execute(
            select(AuditLog)
            .where(AuditLog.target == f"recording:{rec.id}")
            .order_by(AuditLog.occurred_at.desc())
        )
        audit_rows = list(audit_result.scalars().all())

    # Audio bytes — decrypt if encryption is enabled, else read raw path.
    audio_bytes: bytes | None = None
    if rec.audio_encrypted_path:
        enc_path = Path(rec.audio_encrypted_path)
        if enc_path.exists() and crypto is not None:
            tmp_out = enc_path.parent / f"{enc_path.name}.tmp.decrypted"
            res = crypto.decrypt_file(enc_path, tmp_out)
            if res.ok and res.plaintext_path is not None:
                audio_bytes = res.plaintext_path.read_bytes()
                # Best-effort cleanup of the temp plaintext.
                with contextlib.suppress(OSError):
                    res.plaintext_path.unlink()
    elif rec.path:
        raw_path = Path(rec.path)
        if raw_path.exists():
            try:
                audio_bytes = raw_path.read_bytes()
            except OSError:
                audio_bytes = None

    # Build transcripts.
    raw_transcript = "\n".join(s.transcript or "" for s in segments)
    scrubbed_transcript = "\n".join(s.text_scrubbed or "" for s in segments)

    return {
        "audio_bytes": audio_bytes,
        "raw_transcript": raw_transcript,
        "scrubbed_transcript": scrubbed_transcript,
        "segments": segments,
        "chunks": chunks,
        "tags": tags,
        "audit_rows": audit_rows,
        "recording": rec,
    }


def _render_export_zip(
    *,
    recording_id: int,
    reason: str,
    user_id: int,
    requested_at: datetime,
    bundle: dict[str, Any],
) -> bytes:
    """Render an in-memory ZIP for the DSAR export."""
    buf = io.BytesIO()
    prefix = f"audiography_export_{recording_id}"

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest = DSARExportResponse(
            recording_id=recording_id,
            reason=reason,
            requested_by=user_id,
            requested_at=requested_at,
        ).model_dump(mode="json")
        zf.writestr(f"{prefix}/manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

        audio_bytes = bundle.get("audio_bytes")
        if audio_bytes is not None:
            zf.writestr(f"{prefix}/audio/recording.wav", audio_bytes)

        zf.writestr(
            f"{prefix}/transcript/raw.txt", bundle.get("raw_transcript", "") or ""
        )
        zf.writestr(
            f"{prefix}/transcript/scrubbed.txt",
            bundle.get("scrubbed_transcript", "") or "",
        )

        segments_data = [
            {
                "id": s.id,
                "idx": s.idx,
                "start_sec": s.start_sec,
                "end_sec": s.end_sec,
                "transcript": s.transcript,
                "text_scrubbed": s.text_scrubbed,
                "speaker": s.speaker,
                "vad_conf": s.vad_conf,
            }
            for s in bundle["segments"]
        ]
        zf.writestr(
            f"{prefix}/segments.json",
            json.dumps(segments_data, ensure_ascii=False, indent=2, default=str),
        )

        chunks_data = [
            {
                "id": c.id,
                "token_n": c.token_n,
                "content_hash": c.content_hash,
                "segment_ids": c.segment_ids,
                "text": c.text,
            }
            for c in bundle["chunks"]
        ]
        zf.writestr(
            f"{prefix}/chunks.json",
            json.dumps(chunks_data, ensure_ascii=False, indent=2, default=str),
        )

        tags_data = [
            {
                "id": t.id,
                "tag_path": t.tag_path,
                "tag_value": t.tag_value,
                "version": t.version,
                "prompt_version": t.prompt_version,
                "source": t.source,
                "confidence": t.confidence,
                "computed_at": t.computed_at.isoformat() if t.computed_at else None,
            }
            for t in bundle["tags"]
        ]
        zf.writestr(
            f"{prefix}/tags.json",
            json.dumps(tags_data, ensure_ascii=False, indent=2, default=str),
        )

        # Audit history CSV.
        csv_buf = io.StringIO()
        writer = csv.writer(csv_buf)
        writer.writerow(
            ["id", "user_id", "action", "target", "occurred_at", "before", "after"]
        )
        for row in bundle["audit_rows"]:
            writer.writerow(
                [
                    row.id,
                    row.user_id,
                    row.action,
                    row.target,
                    row.occurred_at.isoformat() if row.occurred_at else "",
                    json.dumps(row.before_value or {}, ensure_ascii=False),
                    json.dumps(row.after_value or {}, ensure_ascii=False),
                ]
            )
        zf.writestr(f"{prefix}/audit_history.csv", csv_buf.getvalue())

    return buf.getvalue()


__all__ = ["router"]
