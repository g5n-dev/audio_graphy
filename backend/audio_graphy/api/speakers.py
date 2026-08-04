"""Speakers router — M7 WS-3 T12.

Endpoints (tenant-scoped):
    GET /speakers           — list speaker nodes for current tenant (viewer+)
    GET /speakers/{id}      — single speaker detail (viewer+)

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

from audio_graphy.adapters.protocols import (
    DEFAULT_MAX_SPEAKERS,
    DEFAULT_MIN_SEGMENT_SEC,
    VOICEPRINT_DIM,
)
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
from audio_graphy.models.voiceprint_vector import VoiceprintVector
from audio_graphy.schemas.speakers import (
    RecordingSpeakerListResponse,
    RecordingSpeakerRef,
    SpeakerDetailResponse,
    SpeakerListItem,
    SpeakerListResponse,
    SpeakerRecordingRef,
    VoiceprintPolicyLayer1,
    VoiceprintPolicyLayer2,
    VoiceprintPolicyResponse,
    VoiceprintPolicySampling,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/speakers", tags=["speakers"])
# Recording-scoped speaker lookup belongs under /recordings, not /speakers:
# it answers "who spoke in this recording", not "tell me about this speaker".
recordings_router = APIRouter(prefix="/recordings", tags=["speakers"])


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
    recording_id: int | None = Query(
        default=None,
        description="Only speakers linked to this recording.",
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    _user: AuthUser = Depends(require_role("admin", "inspector", "viewer")),
) -> SpeakerListResponse:
    """List all speaker nodes for the current tenant.

    Role: viewer+. The response carries no biometric data — only the
    truncated voiceprint hash (§17.1), which is already a fingerprint — and
    the reconfirm queue and policy endpoints have always been viewer+, so
    gating the roster higher only meant a viewer could see a merge decision
    without being able to see the speaker it was about. Tenant isolation is
    unchanged; write operations remain inspector+.
    """
    tenant_id = get_tenant_id(request)
    # Built once and applied to both the page and the count: a total derived from
    # len(page) is capped by ``limit``, which tells a paging client the roster ends
    # exactly where its own window does. list_merge_pending below counts properly;
    # this endpoint is the outlier, not the convention.
    filters = [SpeakerNode.tenant_id == tenant_id]
    if speaker_role is not None:
        filters.append(SpeakerNode.speaker_role == speaker_role)
    if ambiguity is not None:
        if ambiguity.lower() == "none":
            filters.append(SpeakerNode.ambiguity_tag.is_(None))
        else:
            filters.append(SpeakerNode.ambiguity_tag == ambiguity)
    if recording_id is not None:
        # Via speaker_links rather than the recordings_list JSON column:
        # the link table is indexed and is the audit trail of record.
        filters.append(
            SpeakerNode.id.in_(
                select(SpeakerLink.canonical_speaker_id).where(
                    SpeakerLink.tenant_id == tenant_id,
                    SpeakerLink.recording_id == recording_id,
                )
            )
        )

    total = (
        await db.execute(select(func.count()).select_from(SpeakerNode).where(*filters))
    ).scalar_one()
    stmt = (
        select(SpeakerNode)
        .where(*filters)
        .order_by(SpeakerNode.recordings_count.desc())
        .limit(limit)
        .offset(offset)
    )

    result = await db.execute(stmt)
    nodes = list(result.scalars().all())
    return SpeakerListResponse(
        items=[_node_to_list_item(n) for n in nodes],
        total=int(total),
    )


@router.get(
    "/merge-pending",
    response_model=SpeakerMergePendingListResponse,
    summary="List pending fuzzy speaker-merge decisions (viewer+)",
)
async def list_merge_pending(
    request: Request,
    status_filter: list[str] | None = Query(
        default=None,
        alias="status",
        description="Filter by status: pending / resolved_inferred / resolved_rejected. "
        "Repeat the parameter to match any of several statuses.",
    ),
    matched_speaker_node_id: int | None = Query(
        default=None,
        description="Filter to rows whose merge target is this SpeakerNode.",
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    _user: AuthUser = Depends(require_role("admin", "inspector", "viewer")),
) -> SpeakerMergePendingListResponse:
    """Return the L8 fuzzy reconfirm queue for this tenant.

    Registered BEFORE ``GET /{speaker_id}`` so the literal path wins
    over the parameterised one (FastAPI matches in registration order).

    ``matched_speaker_node_id`` lets a speaker detail view fetch only its
    own rows, and ``status`` may be repeated to fetch e.g. both resolved
    states at once: filtering client-side after a capped page silently
    drops older rows once the tenant queue exceeds ``limit``.
    """
    tenant_id = get_tenant_id(request)
    stmt = select(SpeakerMergePending).where(SpeakerMergePending.tenant_id == tenant_id)
    count_stmt = (
        select(func.count())
        .select_from(SpeakerMergePending)
        .where(SpeakerMergePending.tenant_id == tenant_id)
    )
    if status_filter:
        stmt = stmt.where(SpeakerMergePending.status.in_(status_filter))
        count_stmt = count_stmt.where(SpeakerMergePending.status.in_(status_filter))
    if matched_speaker_node_id is not None:
        stmt = stmt.where(SpeakerMergePending.matched_speaker_node_id == matched_speaker_node_id)
        count_stmt = count_stmt.where(
            SpeakerMergePending.matched_speaker_node_id == matched_speaker_node_id
        )

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
    "/voiceprint-policy",
    response_model=VoiceprintPolicyResponse,
    summary="Read-only voiceprint sampling & merge policy (viewer+)",
    dependencies=[Depends(require_role("admin", "inspector", "viewer"))],
)
async def get_voiceprint_policy(
    request: Request,
) -> VoiceprintPolicyResponse:
    """Return the speaker-linking thresholds and sampling parameters.

    Pure settings read — no DB access, no biometric data. Registered
    BEFORE ``GET /{speaker_id}`` so the literal path wins (same as
    ``/merge-pending``). Viewer+ so the quality drawer works read-only.

    ``sampling.strategy`` reflects the design spec (candidate voiceprint
    from the speaker's longest diarization segment — speaker_linker.py
    module docstring); ``enable_voiceprint`` reports whether the pipeline
    is live in this deployment.
    """
    settings = request.app.state.settings
    return VoiceprintPolicyResponse(
        enable_voiceprint=bool(settings.enable_voiceprint),
        adapter_voiceprint_mode=str(settings.adapter_voiceprint_mode),
        layer1=VoiceprintPolicyLayer1(
            cosine_threshold=float(settings.voiceprint_cosine_threshold),
            ambiguous_threshold=float(settings.voiceprint_ambiguous_threshold),
        ),
        layer2=VoiceprintPolicyLayer2(
            enabled=bool(settings.enable_speaker_layer2_fuzzy),
            fuzzy_inferred_threshold=float(settings.speaker_fuzzy_inferred_threshold),
            fuzzy_ambiguous_threshold=float(settings.speaker_fuzzy_ambiguous_threshold),
            voiceprint_reconfirm_cosine=float(settings.speaker_fuzzy_voiceprint_reconfirm_cosine),
        ),
        sampling=VoiceprintPolicySampling(
            strategy=str(settings.voiceprint_sampling_strategy),
            # The sampler's own floor, which is stricter than the diarization
            # floor: a segment can survive diarization and still be too short
            # to contribute a trustworthy embedding.
            min_segment_sec=float(settings.voiceprint_sample_min_segment_sec),
            min_total_sec=float(settings.voiceprint_sample_min_total_sec),
            max_segments_per_speaker=int(settings.voiceprint_sample_max_segments),
            # Shared with every VoiceprintAdapter so the drawer cannot drift
            # away from the values the pipeline actually runs with.
            diarization_min_segment_sec=DEFAULT_MIN_SEGMENT_SEC,
            max_speakers=DEFAULT_MAX_SPEAKERS,
            embedding_dim=VOICEPRINT_DIM,
        ),
        retention_cascade=bool(settings.voiceprint_retention_cascade),
    )


@recordings_router.get(
    "/{recording_id}/speakers",
    response_model=RecordingSpeakerListResponse,
    summary="Resolve a recording's diarization labels to speakers (viewer+)",
    dependencies=[Depends(require_role("admin", "inspector", "viewer", "agent"))],
)
async def list_recording_speakers(
    recording_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> RecordingSpeakerListResponse:
    """Map each ``spk_N`` label in this recording to its canonical speaker.

    Transcript and timeline views only have the per-file label; without this
    they can show neither who the speaker is nor how confident that
    attribution was. Viewer+ because it carries no biometric data — just the
    label, the display name, and the quality flags.

    ``agent`` is named explicitly even though it outranks viewer: ``require_role``
    matches names, not levels. Agents are the role that creates streaming
    recordings and the reception workspace is open to them, so excluding them
    here left every timeline line showing a raw ``spk_N`` for exactly the people
    who recorded it. The roster endpoints stay viewer/inspector-only on purpose —
    unlike recordings, they carry no agent-ownership filter.

    Links written before ``source_speaker_label`` existed are omitted: their
    label cannot be reconstructed, and guessing would misattribute speech.
    """
    tenant_id = get_tenant_id(request)
    stmt = (
        select(SpeakerLink, SpeakerNode)
        .join(SpeakerNode, SpeakerNode.id == SpeakerLink.canonical_speaker_id)
        .where(
            SpeakerLink.tenant_id == tenant_id,
            SpeakerLink.recording_id == recording_id,
            SpeakerLink.source_speaker_label.is_not(None),
        )
        .order_by(SpeakerLink.id)
    )
    rows = (await db.execute(stmt)).all()

    # One entry per label; a later link supersedes an earlier one for the
    # same label, which is what a human confirmation overriding a machine
    # guess looks like. A label landing on two *different* speakers is only
    # legitimate in that case, so log it — silently keeping the last one
    # would hide a partial re-run or a genuine linking bug.
    by_label: dict[str, RecordingSpeakerRef] = {}
    for link, node in rows:
        label = str(link.source_speaker_label)
        previous = by_label.get(label)
        if previous is not None and previous.speaker_node_id != int(node.id):
            logger.warning(
                "Recording %d label %s resolves to two speakers (%d via %s, "
                "then %d via %s); reporting the later link",
                recording_id,
                label,
                previous.speaker_node_id,
                previous.strategy,
                int(node.id),
                link.strategy,
            )
        by_label[label] = RecordingSpeakerRef(
            source_speaker_label=label,
            speaker_node_id=int(node.id),
            display_name=str(node.display_name),
            speaker_role=str(node.speaker_role),
            # This link's own tag, never the node's: the node carries whatever
            # its most recent merge decided, which may have been about a
            # completely different recording. Falling back to it would put a
            # warning on speech whose attribution was never in doubt.
            ambiguity_tag=link.ambiguity_tag,
            merge_confidence=(
                float(link.merge_confidence) if link.merge_confidence is not None else None
            ),
            cosine_similarity=(
                float(link.cosine_similarity) if link.cosine_similarity is not None else None
            ),
            strategy=str(link.strategy),
        )

    return RecordingSpeakerListResponse(
        recording_id=recording_id,
        items=sorted(by_label.values(), key=lambda r: r.source_speaker_label),
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
    _user: AuthUser = Depends(require_role("admin", "inspector", "viewer")),
) -> SpeakerDetailResponse:
    """Get a single speaker node + its related recording refs.

    Role: viewer+, matching the roster it is reached from. Returns 404 if
    the speaker does not exist in the caller's tenant (cross-tenant
    isolation enforced).
    """
    tenant_id = get_tenant_id(request)
    node = await db.get(SpeakerNode, speaker_id)
    if node is None or str(node.tenant_id) != tenant_id:
        raise EntityNotFoundError(detail={"speaker_id": speaker_id, "tenant_id": tenant_id})

    # Load related speaker_links (joined via canonical_speaker_id).
    link_stmt = (
        select(SpeakerLink)
        .where(
            SpeakerLink.tenant_id == tenant_id,
            SpeakerLink.canonical_speaker_id == speaker_id,
        )
        .order_by(SpeakerLink.recording_id.desc())
    )
    link_result = await db.execute(link_stmt)
    # The per-recording voiceprint, keyed the way it was written. Reporting the
    # node's own hash on every row instead — as this did — asserts voiceprint
    # evidence for fuzzy and manual links that never had any, directly beside a
    # cosine column that correctly reads "—" for them.
    vector_rows = (
        await db.execute(
            select(
                VoiceprintVector.recording_id,
                VoiceprintVector.voiceprint_id,
                VoiceprintVector.duration_sec,
            ).where(
                VoiceprintVector.tenant_id == tenant_id,
                VoiceprintVector.speaker_entity_id == speaker_id,
            )
        )
    ).all()
    vectors = {int(row.recording_id): row for row in vector_rows}
    related: list[SpeakerRecordingRef] = []
    for link in link_result.scalars():
        vector = vectors.get(int(link.recording_id))
        related.append(
            SpeakerRecordingRef(
                recording_id=int(link.recording_id),
                voiceprint_id=(
                    _voiceprint_short_hash(str(vector.voiceprint_id))
                    if vector is not None
                    else None
                ),
                duration_sec=float(vector.duration_sec) if vector is not None else 0.0,
                strategy=str(link.strategy),
                ambiguity_tag=link.ambiguity_tag,
                cosine_similarity=(
                    float(link.cosine_similarity) if link.cosine_similarity is not None else None
                ),
                merge_confidence=(
                    float(link.merge_confidence) if link.merge_confidence is not None else None
                ),
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
    pending = (
        await db.execute(
            select(SpeakerMergePending)
            .where(
                SpeakerMergePending.id == speaker_id,
                SpeakerMergePending.tenant_id == tenant_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if pending is None:
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
    target = (
        await db.execute(
            select(SpeakerNode)
            .where(
                SpeakerNode.id == target_id,
                SpeakerNode.tenant_id == tenant_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if target is None:
        raise EntityNotFoundError(
            message="Target SpeakerNode not found in this tenant",
            detail={"target_id": target_id, "tenant_id": tenant_id},
        )

    confidence = (
        float(body.voiceprint_score)
        if body.voiceprint_score is not None
        else (
            float(pending.voiceprint_score)
            if pending.voiceprint_score is not None
            else float(pending.fuzzy_score)
        )
    )
    recordings = list(target.recordings_list or [])
    if int(pending.recording_id) not in recordings:
        recordings.append(int(pending.recording_id))
    target.recordings_list = recordings
    target.recordings_count = len(recordings)
    target.total_speech_sec = float(target.total_speech_sec or 0.0) + float(
        pending.candidate_speech_sec or 0.0
    )
    if pending.candidate_first_seen is not None and (
        target.first_seen is None or pending.candidate_first_seen < target.first_seen
    ):
        target.first_seen = pending.candidate_first_seen
    target.merge_confidence = max(float(target.merge_confidence or 0.0), confidence)
    target.merge_strategy = "fuzzy"
    target.ambiguity_tag = None

    # The staged biometric payload is attached only inside this review
    # transaction.  Rejection never reaches this branch.
    if (
        pending.candidate_voiceprint_id
        and pending.candidate_vector_encrypted is not None
        and pending.candidate_encryption_meta is not None
    ):
        existing_vector = (
            await db.execute(
                select(VoiceprintVector).where(
                    VoiceprintVector.tenant_id == tenant_id,
                    VoiceprintVector.voiceprint_id == pending.candidate_voiceprint_id,
                )
            )
        ).scalar_one_or_none()
        if existing_vector is not None and int(existing_vector.speaker_entity_id) != target_id:
            raise ConflictError(
                message="Candidate voiceprint is already attached to another speaker",
                detail={
                    "pending_id": speaker_id,
                    "existing_speaker_id": int(existing_vector.speaker_entity_id),
                },
            )
        if existing_vector is None:
            db.add(
                VoiceprintVector(
                    tenant_id=tenant_id,
                    recording_id=int(pending.recording_id),
                    segment_id=None,
                    speaker_entity_id=target_id,
                    voiceprint_id=str(pending.candidate_voiceprint_id),
                    vector_encrypted=pending.candidate_vector_encrypted,
                    encryption_meta=dict(pending.candidate_encryption_meta),
                    # The audio behind the vector, not the speaker's total
                    # speech: this column ranks representative templates.
                    duration_sec=float(
                        pending.candidate_sampled_sec or pending.candidate_speech_sec or 0.0
                    ),
                    # A human vouched for this attribution, so the vector is
                    # eligible to represent the speaker regardless of the
                    # machine scores that raised the review (ADR-0001).
                    attach_cosine=1.0,
                )
            )

    existing_link_id = (
        await db.execute(
            select(SpeakerLink.id).where(
                SpeakerLink.tenant_id == tenant_id,
                SpeakerLink.canonical_speaker_id == target_id,
                SpeakerLink.recording_id == pending.recording_id,
                SpeakerLink.strategy == "fuzzy",
            )
        )
    ).scalar_one_or_none()
    if existing_link_id is None:
        db.add(
            SpeakerLink(
                tenant_id=tenant_id,
                canonical_speaker_id=target_id,
                source_speaker_id=target_id,
                recording_id=int(pending.recording_id),
                cosine_similarity=(
                    float(body.voiceprint_score)
                    if body.voiceprint_score is not None
                    else (
                        float(pending.voiceprint_score)
                        if pending.voiceprint_score is not None
                        else None
                    )
                ),
                merge_confidence=confidence,
                strategy="fuzzy",
                # Carried from the staged candidate so a confirmed merge maps
                # back to the transcript lines it covers, like Layer-1 links.
                source_speaker_label=pending.candidate_speaker_id,
                ambiguity_tag=None,
            )
        )

    pending.status = "resolved_inferred"
    pending.observation_state = "APPLIED"
    pending.state_version = int(pending.state_version or 1) + 1
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
    pending = (
        await db.execute(
            select(SpeakerMergePending)
            .where(
                SpeakerMergePending.id == speaker_id,
                SpeakerMergePending.tenant_id == tenant_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if pending is None:
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
    pending.observation_state = "REJECTED"
    pending.state_version = int(pending.state_version or 1) + 1
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


__all__ = ["recordings_router", "router"]
