"""Segments router — GET /recordings/{id}/segments.

See: docs/m3-prd.md §4.3.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from audio_graphy.api.deps import get_current_user, get_db
from audio_graphy.auth.middleware import AuthUser
from audio_graphy.auth.tenants import get_agent_filter, get_tenant_id
from audio_graphy.errors import RecordingNotFoundError
from audio_graphy.models.recording import Recording
from audio_graphy.models.segment import Segment
from audio_graphy.schemas.segments import SegmentListResponse, SegmentResponse

router = APIRouter(prefix="/recordings", tags=["segments"])


@router.get(
    "/{recording_id}/segments",
    response_model=SegmentListResponse,
    summary="Get recording segments",
)
async def get_segments(
    recording_id: int,
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _user: AuthUser = Depends(get_current_user),
) -> SegmentListResponse:
    """Get VAD segments for a recording (paginated).

    Cross-tenant access returns 404.
    Agent role: only sees own recordings.
    """
    tenant_id = get_tenant_id(request)
    agent_filter = get_agent_filter(request)

    # Verify recording exists in tenant scope
    rec_stmt = select(Recording).where(
        Recording.id == recording_id,
        Recording.tenant_id == tenant_id,
    )
    if agent_filter is not None:
        rec_stmt = rec_stmt.where(Recording.agent_name == agent_filter)
    rec_result = await db.execute(rec_stmt)
    if rec_result.scalar_one_or_none() is None:
        raise RecordingNotFoundError(detail={"recording_id": recording_id})

    # Count total segments
    count_stmt = select(func.count()).select_from(
        select(Segment)
        .where(
            Segment.recording_id == recording_id,
            Segment.tenant_id == tenant_id,
        )
        .subquery()
    )
    total = (await db.execute(count_stmt)).scalar_one()

    # Paginated query
    offset = (page - 1) * page_size
    stmt = (
        select(Segment)
        .where(
            Segment.recording_id == recording_id,
            Segment.tenant_id == tenant_id,
        )
        .order_by(Segment.idx)
        .offset(offset)
        .limit(page_size)
    )
    result = await db.execute(stmt)
    segments = result.scalars().all()

    items = [
        SegmentResponse(
            id=s.id,
            idx=s.idx,
            start_sec=s.start_sec,
            end_sec=s.end_sec,
            transcript=s.transcript,
            speaker=s.speaker,
            vad_conf=s.vad_conf,
        )
        for s in segments
    ]

    return SegmentListResponse(
        recording_id=recording_id,
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )
