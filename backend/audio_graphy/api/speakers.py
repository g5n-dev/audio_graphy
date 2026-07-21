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
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from audio_graphy.api.deps import get_db
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.roles import require_inspector_or_above
from audio_graphy.auth.tenants import get_tenant_id
from audio_graphy.errors import EntityNotFoundError
from audio_graphy.models.speaker_link import SpeakerLink
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
        raise EntityNotFoundError(
            detail={"speaker_id": speaker_id, "tenant_id": tenant_id}
        )

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
                voiceprint_id=_voiceprint_short_hash(
                    str(getattr(node, "voiceprint_id", ""))
                ),
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


__all__ = ["router"]
