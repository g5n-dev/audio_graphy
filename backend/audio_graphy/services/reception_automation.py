"""Automatic reception discovery and server-owned proposal acceptance."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Never

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.core.pii import scrubbed_segment_text
from audio_graphy.core.reception_merge import (
    MergeFeatureReason,
    ReceptionMerger,
    ReceptionProposal,
    ReceptionTurn,
    RecordingCandidate,
)
from audio_graphy.errors import (
    ConflictError,
    RecordingNotFoundError,
    ValidationError,
)
from audio_graphy.models.reception import ProvenanceEvent, Reception, ReceptionRecording
from audio_graphy.models.recording import Recording
from audio_graphy.models.segment import Segment
from audio_graphy.schemas.receptions import (
    ReceptionCreate,
    ReceptionDiscoveryRequest,
    ReceptionProposalAcceptRequest,
    ReceptionRecordingCreate,
)
from audio_graphy.services.agent_identity import resolve_unique_agent_user_id
from audio_graphy.services.receptions import (
    ReceptionService,
    ReceptionWorkspace,
)

AutomaticCandidateType = Literal[
    "merge_group",
    "recording_split",
    "duration_review",
]
_PROPOSAL_TOKEN_VERSION = 1
_PROPOSAL_TOKEN_TTL_SEC = 15 * 60
_PROPOSAL_TOKEN_CLOCK_SKEW_SEC = 30
_DISCOVERY_MAX_ITEMS = 500
_DISCOVERY_MAX_MERGE_NEIGHBORS = 16
_DISCOVERY_MAX_SEGMENTS_PER_RECORDING = 512
_DISCOVERY_MAX_SEGMENTS = 4_096
_DISCOVERY_MAX_SPLIT_SIGNALS_PER_RECORDING = 20
_DISCOVERY_MAX_SPLIT_SIGNALS = 100


@asynccontextmanager
async def _serialized_write_transaction(
    session: AsyncSession,
) -> AsyncIterator[None]:
    """Use row locks in production and a write lock in SQLite concurrency tests."""

    if session.get_bind().dialect.name != "sqlite":
        async with session.begin():
            yield
        return

    # SQLite ignores SELECT ... FOR UPDATE. BEGIN IMMEDIATE is its closest
    # equivalent: concurrent writers wait before reading the assignment set.
    await session.execute(text("BEGIN IMMEDIATE"))
    try:
        yield
    except BaseException:
        await session.rollback()
        raise
    else:
        await session.commit()


@dataclass(frozen=True, slots=True)
class AutomaticReceptionProposal:
    """One review candidate derived exclusively from persisted evidence."""

    candidate_type: AutomaticCandidateType
    recording_ids: tuple[int, ...]
    decision: Literal["merge", "reject", "needs_review"]
    confidence: float
    reasons: tuple[MergeFeatureReason, ...]
    store_id: str
    started_at: datetime
    ended_at: datetime | None
    duration_available: bool
    split_at_sec: float | None = None
    at_segment_id: int | None = None
    proposal_token: str | None = None
    proposal_expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReceptionDiscoveryResult:
    """Bounded discovery scan result."""

    items: tuple[AutomaticReceptionProposal, ...]
    total: int
    scanned_recordings: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class ReceptionListResult:
    """One page plus the count before pagination."""

    items: tuple[Reception, ...]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True, slots=True)
class ReceptionSplitAcceptance:
    """Atomic result of mapping one immutable recording into two receptions."""

    recording_id: int
    split_at_sec: float
    at_segment_id: int
    source_duration_sec: float
    workspaces: tuple[ReceptionWorkspace, ReceptionWorkspace]
    provenance_event_ids: tuple[int, ...]


class _InvalidProposalTokenError(ValueError):
    """The token is malformed, forged, or scoped to another request."""


class _ExpiredProposalTokenError(ValueError):
    """The signed proposal exceeded its intentionally short review lifetime."""


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    decoded = base64.b64decode(
        value + padding,
        altchars=b"-_",
        validate=True,
    )
    if _base64url_encode(decoded) != value:
        raise ValueError("non-canonical base64url")
    return decoded


def _proposal_signing_key(secret: str) -> bytes:
    """Derive a domain-separated key from the application signing secret."""
    return hmac.new(
        secret.encode("utf-8"),
        b"audio_graphy/reception-split-proposal/v1",
        hashlib.sha256,
    ).digest()


def _segment_snapshot_hash(segments: list[Segment]) -> str:
    """Hash every split-relevant segment field into a compact stable snapshot."""
    snapshot = [
        {
            "end_sec": round(float(segment.end_sec), 6),
            "id": int(segment.id),
            "idx": int(segment.idx),
            "speaker": segment.speaker,
            "start_sec": round(float(segment.start_sec), 6),
            "text": scrubbed_segment_text(
                segment.text_scrubbed,
                segment.transcript,
            ),
            "updated_at": segment.updated_at.isoformat(),
        }
        for segment in segments
    ]
    return hashlib.sha256(
        json.dumps(
            snapshot,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _create_split_proposal_token(
    *,
    secret: str,
    tenant_id: str,
    scenario: str,
    recording: Recording,
    segments: list[Segment],
    duration_sec: float,
    split_at_sec: float,
    at_segment_id: int,
    now: int | None = None,
) -> tuple[str, datetime]:
    if not secret:
        raise ValueError("proposal token secret must not be empty")
    issued_at = int(time.time()) if now is None else int(now)
    expires_at = issued_at + _PROPOSAL_TOKEN_TTL_SEC
    payload = {
        "at_segment_id": at_segment_id,
        "duration_sec": round(duration_sec, 6),
        "exp": expires_at,
        "iat": issued_at,
        "recording_id": recording.id,
        "recording_updated_at": recording.updated_at.isoformat(),
        "scenario": scenario,
        "segment_snapshot": _segment_snapshot_hash(segments),
        "split_at_sec": round(split_at_sec, 6),
        "store_id": recording.store_id,
        "tenant_id": tenant_id,
        "type": "recording_split",
        "v": _PROPOSAL_TOKEN_VERSION,
    }
    encoded_payload = _base64url_encode(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    signature = hmac.new(
        _proposal_signing_key(secret),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return (
        f"{encoded_payload}.{_base64url_encode(signature)}",
        datetime.fromtimestamp(expires_at, tz=UTC),
    )


def _verify_split_proposal_token(
    *,
    secret: str,
    token: str,
    now: int | None = None,
) -> dict[str, Any]:
    try:
        if not secret or not token or len(token) > 2_048 or token.count(".") != 1:
            raise _InvalidProposalTokenError
        encoded_payload, encoded_signature = token.split(".", 1)
        supplied_signature = _base64url_decode(encoded_signature)
        expected_signature = hmac.new(
            _proposal_signing_key(secret),
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise _InvalidProposalTokenError
        payload = json.loads(_base64url_decode(encoded_payload))
        if not isinstance(payload, dict):
            raise _InvalidProposalTokenError

        issued_at = payload.get("iat")
        expires_at = payload.get("exp")
        if (
            payload.get("v") != _PROPOSAL_TOKEN_VERSION
            or payload.get("type") != "recording_split"
            or not isinstance(issued_at, int)
            or not isinstance(expires_at, int)
            or expires_at <= issued_at
            or expires_at - issued_at > _PROPOSAL_TOKEN_TTL_SEC
        ):
            raise _InvalidProposalTokenError
        current_time = int(time.time()) if now is None else int(now)
        if issued_at > current_time + _PROPOSAL_TOKEN_CLOCK_SKEW_SEC:
            raise _InvalidProposalTokenError
        if expires_at <= current_time:
            raise _ExpiredProposalTokenError
        return payload
    except _ExpiredProposalTokenError:
        raise
    except (
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
    ) as exc:
        raise _InvalidProposalTokenError("invalid proposal token") from exc


def _duration_reason(recording_id: int) -> MergeFeatureReason:
    return MergeFeatureReason(
        code="duration_unavailable",
        contribution=-1.0,
        detail=f"recording={recording_id}; no positive persisted segment end time",
        hard_constraint=True,
    )


def _single_recording_reason(recording_id: int) -> MergeFeatureReason:
    return MergeFeatureReason(
        code="single_recording_reception",
        contribution=1.0,
        detail=f"recording={recording_id}",
        hard_constraint=True,
    )


def _merge_neighbor_window_truncated(
    recordings: list[RecordingCandidate],
    *,
    merger: ReceptionMerger,
) -> bool:
    """Report whether a dense time window exceeded the bounded neighbor fan-out."""

    ordered = sorted(
        recordings,
        key=lambda item: (item.started_at, item.ended_at, item.recording_id),
    )
    for index, left in enumerate(ordered):
        first_skipped = index + _DISCOVERY_MAX_MERGE_NEIGHBORS + 1
        if first_skipped >= len(ordered):
            continue
        if ordered[first_skipped].started_at - left.ended_at <= merger.merge_window:
            return True
    return False


class ReceptionAutomationService:
    """Read-only candidate discovery and audited one-click creation."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        reception_service: ReceptionService,
        proposal_secret: str,
    ) -> None:
        if not proposal_secret:
            raise ValueError("proposal_secret must not be empty")
        self._session_factory = session_factory
        self._reception_service = reception_service
        self._proposal_secret = proposal_secret

    async def list_receptions(
        self,
        tenant_id: str,
        *,
        agent_user_id: int | None,
        store_id: str | None,
        status: str | None,
        started_from: datetime | None,
        started_to: datetime | None,
        page: int,
        page_size: int,
    ) -> ReceptionListResult:
        """List only the authenticated tenant/agent slice using stable ordering."""
        timezone_mismatch = (
            started_from is not None
            and started_to is not None
            and (started_from.utcoffset() is None) != (started_to.utcoffset() is None)
        )
        if (
            started_from is not None
            and started_to is not None
            and (timezone_mismatch or started_to < started_from)
        ):
            raise ValidationError(
                "Reception time filters are inconsistent",
                code="RECEPTION_TIME_RANGE_INVALID",
                detail={
                    "started_from": started_from.isoformat(),
                    "started_to": started_to.isoformat(),
                },
            )

        filters = [Reception.tenant_id == tenant_id]
        if agent_user_id is not None:
            filters.append(Reception.agent_user_id == agent_user_id)
        if store_id is not None:
            filters.append(Reception.store_id == store_id)
        if status is not None:
            filters.append(Reception.status == status)
        if started_from is not None:
            filters.append(Reception.started_at >= started_from)
        if started_to is not None:
            filters.append(Reception.started_at <= started_to)

        async with self._session_factory() as session:
            total_result = await session.execute(select(func.count(Reception.id)).where(*filters))
            total = int(total_result.scalar_one())
            rows_result = await session.execute(
                select(Reception)
                .where(*filters)
                .order_by(Reception.started_at.desc(), Reception.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            rows = tuple(rows_result.scalars().all())

        return ReceptionListResult(
            items=rows,
            total=total,
            page=page,
            page_size=page_size,
        )

    async def discover(
        self,
        tenant_id: str,
        body: ReceptionDiscoveryRequest,
        *,
        merger: ReceptionMerger | None = None,
    ) -> ReceptionDiscoveryResult:
        """Find short-fragment groups and long-recording split signals."""
        active_assignment = (
            select(ReceptionRecording.id)
            .join(
                Reception,
                Reception.id == ReceptionRecording.reception_id,
            )
            .where(
                ReceptionRecording.tenant_id == tenant_id,
                ReceptionRecording.recording_id == Recording.id,
                Reception.tenant_id == tenant_id,
                Reception.status != "archived",
            )
            .exists()
        )
        async with self._session_factory() as session:
            recording_result = await session.execute(
                select(Recording)
                .where(
                    Recording.tenant_id == tenant_id,
                    Recording.store_id == body.store_id,
                    Recording.status != "archived",
                    Recording.recorded_at.is_not(None),
                    Recording.recorded_at >= body.recorded_from,
                    Recording.recorded_at <= body.recorded_to,
                    ~active_assignment,
                )
                .order_by(Recording.recorded_at, Recording.id)
                .limit(body.limit + 1)
            )
            loaded = list(recording_result.scalars().all())
            recording_scan_truncated = len(loaded) > body.limit
            recordings = loaded[: body.limit]
            recording_ids = [recording.id for recording in recordings]

            durations: dict[int, float] = {}
            segment_counts: dict[int, int] = {}
            if recording_ids:
                duration_result = await session.execute(
                    select(
                        Segment.recording_id,
                        func.max(Segment.end_sec),
                        func.count(Segment.id),
                    )
                    .where(
                        Segment.tenant_id == tenant_id,
                        Segment.recording_id.in_(recording_ids),
                    )
                    .group_by(Segment.recording_id)
                )
                duration_rows = duration_result.all()
                durations = {
                    int(recording_id): float(duration)
                    for recording_id, duration, _count in duration_rows
                    if duration is not None and float(duration) > 0
                }
                segment_counts = {
                    int(recording_id): int(count)
                    for recording_id, _duration, count in duration_rows
                }

            segments_by_recording: dict[int, list[Segment]] = {}
            long_recording_ids = {
                recording_id
                for recording_id, duration in durations.items()
                if duration > body.short_recording_max_sec
            }
            bounded_long_recording_ids: list[int] = []
            remaining_segment_budget = _DISCOVERY_MAX_SEGMENTS
            segment_scan_truncated = False
            for recording in recordings:
                if recording.id not in long_recording_ids:
                    continue
                segment_count = segment_counts.get(recording.id, 0)
                if (
                    segment_count > _DISCOVERY_MAX_SEGMENTS_PER_RECORDING
                    or segment_count > remaining_segment_budget
                ):
                    segment_scan_truncated = True
                    continue
                bounded_long_recording_ids.append(recording.id)
                remaining_segment_budget -= segment_count

            if bounded_long_recording_ids:
                segment_result = await session.execute(
                    select(Segment)
                    .where(
                        Segment.tenant_id == tenant_id,
                        Segment.recording_id.in_(bounded_long_recording_ids),
                    )
                    .order_by(Segment.recording_id, Segment.start_sec, Segment.id)
                )
                for segment in segment_result.scalars().all():
                    segments_by_recording.setdefault(
                        segment.recording_id,
                        [],
                    ).append(segment)

        engine = merger or ReceptionMerger()
        by_string_id: dict[str, tuple[Recording, float]] = {}
        short_candidates: list[RecordingCandidate] = []
        items: list[AutomaticReceptionProposal] = []
        split_signals_truncated = False
        remaining_split_signal_budget = _DISCOVERY_MAX_SPLIT_SIGNALS

        for recording in recordings:
            assert recording.recorded_at is not None
            duration = durations.get(recording.id)
            if duration is None:
                items.append(
                    AutomaticReceptionProposal(
                        candidate_type="duration_review",
                        recording_ids=(recording.id,),
                        decision="needs_review",
                        confidence=0.0,
                        reasons=(_duration_reason(recording.id),),
                        store_id=recording.store_id,
                        started_at=recording.recorded_at,
                        ended_at=None,
                        duration_available=False,
                    )
                )
                continue

            by_string_id[str(recording.id)] = (recording, duration)
            if duration <= body.short_recording_max_sec:
                short_candidates.append(
                    RecordingCandidate(
                        recording_id=str(recording.id),
                        tenant_id=tenant_id,
                        store_id=recording.store_id,
                        started_at=recording.recorded_at,
                        ended_at=recording.recorded_at + timedelta(seconds=duration),
                        agent_id=recording.agent_name,
                        customer_voiceprint_id=recording.customer_hash,
                    )
                )
                continue

            recording_segments = segments_by_recording.get(recording.id, [])
            if not recording_segments:
                continue
            turns = [
                ReceptionTurn(
                    segment_id=str(segment.id),
                    start_sec=segment.start_sec,
                    end_sec=segment.end_sec,
                    transcript=scrubbed_segment_text(
                        segment.text_scrubbed,
                        segment.transcript,
                    ),
                    speaker=segment.speaker,
                    customer_voiceprint_id=recording.customer_hash,
                )
                for segment in recording_segments
            ]
            signal_limit = min(
                _DISCOVERY_MAX_SPLIT_SIGNALS_PER_RECORDING,
                remaining_split_signal_budget,
            )
            if signal_limit <= 0:
                split_signals_truncated = True
                continue
            signals = engine.detect_recording_splits(
                turns,
                max_signals=signal_limit + 1,
            )
            if len(signals) > signal_limit:
                split_signals_truncated = True
                signals = signals[:signal_limit]
            remaining_split_signal_budget -= len(signals)
            for signal in signals:
                proposal_token, proposal_expires_at = _create_split_proposal_token(
                    secret=self._proposal_secret,
                    tenant_id=tenant_id,
                    scenario=body.scenario,
                    recording=recording,
                    segments=recording_segments,
                    duration_sec=duration,
                    split_at_sec=signal.at_sec,
                    at_segment_id=int(signal.at_segment_id),
                )
                items.append(
                    AutomaticReceptionProposal(
                        candidate_type="recording_split",
                        recording_ids=(recording.id,),
                        decision="needs_review",
                        confidence=signal.confidence,
                        reasons=signal.reasons,
                        store_id=recording.store_id,
                        started_at=recording.recorded_at,
                        ended_at=recording.recorded_at + timedelta(seconds=duration),
                        duration_available=True,
                        split_at_sec=signal.at_sec,
                        at_segment_id=int(signal.at_segment_id),
                        proposal_token=proposal_token,
                        proposal_expires_at=proposal_expires_at,
                    )
                )

        merge_window_truncated = _merge_neighbor_window_truncated(
            short_candidates,
            merger=engine,
        )
        for proposal in engine.propose_groups(
            short_candidates,
            max_neighbors=_DISCOVERY_MAX_MERGE_NEIGHBORS,
        ):
            member_rows = [by_string_id[recording_id] for recording_id in proposal.recording_ids]
            items.append(
                AutomaticReceptionProposal(
                    candidate_type="merge_group",
                    recording_ids=tuple(
                        int(recording_id) for recording_id in proposal.recording_ids
                    ),
                    decision=proposal.decision,
                    confidence=proposal.confidence,
                    reasons=proposal.reasons,
                    store_id=body.store_id,
                    started_at=min(
                        recording.recorded_at
                        for recording, _duration in member_rows
                        if recording.recorded_at is not None
                    ),
                    ended_at=max(
                        recording.recorded_at + timedelta(seconds=duration)
                        for recording, duration in member_rows
                        if recording.recorded_at is not None
                    ),
                    duration_available=True,
                )
            )

        candidate_priority = {
            "merge_group": 0,
            "recording_split": 1,
            "duration_review": 2,
        }
        items.sort(
            key=lambda item: (
                item.started_at,
                candidate_priority[item.candidate_type],
                item.recording_ids,
            )
        )
        total = len(items)
        return ReceptionDiscoveryResult(
            items=tuple(items[:_DISCOVERY_MAX_ITEMS]),
            total=total,
            scanned_recordings=len(recordings),
            truncated=(
                recording_scan_truncated
                or segment_scan_truncated
                or split_signals_truncated
                or merge_window_truncated
                or total > _DISCOVERY_MAX_ITEMS
            ),
        )

    @staticmethod
    def _split_external_session_ids(
        external_session_id: str | None,
    ) -> tuple[str | None, str | None]:
        if external_session_id is None:
            return None, None
        # ``Reception.external_session_id`` is tenant-unique and capped at 128
        # characters. Keep the human supplied stem while making both children
        # independently addressable.
        stem = external_session_id[:120]
        return f"{stem}:split-1", f"{stem}:split-2"

    async def _accept_recording_split(
        self,
        tenant_id: str,
        body: ReceptionProposalAcceptRequest,
        *,
        actor: str,
        merger: ReceptionMerger | None,
    ) -> ReceptionSplitAcceptance:
        """Revalidate and atomically map one immutable source into two receptions."""
        recording_id = body.recording_ids[0]
        assert body.split_at_sec is not None
        assert body.at_segment_id is not None
        assert body.proposal_token is not None

        engine = merger or ReceptionMerger()
        child_reception_ids: tuple[int, int]
        provenance_event_ids: tuple[int, ...]
        source_duration_sec: float

        async with self._session_factory() as session, session.begin():
            try:
                token_payload = _verify_split_proposal_token(
                    secret=self._proposal_secret,
                    token=body.proposal_token,
                )
            except _ExpiredProposalTokenError as exc:
                raise ConflictError(
                    "The recording split proposal has expired",
                    code="RECEPTION_PROPOSAL_STALE",
                    detail={"recording_id": recording_id, "reason": "token_expired"},
                ) from exc
            except _InvalidProposalTokenError as exc:
                raise ValidationError(
                    "The recording split proposal token is invalid",
                    code="RECEPTION_PROPOSAL_TOKEN_INVALID",
                    detail={"recording_id": recording_id},
                ) from exc

            if (
                token_payload.get("tenant_id") != tenant_id
                or token_payload.get("recording_id") != recording_id
                or token_payload.get("scenario") != body.scenario
            ):
                raise ValidationError(
                    "The recording split proposal token does not match this request",
                    code="RECEPTION_PROPOSAL_TOKEN_MISMATCH",
                    detail={"recording_id": recording_id},
                )

            recording_result = await session.execute(
                select(Recording)
                .where(
                    Recording.tenant_id == tenant_id,
                    Recording.id == recording_id,
                )
                .with_for_update()
            )
            recording = recording_result.scalar_one_or_none()
            if recording is None:
                raise RecordingNotFoundError(
                    detail={"recording_ids": [recording_id]},
                )
            if recording.status == "archived":
                raise ConflictError(
                    "The recording split proposal is no longer active",
                    code="RECEPTION_PROPOSAL_STALE",
                    detail={"recording_id": recording_id, "reason": "recording_archived"},
                )

            assignment_result = await session.execute(
                select(
                    ReceptionRecording.recording_id,
                    ReceptionRecording.reception_id,
                )
                .join(
                    Reception,
                    Reception.id == ReceptionRecording.reception_id,
                )
                .where(
                    ReceptionRecording.tenant_id == tenant_id,
                    ReceptionRecording.recording_id == recording_id,
                    Reception.tenant_id == tenant_id,
                    Reception.status != "archived",
                )
                .with_for_update()
            )
            assignments = [
                {
                    "recording_id": int(assigned_recording_id),
                    "reception_id": int(reception_id),
                }
                for assigned_recording_id, reception_id in assignment_result.all()
            ]
            if assignments:
                raise ConflictError(
                    "The recording already belongs to an active reception",
                    code="RECORDING_ALREADY_ASSIGNED",
                    detail={"assignments": assignments},
                )

            segment_result = await session.execute(
                select(Segment)
                .where(
                    Segment.tenant_id == tenant_id,
                    Segment.recording_id == recording_id,
                )
                .order_by(Segment.start_sec, Segment.id)
                .with_for_update()
            )
            segments = list(segment_result.scalars().all())
            source_duration_sec = max(
                (float(segment.end_sec) for segment in segments),
                default=0.0,
            )
            if source_duration_sec <= 0:
                raise ValidationError(
                    "Recording duration is unavailable; index the recording first",
                    code="RECORDING_DURATION_UNAVAILABLE",
                    detail={"recording_ids": [recording_id]},
                )
            if not 0 < body.split_at_sec < source_duration_sec:
                raise ValidationError(
                    "The recording split boundary is outside the source duration",
                    code="RECEPTION_SPLIT_BOUNDARY_INVALID",
                    detail={
                        "recording_id": recording_id,
                        "split_at_sec": body.split_at_sec,
                        "source_duration_sec": source_duration_sec,
                    },
                )
            if recording.recorded_at is None:
                raise ValidationError(
                    "Recording start time is unavailable",
                    code="RECORDING_TIME_UNAVAILABLE",
                    detail={"recording_ids": [recording_id]},
                )

            request_matches_token = token_payload.get(
                "at_segment_id"
            ) == body.at_segment_id and token_payload.get("split_at_sec") == round(
                body.split_at_sec, 6
            )
            current_snapshot_matches = (
                token_payload.get("duration_sec") == round(source_duration_sec, 6)
                and token_payload.get("recording_updated_at") == recording.updated_at.isoformat()
                and token_payload.get("store_id") == recording.store_id
                and token_payload.get("segment_snapshot") == _segment_snapshot_hash(segments)
            )
            if not request_matches_token:
                raise ValidationError(
                    "The recording split boundary does not match its signed proposal",
                    code="RECEPTION_PROPOSAL_TOKEN_MISMATCH",
                    detail={"recording_id": recording_id},
                )
            if not current_snapshot_matches:
                raise ConflictError(
                    "The recording split proposal is stale",
                    code="RECEPTION_PROPOSAL_STALE",
                    detail={"recording_id": recording_id, "reason": "source_changed"},
                )

            turns = [
                ReceptionTurn(
                    segment_id=str(segment.id),
                    start_sec=segment.start_sec,
                    end_sec=segment.end_sec,
                    transcript=scrubbed_segment_text(
                        segment.text_scrubbed,
                        segment.transcript,
                    ),
                    speaker=segment.speaker,
                    customer_voiceprint_id=recording.customer_hash,
                )
                for segment in segments
            ]
            current_signal = next(
                (
                    signal
                    for signal in engine.detect_recording_splits(turns)
                    if int(signal.at_segment_id) == body.at_segment_id
                    and abs(signal.at_sec - body.split_at_sec) <= 0.001
                ),
                None,
            )
            if current_signal is None:
                raise ConflictError(
                    "The recording split boundary is no longer a current proposal",
                    code="RECEPTION_PROPOSAL_STALE",
                    detail={"recording_id": recording_id, "reason": "boundary_changed"},
                )

            child_external_ids = self._split_external_session_ids(
                body.external_session_id,
            )
            external_ids = [
                external_id for external_id in child_external_ids if external_id is not None
            ]
            if external_ids:
                duplicate_result = await session.execute(
                    select(Reception.external_session_id)
                    .where(
                        Reception.tenant_id == tenant_id,
                        Reception.external_session_id.in_(external_ids),
                    )
                    .with_for_update()
                )
                duplicates = sorted(
                    str(external_id)
                    for external_id in duplicate_result.scalars().all()
                    if external_id is not None
                )
                if duplicates:
                    raise ConflictError(
                        "Reception external session already exists",
                        code="DUPLICATE_RECEPTION_SESSION",
                        detail={"external_session_ids": duplicates},
                    )

            child_spans = (
                (0.0, body.split_at_sec),
                (body.split_at_sec, source_duration_sec),
            )
            reason_payload = [
                {
                    "code": reason.code,
                    "contribution": reason.contribution,
                    "detail": reason.detail,
                    "hard_constraint": reason.hard_constraint,
                }
                for reason in current_signal.reasons
            ]
            token_hash = hashlib.sha256(body.proposal_token.encode("utf-8")).hexdigest()
            agent_user_id = await resolve_unique_agent_user_id(
                session,
                tenant_id=tenant_id,
                agent_name=recording.agent_name,
            )
            receptions: list[Reception] = []
            for (source_start, source_end), external_id in zip(
                child_spans,
                child_external_ids,
                strict=True,
            ):
                reception = Reception(
                    tenant_id=tenant_id,
                    external_session_id=external_id,
                    scenario=body.scenario,
                    store_id=recording.store_id,
                    agent_name=recording.agent_name,
                    agent_user_id=agent_user_id,
                    customer_hash=recording.customer_hash,
                    status="confirmed",
                    merge_mode="logical",
                    merge_confidence=current_signal.confidence,
                    started_at=recording.recorded_at + timedelta(seconds=source_start),
                    ended_at=recording.recorded_at + timedelta(seconds=source_end),
                    merged_audio_path=None,
                    version=1,
                )
                session.add(reception)
                receptions.append(reception)
            await session.flush()
            child_reception_ids = (receptions[0].id, receptions[1].id)

            mappings: list[ReceptionRecording] = []
            for part_index, (reception, (source_start, source_end)) in enumerate(
                zip(receptions, child_spans, strict=True),
                start=1,
            ):
                mapping = ReceptionRecording(
                    tenant_id=tenant_id,
                    reception_id=reception.id,
                    recording_id=recording_id,
                    sequence_no=0,
                    timeline_start_sec=0.0,
                    timeline_end_sec=source_end - source_start,
                    source_start_sec=source_start,
                    source_end_sec=source_end,
                    gap_before_sec=0.0,
                    decision_source="manual",
                    merge_confidence=current_signal.confidence,
                    merge_reasons={
                        "accepted_by": actor,
                        "at_segment_id": body.at_segment_id,
                        "candidate_type": "recording_split",
                        "part_index": part_index,
                        "proposal_decision": "needs_review",
                        "proposal_token_sha256": token_hash,
                        "reasons": reason_payload,
                        "server_constructed_timeline": True,
                        "source_recording_immutable": True,
                        "split_at_sec": body.split_at_sec,
                    },
                )
                session.add(mapping)
                mappings.append(mapping)
            await session.flush()

            occurred_at = datetime.now(UTC)
            source_snapshot_ref = f"{recording_id}@{str(token_payload['segment_snapshot'])[:16]}"
            recording_event = ProvenanceEvent(
                tenant_id=tenant_id,
                reception_id=None,
                object_type="recording",
                object_ref=str(recording_id),
                event_type="split",
                actor=actor,
                algorithm_version="recording-split-v1",
                parent_refs=[
                    {
                        "object_ref": source_snapshot_ref,
                        "object_type": "recording_snapshot",
                        "segment_snapshot": token_payload["segment_snapshot"],
                    }
                ],
                evidence_refs=[
                    {
                        "kind": "audio",
                        "recording_id": recording_id,
                        "source_end_sec": source_end,
                        "source_start_sec": source_start,
                    }
                    for source_start, source_end in child_spans
                ],
                payload={
                    "at_segment_id": body.at_segment_id,
                    "child_reception_ids": list(child_reception_ids),
                    "proposal_token_sha256": token_hash,
                    "source_duration_sec": source_duration_sec,
                    "source_recording_immutable": True,
                    "split_at_sec": body.split_at_sec,
                },
                occurred_at=occurred_at,
            )
            session.add(recording_event)
            child_events: list[ProvenanceEvent] = []
            for part_index, (reception, mapping) in enumerate(
                zip(receptions, mappings, strict=True),
                start=1,
            ):
                sibling_id = child_reception_ids[1 if part_index == 1 else 0]
                child_event = ProvenanceEvent(
                    tenant_id=tenant_id,
                    reception_id=reception.id,
                    object_type="reception",
                    object_ref=str(reception.id),
                    event_type="split",
                    actor=actor,
                    algorithm_version="recording-split-v1",
                    parent_refs=[
                        {
                            "object_ref": str(recording_id),
                            "object_type": "recording",
                        }
                    ],
                    evidence_refs=[
                        {
                            "kind": "audio",
                            "recording_id": recording_id,
                            "source_end_sec": mapping.source_end_sec,
                            "source_start_sec": mapping.source_start_sec,
                            "timeline_end_sec": mapping.timeline_end_sec,
                            "timeline_start_sec": mapping.timeline_start_sec,
                        }
                    ],
                    payload={
                        "agent_user_id": agent_user_id,
                        "at_segment_id": body.at_segment_id,
                        "part_index": part_index,
                        "sibling_reception_id": sibling_id,
                        "source_recording_immutable": True,
                        "split_at_sec": body.split_at_sec,
                        "version": 1,
                    },
                    occurred_at=occurred_at,
                )
                session.add(child_event)
                child_events.append(child_event)
            await session.flush()
            provenance_event_ids = (
                recording_event.id,
                child_events[0].id,
                child_events[1].id,
            )

        workspaces = (
            await self._reception_service.get_workspace(
                child_reception_ids[0],
                tenant_id,
            ),
            await self._reception_service.get_workspace(
                child_reception_ids[1],
                tenant_id,
            ),
        )
        return ReceptionSplitAcceptance(
            recording_id=recording_id,
            split_at_sec=body.split_at_sec,
            at_segment_id=body.at_segment_id,
            source_duration_sec=source_duration_sec,
            workspaces=workspaces,
            provenance_event_ids=provenance_event_ids,
        )

    async def accept(
        self,
        tenant_id: str,
        body: ReceptionProposalAcceptRequest,
        *,
        actor: str,
        merger: ReceptionMerger | None = None,
    ) -> ReceptionWorkspace | ReceptionSplitAcceptance:
        """Re-evaluate a proposal, derive geometry, then create the reception."""
        if body.candidate_type == "recording_split":
            return await self._accept_recording_split(
                tenant_id,
                body,
                actor=actor,
                merger=merger,
            )

        requested_ids = list(body.recording_ids)
        reception_id: int
        try:
            async with (
                self._session_factory() as session,
                _serialized_write_transaction(session),
            ):
                recording_result = await session.execute(
                    select(Recording)
                    .where(
                        Recording.tenant_id == tenant_id,
                        Recording.id.in_(requested_ids),
                    )
                    .order_by(Recording.id)
                    .with_for_update()
                )
                by_id = {recording.id: recording for recording in recording_result.scalars().all()}
                missing = [
                    recording_id for recording_id in requested_ids if recording_id not in by_id
                ]
                if missing:
                    raise RecordingNotFoundError(
                        detail={"recording_ids": missing},
                    )

                assignment_result = await session.execute(
                    select(
                        ReceptionRecording.recording_id,
                        ReceptionRecording.reception_id,
                    )
                    .join(
                        Reception,
                        Reception.id == ReceptionRecording.reception_id,
                    )
                    .where(
                        ReceptionRecording.tenant_id == tenant_id,
                        ReceptionRecording.recording_id.in_(requested_ids),
                        Reception.tenant_id == tenant_id,
                        Reception.status != "archived",
                    )
                    .order_by(
                        ReceptionRecording.recording_id,
                        ReceptionRecording.reception_id,
                    )
                    .with_for_update()
                )
                assignments = [
                    (int(recording_id), int(assigned_reception_id))
                    for recording_id, assigned_reception_id in assignment_result.all()
                ]
                if assignments:
                    idempotent_id = await self._idempotent_merge_group_reception_id(
                        session,
                        tenant_id=tenant_id,
                        body=body,
                        assignments=assignments,
                    )
                    if idempotent_id is None:
                        self._raise_recording_assignment_conflict(assignments)
                    reception_id = idempotent_id
                else:
                    segment_result = await session.execute(
                        select(Segment.recording_id, Segment.end_sec)
                        .where(
                            Segment.tenant_id == tenant_id,
                            Segment.recording_id.in_(requested_ids),
                        )
                        .order_by(Segment.recording_id, Segment.id)
                        .with_for_update()
                    )
                    durations: dict[int, float] = {}
                    for recording_id, end_sec in segment_result.all():
                        parsed_id = int(recording_id)
                        durations[parsed_id] = max(
                            durations.get(parsed_id, 0.0),
                            float(end_sec),
                        )

                    create_body = self._build_merge_group_create_body(
                        tenant_id=tenant_id,
                        body=body,
                        actor=actor,
                        by_id=by_id,
                        durations=durations,
                        merger=merger,
                    )
                    reception_id = await self._persist_merge_group_reception(
                        session,
                        tenant_id=tenant_id,
                        body=create_body,
                        actor=actor,
                    )
        except IntegrityError as exc:
            if body.external_session_id is not None and await self._external_session_exists(
                tenant_id,
                body.external_session_id,
            ):
                raise ConflictError(
                    "Reception external session already exists",
                    code="DUPLICATE_RECEPTION_SESSION",
                    detail={"external_session_id": body.external_session_id},
                ) from exc
            raise

        return await self._reception_service.get_workspace(reception_id, tenant_id)

    async def _idempotent_merge_group_reception_id(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        body: ReceptionProposalAcceptRequest,
        assignments: list[tuple[int, int]],
    ) -> int | None:
        """Resolve an exact external-session replay, never a keyless retry."""

        if body.external_session_id is None:
            return None
        reception_ids = {reception_id for _recording_id, reception_id in assignments}
        if len(reception_ids) != 1:
            return None
        reception_id = next(iter(reception_ids))
        reception = (
            await session.execute(
                select(Reception)
                .where(
                    Reception.id == reception_id,
                    Reception.tenant_id == tenant_id,
                    Reception.status != "archived",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            reception is None
            or reception.external_session_id != body.external_session_id
            or reception.scenario != body.scenario
            or reception.merge_mode != body.merge_mode
        ):
            return None
        mapped_ids = {
            int(recording_id)
            for recording_id in (
                await session.execute(
                    select(ReceptionRecording.recording_id).where(
                        ReceptionRecording.tenant_id == tenant_id,
                        ReceptionRecording.reception_id == reception_id,
                    )
                )
            ).scalars()
        }
        return reception_id if mapped_ids == set(body.recording_ids) else None

    @staticmethod
    def _raise_recording_assignment_conflict(
        assignments: list[tuple[int, int]],
    ) -> Never:
        raise ConflictError(
            "One or more recordings already belong to an active reception",
            code="RECORDING_ALREADY_ASSIGNED",
            detail={
                "assignments": [
                    {
                        "recording_id": recording_id,
                        "reception_id": reception_id,
                    }
                    for recording_id, reception_id in assignments
                ]
            },
        )

    @staticmethod
    def _build_merge_group_create_body(
        *,
        tenant_id: str,
        body: ReceptionProposalAcceptRequest,
        actor: str,
        by_id: dict[int, Recording],
        durations: dict[int, float],
        merger: ReceptionMerger | None,
    ) -> ReceptionCreate:
        requested_ids = list(body.recording_ids)
        unavailable_duration = [
            recording_id for recording_id in requested_ids if durations.get(recording_id, 0) <= 0
        ]
        if unavailable_duration:
            raise ValidationError(
                "Recording duration is unavailable; index the recording first",
                code="RECORDING_DURATION_UNAVAILABLE",
                detail={"recording_ids": unavailable_duration},
            )
        unavailable_time = [
            recording_id
            for recording_id in requested_ids
            if by_id[recording_id].recorded_at is None
        ]
        if unavailable_time:
            raise ValidationError(
                "Recording start time is unavailable",
                code="RECORDING_TIME_UNAVAILABLE",
                detail={"recording_ids": unavailable_time},
            )
        stores = {by_id[recording_id].store_id for recording_id in requested_ids}
        if len(stores) != 1:
            raise ValidationError(
                "A reception proposal cannot span stores",
                code="RECEPTION_STORE_MISMATCH",
                detail={"recording_ids": requested_ids},
            )

        ordered = sorted(
            (by_id[recording_id] for recording_id in requested_ids),
            key=lambda recording: (recording.recorded_at, recording.id),
        )
        accepted_proposal: ReceptionProposal | None
        if len(ordered) == 1:
            recording = ordered[0]
            accepted_proposal = ReceptionProposal(
                recording_ids=(str(recording.id),),
                decision="merge",
                confidence=1.0,
                reasons=(_single_recording_reason(recording.id),),
                manual_override=True,
            )
        else:
            engine = merger or ReceptionMerger()
            candidates = [
                RecordingCandidate(
                    recording_id=str(recording.id),
                    tenant_id=tenant_id,
                    store_id=recording.store_id,
                    started_at=recording.recorded_at,
                    ended_at=recording.recorded_at + timedelta(seconds=durations[recording.id]),
                    agent_id=recording.agent_name,
                    customer_voiceprint_id=recording.customer_hash,
                )
                for recording in ordered
                if recording.recorded_at is not None
            ]
            requested_string_ids = {str(recording.id) for recording in ordered}
            accepted_proposal = next(
                (
                    proposal
                    for proposal in engine.propose_groups(candidates)
                    if set(proposal.recording_ids) == requested_string_ids
                    and proposal.decision in {"merge", "needs_review"}
                ),
                None,
            )
            if accepted_proposal is None:
                raise ValidationError(
                    "The selected recordings are not an acceptable current proposal",
                    code="RECEPTION_PROPOSAL_NOT_ACCEPTABLE",
                    detail={"recording_ids": requested_ids},
                )

        assert accepted_proposal is not None
        reason_payload = [
            {
                "code": reason.code,
                "contribution": reason.contribution,
                "detail": reason.detail,
                "hard_constraint": reason.hard_constraint,
            }
            for reason in accepted_proposal.reasons
        ]
        cursor = 0.0
        mappings: list[ReceptionRecordingCreate] = []
        for sequence_no, recording in enumerate(ordered):
            duration = durations[recording.id]
            mappings.append(
                ReceptionRecordingCreate(
                    recording_id=recording.id,
                    sequence_no=sequence_no,
                    timeline_start_sec=cursor,
                    timeline_end_sec=cursor + duration,
                    source_start_sec=0.0,
                    source_end_sec=duration,
                    gap_before_sec=0.0,
                    decision_source="manual",
                    merge_confidence=accepted_proposal.confidence,
                    merge_reasons={
                        "candidate_type": "merge_group",
                        "proposal_decision": accepted_proposal.decision,
                        "accepted_by": actor,
                        "server_constructed_timeline": True,
                        "reasons": reason_payload,
                    },
                )
            )
            cursor += duration

        agent_names = {recording.agent_name for recording in ordered if recording.agent_name}
        customer_hashes = {
            recording.customer_hash for recording in ordered if recording.customer_hash
        }
        start_times = [
            recording.recorded_at for recording in ordered if recording.recorded_at is not None
        ]
        end_times = [
            recording.recorded_at + timedelta(seconds=durations[recording.id])
            for recording in ordered
            if recording.recorded_at is not None
        ]
        return ReceptionCreate(
            external_session_id=body.external_session_id,
            scenario=body.scenario,
            store_id=next(iter(stores)),
            agent_name=next(iter(agent_names)) if len(agent_names) == 1 else None,
            customer_hash=(next(iter(customer_hashes)) if len(customer_hashes) == 1 else None),
            status="confirmed",
            merge_mode=body.merge_mode,
            merge_confidence=accepted_proposal.confidence,
            started_at=min(start_times),
            ended_at=max(end_times),
            recordings=mappings,
        )

    @staticmethod
    async def _persist_merge_group_reception(
        session: AsyncSession,
        *,
        tenant_id: str,
        body: ReceptionCreate,
        actor: str,
    ) -> int:
        if body.external_session_id is not None:
            duplicate = (
                await session.execute(
                    select(Reception.id)
                    .where(
                        Reception.tenant_id == tenant_id,
                        Reception.external_session_id == body.external_session_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if duplicate is not None:
                raise ConflictError(
                    "Reception external session already exists",
                    code="DUPLICATE_RECEPTION_SESSION",
                    detail={"external_session_id": body.external_session_id},
                )

        physical_pending = body.merge_mode in {"physical", "both"}
        agent_user_id = await resolve_unique_agent_user_id(
            session,
            tenant_id=tenant_id,
            agent_name=body.agent_name,
        )
        reception = Reception(
            tenant_id=tenant_id,
            external_session_id=body.external_session_id,
            scenario=body.scenario,
            store_id=body.store_id,
            agent_name=body.agent_name,
            agent_user_id=agent_user_id,
            customer_hash=body.customer_hash,
            status="needs_review" if physical_pending else body.status,
            merge_mode=body.merge_mode,
            merge_confidence=body.merge_confidence,
            started_at=body.started_at,
            ended_at=body.ended_at,
            merged_audio_path=None,
            version=1,
        )
        session.add(reception)
        await session.flush()
        for mapping in body.recordings:
            session.add(
                ReceptionRecording(
                    tenant_id=tenant_id,
                    reception_id=reception.id,
                    recording_id=mapping.recording_id,
                    sequence_no=mapping.sequence_no,
                    timeline_start_sec=mapping.timeline_start_sec,
                    timeline_end_sec=mapping.timeline_end_sec,
                    source_start_sec=mapping.source_start_sec,
                    source_end_sec=mapping.source_end_sec,
                    gap_before_sec=mapping.gap_before_sec,
                    decision_source=mapping.decision_source,
                    merge_confidence=mapping.merge_confidence,
                    merge_reasons=deepcopy(mapping.merge_reasons),
                )
            )
        session.add(
            ProvenanceEvent(
                tenant_id=tenant_id,
                reception_id=reception.id,
                object_type="reception",
                object_ref=str(reception.id),
                event_type="created",
                actor=actor,
                algorithm_version=None,
                parent_refs=[],
                evidence_refs=[
                    {
                        "kind": "audio",
                        "recording_id": mapping.recording_id,
                        "source_start_sec": mapping.source_start_sec,
                        "source_end_sec": mapping.source_end_sec,
                        "timeline_start_sec": mapping.timeline_start_sec,
                        "timeline_end_sec": mapping.timeline_end_sec,
                    }
                    for mapping in body.recordings
                ],
                payload={
                    "reception_id": reception.id,
                    "scenario": body.scenario,
                    "store_id": body.store_id,
                    "agent_user_id": agent_user_id,
                    "requested_merge_mode": body.merge_mode,
                    "physical_audio_status": ("pending" if physical_pending else "not_requested"),
                    "version": 1,
                },
                occurred_at=datetime.now(UTC),
            )
        )
        await session.flush()
        return reception.id

    async def _external_session_exists(
        self,
        tenant_id: str,
        external_session_id: str,
    ) -> bool:
        async with self._session_factory() as session:
            return (
                await session.scalar(
                    select(Reception.id).where(
                        Reception.tenant_id == tenant_id,
                        Reception.external_session_id == external_session_id,
                    )
                )
                is not None
            )


__all__ = [
    "AutomaticReceptionProposal",
    "ReceptionAutomationService",
    "ReceptionDiscoveryResult",
    "ReceptionListResult",
    "ReceptionSplitAcceptance",
]
