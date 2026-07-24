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

import asyncio
import contextlib
import csv
import inspect
import io
import json
import logging
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, select
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
from audio_graphy.services.reception_erasure import (
    erase_reception_artifacts,
    invalidate_receptions_for_recording,
)

logger = logging.getLogger(__name__)
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


def _remove_graph_refs_after_erase(
    request: Request,
    graph_store: Any,
    recording_id: int,
    tenant_id: str,
) -> None:
    """Remove a recording's graph references with an advanced-graph fallback.

    Lifespan-managed deployments expose the configured ``RetentionEnforcer``;
    reusing it preserves the bi-temporal cascade when that feature is enabled.
    Tests and deployments without encryption may not create an enforcer, so the
    baseline GraphML cleanup is kept here as a dependency-free fallback.
    """
    retention_enforcer = getattr(request.app.state, "retention_enforcer", None)
    if retention_enforcer is not None:
        retention_enforcer._remove_graph_refs(
            graph_store,
            recording_id,
            tenant_id=tenant_id,
        )
        return

    from audio_graphy.core.types import _list_to_str, _str_to_list

    graph = graph_store.graph
    recording_id_text = str(recording_id)
    nodes_to_remove: list[str] = []
    for node_id, attrs in list(graph.nodes(data=True)):
        recording_ids_raw = attrs.get("recording_ids")
        if recording_ids_raw is None:
            continue
        recording_ids = _str_to_list(str(recording_ids_raw))
        if recording_id_text not in recording_ids:
            continue
        if len(recording_ids) <= 1:
            nodes_to_remove.append(node_id)
        else:
            graph.nodes[node_id]["recording_ids"] = _list_to_str(
                [item for item in recording_ids if item != recording_id_text]
            )

    graph.remove_nodes_from(nodes_to_remove)
    invalidate_projection = getattr(graph_store, "invalidate_path_projection", None)
    if callable(invalidate_projection):
        invalidate_projection()


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


async def _get_or_load_graph_store(request: Request, tenant_id: str) -> Any:
    """Resolve a cached graph store or cold-load the tenant's GraphML."""
    configured_factory = getattr(
        request.app.state,
        "graph_store_factory",
        None,
    )
    if callable(configured_factory):
        result = configured_factory(tenant_id)
        store = await result if inspect.isawaitable(result) else result
        if store is not None:
            return store

    graph_stores: dict[str, Any] | None = getattr(
        request.app.state,
        "graph_stores",
        None,
    )
    if graph_stores is None:
        graph_stores = {}
        request.app.state.graph_stores = graph_stores

    store = graph_stores.get(tenant_id)
    if store is not None:
        if not bool(getattr(store, "_loaded", True)):
            await store.load()
        return store

    from audio_graphy.storage.graph_networkx import NetworkXGraphStore

    store = NetworkXGraphStore(
        Path(request.app.state.settings.working_dir),
        tenant_id=tenant_id,
    )
    graph_stores[tenant_id] = store
    try:
        await store.load()
    except Exception:
        if graph_stores.get(tenant_id) is store:
            graph_stores.pop(tenant_id, None)
        raise
    return store


def _get_or_create_file_index(request: Request, tenant_id: str) -> Any:
    """Resolve the tenant FileIndex so DSAR also erases JSON/cache copies."""
    file_indexes: dict[str, Any] | None = getattr(
        request.app.state,
        "file_indexes",
        None,
    )
    if file_indexes is None:
        file_indexes = {}
        request.app.state.file_indexes = file_indexes
    index = file_indexes.get(tenant_id)
    if index is None:
        from audio_graphy.storage.file_index import FileIndex

        index = FileIndex(
            Path(request.app.state.settings.working_dir),
            tenant_id=tenant_id,
        )
        file_indexes[tenant_id] = index
    return index


async def _fetch_recording(session: AsyncSession, recording_id: int, tenant_id: str) -> Recording:
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

    # GraphML is a durable PII-bearing store, not a best-effort cache. Scrub
    # and flush it before deleting the DB row so persistence failures remain
    # visible and retryable through the API.
    graph_store = await _get_or_load_graph_store(request, user.tenant_id)
    _remove_graph_refs_after_erase(
        request,
        graph_store,
        recording_id,
        user.tenant_id,
    )
    await graph_store.save()

    # FileIndex contains transcript/chunk copies and an opaque LLM cache.
    # Clear it before DB deletion so checkpoint failures leave a retryable
    # source row instead of falsely reporting a complete erasure.
    file_index = _get_or_create_file_index(request, user.tenant_id)
    await file_index.erase_recording(recording_id)

    # Both paths may coexist while the indexing pipeline still needs
    # plaintext. DSAR must erase both, not choose one and strand the other.
    audio_targets = {Path(path) for path in (str(rec.path), rec.audio_encrypted_path) if path}
    for audio_path in audio_targets:
        if audio_path.exists():
            # Audit already records intent; surface nothing to client.
            try:
                audio_path.unlink()
            except OSError as exc:
                raise OSError(f"DSAR could not unlink audio for recording {recording_id}") from exc

    # DB rows — explicit deletes (mirror RetentionEnforcer strategy). Any
    # reception derived from this source is invalidated in the same commit so
    # stale transcript labels and timeline coordinates cannot survive DSAR.
    async with factory() as s:
        reception_artifacts = await invalidate_receptions_for_recording(
            s,
            tenant_id=user.tenant_id,
            recording_id=recording_id,
            actor=f"dsar:user:{user.id}",
        )
        await s.execute(
            delete(TagFact).where(
                TagFact.tenant_id == user.tenant_id,
                TagFact.recording_id == recording_id,
            )
        )
        await s.execute(
            delete(Chunk).where(
                Chunk.tenant_id == user.tenant_id,
                Chunk.recording_id == recording_id,
            )
        )
        await s.execute(
            delete(Segment).where(
                Segment.tenant_id == user.tenant_id,
                Segment.recording_id == recording_id,
            )
        )
        await s.execute(
            delete(Recording).where(
                Recording.tenant_id == user.tenant_id,
                Recording.id == recording_id,
            )
        )
        if reception_artifacts:
            await asyncio.to_thread(
                erase_reception_artifacts,
                reception_artifacts,
                allowed_root=Path(request.app.state.settings.working_dir),
            )
        await s.commit()

    # M7 — voiceprint cascade (must run AFTER recordings delete so FK CASCADE
    # has cleaned up speaker_links; we still need to manually handle the
    # speaker_node aggregation decrement).
    try:
        await _cascade_voiceprint_after_erase(factory, recording_id, user.tenant_id)
    except Exception as exc:
        # Voiceprint cascade is best-effort — the main erase has succeeded.
        logger.warning(
            "Voiceprint cascade failed for recording %d tenant=%s: %s",
            recording_id,
            user.tenant_id,
            exc,
        )

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
        select(func.count()).select_from(AuditLog).where(AuditLog.tenant_id == user.tenant_id)
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


async def _build_export_bundle(
    factory: Any,
    rec: Recording,
    crypto: AudioCrypto | None,
) -> dict[str, Any]:
    """Collect all rows + audio bytes for one recording."""
    async with factory() as s:
        seg_result = await s.execute(select(Segment).where(Segment.recording_id == rec.id))
        segments = list(seg_result.scalars().all())
        chunk_result = await s.execute(select(Chunk).where(Chunk.recording_id == rec.id))
        chunks = list(chunk_result.scalars().all())
        tag_result = await s.execute(select(TagFact).where(TagFact.recording_id == rec.id))
        tags = list(tag_result.scalars().all())
        audit_result = await s.execute(
            select(AuditLog)
            .where(AuditLog.target == f"recording:{rec.id}")
            .order_by(AuditLog.occurred_at.desc())
        )
        audit_rows = list(audit_result.scalars().all())

        # M7 — voiceprint metadata (NO raw vectors per PIPL §14.3).
        voiceprints_meta: list[dict[str, Any]] = []
        try:
            from audio_graphy.models.voiceprint_vector import VoiceprintVector

            vp_result = await s.execute(
                select(VoiceprintVector).where(
                    VoiceprintVector.recording_id == rec.id,
                    VoiceprintVector.tenant_id == str(rec.tenant_id),
                )
            )
            for vp in vp_result.scalars().all():
                voiceprints_meta.append(
                    {
                        "voiceprint_id": vp.voiceprint_id,
                        "speaker_entity_id": vp.speaker_entity_id,
                        "duration_sec": vp.duration_sec,
                        "created_at": vp.created_at.isoformat() if vp.created_at else None,
                    }
                )
        except Exception:
            # voiceprint table may be absent (enable_voiceprint=False) — skip.
            pass

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
        "voiceprints_meta": voiceprints_meta,
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

        zf.writestr(f"{prefix}/transcript/raw.txt", bundle.get("raw_transcript", "") or "")
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

        # M7 — voiceprints metadata only (PIPL §14.3: NO raw vectors).
        voiceprints_meta = bundle.get("voiceprints_meta") or []
        zf.writestr(
            f"{prefix}/voiceprints.json",
            json.dumps(voiceprints_meta, ensure_ascii=False, indent=2, default=str),
        )

        # Audit history CSV.
        csv_buf = io.StringIO()
        writer = csv.writer(csv_buf)
        writer.writerow(["id", "user_id", "action", "target", "occurred_at", "before", "after"])
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


# ------------------------------------------------------------------
# M7 helpers
# ------------------------------------------------------------------


async def _cascade_voiceprint_after_erase(
    factory: Any,
    recording_id: int,
    tenant_id: str,
) -> None:
    """M7 PIPL §14.3 cascade after DSAR erase.

    The recordings FK CASCADE already cleaned up:
        - vectors_voiceprint (FK on recording_id CASCADE)
        - speaker_links (FK on recording_id CASCADE)
    What remains is the ``speaker_nodes.recordings_list`` denormalised
    aggregation: we must drop the recording from each affected speaker's
    list and delete the speaker_node when the list becomes empty.
    """
    from sqlalchemy import select

    from audio_graphy.models.speaker_node import SpeakerNode

    async with factory() as s:
        # Find speaker_nodes whose recordings_list still contains this recording.
        # JSON containment is DB-specific; here we load all tenant speakers
        # and filter in Python (typical N ≤ 10^3 per tenant).
        stmt = select(SpeakerNode).where(SpeakerNode.tenant_id == tenant_id)
        nodes = list((await s.execute(stmt)).scalars().all())

        for node in nodes:
            rids = list(node.recordings_list or [])
            if recording_id not in rids:
                continue
            rids.remove(recording_id)
            if not rids:
                await s.delete(node)
            else:
                node.recordings_list = rids
                node.recordings_count = len(rids)
        await s.commit()
