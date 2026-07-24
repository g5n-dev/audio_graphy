"""Speakers router — M7 WS-3 T12.

Endpoints (tenant-scoped, inspector+ read access):
    GET /speakers           — list speaker nodes for current tenant
    GET /speakers/{id}      — single speaker detail (with related recordings)

PIPL §14.3 compliance:
    - Never expose raw voiceprint vectors.
    - ``voiceprint_hash`` field is the first 8 chars of the sha256 hash
      (already a fingerprint, not biometric raw data).
    - Admin-level access only for write operations (deferred to M8).

See: docs/m7-architecture.md §7 (speaker node schema) and §17.1 (ID encoding).

M9 R2 T13 additions (L8 fuzzy reconfirm work-queue):
    GET  /speakers/merge-pending                — viewer+ read
    POST /speakers/{speaker_id}/merge/{target_id}  — inspector/admin
    POST /speakers/{speaker_id}/reject-merge       — inspector/admin
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from audio_graphy.api.deps import get_db
from audio_graphy.api.schemas_m9 import (
    SpeakerConfirmMergeRequest,
    SpeakerConfirmMergeResponse,
    SpeakerMergePendingListItem,
    SpeakerMergePendingListResponse,
    SpeakerRejectMergeRequest,
)
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.roles import (
    require_inspector_or_above,
    require_role,
)
from audio_graphy.auth.tenants import get_tenant_id
from audio_graphy.errors import (
    ConflictError,
    EntityNotFoundError,
)
from audio_graphy.models.speaker_link import SpeakerLink
from audio_graphy.models.speaker_merge_pending import SpeakerMergePending
from audio_graphy.models.speaker_node import SpeakerNode
from audio_graphy.schemas.speakers import (
    SpeakerDetailResponse,
    SpeakerListItem,
    SpeakerListResponse,
    SpeakerRecordingRef,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/speakers", tags=["speakers"])


# ============================================================
# Helpers
# ============================================================


def _voiceprint_short_hash(voiceprint_id: str) -> str:
    """Return ``vp_xxxxxxxx`` (first 8 chars of voiceprint_id).

    Per architecture §17.1: cross-recording display name uses the first
    8 chars of the sha256 hash. The full hash is admin-only.
    """
    if not voiceprint_id:
        return "vp_unknown"
    return f"vp_{voiceprint_id[:8]}"


def _node_to_list_item(node: SpeakerNode) -> SpeakerListItem:
    """Convert a SpeakerNode ORM row to a SpeakerListItem schema instance."""
    return SpeakerListItem(
        id=int(node.id),
        tenant_id=str(node.tenant_id),
        display_name=str(node.display_name),
        voiceprint_hash=_voiceprint_short_hash(str(node.voiceprint_id)),
        speaker_role=str(node.speaker_role),
        recordings_count=int(node.recordings_count or 0),
        first_seen=node.first_seen,
        total_speech_sec=float(node.total_speech_sec or 0.0),
        merge_confidence=float(node.merge_confidence or 0.0),
        merge_strategy=str(node.merge_strategy),
        ambiguity_tag=node.ambiguity_tag,
    )


# ============================================================
# Endpoints
# ============================================================


@router.get(
    "",
    response_model=SpeakerListResponse,
    summary="List speaker nodes (tenant-scoped)",
)
async def list_speakers(
    request: Request,
    speaker_role: str | None = Query(
        default=None,
        description="Filter by role (agent / customer / unknown)",
    ),
    ambiguity: str | None = Query(
        default=None,
        description="Filter by ambiguity_tag (AMBIGUOUS / PENDING_REVIEW). "
        "Pass 'none' to filter to non-ambiguous speakers only.",
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    _user: AuthUser = Depends(require_inspector_or_above()),
) -> SpeakerListResponse:
    """List all speaker nodes for the current tenant.

    Role: inspector+ (per architecture §7.3 — tenant isolation enforced).
    """
    tenant_id = get_tenant_id(request)
    stmt = select(SpeakerNode).where(SpeakerNode.tenant_id == tenant_id)
    if speaker_role is not None:
        stmt = stmt.where(SpeakerNode.speaker_role == speaker_role)
    if ambiguity is not None:
        if ambiguity.lower() == "none":
            stmt = stmt.where(SpeakerNode.ambiguity_tag.is_(None))
        else:
            stmt = stmt.where(SpeakerNode.ambiguity_tag == ambiguity)
    stmt = stmt.order_by(SpeakerNode.recordings_count.desc()).limit(limit).offset(offset)

    result = await db.execute(stmt)
    nodes = list(result.scalars().all())
    return SpeakerListResponse(
        items=[_node_to_list_item(n) for n in nodes],
        total=len(nodes),
    )


@router.get(
    "/merge-pending",
    response_model=SpeakerMergePendingListResponse,
    summary="List pending fuzzy speaker-merge decisions (viewer+)",
)
async def list_merge_pending(
    request: Request,
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description="Filter by status: pending / resolved_inferred / resolved_rejected.",
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    _user: AuthUser = Depends(require_role("admin", "inspector", "viewer")),
) -> SpeakerMergePendingListResponse:
    """Return the L8 fuzzy reconfirm queue for this tenant.

    Registered BEFORE ``GET /{speaker_id}`` so the literal path wins
    over the parameterised one (FastAPI matches in registration order).
    """
    tenant_id = get_tenant_id(request)
    stmt = select(SpeakerMergePending).where(SpeakerMergePending.tenant_id == tenant_id)
    count_stmt = (
        select(func.count())
        .select_from(SpeakerMergePending)
        .where(SpeakerMergePending.tenant_id == tenant_id)
    )
    if status_filter is not None:
        stmt = stmt.where(SpeakerMergePending.status == status_filter)
        count_stmt = count_stmt.where(SpeakerMergePending.status == status_filter)

    total = (await db.execute(count_stmt)).scalar_one()
    stmt = stmt.order_by(SpeakerMergePending.id.desc()).limit(limit).offset(offset)
    rows = list((await db.execute(stmt)).scalars().all())
    return SpeakerMergePendingListResponse(
        items=[_merge_pending_to_item(r) for r in rows],
        total=int(total),
        page=offset // limit + 1 if limit else 1,
        page_size=limit,
    )


@router.get(
    "/{speaker_id}",
    response_model=SpeakerDetailResponse,
    summary="Get speaker detail with related recordings",
)
async def get_speaker(
    speaker_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _user: AuthUser = Depends(require_inspector_or_above()),
) -> SpeakerDetailResponse:
    """Get a single speaker node + its related recording refs.

    Role: inspector+. Returns 404 if the speaker does not exist in the
    caller's tenant (cross-tenant isolation enforced).
    """
    tenant_id = get_tenant_id(request)
    node = await db.get(SpeakerNode, speaker_id)
    if node is None or str(node.tenant_id) != tenant_id:
        raise EntityNotFoundError(detail={"speaker_id": speaker_id, "tenant_id": tenant_id})

    # Load related speaker_links (joined via canonical_speaker_id).
    link_stmt = (
        select(SpeakerLink)
        .where(SpeakerLink.canonical_speaker_id == speaker_id)
        .order_by(SpeakerLink.recording_id.desc())
    )
    link_result = await db.execute(link_stmt)
    related: list[SpeakerRecordingRef] = []
    for link in link_result.scalars():
        related.append(
            SpeakerRecordingRef(
                recording_id=int(link.recording_id),
                voiceprint_id=_voiceprint_short_hash(str(getattr(node, "voiceprint_id", ""))),
                duration_sec=0.0,  # not stored on speaker_link in M7
                strategy=str(link.strategy),
                ambiguity_tag=link.ambiguity_tag,
            )
        )

    list_item = _node_to_list_item(node)
    return SpeakerDetailResponse(
        **list_item.model_dump(),
        recordings_list=list(node.recordings_list or []),
        related_recordings=related,
    )


# ============================================================
# M9 R2 T13 — Speaker merge-pending (L8 fuzzy reconfirm queue)
# ============================================================


def _merge_pending_to_item(row: SpeakerMergePending) -> SpeakerMergePendingListItem:
    """ORM row → response item."""
    return SpeakerMergePendingListItem(
        id=int(row.id),
        recording_id=int(row.recording_id),
        candidate_name=str(row.candidate_name),
        matched_speaker_node_id=int(row.matched_speaker_node_id),
        fuzzy_score=float(row.fuzzy_score),
        status=str(row.status),
        voiceprint_score=(
            float(row.voiceprint_score) if row.voiceprint_score is not None else None
        ),
        resolved_by=row.resolved_by,
        resolved_at=row.resolved_at,
        notes=row.notes,
        created_at=None,
    )


@router.post(
    "/{speaker_id}/merge/{target_id}",
    response_model=SpeakerConfirmMergeResponse,
    status_code=status.HTTP_200_OK,
    summary="Confirm a pending fuzzy merge (inspector/admin)",
)
async def confirm_merge(
    speaker_id: int,
    target_id: int,
    body: SpeakerConfirmMergeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(require_inspector_or_above()),
) -> SpeakerConfirmMergeResponse:
    """Confirm one pending fuzzy merge.

    The pending row (``speaker_id``) is marked ``resolved_inferred`` and
    its ``matched_speaker_node_id`` is updated to ``target_id``. Caller
    must be inspector+ (L10 RBAC).

    Args:
        speaker_id: SpeakerMergePending.id (the pending decision).
        target_id: SpeakerNode.id (the canonical merge target).
    """
    tenant_id = get_tenant_id(request)
    pending = await db.get(SpeakerMergePending, speaker_id)
    if pending is None or str(pending.tenant_id) != tenant_id:
        raise EntityNotFoundError(
            message="SpeakerMergePending row not found in this tenant",
            detail={"pending_id": speaker_id, "tenant_id": tenant_id},
        )
    if pending.status != "pending":
        raise ConflictError(
            message=f"Pending row already resolved: status={pending.status}",
            detail={"pending_id": speaker_id, "current_status": pending.status},
        )
    # Verify target exists in tenant.
    target = await db.get(SpeakerNode, target_id)
    if target is None or str(target.tenant_id) != tenant_id:
        raise EntityNotFoundError(
            message="Target SpeakerNode not found in this tenant",
            detail={"target_id": target_id, "tenant_id": tenant_id},
        )

    pending.status = "resolved_inferred"
    pending.matched_speaker_node_id = target_id
    pending.resolved_by = "human"
    pending.resolved_at = datetime.now(UTC)
    if body.voiceprint_score is not None:
        pending.voiceprint_score = body.voiceprint_score
    if body.notes is not None:
        pending.notes = body.notes
    await db.commit()
    await db.refresh(pending)

    return SpeakerConfirmMergeResponse(
        pending_id=int(pending.id),
        status=str(pending.status),
        resolved_by=str(pending.resolved_by or "human"),
        voiceprint_score=(
            float(pending.voiceprint_score) if pending.voiceprint_score is not None else None
        ),
    )


@router.post(
    "/{speaker_id}/reject-merge",
    response_model=SpeakerConfirmMergeResponse,
    status_code=status.HTTP_200_OK,
    summary="Reject a pending fuzzy merge (inspector/admin)",
)
async def reject_merge(
    speaker_id: int,
    body: SpeakerRejectMergeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(require_inspector_or_above()),
) -> SpeakerConfirmMergeResponse:
    """Reject one pending fuzzy merge.

    The pending row is marked ``resolved_rejected``.
    """
    tenant_id = get_tenant_id(request)
    pending = await db.get(SpeakerMergePending, speaker_id)
    if pending is None or str(pending.tenant_id) != tenant_id:
        raise EntityNotFoundError(
            message="SpeakerMergePending row not found in this tenant",
            detail={"pending_id": speaker_id, "tenant_id": tenant_id},
        )
    if pending.status != "pending":
        raise ConflictError(
            message=f"Pending row already resolved: status={pending.status}",
            detail={"pending_id": speaker_id, "current_status": pending.status},
        )

    pending.status = "resolved_rejected"
    pending.resolved_by = "human"
    pending.resolved_at = datetime.now(UTC)
    if body.notes is not None:
        pending.notes = body.notes
    await db.commit()
    await db.refresh(pending)

    return SpeakerConfirmMergeResponse(
        pending_id=int(pending.id),
        status=str(pending.status),
        resolved_by=str(pending.resolved_by or "human"),
        voiceprint_score=None,
    )


__all__ = ["router"]
