"""Reception workspace orchestration and auditable manual editing."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from itertools import combinations
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self, cast

from sqlalchemy import String, and_, delete, func, or_, select, update
from sqlalchemy import cast as sql_cast
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.adapters.protocols import EmbedAdapter
from audio_graphy.core.audio_assembler import (
    AudioAssembler,
    AudioAssemblyManifest,
    AudioAssemblySource,
)
from audio_graphy.core.audio_timeline import (
    AudioTimelinePlanner,
    AudioTimelineSource,
    milliseconds_to_seconds,
    seconds_to_milliseconds,
    verified_recording_duration_ms,
)
from audio_graphy.core.dialogue_segmentation import (
    DialogueSegment,
    DialogueSegmenter,
    SalesScenario,
)
from audio_graphy.core.pii import scrubbed_segment_text
from audio_graphy.core.reception_merge import (
    ManualReceptionConstraints,
    ReceptionMerger,
    ReceptionProposal,
    RecordingCandidate,
)
from audio_graphy.errors import (
    APIError,
    ConflictError,
    NotFoundError,
    RecordingNotFoundError,
    ValidationError,
)
from audio_graphy.models.pipeline import RecordingPipelineRun
from audio_graphy.models.reception import (
    DialogueStateTransition,
    DialogueTagAssignment,
    DialogueUnit,
    ProvenanceEvent,
    Reception,
    ReceptionRecording,
)
from audio_graphy.models.reception_audio import ReceptionAudioOperation
from audio_graphy.models.recording import Recording
from audio_graphy.models.segment import Segment
from audio_graphy.models.tag_governance import (
    TagAssignmentCurrent,
    TagAssignmentFact,
)
from audio_graphy.schemas.receptions import (
    MAX_WORKSPACE_DIALOGUE_UNITS,
    MAX_WORKSPACE_PROVENANCE_EVENTS,
    MAX_WORKSPACE_STATE_TRANSITIONS,
    MAX_WORKSPACE_TAG_ASSIGNMENTS,
    MAX_WORKSPACE_TRANSCRIPT_ITEMS,
    DialogueUnitMergeRequest,
    DialogueUnitSplitRequest,
    ReceptionCreate,
    ReceptionMergeProposalRequest,
    ReceptionMergeRequest,
    ReceptionSegmentRequest,
)
from audio_graphy.services.agent_identity import resolve_unique_agent_user_id
from audio_graphy.services.tag_invalidation import (
    invalidate_dialogue_unit_currents_in_session,
    invalidate_dialogue_units_in_session,
)

if TYPE_CHECKING:
    from audio_graphy.core.crypto import AudioCrypto

_OBJECT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_AUDIO_MEDIA_TYPES = {
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
}
_PLAYBACK_GRANT_VERSION = 1
_PLAYBACK_GRANT_TTL_SEC = 5 * 60
_PLAYBACK_GRANT_CLOCK_SKEW_SEC = 30
logger = logging.getLogger(__name__)


def _actor_user_id(actor: str) -> int:
    """Resolve the audit user encoded by API actors; system jobs use 0."""

    prefix, separator, raw_id = actor.partition(":")
    if prefix == "user" and separator:
        try:
            value = int(raw_id)
        except ValueError:
            return 0
        return value if value >= 0 else 0
    return 0


@dataclass(frozen=True, slots=True)
class ReceptionTranscriptItem:
    """Persisted Segment translated onto the reception timeline."""

    segment_id: int
    recording_id: int
    segment_index: int
    source_start_sec: float
    source_end_sec: float
    timeline_start_sec: float
    timeline_end_sec: float
    speaker: str | None
    text: str
    vad_confidence: float | None


@dataclass(frozen=True, slots=True)
class ReceptionAudioAsset:
    """Authorized and root-confined file ready for HTTP range streaming."""

    path: Path
    media_type: str
    delete_after_open: bool = False
    time_origin_ms: int = 0
    legal_source_start_ms: int = 0
    legal_source_end_ms: int | None = None


@dataclass(frozen=True, slots=True)
class ReceptionPlaybackGeometry:
    """Canonical integer playback coordinates for one reception source."""

    source_start_ms: int
    source_end_ms: int | None
    timeline_start_ms: int
    timeline_end_ms: int
    gap_before_ms: int
    time_origin_ms: int
    legal_source_start_ms: int
    legal_source_end_ms: int


@dataclass(frozen=True, slots=True)
class PlaybackGrantClaims:
    """Verified, short-lived authorization context for native audio playback."""

    subject_id: int
    tenant_id: str
    role: str
    path: str
    issued_at: int
    expires_at: int


def reception_mapping_playback_geometry(
    mapping: ReceptionRecording,
) -> ReceptionPlaybackGeometry:
    """Read persisted millisecond geometry with a legacy-seconds fallback."""

    persisted_source_start_ms = int(getattr(mapping, "source_start_ms", 0) or 0)
    source_start_ms = (
        persisted_source_start_ms
        if persisted_source_start_ms > 0
        else seconds_to_milliseconds(mapping.source_start_sec)
    )
    persisted_source_end_ms = mapping.source_end_ms
    source_end_ms = (
        int(persisted_source_end_ms)
        if persisted_source_end_ms is not None
        else (
            seconds_to_milliseconds(mapping.source_end_sec)
            if mapping.source_end_sec is not None
            else None
        )
    )
    persisted_timeline_start_ms = int(getattr(mapping, "timeline_start_ms", 0) or 0)
    timeline_start_ms = (
        persisted_timeline_start_ms
        if persisted_timeline_start_ms > 0
        else seconds_to_milliseconds(mapping.timeline_start_sec)
    )
    persisted_timeline_end_ms = int(getattr(mapping, "timeline_end_ms", 0) or 0)
    timeline_end_ms = (
        persisted_timeline_end_ms
        if persisted_timeline_end_ms > 0
        else seconds_to_milliseconds(mapping.timeline_end_sec)
    )
    persisted_gap_before_ms = int(getattr(mapping, "gap_before_ms", 0) or 0)
    gap_before_ms = (
        persisted_gap_before_ms
        if persisted_gap_before_ms > 0
        else seconds_to_milliseconds(mapping.gap_before_sec)
    )
    legal_source_end_ms = source_end_ms
    if legal_source_end_ms is None:
        legal_source_end_ms = source_start_ms + (timeline_end_ms - timeline_start_ms)
    return ReceptionPlaybackGeometry(
        source_start_ms=source_start_ms,
        source_end_ms=source_end_ms,
        timeline_start_ms=timeline_start_ms,
        timeline_end_ms=timeline_end_ms,
        gap_before_ms=gap_before_ms,
        time_origin_ms=timeline_start_ms - source_start_ms,
        legal_source_start_ms=source_start_ms,
        legal_source_end_ms=legal_source_end_ms,
    )


@dataclass(frozen=True, slots=True)
class ReceptionMergeProposalResult:
    """Pure merge analysis; no reception row is created by this result."""

    recording_ids: list[int]
    proposals: list[ReceptionProposal]
    groups: list[ReceptionProposal]


@dataclass(frozen=True, slots=True)
class ReceptionTimelineSliceOverride:
    """Service-internal exact geometry supplied by a verified audio plan."""

    source_start_sec: float
    source_end_sec: float
    gap_before_sec: float = 0.0


@dataclass(frozen=True, slots=True)
class _DialogueSegmentationInputs:
    """One immutable, generation-scoped read snapshot for dialogue derivation."""

    segments: tuple[DialogueSegment, ...]
    evidence_by_segment: dict[str, dict[str, Any]]
    speaker_by_segment: dict[str, str | None]
    input_generation: dict[str, int | str]
    legacy_fallback_recording_ids: tuple[int, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class _SemanticSegmentationCapability:
    """What semantic signal was actually available for this concrete run."""

    segments: tuple[DialogueSegment, ...]
    status: str
    model: str | None = None
    dim: int | None = None
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class _RecordingSourceSnapshot:
    """Audio identity plus timeline geometry checked again before publication."""

    recording_id: int
    path: str
    encrypted_path: str | None
    source_start_sec: float
    source_end_sec: float | None
    gap_before_sec: float
    audio_sha256: str | None
    audio_size_bytes: int | None
    audio_duration_ms: int | None
    audio_sample_rate: int | None
    audio_channels: int | None
    source_revision: int | None


@dataclass(frozen=True, slots=True)
class _PreparedPhysicalMerge:
    """External audio build bound to one optimistic reception snapshot."""

    manifest: AudioAssemblyManifest
    merged_audio_path: str
    durations: dict[int, float]
    recording_sources: tuple[_RecordingSourceSnapshot, ...]


ReceptionMergeBeforeCommitHook = Callable[
    [
        AsyncSession,
        Reception,
        tuple[ReceptionRecording, ...],
        _PreparedPhysicalMerge | None,
        str | None,
    ],
    Awaitable[None],
]
ReceptionPhysicalPrepareHook = Callable[
    [_PreparedPhysicalMerge],
    Awaitable[None],
]
ReceptionPhysicalStageHook = Callable[[str], Awaitable[None]]


def _recording_source_snapshot(
    recording: Recording,
    mapping: ReceptionRecording | None,
    *,
    gap_before_sec: float,
    geometry_override: ReceptionTimelineSliceOverride | None = None,
) -> _RecordingSourceSnapshot:
    def optional_positive_int(field_name: str) -> int | None:
        raw = getattr(recording, field_name, None)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            return None
        return raw

    raw_hash = getattr(recording, "audio_sha256", None)
    return _RecordingSourceSnapshot(
        recording_id=recording.id,
        path=str(recording.path),
        encrypted_path=(
            str(recording.audio_encrypted_path)
            if recording.audio_encrypted_path is not None
            else None
        ),
        source_start_sec=(
            geometry_override.source_start_sec
            if geometry_override is not None
            else mapping.source_start_sec
            if mapping is not None
            else 0.0
        ),
        source_end_sec=(
            geometry_override.source_end_sec
            if geometry_override is not None
            else mapping.source_end_sec
            if mapping is not None
            else None
        ),
        gap_before_sec=gap_before_sec,
        audio_sha256=raw_hash if isinstance(raw_hash, str) and raw_hash else None,
        audio_size_bytes=optional_positive_int("audio_size_bytes"),
        audio_duration_ms=verified_recording_duration_ms(recording),
        audio_sample_rate=optional_positive_int("audio_sample_rate"),
        audio_channels=optional_positive_int("audio_channels"),
        source_revision=optional_positive_int("source_revision"),
    )


def _normalize_timeline_override(
    recording_ids: Sequence[int],
    timeline_override: Mapping[int, ReceptionTimelineSliceOverride] | None,
) -> dict[int, ReceptionTimelineSliceOverride] | None:
    """Freeze and canonicalize an internal plan on the millisecond grid."""

    if timeline_override is None:
        return None
    if any(isinstance(key, bool) or not isinstance(key, int) for key in timeline_override):
        raise ValidationError(
            "Audio timeline override keys must be recording IDs",
            code="RECEPTION_TIMELINE_OVERRIDE_INVALID",
        )
    expected_ids = set(recording_ids)
    supplied_ids = set(timeline_override)
    if supplied_ids != expected_ids:
        raise ValidationError(
            "Audio timeline override must cover exactly the requested recordings",
            code="RECEPTION_TIMELINE_OVERRIDE_INVALID",
            detail={
                "missing_recording_ids": sorted(expected_ids - supplied_ids),
                "unexpected_recording_ids": sorted(supplied_ids - expected_ids),
            },
        )

    normalized: dict[int, ReceptionTimelineSliceOverride] = {}
    for sequence_no, recording_id in enumerate(recording_ids):
        geometry = timeline_override[recording_id]
        if not isinstance(geometry, ReceptionTimelineSliceOverride):
            raise ValidationError(
                "Audio timeline override contains an invalid geometry",
                code="RECEPTION_TIMELINE_OVERRIDE_INVALID",
                detail={"recording_id": recording_id},
            )
        try:
            source_start_ms = seconds_to_milliseconds(geometry.source_start_sec)
            source_end_ms = seconds_to_milliseconds(geometry.source_end_sec)
            gap_before_ms = seconds_to_milliseconds(geometry.gap_before_sec)
        except ValueError as exc:
            raise ValidationError(
                "Audio timeline override contains non-finite geometry",
                code="RECEPTION_TIMELINE_OVERRIDE_INVALID",
                detail={"recording_id": recording_id},
            ) from exc
        if source_end_ms <= source_start_ms:
            raise ValidationError(
                "Audio timeline override source interval must be positive",
                code="RECEPTION_TIMELINE_OVERRIDE_INVALID",
                detail={"recording_id": recording_id},
            )
        if sequence_no == 0 and gap_before_ms != 0:
            raise ValidationError(
                "The first audio timeline source cannot have a gap",
                code="RECEPTION_TIMELINE_OVERRIDE_INVALID",
                detail={"recording_id": recording_id},
            )
        normalized[recording_id] = ReceptionTimelineSliceOverride(
            source_start_sec=milliseconds_to_seconds(source_start_ms),
            source_end_sec=milliseconds_to_seconds(source_end_ms),
            gap_before_sec=milliseconds_to_seconds(gap_before_ms),
        )
    return normalized


class _PhysicalArtifactGuard:
    """Delete an unpublished generation on CAS/transaction failure."""

    def __init__(self, prepared: _PreparedPhysicalMerge | None) -> None:
        self._prepared = prepared

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc, traceback
        # This guard is the outermost context around ``session.begin()``.
        # Therefore a normal exit is observable only after the transaction
        # commit has succeeded; a commit failure reaches us as ``exc_type``.
        if self._prepared is not None and exc_type is not None:
            await asyncio.to_thread(
                Path(self._prepared.merged_audio_path).unlink,
                missing_ok=True,
            )


@dataclass(slots=True)
class ReceptionWorkspace:
    """Loaded ORM snapshot used by both workspace and mutation responses."""

    reception: Reception
    recordings: list[tuple[ReceptionRecording, Recording]]
    dialogue_units: list[DialogueUnit]
    tag_assignments: list[DialogueTagAssignment]
    tag_assignments_by_unit: dict[int, list[DialogueTagAssignment]]
    state_transitions: list[DialogueStateTransition]
    transcript_items: list[ReceptionTranscriptItem]
    provenance_events: list[ProvenanceEvent]
    window: ReceptionWorkspaceWindow | None = None
    previous_dialogue_unit: DialogueUnit | None = None
    next_dialogue_unit: DialogueUnit | None = None
    active_audio_operation: ReceptionAudioOperation | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceCollectionWindow:
    """Cardinality metadata for one bounded timeline-window collection."""

    total: int
    returned: int
    limit: int

    @property
    def truncated(self) -> bool:
        return self.returned < self.total


@dataclass(frozen=True, slots=True)
class ReceptionWorkspaceWindow:
    """Timeline navigation plus hard per-collection output bounds."""

    start_sec: float
    end_sec: float
    size_sec: float
    reception_duration_sec: float
    total_dialogue_units: int
    protected_dialogue_units: int
    dialogue_units: WorkspaceCollectionWindow
    tag_assignments: WorkspaceCollectionWindow
    state_transitions: WorkspaceCollectionWindow
    transcript_items: WorkspaceCollectionWindow
    provenance_events: WorkspaceCollectionWindow

    @property
    def collections(self) -> tuple[WorkspaceCollectionWindow, ...]:
        return (
            self.dialogue_units,
            self.tag_assignments,
            self.state_transitions,
            self.transcript_items,
            self.provenance_events,
        )

    @property
    def truncated(self) -> bool:
        return any(item.truncated for item in self.collections)

    @property
    def has_previous(self) -> bool:
        return self.start_sec > 0

    @property
    def has_next(self) -> bool:
        return self.end_sec < self.reception_duration_sec

    @property
    def previous_start_sec(self) -> float | None:
        if not self.has_previous:
            return None
        return max(self.start_sec - self.size_sec, 0.0)

    @property
    def next_start_sec(self) -> float | None:
        return self.end_sec if self.has_next else None


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    decoded = base64.b64decode(
        value + padding,
        altchars=b"-_",
        validate=True,
    )
    # Reject alternate/non-canonical encodings of the same signed bytes so
    # changing any grant character is always observable.
    if _base64url_encode(decoded) != value:
        raise ValueError("non-canonical base64url")
    return decoded


def create_playback_grant(
    *,
    secret: str,
    subject_id: int,
    tenant_id: str,
    role: str,
    path: str,
    ttl_sec: int = _PLAYBACK_GRANT_TTL_SEC,
    now: int | None = None,
) -> str:
    """Sign a resource-bound grant suitable for a browser-native audio URL."""
    if (
        not secret
        or subject_id <= 0
        or not tenant_id
        or role not in {"admin", "inspector", "agent", "viewer"}
        or not path.startswith("/api/v1/receptions/")
        or "?" in path
        or "#" in path
        or not 1 <= ttl_sec <= _PLAYBACK_GRANT_TTL_SEC
    ):
        raise ValueError("invalid playback grant input")
    issued_at = int(time.time()) if now is None else int(now)
    payload = {
        "exp": issued_at + ttl_sec,
        "iat": issued_at,
        "path": path,
        "role": role,
        "sub": subject_id,
        "tid": tenant_id,
        "v": _PLAYBACK_GRANT_VERSION,
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
        secret.encode("utf-8"),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{encoded_payload}.{_base64url_encode(signature)}"


def verify_playback_grant(
    *,
    secret: str,
    grant: str,
    expected_path: str,
    now: int | None = None,
) -> PlaybackGrantClaims:
    """Verify signature, TTL and exact resource scope without leaking failure cause."""
    try:
        if not secret or not grant or len(grant) > 2_048 or grant.count(".") != 1:
            raise ValueError
        encoded_payload, encoded_signature = grant.split(".", 1)
        supplied_signature = _base64url_decode(encoded_signature)
        expected_signature = hmac.new(
            secret.encode("utf-8"),
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ValueError
        payload = json.loads(_base64url_decode(encoded_payload))
        if not isinstance(payload, dict):
            raise ValueError

        subject_id = payload.get("sub")
        tenant_id = payload.get("tid")
        role = payload.get("role")
        path = payload.get("path")
        issued_at = payload.get("iat")
        expires_at = payload.get("exp")
        if (
            payload.get("v") != _PLAYBACK_GRANT_VERSION
            or not isinstance(subject_id, int)
            or subject_id <= 0
            or not isinstance(tenant_id, str)
            or not tenant_id
            or role not in {"admin", "inspector", "agent", "viewer"}
            or path != expected_path
            or not isinstance(issued_at, int)
            or not isinstance(expires_at, int)
            or expires_at - issued_at > _PLAYBACK_GRANT_TTL_SEC
            or expires_at <= issued_at
        ):
            raise ValueError

        current_time = int(time.time()) if now is None else int(now)
        if issued_at > current_time + _PLAYBACK_GRANT_CLOCK_SKEW_SEC or expires_at <= current_time:
            raise ValueError
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid playback grant") from exc

    return PlaybackGrantClaims(
        subject_id=subject_id,
        tenant_id=tenant_id,
        role=role,
        path=path,
        issued_at=issued_at,
        expires_at=expires_at,
    )


def resolve_safe_audio_output(root: Path, candidate: str) -> Path:
    """Resolve a relative WAV path without allowing traversal or hidden files."""
    relative = Path(candidate)
    if (
        not candidate
        or "\x00" in candidate
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.name.startswith(".")
        or relative.suffix.casefold() != ".wav"
    ):
        raise ValueError("audio output must be a non-hidden relative .wav path")

    resolved_root = root.resolve()
    resolved = (resolved_root / relative).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("audio output must stay below the configured root") from exc
    if resolved == resolved_root:
        raise ValueError("audio output must name a file")
    return resolved


_PHYSICAL_GENERATION_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,96}")


def reception_physical_generation_relative_path(
    *,
    tenant_id: str,
    reception_id: int,
    reception_version: int,
    generation: str,
) -> str:
    """Return the sole generated-file namespace for one immutable generation."""
    tenant_component = Path(tenant_id)
    if (
        not tenant_id
        or "\x00" in tenant_id
        or tenant_component.is_absolute()
        or len(tenant_component.parts) != 1
        or tenant_component.name in {".", ".."}
    ):
        raise ValueError("tenant_id is not a safe path component")
    if reception_id <= 0 or reception_version <= 0:
        raise ValueError("reception identity and version must be positive")
    if _PHYSICAL_GENERATION_PATTERN.fullmatch(generation) is None:
        raise ValueError("physical generation is invalid")
    return (
        f"assembled_audio/{tenant_id}/receptions/"
        f"reception-{reception_id}/v{reception_version}-{generation}.wav"
    )


def _resolve_confined_regular_file(root: Path, candidate: str) -> Path:
    """Resolve a regular file without escaping root or traversing symlinks."""
    try:
        resolved_root = root.resolve(strict=True)
        raw_path = Path(candidate)
        unresolved = raw_path if raw_path.is_absolute() else resolved_root / raw_path
        resolved = unresolved.resolve(strict=True)
        relative = resolved.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("audio file is unavailable") from exc

    cursor = resolved_root
    for component in relative.parts:
        cursor = cursor / component
        if cursor.is_symlink():
            raise ValueError("audio file is unavailable")
    if not resolved.is_file():
        raise ValueError("audio file is unavailable")
    return resolved


def resolve_confined_audio_file(root: Path, candidate: str) -> ReceptionAudioAsset:
    """Resolve a persisted audio path without escaping or traversing symlinks."""
    resolved = _resolve_confined_regular_file(root, candidate)
    media_type = _AUDIO_MEDIA_TYPES.get(resolved.suffix.casefold())
    if media_type is None:
        raise ValueError("audio file is unavailable")
    return ReceptionAudioAsset(path=resolved, media_type=media_type)


def _allocate_private_audio_path(
    root: Path,
    *,
    tenant_id: str,
    suffix: str,
) -> Path:
    """Allocate a mode-0600 temporary file below the configured audio root."""
    tenant_token = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:16]
    directory = root.resolve(strict=True) / "runtime_plaintext" / tenant_token
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    descriptor, raw_path = tempfile.mkstemp(
        prefix="audio-",
        suffix=suffix,
        dir=directory,
    )
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    return Path(raw_path)


def _json_fingerprint(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _merge_json_values(*collections: Sequence[Any]) -> list[Any]:
    merged: list[Any] = []
    seen: set[str] = set()
    for collection in collections:
        for item in collection:
            fingerprint = _json_fingerprint(item)
            if fingerprint not in seen:
                merged.append(deepcopy(item))
                seen.add(fingerprint)
    return merged


def _unit_snapshot(unit: DialogueUnit) -> dict[str, Any]:
    return {
        "id": unit.id,
        "unit_index": unit.unit_index,
        "version": unit.version,
        "start_sec": unit.start_sec,
        "end_sec": unit.end_sec,
        "topic": unit.topic,
        "business_stage": unit.business_stage,
        "summary": unit.summary,
        "edit_status": unit.edit_status,
    }


def _mapping_snapshot(mapping: ReceptionRecording) -> dict[str, Any]:
    return {
        "recording_id": mapping.recording_id,
        "sequence_no": mapping.sequence_no,
        "timeline_start_sec": mapping.timeline_start_sec,
        "timeline_end_sec": mapping.timeline_end_sec,
        "source_start_sec": mapping.source_start_sec,
        "source_end_sec": mapping.source_end_sec,
        "gap_before_sec": mapping.gap_before_sec,
    }


def _tag_snapshot(tag: DialogueTagAssignment) -> dict[str, Any]:
    return {
        "id": tag.id,
        "dialogue_unit_id": tag.dialogue_unit_id,
        "group_key": tag.group_key,
        "group_version": tag.group_version,
        "label_key": tag.label_key,
        "label_value": tag.label_value,
        "confidence": tag.confidence,
        "source": tag.source,
        "priority": tag.priority,
        "evidence_refs": deepcopy(tag.evidence_refs),
        "is_current": tag.is_current,
    }


def _canonical_tag_assignment(fact: TagAssignmentFact) -> DialogueTagAssignment:
    """Expose one canonical fact through the existing workspace response contract.

    This is a transient compatibility projection only.  The authoritative row
    remains ``tag_assignment_facts`` and can be resolved through ``fact:{id}``.
    """

    value = fact.tag_value
    label_value = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    )
    tagger_ref = (
        f"tagger:{fact.tagger_version_id}"
        if fact.tagger_version_id is not None
        else "tagger:manual"
    )
    evidence_refs: list[Any] = []
    for index, raw_ref in enumerate(fact.evidence_refs):
        if not isinstance(raw_ref, Mapping):
            evidence_refs.append(deepcopy(raw_ref))
            continue
        ref = deepcopy(dict(raw_ref))
        segment_id = ref.get("segment_id")
        start_sec = _finite_number(ref.get("start_sec"))
        end_sec = _finite_number(ref.get("end_sec"))
        timeline_start_sec = _finite_number(ref.get("timeline_start_sec"))
        timeline_end_sec = _finite_number(ref.get("timeline_end_sec"))
        ref.setdefault(
            "ref_id",
            (
                f"segment:{segment_id}"
                if segment_id is not None
                else f"fact:{fact.id}:evidence:{index}"
            ),
        )
        ref.setdefault("kind", "audio")
        ref.setdefault(
            "coordinate_space",
            "both" if timeline_start_sec is not None and timeline_end_sec is not None else "source",
        )
        if start_sec is not None:
            ref.setdefault("start_ms", round(start_sec * 1_000))
            ref.setdefault("source_start_ms", round(start_sec * 1_000))
        if end_sec is not None:
            ref.setdefault("end_ms", round(end_sec * 1_000))
            ref.setdefault("source_end_ms", round(end_sec * 1_000))
        if timeline_start_sec is not None:
            ref.setdefault("timeline_start_ms", round(timeline_start_sec * 1_000))
        if timeline_end_sec is not None:
            ref.setdefault("timeline_end_ms", round(timeline_end_sec * 1_000))
        if "text_excerpt" not in ref and isinstance(ref.get("text"), str):
            ref["text_excerpt"] = ref["text"]
        evidence_refs.append(ref)

    assignment = DialogueTagAssignment(
        id=fact.id,
        tenant_id=fact.tenant_id,
        reception_id=cast(int, fact.reception_id),
        dialogue_unit_id=fact.dialogue_unit_id or fact.subject_id,
        group_key="canonical",
        group_version=f"schema:{fact.schema_version_id}|{tagger_ref}",
        label_key=fact.tag_key,
        label_value=label_value,
        confidence=fact.confidence,
        source=fact.source,
        priority=1_000 if fact.source == "manual" else 500,
        evidence_refs=evidence_refs,
        model_run_id=f"fact:{fact.id}",
        is_current=True,
        assigned_at=fact.assigned_at,
    )
    return assignment


def _state_transition_snapshot(
    transition: DialogueStateTransition,
) -> dict[str, Any]:
    return {
        "id": transition.id,
        "dialogue_unit_id": transition.dialogue_unit_id,
        "sequence_no": transition.sequence_no,
        "from_state": transition.from_state,
        "to_state": transition.to_state,
        "trigger": transition.trigger,
        "confidence": transition.confidence,
        "evidence_refs": deepcopy(transition.evidence_refs),
        "algorithm_version": transition.algorithm_version,
    }


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _unit_stage_confidence(unit: DialogueUnit) -> float:
    """Read stage certainty without reusing unrelated boundary certainty."""
    direct = _finite_number(getattr(unit, "stage_confidence", None))
    if direct is not None:
        return min(1.0, direct)
    for reason in unit.boundary_reasons:
        if not isinstance(reason, Mapping):
            continue
        candidate = _finite_number(reason.get("stage_confidence"))
        if candidate is not None:
            return min(1.0, candidate)
    return 1.0 if unit.business_stage else 0.0


def _clip_evidence_refs(
    evidence_refs: Sequence[Any],
    *,
    start_sec: float,
    end_sec: float,
) -> list[Any]:
    """Clip timeline-aware evidence while retaining source-only lineage.

    Dialogue-unit windows are expressed on the assembled reception timeline.
    Explicit timeline coordinates therefore control overlap.  When a reference
    also carries source coordinates, both coordinate systems are clipped by
    the same proportional offset so later playback remains geometrically
    correct.  Untimed and source-only references are retained conservatively.
    """
    clipped: list[Any] = []
    for raw_evidence in evidence_refs:
        if not isinstance(raw_evidence, Mapping):
            clipped.append(deepcopy(raw_evidence))
            continue

        evidence = deepcopy(dict(raw_evidence))
        coordinate_space = evidence.get("coordinate_space")
        timeline_start = _finite_number(evidence.get("timeline_start_sec"))
        timeline_end = _finite_number(evidence.get("timeline_end_sec"))
        timeline_kind = "timeline_sec"

        if (timeline_start is None or timeline_end is None) and coordinate_space != "source":
            timeline_start = _finite_number(evidence.get("start_sec"))
            timeline_end = _finite_number(evidence.get("end_sec"))
            timeline_kind = "generic_sec"

        if timeline_start is None or timeline_end is None:
            timeline_start_ms = _finite_number(evidence.get("timeline_start_ms"))
            timeline_end_ms = _finite_number(evidence.get("timeline_end_ms"))
            timeline_kind = "timeline_ms"
            if (
                timeline_start_ms is None or timeline_end_ms is None
            ) and coordinate_space != "source":
                timeline_start_ms = _finite_number(evidence.get("start_ms"))
                timeline_end_ms = _finite_number(evidence.get("end_ms"))
                timeline_kind = "generic_ms"
            if timeline_start_ms is not None and timeline_end_ms is not None:
                timeline_start = timeline_start_ms / 1_000
                timeline_end = timeline_end_ms / 1_000

        if timeline_start is None or timeline_end is None or timeline_end <= timeline_start:
            clipped.append(evidence)
            continue

        overlap_start = max(timeline_start, start_sec)
        overlap_end = min(timeline_end, end_sec)
        if overlap_end <= overlap_start:
            continue

        source_start = _finite_number(evidence.get("source_start_sec"))
        source_end = _finite_number(evidence.get("source_end_sec"))
        if source_start is not None and source_end is not None and source_end > source_start:
            scale = (source_end - source_start) / (timeline_end - timeline_start)
            clipped_source_start = source_start + (overlap_start - timeline_start) * scale
            clipped_source_end = source_end - (timeline_end - overlap_end) * scale
            evidence["source_start_sec"] = clipped_source_start
            evidence["source_end_sec"] = clipped_source_end
            if "source_start_ms" in evidence or "source_end_ms" in evidence:
                evidence["source_start_ms"] = round(clipped_source_start * 1_000)
                evidence["source_end_ms"] = round(clipped_source_end * 1_000)

        evidence["timeline_start_sec"] = overlap_start
        evidence["timeline_end_sec"] = overlap_end
        if timeline_kind in {"timeline_ms", "generic_ms"} or (
            "timeline_start_ms" in evidence or "timeline_end_ms" in evidence
        ):
            evidence["timeline_start_ms"] = round(overlap_start * 1_000)
            evidence["timeline_end_ms"] = round(overlap_end * 1_000)
        if timeline_kind == "generic_sec":
            evidence["start_sec"] = overlap_start
            evidence["end_sec"] = overlap_end
        # Generic milliseconds are the playback coordinates consumed first by
        # current clients. Whenever timeline geometry controls the clip, keep
        # these aliases canonical even if the original reference also exposed
        # explicit timeline seconds.
        evidence["start_ms"] = round(overlap_start * 1_000)
        evidence["end_ms"] = round(overlap_end * 1_000)

        evidence["coordinate_space"] = (
            "both" if source_start is not None and source_end is not None else "reception_timeline"
        )
        clipped.append(evidence)

    return clipped


def _tag_applies_to_window(
    tag: DialogueTagAssignment,
    *,
    start_sec: float,
    end_sec: float,
) -> bool:
    """Use timed evidence when available; otherwise conservatively retain."""
    windows: list[tuple[float, float]] = []
    for raw_evidence in tag.evidence_refs:
        if not isinstance(raw_evidence, Mapping):
            continue
        evidence = dict(raw_evidence)
        # Dialogue-unit windows use reception-timeline coordinates. Prefer
        # explicit timeline coordinates from the segmentation evidence
        # contract; never compare source-file seconds with reception seconds.
        evidence_start = _finite_number(evidence.get("timeline_start_sec"))
        evidence_end = _finite_number(evidence.get("timeline_end_sec"))
        coordinate_space = evidence.get("coordinate_space")
        if (evidence_start is None or evidence_end is None) and coordinate_space != "source":
            evidence_start = _finite_number(evidence.get("start_sec"))
            evidence_end = _finite_number(evidence.get("end_sec"))
        if evidence_start is None or evidence_end is None:
            start_ms = _finite_number(evidence.get("timeline_start_ms"))
            end_ms = _finite_number(evidence.get("timeline_end_ms"))
            if (start_ms is None or end_ms is None) and coordinate_space != "source":
                start_ms = _finite_number(evidence.get("start_ms"))
                end_ms = _finite_number(evidence.get("end_ms"))
            if start_ms is not None and end_ms is not None:
                evidence_start = start_ms / 1_000
                evidence_end = end_ms / 1_000
        if (
            evidence_start is not None
            and evidence_end is not None
            and evidence_end > evidence_start
        ):
            windows.append((evidence_start, evidence_end))

    if not windows:
        return True
    return any(
        evidence_start < end_sec and evidence_end > start_sec
        for evidence_start, evidence_end in windows
    )


class ReceptionService:
    """Tenant-safe reception CRUD and optimistic manual-edit operations."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        audio_root: Path,
        audio_assembler: AudioAssembler | None = None,
        audio_crypto: AudioCrypto | None = None,
        embed_adapter: EmbedAdapter | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._audio_root = audio_root
        self._audio_assembler = audio_assembler
        self._audio_crypto = audio_crypto
        self._embed_adapter = embed_adapter

    @property
    def audio_root(self) -> Path:
        """Root used for generated artifacts and safety confinement."""
        return self._audio_root

    async def _decrypt_audio_asset(
        self,
        encrypted_path: str,
        *,
        original_path: str,
        tenant_id: str,
    ) -> ReceptionAudioAsset:
        """Materialize verified plaintext privately for one authorized use."""
        if self._audio_crypto is None:
            raise APIError(
                "Encrypted audio is temporarily unavailable",
                code="AUDIO_DECRYPTION_UNAVAILABLE",
                status_code=503,
            )
        suffix = Path(original_path).suffix.casefold()
        media_type = _AUDIO_MEDIA_TYPES.get(suffix)
        if media_type is None:
            raise NotFoundError("Audio asset not found", code="AUDIO_NOT_FOUND")
        try:
            cipher = _resolve_confined_regular_file(
                self._audio_root,
                encrypted_path,
            )
            plaintext = await asyncio.to_thread(
                _allocate_private_audio_path,
                self._audio_root,
                tenant_id=tenant_id,
                suffix=suffix,
            )
        except ValueError as exc:
            raise NotFoundError(
                "Audio asset not found",
                code="AUDIO_NOT_FOUND",
            ) from exc

        result = await asyncio.to_thread(
            self._audio_crypto.decrypt_file,
            cipher,
            plaintext,
        )
        if not result.ok or result.plaintext_path != plaintext:
            await asyncio.to_thread(plaintext.unlink, missing_ok=True)
            raise APIError(
                "Encrypted audio could not be verified",
                code="AUDIO_DECRYPTION_FAILED",
                status_code=503,
            )
        try:
            resolved = _resolve_confined_regular_file(self._audio_root, str(plaintext))
        except ValueError as exc:
            await asyncio.to_thread(plaintext.unlink, missing_ok=True)
            raise APIError(
                "Decrypted audio could not be secured",
                code="AUDIO_DECRYPTION_FAILED",
                status_code=503,
            ) from exc
        return ReceptionAudioAsset(
            path=resolved,
            media_type=media_type,
            delete_after_open=True,
        )

    async def _recording_audio_asset(
        self,
        recording: Recording,
        *,
        tenant_id: str,
    ) -> ReceptionAudioAsset:
        if recording.audio_encrypted_path:
            return await self._decrypt_audio_asset(
                str(recording.audio_encrypted_path),
                original_path=str(recording.path),
                tenant_id=tenant_id,
            )
        try:
            return resolve_confined_audio_file(
                self._audio_root,
                str(recording.path),
            )
        except ValueError as exc:
            raise NotFoundError(
                "Audio asset not found",
                code="AUDIO_NOT_FOUND",
            ) from exc

    @staticmethod
    def _mapping_requires_clipped_playback(
        mapping: ReceptionRecording,
        recording: Recording,
    ) -> bool:
        reasons = mapping.merge_reasons if isinstance(mapping.merge_reasons, Mapping) else {}
        if reasons.get("candidate_type") == "recording_split":
            return True
        verified_duration_ms = verified_recording_duration_ms(recording)
        # Full-source playback is the privileged fast path.  It is permitted
        # only when immutable media facts prove that the mapping spans the
        # complete source and its logical geometry is isomorphic.  Unknown
        # duration/endpoints fail closed to a bounded materialized clip.
        if verified_duration_ms is None or mapping.source_end_sec is None:
            return True
        source_start_ms = seconds_to_milliseconds(mapping.source_start_sec)
        source_end_ms = seconds_to_milliseconds(mapping.source_end_sec)
        timeline_duration_ms = seconds_to_milliseconds(
            mapping.timeline_end_sec - mapping.timeline_start_sec
        )
        source_duration_ms = source_end_ms - source_start_ms
        return not (
            source_start_ms == 0
            and abs(source_end_ms - verified_duration_ms) <= 1
            and timeline_duration_ms == source_duration_ms
        )

    async def _materialize_mapping_audio_asset(
        self,
        *,
        mapping: ReceptionRecording,
        recording: Recording,
        tenant_id: str,
    ) -> ReceptionAudioAsset:
        """Create an unlink-after-open WAV bounded to one legal source slice."""
        if self._audio_assembler is None:
            raise APIError(
                "Clipped recording playback is temporarily unavailable",
                code="AUDIO_CLIP_ASSEMBLER_UNAVAILABLE",
                status_code=503,
            )
        source_asset = await self._recording_audio_asset(
            recording,
            tenant_id=tenant_id,
        )
        source_end_sec = mapping.source_end_sec
        if source_end_sec is None:
            source_end_sec = mapping.source_start_sec + (
                mapping.timeline_end_sec - mapping.timeline_start_sec
            )
        tenant_token = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:16]
        target_relative = (
            f"runtime_plaintext/{tenant_token}/"
            f"reception-{mapping.reception_id}-mapping-{mapping.id}-"
            f"{secrets.token_hex(8)}.wav"
        )
        target = resolve_safe_audio_output(self._audio_root, target_relative)
        try:
            manifest = await self._audio_assembler.assemble(
                [
                    AudioAssemblySource(
                        path=source_asset.path,
                        source_start_sec=mapping.source_start_sec,
                        source_end_sec=source_end_sec,
                    )
                ],
                target_relative,
            )
            resolved_output = resolve_safe_audio_output(
                self._audio_root,
                manifest.output_path,
            )
            if (
                resolved_output != target
                or not resolved_output.is_file()
                or len(manifest.inputs) != 1
            ):
                raise ValueError("clip assembler returned an invalid output")
            return ReceptionAudioAsset(
                path=resolved_output,
                media_type="audio/wav",
                delete_after_open=True,
            )
        except Exception as exc:
            await asyncio.to_thread(target.unlink, missing_ok=True)
            raise APIError(
                "Clipped recording playback could not be prepared",
                code="AUDIO_CLIP_PLAYBACK_FAILED",
                status_code=503,
            ) from exc
        finally:
            if source_asset.delete_after_open:
                await asyncio.to_thread(source_asset.path.unlink, missing_ok=True)

    async def _retire_physical_artifact(
        self,
        persisted_path: str | None,
        *,
        reception_id: int,
        tenant_id: str,
    ) -> None:
        """Best-effort cleanup restricted to this reception's generated directory."""
        if not persisted_path:
            return
        try:
            resolved = _resolve_confined_regular_file(
                self._audio_root,
                persisted_path,
            )
            expected_root = (
                self._audio_root.resolve(strict=True)
                / "assembled_audio"
                / tenant_id
                / "receptions"
                / f"reception-{reception_id}"
            ).resolve(strict=False)
            if not resolved.is_relative_to(expected_root):
                logger.warning(
                    "Refusing to retire non-generated reception audio",
                    extra={
                        "reception_id": reception_id,
                        "tenant_id": tenant_id,
                    },
                )
                return
            await asyncio.to_thread(resolved.unlink, missing_ok=True)
        except (OSError, RuntimeError, ValueError):
            logger.exception(
                "Failed to retire reception audio artifact",
                extra={
                    "reception_id": reception_id,
                    "tenant_id": tenant_id,
                },
            )

    @staticmethod
    def _reception_not_found(reception_id: int) -> NotFoundError:
        return NotFoundError(
            "Reception not found",
            code="RECEPTION_NOT_FOUND",
            detail={"reception_id": reception_id},
        )

    @staticmethod
    def _unit_not_found(unit_id: int) -> NotFoundError:
        return NotFoundError(
            "Dialogue unit not found",
            code="DIALOGUE_UNIT_NOT_FOUND",
            detail={"dialogue_unit_id": unit_id},
        )

    @staticmethod
    def _version_conflict(
        *,
        object_type: str,
        object_id: int,
        expected: int,
        actual: int,
    ) -> ConflictError:
        return ConflictError(
            "The workspace changed; reload before editing",
            code="VERSION_CONFLICT",
            detail={
                "object_type": object_type,
                "object_id": object_id,
                "expected_version": expected,
                "actual_version": actual,
            },
        )

    async def _find_reception(
        self,
        session: AsyncSession,
        reception_id: int,
        tenant_id: str,
        *,
        agent_user_id: int | None = None,
        for_update: bool = False,
    ) -> Reception:
        stmt = select(Reception).where(
            Reception.id == reception_id,
            Reception.tenant_id == tenant_id,
        )
        if agent_user_id is not None:
            stmt = stmt.where(Reception.agent_user_id == agent_user_id)
        if for_update:
            stmt = stmt.with_for_update()
        result = await session.execute(stmt)
        reception = result.scalar_one_or_none()
        if reception is None:
            raise self._reception_not_found(reception_id)
        return reception

    async def _load_workspace(
        self,
        session: AsyncSession,
        reception: Reception,
        *,
        window_start_sec: float | None = None,
        window_size_sec: float | None = None,
    ) -> ReceptionWorkspace:
        """Load a full internal snapshot or one strictly bounded time window."""
        if (window_start_sec is None) != (window_size_sec is None):
            raise ValueError("window_start_sec and window_size_sec must be provided together")
        bounded = window_start_sec is not None and window_size_sec is not None

        mapping_result = await session.execute(
            select(ReceptionRecording, Recording)
            .join(Recording, Recording.id == ReceptionRecording.recording_id)
            .where(
                ReceptionRecording.tenant_id == reception.tenant_id,
                ReceptionRecording.reception_id == reception.id,
                Recording.tenant_id == reception.tenant_id,
            )
            .order_by(ReceptionRecording.sequence_no)
        )
        recordings = [(row[0], row[1]) for row in mapping_result.all()]

        mapped_duration = max(
            (mapping.timeline_end_sec for mapping, _recording in recordings),
            default=0.0,
        )
        wall_duration = max(
            (reception.ended_at - reception.started_at).total_seconds(),
            0.0,
        )
        reception_duration = mapped_duration if recordings else wall_duration
        effective_window_size = window_size_sec if window_size_sec is not None else 1.0
        effective_window_start = (
            min(window_start_sec, reception_duration) if window_start_sec is not None else 0.0
        )
        effective_window_end = (
            min(effective_window_start + effective_window_size, reception_duration)
            if bounded
            else reception_duration
        )

        all_unit_filters = (
            DialogueUnit.tenant_id == reception.tenant_id,
            DialogueUnit.reception_id == reception.id,
        )
        unit_filters = list(all_unit_filters)
        if bounded:
            unit_filters.extend(
                [
                    DialogueUnit.end_sec > effective_window_start,
                    DialogueUnit.start_sec < effective_window_end,
                ]
            )
        units_stmt = (
            select(DialogueUnit)
            .where(*unit_filters)
            .order_by(
                DialogueUnit.start_sec,
                DialogueUnit.unit_index,
                DialogueUnit.id,
            )
        )
        if bounded:
            units_stmt = units_stmt.limit(MAX_WORKSPACE_DIALOGUE_UNITS)
            unit_total = int(
                (
                    await session.execute(select(func.count(DialogueUnit.id)).where(*unit_filters))
                ).scalar_one()
            )
        else:
            unit_total = 0
        units_result = await session.execute(units_stmt)
        units = list(units_result.scalars().all())
        returned_unit_ids = tuple(unit.id for unit in units)
        if not bounded:
            unit_total = len(units)
            reception_unit_total = unit_total
        else:
            reception_unit_total = int(
                (
                    await session.execute(
                        select(func.count(DialogueUnit.id)).where(*all_unit_filters)
                    )
                ).scalar_one()
            )

        tag_filters = (
            DialogueTagAssignment.tenant_id == reception.tenant_id,
            DialogueTagAssignment.reception_id == reception.id,
            DialogueTagAssignment.is_current.is_(True),
        )
        tag_query_unit_filters = (
            [DialogueUnit.id.in_(returned_unit_ids)] if bounded else unit_filters
        )
        tags_stmt = (
            select(DialogueTagAssignment)
            .join(
                DialogueUnit,
                and_(
                    DialogueUnit.id == DialogueTagAssignment.dialogue_unit_id,
                    DialogueUnit.tenant_id == reception.tenant_id,
                    DialogueUnit.reception_id == reception.id,
                ),
            )
            .where(*tag_filters, *tag_query_unit_filters)
            .order_by(
                DialogueUnit.unit_index,
                DialogueTagAssignment.group_key,
                DialogueTagAssignment.group_version,
                DialogueTagAssignment.label_key,
                DialogueTagAssignment.assigned_at,
                DialogueTagAssignment.id,
            )
        )
        if bounded:
            tags_stmt = tags_stmt.limit(MAX_WORKSPACE_TAG_ASSIGNMENTS)
            tag_total = int(
                (
                    await session.execute(
                        select(func.count(DialogueTagAssignment.id))
                        .select_from(DialogueTagAssignment)
                        .join(
                            DialogueUnit,
                            and_(
                                DialogueUnit.id == DialogueTagAssignment.dialogue_unit_id,
                                DialogueUnit.tenant_id == reception.tenant_id,
                                DialogueUnit.reception_id == reception.id,
                            ),
                        )
                        .where(*tag_filters, *unit_filters)
                    )
                ).scalar_one()
            )
        else:
            tag_total = 0
        tags_result = await session.execute(tags_stmt)
        legacy_tag_assignments = list(tags_result.scalars().all())

        canonical_stmt = (
            select(TagAssignmentFact)
            .join(
                TagAssignmentCurrent,
                and_(
                    TagAssignmentCurrent.fact_id == TagAssignmentFact.id,
                    TagAssignmentCurrent.tenant_id == TagAssignmentFact.tenant_id,
                ),
            )
            .join(
                DialogueUnit,
                and_(
                    DialogueUnit.id == TagAssignmentCurrent.subject_id,
                    DialogueUnit.tenant_id == reception.tenant_id,
                    DialogueUnit.reception_id == reception.id,
                ),
            )
            .where(
                TagAssignmentCurrent.tenant_id == reception.tenant_id,
                TagAssignmentCurrent.subject_type == "dialogue_unit",
                TagAssignmentFact.tenant_id == reception.tenant_id,
                TagAssignmentFact.subject_type == "dialogue_unit",
                TagAssignmentFact.reception_id == reception.id,
                *tag_query_unit_filters,
            )
            .order_by(
                DialogueUnit.unit_index,
                TagAssignmentFact.tag_key,
                TagAssignmentFact.assigned_at,
                TagAssignmentFact.id,
            )
        )
        canonical_facts = list((await session.execute(canonical_stmt)).scalars().all())
        canonical_identities = {(fact.subject_id, fact.tag_key) for fact in canonical_facts}
        canonical_assignments = [
            _canonical_tag_assignment(fact) for fact in canonical_facts if not fact.tombstone
        ]
        legacy_supplements = [
            assignment
            for assignment in legacy_tag_assignments
            if (assignment.dialogue_unit_id, assignment.label_key) not in canonical_identities
        ]
        unit_order = {unit.id: unit.unit_index for unit in units}
        tag_assignments = sorted(
            [*canonical_assignments, *legacy_supplements],
            key=lambda assignment: (
                unit_order.get(assignment.dialogue_unit_id, 2**31),
                assignment.label_key,
                -assignment.priority,
                assignment.group_key,
                assignment.group_version,
                assignment.id,
            ),
        )
        if bounded:
            tag_assignments = tag_assignments[:MAX_WORKSPACE_TAG_ASSIGNMENTS]
            canonical_visible_total = int(
                (
                    await session.execute(
                        select(func.count(TagAssignmentCurrent.id))
                        .select_from(TagAssignmentCurrent)
                        .join(
                            TagAssignmentFact,
                            and_(
                                TagAssignmentFact.id == TagAssignmentCurrent.fact_id,
                                TagAssignmentFact.tenant_id == TagAssignmentCurrent.tenant_id,
                            ),
                        )
                        .join(
                            DialogueUnit,
                            and_(
                                DialogueUnit.id == TagAssignmentCurrent.subject_id,
                                DialogueUnit.tenant_id == reception.tenant_id,
                                DialogueUnit.reception_id == reception.id,
                            ),
                        )
                        .where(
                            TagAssignmentCurrent.tenant_id == reception.tenant_id,
                            TagAssignmentCurrent.subject_type == "dialogue_unit",
                            TagAssignmentFact.tenant_id == reception.tenant_id,
                            TagAssignmentFact.subject_type == "dialogue_unit",
                            TagAssignmentFact.reception_id == reception.id,
                            TagAssignmentFact.tombstone.is_(False),
                            *unit_filters,
                        )
                    )
                ).scalar_one()
            )
            current_for_legacy = (
                select(TagAssignmentCurrent.id)
                .join(
                    TagAssignmentFact,
                    and_(
                        TagAssignmentFact.id == TagAssignmentCurrent.fact_id,
                        TagAssignmentFact.tenant_id == TagAssignmentCurrent.tenant_id,
                    ),
                )
                .where(
                    TagAssignmentCurrent.tenant_id == reception.tenant_id,
                    TagAssignmentCurrent.subject_type == "dialogue_unit",
                    TagAssignmentCurrent.subject_id == DialogueTagAssignment.dialogue_unit_id,
                    TagAssignmentCurrent.tag_key == DialogueTagAssignment.label_key,
                    TagAssignmentFact.tenant_id == reception.tenant_id,
                    TagAssignmentFact.subject_type == "dialogue_unit",
                )
                .exists()
            )
            legacy_supplement_total = int(
                (
                    await session.execute(
                        select(func.count(DialogueTagAssignment.id))
                        .select_from(DialogueTagAssignment)
                        .join(
                            DialogueUnit,
                            and_(
                                DialogueUnit.id == DialogueTagAssignment.dialogue_unit_id,
                                DialogueUnit.tenant_id == reception.tenant_id,
                                DialogueUnit.reception_id == reception.id,
                            ),
                        )
                        .where(
                            *tag_filters,
                            *unit_filters,
                            ~current_for_legacy,
                        )
                    )
                ).scalar_one()
            )
            tag_total = canonical_visible_total + legacy_supplement_total
        else:
            tag_total = len(tag_assignments)
        tags_by_unit: dict[int, list[DialogueTagAssignment]] = {}
        for assignment in tag_assignments:
            tags_by_unit.setdefault(assignment.dialogue_unit_id, []).append(assignment)

        transition_filters = [
            DialogueStateTransition.tenant_id == reception.tenant_id,
            DialogueStateTransition.reception_id == reception.id,
        ]
        transition_total_filters = list(transition_filters)
        if bounded:
            window_unit_ids = select(DialogueUnit.id).where(*unit_filters)
            if effective_window_start == 0:
                transition_filters.append(
                    or_(
                        DialogueStateTransition.dialogue_unit_id.in_(returned_unit_ids),
                        DialogueStateTransition.dialogue_unit_id.is_(None),
                    )
                )
                transition_total_filters.append(
                    or_(
                        DialogueStateTransition.dialogue_unit_id.in_(window_unit_ids),
                        DialogueStateTransition.dialogue_unit_id.is_(None),
                    )
                )
            else:
                transition_filters.append(
                    DialogueStateTransition.dialogue_unit_id.in_(returned_unit_ids)
                )
                transition_total_filters.append(
                    DialogueStateTransition.dialogue_unit_id.in_(window_unit_ids)
                )
        transitions_stmt = (
            select(DialogueStateTransition)
            .where(*transition_filters)
            .order_by(
                DialogueStateTransition.sequence_no,
                DialogueStateTransition.id,
            )
        )
        if bounded:
            transitions_stmt = transitions_stmt.limit(MAX_WORKSPACE_STATE_TRANSITIONS)
            transition_total = int(
                (
                    await session.execute(
                        select(func.count(DialogueStateTransition.id)).where(
                            *transition_total_filters
                        )
                    )
                ).scalar_one()
            )
        else:
            transition_total = 0
        transitions_result = await session.execute(transitions_stmt)
        transitions = list(transitions_result.scalars().all())
        if not bounded:
            transition_total = len(transitions)

        protected_unit_total = int(
            (
                await session.execute(
                    select(func.count(DialogueUnit.id)).where(
                        *all_unit_filters,
                        or_(
                            DialogueUnit.edit_status != "auto",
                            DialogueUnit.id.in_(
                                select(DialogueTagAssignment.dialogue_unit_id).where(
                                    DialogueTagAssignment.tenant_id == reception.tenant_id,
                                    DialogueTagAssignment.reception_id == reception.id,
                                    DialogueTagAssignment.is_current.is_(True),
                                )
                            ),
                        ),
                    )
                )
            ).scalar_one()
        )

        transcript_items: list[ReceptionTranscriptItem] = []
        mapping_source_end = func.coalesce(
            ReceptionRecording.source_end_sec,
            ReceptionRecording.source_start_sec
            + ReceptionRecording.timeline_end_sec
            - ReceptionRecording.timeline_start_sec,
        )
        transcript_filters = [
            ReceptionRecording.tenant_id == reception.tenant_id,
            ReceptionRecording.reception_id == reception.id,
            Segment.tenant_id == reception.tenant_id,
            Segment.end_sec > ReceptionRecording.source_start_sec,
            Segment.start_sec < mapping_source_end,
            or_(
                and_(
                    Segment.text_scrubbed.is_not(None),
                    Segment.text_scrubbed != "",
                ),
                and_(
                    Segment.text_scrubbed.is_(None),
                    Segment.transcript.is_not(None),
                    Segment.transcript != "",
                ),
            ),
        ]
        if bounded:
            transcript_filters.extend(
                [
                    ReceptionRecording.timeline_end_sec > effective_window_start,
                    ReceptionRecording.timeline_start_sec < effective_window_end,
                    Segment.end_sec
                    > ReceptionRecording.source_start_sec
                    + effective_window_start
                    - ReceptionRecording.timeline_start_sec,
                    Segment.start_sec
                    < ReceptionRecording.source_start_sec
                    + effective_window_end
                    - ReceptionRecording.timeline_start_sec,
                ]
            )
        transcript_stmt = (
            select(Segment, ReceptionRecording)
            .join(
                ReceptionRecording,
                and_(
                    ReceptionRecording.recording_id == Segment.recording_id,
                    ReceptionRecording.tenant_id == Segment.tenant_id,
                ),
            )
            .where(*transcript_filters)
            .order_by(
                ReceptionRecording.sequence_no,
                Segment.idx,
                Segment.id,
            )
        )
        if bounded:
            transcript_stmt = transcript_stmt.limit(MAX_WORKSPACE_TRANSCRIPT_ITEMS)
            transcript_total = int(
                (
                    await session.execute(
                        select(func.count(Segment.id))
                        .select_from(Segment)
                        .join(
                            ReceptionRecording,
                            and_(
                                ReceptionRecording.recording_id == Segment.recording_id,
                                ReceptionRecording.tenant_id == Segment.tenant_id,
                            ),
                        )
                        .where(*transcript_filters)
                    )
                ).scalar_one()
            )
        else:
            transcript_total = 0
        segments_result = await session.execute(transcript_stmt)
        for segment, mapping in segments_result.all():
            text = scrubbed_segment_text(segment.text_scrubbed, segment.transcript)
            if not text:
                continue
            source_end = mapping.source_end_sec
            if source_end is None:
                source_end = mapping.source_start_sec + (
                    mapping.timeline_end_sec - mapping.timeline_start_sec
                )
            overlap_start_candidates = [segment.start_sec, mapping.source_start_sec]
            overlap_end_candidates = [segment.end_sec, source_end]
            if bounded:
                overlap_start_candidates.append(
                    mapping.source_start_sec + effective_window_start - mapping.timeline_start_sec
                )
                overlap_end_candidates.append(
                    mapping.source_start_sec + effective_window_end - mapping.timeline_start_sec
                )
            overlap_start = max(overlap_start_candidates)
            overlap_end = min(overlap_end_candidates)
            if overlap_end <= overlap_start:
                continue
            transcript_items.append(
                ReceptionTranscriptItem(
                    segment_id=segment.id,
                    recording_id=segment.recording_id,
                    segment_index=segment.idx,
                    source_start_sec=overlap_start,
                    source_end_sec=overlap_end,
                    timeline_start_sec=(
                        mapping.timeline_start_sec + overlap_start - mapping.source_start_sec
                    ),
                    timeline_end_sec=(
                        mapping.timeline_start_sec + overlap_end - mapping.source_start_sec
                    ),
                    speaker=segment.speaker,
                    text=text,
                    vad_confidence=segment.vad_conf,
                )
            )
        if not bounded:
            transcript_total = len(transcript_items)

        recording_ids = [recording.id for _, recording in recordings]
        provenance_filters = []
        provenance_total_filters = []
        if not bounded or effective_window_start == 0:
            reception_provenance_filter = and_(
                ProvenanceEvent.object_type == "reception",
                ProvenanceEvent.object_ref == str(reception.id),
            )
            provenance_filters.append(reception_provenance_filter)
            provenance_total_filters.append(reception_provenance_filter)
        if recording_ids and (not bounded or effective_window_start == 0):
            recording_provenance_filter = and_(
                ProvenanceEvent.object_type == "recording",
                ProvenanceEvent.object_ref.in_(
                    [str(recording_id) for recording_id in recording_ids]
                ),
            )
            provenance_filters.append(recording_provenance_filter)
            provenance_total_filters.append(recording_provenance_filter)
        if bounded:
            provenance_filters.extend(
                [
                    and_(
                        ProvenanceEvent.object_type == "dialogue_unit",
                        ProvenanceEvent.object_ref.in_(
                            [str(unit_id) for unit_id in returned_unit_ids]
                        ),
                    ),
                    and_(
                        ProvenanceEvent.object_type == "dialogue_tag_assignment",
                        ProvenanceEvent.object_ref.in_(
                            [str(assignment.id) for assignment in legacy_tag_assignments]
                        ),
                    ),
                    and_(
                        ProvenanceEvent.object_type == "dialogue_state_transition",
                        ProvenanceEvent.object_ref.in_(
                            [str(transition.id) for transition in transitions]
                        ),
                    ),
                ]
            )
        else:
            provenance_filters.extend(
                [
                    and_(
                        ProvenanceEvent.object_type == "dialogue_unit",
                        ProvenanceEvent.object_ref.in_(
                            select(sql_cast(DialogueUnit.id, String)).where(*unit_filters)
                        ),
                    ),
                    and_(
                        ProvenanceEvent.object_type == "dialogue_tag_assignment",
                        ProvenanceEvent.object_ref.in_(
                            select(sql_cast(DialogueTagAssignment.id, String)).where(
                                DialogueTagAssignment.tenant_id == reception.tenant_id,
                                DialogueTagAssignment.reception_id == reception.id,
                                DialogueTagAssignment.is_current.is_(True),
                                DialogueTagAssignment.dialogue_unit_id.in_(
                                    select(DialogueUnit.id).where(*unit_filters)
                                ),
                            )
                        ),
                    ),
                    and_(
                        ProvenanceEvent.object_type == "dialogue_state_transition",
                        ProvenanceEvent.object_ref.in_(
                            select(sql_cast(DialogueStateTransition.id, String)).where(
                                *transition_filters
                            )
                        ),
                    ),
                ]
            )
        provenance_total_filters.extend(
            [
                and_(
                    ProvenanceEvent.object_type == "dialogue_unit",
                    ProvenanceEvent.object_ref.in_(
                        select(sql_cast(DialogueUnit.id, String)).where(*unit_filters)
                    ),
                ),
                and_(
                    ProvenanceEvent.object_type == "dialogue_tag_assignment",
                    ProvenanceEvent.object_ref.in_(
                        select(sql_cast(DialogueTagAssignment.id, String)).where(
                            DialogueTagAssignment.tenant_id == reception.tenant_id,
                            DialogueTagAssignment.reception_id == reception.id,
                            DialogueTagAssignment.is_current.is_(True),
                            DialogueTagAssignment.dialogue_unit_id.in_(
                                select(DialogueUnit.id).where(*unit_filters)
                            ),
                        )
                    ),
                ),
                and_(
                    ProvenanceEvent.object_type == "dialogue_state_transition",
                    ProvenanceEvent.object_ref.in_(
                        select(sql_cast(DialogueStateTransition.id, String)).where(
                            *transition_total_filters
                        )
                    ),
                ),
            ]
        )
        provenance_where = and_(
            ProvenanceEvent.tenant_id == reception.tenant_id,
            or_(*provenance_filters),
        )
        provenance_total_where = and_(
            ProvenanceEvent.tenant_id == reception.tenant_id,
            or_(*provenance_total_filters),
        )
        provenance_stmt = (
            select(ProvenanceEvent)
            .where(provenance_where)
            .order_by(ProvenanceEvent.occurred_at, ProvenanceEvent.id)
        )
        if bounded:
            provenance_stmt = provenance_stmt.limit(MAX_WORKSPACE_PROVENANCE_EVENTS)
            provenance_total = int(
                (
                    await session.execute(
                        select(func.count(ProvenanceEvent.id)).where(provenance_total_where)
                    )
                ).scalar_one()
            )
        else:
            provenance_total = 0
        provenance_result = await session.execute(provenance_stmt)
        provenance_events = list(provenance_result.scalars().all())
        if not bounded:
            provenance_total = len(provenance_events)

        previous_dialogue_unit: DialogueUnit | None = None
        next_dialogue_unit: DialogueUnit | None = None
        if bounded:
            if units:
                previous_predicate = DialogueUnit.unit_index < min(
                    unit.unit_index for unit in units
                )
                next_predicate = DialogueUnit.unit_index > max(unit.unit_index for unit in units)
            else:
                previous_predicate = DialogueUnit.end_sec <= effective_window_start
                next_predicate = DialogueUnit.start_sec >= effective_window_end
            previous_dialogue_unit = (
                await session.execute(
                    select(DialogueUnit)
                    .where(*all_unit_filters, previous_predicate)
                    .order_by(
                        DialogueUnit.unit_index.desc(),
                        DialogueUnit.version.desc(),
                        DialogueUnit.id.desc(),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            next_dialogue_unit = (
                await session.execute(
                    select(DialogueUnit)
                    .where(*all_unit_filters, next_predicate)
                    .order_by(
                        DialogueUnit.unit_index,
                        DialogueUnit.version.desc(),
                        DialogueUnit.id,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()

        active_audio_operation = (
            await session.execute(
                select(ReceptionAudioOperation)
                .where(
                    ReceptionAudioOperation.tenant_id == reception.tenant_id,
                    ReceptionAudioOperation.reception_id == reception.id,
                    ReceptionAudioOperation.status.in_(
                        (
                            "queued",
                            "claimed",
                            "probing",
                            "slicing",
                            "assembling",
                            "encrypting",
                            "verifying",
                            "committing",
                        )
                    ),
                )
                .order_by(ReceptionAudioOperation.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        window = (
            ReceptionWorkspaceWindow(
                start_sec=effective_window_start,
                end_sec=effective_window_end,
                size_sec=effective_window_size,
                reception_duration_sec=reception_duration,
                total_dialogue_units=reception_unit_total,
                protected_dialogue_units=protected_unit_total,
                dialogue_units=WorkspaceCollectionWindow(
                    total=unit_total,
                    returned=len(units),
                    limit=MAX_WORKSPACE_DIALOGUE_UNITS,
                ),
                tag_assignments=WorkspaceCollectionWindow(
                    total=tag_total,
                    returned=len(tag_assignments),
                    limit=MAX_WORKSPACE_TAG_ASSIGNMENTS,
                ),
                state_transitions=WorkspaceCollectionWindow(
                    total=transition_total,
                    returned=len(transitions),
                    limit=MAX_WORKSPACE_STATE_TRANSITIONS,
                ),
                transcript_items=WorkspaceCollectionWindow(
                    total=transcript_total,
                    returned=len(transcript_items),
                    limit=MAX_WORKSPACE_TRANSCRIPT_ITEMS,
                ),
                provenance_events=WorkspaceCollectionWindow(
                    total=provenance_total,
                    returned=len(provenance_events),
                    limit=MAX_WORKSPACE_PROVENANCE_EVENTS,
                ),
            )
            if bounded
            else None
        )

        return ReceptionWorkspace(
            reception=reception,
            recordings=recordings,
            dialogue_units=units,
            tag_assignments=tag_assignments,
            tag_assignments_by_unit=tags_by_unit,
            state_transitions=transitions,
            transcript_items=transcript_items,
            provenance_events=provenance_events,
            window=window,
            previous_dialogue_unit=previous_dialogue_unit,
            next_dialogue_unit=next_dialogue_unit,
            active_audio_operation=active_audio_operation,
        )

    async def _renumber_dialogue_units(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        reception_id: int,
    ) -> None:
        """Keep unit_index dense and chronological without unique-key swaps."""
        result = await session.execute(
            select(DialogueUnit)
            .where(
                DialogueUnit.tenant_id == tenant_id,
                DialogueUnit.reception_id == reception_id,
            )
            .order_by(DialogueUnit.start_sec, DialogueUnit.end_sec, DialogueUnit.id)
        )
        units = list(result.scalars().all())
        if not units:
            return

        temporary_base = max(unit.unit_index for unit in units) + len(units) + 1_024
        for position, unit in enumerate(units):
            unit.unit_index = temporary_base + position
        await session.flush()
        for position, unit in enumerate(units):
            unit.unit_index = position
        await session.flush()

    async def _state_transition_snapshots(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        reception_id: int,
    ) -> list[dict[str, Any]]:
        result = await session.execute(
            select(DialogueStateTransition)
            .where(
                DialogueStateTransition.tenant_id == tenant_id,
                DialogueStateTransition.reception_id == reception_id,
            )
            .order_by(
                DialogueStateTransition.sequence_no,
                DialogueStateTransition.id,
            )
        )
        return [_state_transition_snapshot(transition) for transition in result.scalars().all()]

    @staticmethod
    async def _speaker_refs_for_evidence(
        session: AsyncSession,
        *,
        tenant_id: str,
        evidence_refs: Sequence[Any],
    ) -> list[str]:
        segment_ids: set[int] = set()
        for raw_ref in evidence_refs:
            if not isinstance(raw_ref, Mapping):
                continue
            raw_segment_id = raw_ref.get("segment_id")
            if raw_segment_id is None or isinstance(raw_segment_id, bool):
                continue
            try:
                segment_id = int(raw_segment_id)
            except (TypeError, ValueError):
                continue
            if segment_id > 0:
                segment_ids.add(segment_id)
        if not segment_ids:
            return []
        result = await session.execute(
            select(Segment.speaker)
            .where(
                Segment.tenant_id == tenant_id,
                Segment.id.in_(segment_ids),
                Segment.speaker.is_not(None),
            )
            .order_by(Segment.id)
        )
        return list(
            dict.fromkeys(
                speaker for speaker in result.scalars().all() if speaker is not None and speaker
            )
        )

    async def _rebuild_state_transitions(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        reception_id: int,
        trigger: str,
    ) -> None:
        """Rebuild one dense, chronological state chain after geometry edits."""
        await session.execute(
            delete(DialogueStateTransition).where(
                DialogueStateTransition.tenant_id == tenant_id,
                DialogueStateTransition.reception_id == reception_id,
            )
        )
        await session.flush()

        units_result = await session.execute(
            select(DialogueUnit)
            .where(
                DialogueUnit.tenant_id == tenant_id,
                DialogueUnit.reception_id == reception_id,
            )
            .order_by(
                DialogueUnit.unit_index,
                DialogueUnit.start_sec,
                DialogueUnit.id,
            )
        )
        units = list(units_result.scalars().all())
        previous_state = "__start__"
        rebuilt: list[DialogueStateTransition] = []
        for sequence_no, unit in enumerate(units):
            to_state = unit.business_stage or "__unknown__"
            confidence = _unit_stage_confidence(unit)
            rebuilt.append(
                DialogueStateTransition(
                    tenant_id=tenant_id,
                    reception_id=reception_id,
                    dialogue_unit_id=unit.id,
                    sequence_no=sequence_no,
                    from_state=previous_state,
                    to_state=to_state,
                    trigger=trigger[:128],
                    confidence=confidence,
                    evidence_refs=_clip_evidence_refs(
                        unit.segment_refs,
                        start_sec=unit.start_sec,
                        end_sec=unit.end_sec,
                    ),
                    algorithm_version="manual-edit-v1",
                )
            )
            previous_state = to_state
        session.add_all(rebuilt)
        await session.flush()

    async def create_reception(
        self,
        tenant_id: str,
        body: ReceptionCreate,
        *,
        actor: str,
    ) -> ReceptionWorkspace:
        """Persist a validated logical mapping and its creation provenance."""
        recording_ids = [mapping.recording_id for mapping in body.recordings]
        async with self._session_factory() as session, session.begin():
            recordings_result = await session.execute(
                select(Recording).where(
                    Recording.tenant_id == tenant_id,
                    Recording.id.in_(recording_ids),
                )
            )
            recordings = {
                recording.id: recording for recording in recordings_result.scalars().all()
            }
            missing = [
                recording_id for recording_id in recording_ids if recording_id not in recordings
            ]
            if missing:
                raise RecordingNotFoundError(
                    detail={"recording_ids": missing},
                )
            active_assignments = (
                await session.execute(
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
                        ReceptionRecording.recording_id.in_(recording_ids),
                        Reception.tenant_id == tenant_id,
                        Reception.status != "archived",
                    )
                )
            ).all()
            if active_assignments:
                raise ConflictError(
                    "A recording already belongs to an active reception",
                    code="RECORDING_ALREADY_ASSIGNED",
                    detail={
                        "assignments": [
                            {
                                "recording_id": int(recording_id),
                                "reception_id": int(assigned_reception_id),
                            }
                            for recording_id, assigned_reception_id in active_assignments
                        ]
                    },
                )

            mismatched_store = [
                recording.id
                for recording in recordings.values()
                if recording.store_id != body.store_id
            ]
            if mismatched_store:
                raise ValidationError(
                    "All reception recordings must belong to the reception store",
                    code="RECEPTION_STORE_MISMATCH",
                    detail={
                        "store_id": body.store_id,
                        "recording_ids": sorted(mismatched_store),
                    },
                )
            normalized_source_ends: dict[int, float | None] = {}
            out_of_bounds: list[int] = []
            for mapping in body.recordings:
                recording = recordings[mapping.recording_id]
                verified_duration_ms = verified_recording_duration_ms(recording)
                verified_duration_sec = (
                    verified_duration_ms / 1_000 if verified_duration_ms is not None else None
                )
                source_end = mapping.source_end_sec
                if source_end is None and verified_duration_sec is not None:
                    source_end = verified_duration_sec
                if verified_duration_sec is not None and (
                    mapping.source_start_sec >= verified_duration_sec
                    or (source_end is not None and source_end > verified_duration_sec + 0.001)
                ):
                    out_of_bounds.append(mapping.recording_id)
                normalized_source_ends[mapping.recording_id] = source_end
            if out_of_bounds:
                raise ValidationError(
                    "Reception source interval exceeds verified audio duration",
                    code="RECEPTION_SOURCE_OUT_OF_BOUNDS",
                    detail={"recording_ids": sorted(set(out_of_bounds))},
                )

            if body.external_session_id is not None:
                duplicate_result = await session.execute(
                    select(Reception.id).where(
                        Reception.tenant_id == tenant_id,
                        Reception.external_session_id == body.external_session_id,
                    )
                )
                if duplicate_result.scalar_one_or_none() is not None:
                    raise ConflictError(
                        "Reception external session already exists",
                        code="DUPLICATE_RECEPTION_SESSION",
                        detail={"external_session_id": body.external_session_id},
                    )

            # Creation persists only the logical map.  Physical publication is
            # explicit through POST /receptions/{id}/merge, even when an
            # assembler happens to be configured.
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
                source_end = normalized_source_ends[mapping.recording_id]
                session.add(
                    ReceptionRecording(
                        tenant_id=tenant_id,
                        reception_id=reception.id,
                        recording_id=mapping.recording_id,
                        sequence_no=mapping.sequence_no,
                        timeline_start_sec=mapping.timeline_start_sec,
                        timeline_end_sec=mapping.timeline_end_sec,
                        source_start_sec=mapping.source_start_sec,
                        source_end_sec=source_end,
                        source_start_ms=seconds_to_milliseconds(mapping.source_start_sec),
                        source_end_ms=(
                            seconds_to_milliseconds(source_end) if source_end is not None else None
                        ),
                        timeline_start_ms=seconds_to_milliseconds(mapping.timeline_start_sec),
                        timeline_end_ms=seconds_to_milliseconds(mapping.timeline_end_sec),
                        gap_before_ms=seconds_to_milliseconds(mapping.gap_before_sec),
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
                            "source_end_sec": normalized_source_ends[mapping.recording_id],
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
                        "physical_audio_status": (
                            "pending" if physical_pending else "not_requested"
                        ),
                        "version": 1,
                    },
                    occurred_at=datetime.now(UTC),
                )
            )

        return await self.get_workspace(reception.id, tenant_id)

    async def get_workspace(
        self,
        reception_id: int,
        tenant_id: str,
        *,
        agent_user_id: int | None = None,
    ) -> ReceptionWorkspace:
        """Load the entire listening workbench with bounded explicit queries."""
        async with self._session_factory() as session:
            reception = await self._find_reception(
                session,
                reception_id,
                tenant_id,
                agent_user_id=agent_user_id,
            )
            return await self._load_workspace(session, reception)

    async def get_workspace_window(
        self,
        reception_id: int,
        tenant_id: str,
        *,
        window_start_sec: float,
        window_size_sec: float,
        agent_user_id: int | None = None,
    ) -> ReceptionWorkspace:
        """Load one bounded, tenant-authorized reception-timeline window."""
        async with self._session_factory() as session:
            reception = await self._find_reception(
                session,
                reception_id,
                tenant_id,
                agent_user_id=agent_user_id,
            )
            return await self._load_workspace(
                session,
                reception,
                window_start_sec=window_start_sec,
                window_size_sec=window_size_sec,
            )

    async def get_audio_asset(
        self,
        reception_id: int,
        tenant_id: str,
        *,
        recording_id: int | None = None,
        agent_user_id: int | None = None,
    ) -> ReceptionAudioAsset:
        """Authorize one source/merged audio file and confine it to working_dir."""
        playback_geometry: ReceptionPlaybackGeometry | None = None
        merged_legal_end_ms: int | None = None
        async with self._session_factory() as session:
            reception = await self._find_reception(
                session,
                reception_id,
                tenant_id,
                agent_user_id=agent_user_id,
            )
            if recording_id is None:
                persisted_path = reception.merged_audio_path
                recording = None
                mapping = None
                mappings = list(
                    (
                        await session.execute(
                            select(ReceptionRecording).where(
                                ReceptionRecording.tenant_id == tenant_id,
                                ReceptionRecording.reception_id == reception_id,
                            )
                        )
                    ).scalars()
                )
                if mappings:
                    merged_legal_end_ms = max(
                        reception_mapping_playback_geometry(item).timeline_end_ms
                        for item in mappings
                    )
            else:
                result = await session.execute(
                    select(ReceptionRecording, Recording)
                    .join(
                        Recording,
                        Recording.id == ReceptionRecording.recording_id,
                    )
                    .where(
                        ReceptionRecording.tenant_id == tenant_id,
                        ReceptionRecording.reception_id == reception_id,
                        ReceptionRecording.recording_id == recording_id,
                        Recording.tenant_id == tenant_id,
                    )
                )
                row = result.one_or_none()
                mapping, recording = row if row is not None else (None, None)
                persisted_path = recording.path if recording is not None else None
                if mapping is not None:
                    playback_geometry = reception_mapping_playback_geometry(mapping)

        if not persisted_path:
            raise NotFoundError(
                "Audio asset not found",
                code="AUDIO_NOT_FOUND",
            )
        if recording is not None:
            assert mapping is not None
            assert playback_geometry is not None
            if self._mapping_requires_clipped_playback(mapping, recording):
                asset = await self._materialize_mapping_audio_asset(
                    mapping=mapping,
                    recording=recording,
                    tenant_id=tenant_id,
                )
            else:
                asset = await self._recording_audio_asset(
                    recording,
                    tenant_id=tenant_id,
                )
            return replace(
                asset,
                time_origin_ms=playback_geometry.time_origin_ms,
                legal_source_start_ms=playback_geometry.legal_source_start_ms,
                legal_source_end_ms=playback_geometry.legal_source_end_ms,
            )
        if str(persisted_path).casefold().endswith(".enc"):
            asset = await self._decrypt_audio_asset(
                str(persisted_path),
                original_path=str(persisted_path)[: -len(".enc")],
                tenant_id=tenant_id,
            )
        else:
            try:
                asset = resolve_confined_audio_file(
                    self._audio_root,
                    str(persisted_path),
                )
            except ValueError as exc:
                raise NotFoundError(
                    "Audio asset not found",
                    code="AUDIO_NOT_FOUND",
                ) from exc
        return replace(
            asset,
            time_origin_ms=0,
            legal_source_start_ms=0,
            legal_source_end_ms=merged_legal_end_ms,
        )

    async def propose_reception_groups(
        self,
        tenant_id: str,
        body: ReceptionMergeProposalRequest,
        *,
        merger: ReceptionMerger | None = None,
    ) -> ReceptionMergeProposalResult:
        """Explain pair and group decisions without mutating persistent state."""
        async with self._session_factory() as session:
            recording_result = await session.execute(
                select(Recording).where(
                    Recording.tenant_id == tenant_id,
                    Recording.id.in_(body.recording_ids),
                )
            )
            by_id = {recording.id: recording for recording in recording_result.scalars().all()}
            missing = [
                recording_id for recording_id in body.recording_ids if recording_id not in by_id
            ]
            if missing:
                raise RecordingNotFoundError(
                    detail={"recording_ids": missing},
                )

            durations = {
                recording_id: verified_duration_ms / 1_000
                for recording_id, recording in by_id.items()
                if (verified_duration_ms := verified_recording_duration_ms(recording)) is not None
            }

        unavailable_duration = [
            recording_id for recording_id in body.recording_ids if recording_id not in durations
        ]
        if unavailable_duration:
            raise ValidationError(
                "Recording duration is unavailable; index the recording first",
                code="RECORDING_DURATION_UNAVAILABLE",
                detail={"recording_ids": unavailable_duration},
            )
        unavailable_time = [
            recording_id
            for recording_id in body.recording_ids
            if by_id[recording_id].recorded_at is None
        ]
        if unavailable_time:
            raise ValidationError(
                "Recording start time is unavailable",
                code="RECORDING_TIME_UNAVAILABLE",
                detail={"recording_ids": unavailable_time},
            )

        for left_id, right_id in body.force_merge:
            if by_id[left_id].store_id != by_id[right_id].store_id:
                raise ValidationError(
                    "Manual constraints cannot merge recordings across stores",
                    code="RECEPTION_STORE_MISMATCH",
                    detail={"recording_ids": [left_id, right_id]},
                )

        constraints = ManualReceptionConstraints.from_pairs(
            force_merge=((str(left_id), str(right_id)) for left_id, right_id in body.force_merge),
            force_split=((str(left_id), str(right_id)) for left_id, right_id in body.force_split),
        )
        candidates: list[RecordingCandidate] = []
        for recording_id in body.recording_ids:
            recording = by_id[recording_id]
            assert recording.recorded_at is not None
            duration = durations[recording.id]
            assert duration is not None
            candidates.append(
                RecordingCandidate(
                    recording_id=str(recording.id),
                    tenant_id=tenant_id,
                    store_id=recording.store_id,
                    started_at=recording.recorded_at,
                    ended_at=(recording.recorded_at + timedelta(seconds=duration)),
                    agent_id=recording.agent_name,
                    customer_voiceprint_id=recording.customer_hash,
                )
            )

        engine = merger or ReceptionMerger()
        proposals = [
            engine.evaluate_pair(left, right, constraints=constraints)
            for left, right in combinations(candidates, 2)
        ]
        groups = engine.propose_groups(candidates, constraints=constraints)
        return ReceptionMergeProposalResult(
            recording_ids=list(body.recording_ids),
            proposals=proposals,
            groups=groups,
        )

    @staticmethod
    def _recording_durations(
        *,
        recordings: Sequence[Recording],
        existing: dict[int, ReceptionRecording],
    ) -> dict[int, float]:
        durations: dict[int, float] = {}
        for recording in recordings:
            mapping = existing.get(recording.id)
            if mapping is not None:
                if mapping.source_end_sec is not None:
                    durations[recording.id] = mapping.source_end_sec - mapping.source_start_sec
                else:
                    durations[recording.id] = mapping.timeline_end_sec - mapping.timeline_start_sec
            else:
                verified_duration_ms = verified_recording_duration_ms(recording)
                if verified_duration_ms is not None:
                    durations[recording.id] = verified_duration_ms / 1_000

        unavailable = [recording.id for recording in recordings if recording.id not in durations]
        if unavailable:
            raise ValidationError(
                "Recording duration is unavailable; index the recording first",
                code="RECORDING_DURATION_UNAVAILABLE",
                detail={"recording_ids": unavailable},
            )
        return durations

    async def _prepare_physical_merge(
        self,
        reception_id: int,
        tenant_id: str,
        body: ReceptionMergeRequest,
        *,
        timeline_override: Mapping[int, ReceptionTimelineSliceOverride] | None = None,
        physical_generation: str | None = None,
        on_physical_stage: ReceptionPhysicalStageHook | None = None,
    ) -> _PreparedPhysicalMerge:
        """Build one unique physical generation without holding DB locks."""
        assert self._audio_assembler is not None
        normalized_override = _normalize_timeline_override(
            body.recording_ids,
            timeline_override,
        )
        async with self._session_factory() as session:
            reception = await self._find_reception(
                session,
                reception_id,
                tenant_id,
            )
            if reception.version != body.expected_version:
                raise self._version_conflict(
                    object_type="reception",
                    object_id=reception.id,
                    expected=body.expected_version,
                    actual=reception.version,
                )
            recording_result = await session.execute(
                select(Recording).where(
                    Recording.tenant_id == tenant_id,
                    Recording.id.in_(body.recording_ids),
                )
            )
            by_id = {recording.id: recording for recording in recording_result.scalars().all()}
            missing = [
                recording_id for recording_id in body.recording_ids if recording_id not in by_id
            ]
            if missing:
                raise RecordingNotFoundError(detail={"recording_ids": missing})
            recordings = [by_id[recording_id] for recording_id in body.recording_ids]
            store_mismatch = [
                recording.id for recording in recordings if recording.store_id != reception.store_id
            ]
            if store_mismatch:
                raise ValidationError(
                    "All reception recordings must belong to the reception store",
                    code="RECEPTION_STORE_MISMATCH",
                    detail={"recording_ids": store_mismatch},
                )
            existing_result = await session.execute(
                select(ReceptionRecording).where(
                    ReceptionRecording.tenant_id == tenant_id,
                    ReceptionRecording.reception_id == reception.id,
                )
            )
            existing = {row.recording_id: row for row in existing_result.scalars().all()}
            recording_sources = tuple(
                _recording_source_snapshot(
                    recording,
                    existing.get(recording.id),
                    gap_before_sec=(
                        normalized_override[recording.id].gap_before_sec
                        if normalized_override is not None
                        else existing[recording.id].gap_before_sec
                        if sequence_no > 0 and recording.id in existing
                        else 0.0
                    ),
                    geometry_override=(
                        normalized_override[recording.id]
                        if normalized_override is not None
                        else None
                    ),
                )
                for sequence_no, recording in enumerate(recordings)
            )
            assembly_sources = [
                AudioAssemblySource(
                    path=recording.path,
                    source_start_sec=(
                        normalized_override[recording.id].source_start_sec
                        if normalized_override is not None
                        else existing[recording.id].source_start_sec
                        if recording.id in existing
                        else 0.0
                    ),
                    source_end_sec=(
                        normalized_override[recording.id].source_end_sec
                        if normalized_override is not None
                        else existing[recording.id].source_end_sec
                        if recording.id in existing
                        else None
                    ),
                    gap_before_sec=(
                        normalized_override[recording.id].gap_before_sec
                        if normalized_override is not None
                        else existing[recording.id].gap_before_sec
                        if sequence_no > 0 and recording.id in existing
                        else 0.0
                    ),
                )
                for sequence_no, recording in enumerate(recordings)
            ]

        generation = physical_generation or secrets.token_hex(8)
        target_relative = reception_physical_generation_relative_path(
            tenant_id=tenant_id,
            reception_id=reception_id,
            reception_version=body.expected_version + 1,
            generation=generation,
        )
        output = resolve_safe_audio_output(self._audio_root, target_relative)
        encrypted_output = Path(f"{output}.enc")
        source_assets: list[ReceptionAudioAsset] = []
        resolved_assembly_sources: list[AudioAssemblySource] = []
        try:
            for recording, assembly_source in zip(
                recordings,
                assembly_sources,
                strict=True,
            ):
                if recording.audio_encrypted_path:
                    asset = await self._recording_audio_asset(
                        recording,
                        tenant_id=tenant_id,
                    )
                    source_assets.append(asset)
                else:
                    # The production assembler performs strict root, symlink,
                    # extension, identity, and size validation immediately
                    # before launching ffmpeg.
                    asset = ReceptionAudioAsset(
                        path=Path(str(recording.path)),
                        media_type="application/octet-stream",
                    )
                    source_assets.append(asset)
                resolved_assembly_sources.append(
                    AudioAssemblySource(
                        path=asset.path,
                        source_start_sec=assembly_source.source_start_sec,
                        source_end_sec=assembly_source.source_end_sec,
                        gap_before_sec=assembly_source.gap_before_sec,
                    )
                )
            try:
                audio_manifest = await self._audio_assembler.assemble(
                    resolved_assembly_sources,
                    target_relative,
                )
            except Exception as exc:
                raise APIError(
                    "Physical audio assembly failed",
                    code="AUDIO_ASSEMBLY_FAILED",
                    status_code=503,
                    detail={"reason": str(exc)},
                ) from exc
            manifest_output = resolve_safe_audio_output(
                self._audio_root,
                audio_manifest.output_path,
            )
            if (
                manifest_output != output
                or not manifest_output.is_file()
                or len(audio_manifest.inputs) != len(recordings)
                or audio_manifest.total_duration_sec <= 0
            ):
                raise APIError(
                    "Audio assembler did not produce the requested safe output",
                    code="AUDIO_ASSEMBLY_INVALID_OUTPUT",
                    status_code=503,
                )
            durations = {
                recording.id: manifest_input.duration_sec
                for recording, manifest_input in zip(
                    recordings,
                    audio_manifest.inputs,
                    strict=True,
                )
            }
            if any(duration <= 0 for duration in durations.values()):
                raise APIError(
                    "Audio assembler returned an invalid input duration",
                    code="AUDIO_ASSEMBLY_INVALID_OUTPUT",
                    status_code=503,
                )

            if on_physical_stage is not None:
                await on_physical_stage("encrypting")
            if self._audio_crypto is None:
                merged_audio_path = str(output)
            else:
                try:
                    encryption_meta = await asyncio.to_thread(
                        self._audio_crypto.encrypt_file,
                        output,
                        encrypted_output,
                    )
                    if encryption_meta.size_bytes <= 0 or not encrypted_output.is_file():
                        raise ValueError("encrypted output is empty")
                except Exception as exc:
                    raise APIError(
                        "Merged audio encryption failed",
                        code="AUDIO_ENCRYPTION_FAILED",
                        status_code=503,
                    ) from exc
                await asyncio.to_thread(output.unlink, missing_ok=True)
                merged_audio_path = str(encrypted_output)

            if on_physical_stage is not None:
                await on_physical_stage("verifying")
            return _PreparedPhysicalMerge(
                manifest=audio_manifest,
                merged_audio_path=merged_audio_path,
                durations=durations,
                recording_sources=recording_sources,
            )
        except BaseException:
            await asyncio.gather(
                asyncio.to_thread(output.unlink, missing_ok=True),
                asyncio.to_thread(encrypted_output.unlink, missing_ok=True),
            )
            raise
        finally:
            for asset in source_assets:
                if asset.delete_after_open:
                    await asyncio.to_thread(asset.path.unlink, missing_ok=True)

    async def merge_recordings(
        self,
        reception_id: int,
        tenant_id: str,
        body: ReceptionMergeRequest,
        *,
        actor: str,
        timeline_override: Mapping[int, ReceptionTimelineSliceOverride] | None = None,
        before_commit: ReceptionMergeBeforeCommitHook | None = None,
        physical_generation: str | None = None,
        on_physical_stage: ReceptionPhysicalStageHook | None = None,
        after_physical_prepare: ReceptionPhysicalPrepareHook | None = None,
    ) -> ReceptionWorkspace:
        """Append/reorder mappings and optionally publish a verified internal plan."""
        normalized_override = _normalize_timeline_override(
            body.recording_ids,
            timeline_override,
        )
        if body.mode in {"physical", "both"} and self._audio_assembler is None:
            raise APIError(
                "Physical audio assembler is not configured",
                code="AUDIO_ASSEMBLER_UNAVAILABLE",
                status_code=503,
                detail={"requested_mode": body.mode},
            )

        prepared = (
            await self._prepare_physical_merge(
                reception_id,
                tenant_id,
                body,
                timeline_override=normalized_override,
                physical_generation=physical_generation,
                on_physical_stage=on_physical_stage,
            )
            if body.mode in {"physical", "both"}
            else None
        )
        retired_audio_path: str | None = None
        async with (
            _PhysicalArtifactGuard(prepared),
            self._session_factory() as session,
            session.begin(),
        ):
            if prepared is not None and after_physical_prepare is not None:
                await after_physical_prepare(prepared)
            reception = await self._find_reception(
                session,
                reception_id,
                tenant_id,
                for_update=True,
            )
            if reception.version != body.expected_version:
                raise self._version_conflict(
                    object_type="reception",
                    object_id=reception.id,
                    expected=body.expected_version,
                    actual=reception.version,
                )
            previous_merged_audio_path = reception.merged_audio_path

            recording_result = await session.execute(
                select(Recording).where(
                    Recording.tenant_id == tenant_id,
                    Recording.id.in_(body.recording_ids),
                )
            )
            by_id = {recording.id: recording for recording in recording_result.scalars().all()}
            missing = [
                recording_id for recording_id in body.recording_ids if recording_id not in by_id
            ]
            if missing:
                raise RecordingNotFoundError(
                    detail={"recording_ids": missing},
                )
            recordings = [by_id[recording_id] for recording_id in body.recording_ids]
            store_mismatch = [
                recording.id for recording in recordings if recording.store_id != reception.store_id
            ]
            if store_mismatch:
                raise ValidationError(
                    "All reception recordings must belong to the reception store",
                    code="RECEPTION_STORE_MISMATCH",
                    detail={"recording_ids": store_mismatch},
                )
            existing_result = await session.execute(
                select(ReceptionRecording).where(
                    ReceptionRecording.tenant_id == tenant_id,
                    ReceptionRecording.reception_id == reception.id,
                )
            )
            existing_rows = list(existing_result.scalars().all())
            existing = {row.recording_id: row for row in existing_rows}
            if prepared is not None:
                current_recording_sources = tuple(
                    _recording_source_snapshot(
                        recording,
                        existing.get(recording.id),
                        gap_before_sec=(
                            normalized_override[recording.id].gap_before_sec
                            if normalized_override is not None
                            else existing[recording.id].gap_before_sec
                            if sequence_no > 0 and recording.id in existing
                            else 0.0
                        ),
                        geometry_override=(
                            normalized_override[recording.id]
                            if normalized_override is not None
                            else None
                        ),
                    )
                    for sequence_no, recording in enumerate(recordings)
                )
                if current_recording_sources != prepared.recording_sources:
                    raise ConflictError(
                        "A recording audio source or timeline changed during physical assembly",
                        code="RECORDING_AUDIO_CHANGED",
                        detail={"recording_ids": body.recording_ids},
                    )
            dialogue_result = await session.execute(
                select(DialogueUnit)
                .where(
                    DialogueUnit.tenant_id == tenant_id,
                    DialogueUnit.reception_id == reception.id,
                )
                .order_by(DialogueUnit.unit_index)
                .with_for_update()
            )
            dialogue_units = list(dialogue_result.scalars().all())
            locked_unit_ids = [unit.id for unit in dialogue_units if unit.edit_status == "locked"]

            if prepared is not None:
                merged_audio_path: str | None = prepared.merged_audio_path
                audio_manifest: AudioAssemblyManifest | None = prepared.manifest
                durations = dict(prepared.durations)
            else:
                merged_audio_path = None
                audio_manifest = None
                durations = self._recording_durations(
                    recordings=recordings,
                    existing=existing,
                )

            manifest_inputs = (
                list(audio_manifest.inputs)
                if audio_manifest is not None
                else [None] * len(recordings)
            )
            requested_geometry: list[tuple[Recording, float, float, float]] = []
            timeline_sources: list[AudioTimelineSource] = []
            for _sequence_no, (recording, manifest_input) in enumerate(
                zip(recordings, manifest_inputs, strict=True)
            ):
                old = existing.get(recording.id)
                duration = durations[recording.id]
                source_start = (
                    normalized_override[recording.id].source_start_sec
                    if normalized_override is not None
                    else manifest_input.source_start_sec
                    if manifest_input is not None
                    else old.source_start_sec
                    if old is not None and body.mode == "logical"
                    else 0.0
                )
                source_end = (
                    normalized_override[recording.id].source_end_sec
                    if normalized_override is not None
                    else manifest_input.source_end_sec
                    if manifest_input is not None
                    else source_start + duration
                )
                assert source_end is not None
                gap_before = (
                    normalized_override[recording.id].gap_before_sec
                    if normalized_override is not None
                    else manifest_input.gap_before_sec
                    if manifest_input is not None
                    else 0.0
                )
                source_start_ms = seconds_to_milliseconds(source_start)
                source_end_ms = seconds_to_milliseconds(source_end)
                verified_duration_ms = verified_recording_duration_ms(recording) or source_end_ms
                requested_geometry.append((recording, source_start, source_end, gap_before))
                timeline_sources.append(
                    AudioTimelineSource(
                        source_id=recording.id,
                        source_start_ms=source_start_ms,
                        source_end_ms=source_end_ms,
                        verified_duration_ms=max(
                            verified_duration_ms,
                            source_end_ms,
                        ),
                        gap_before_ms=seconds_to_milliseconds(gap_before),
                    )
                )

            canonical_timeline = AudioTimelinePlanner().plan(timeline_sources)
            plans: list[dict[str, Any]] = []
            for (recording, _source_start, _source_end, _gap), planned in zip(
                requested_geometry,
                canonical_timeline.slices,
                strict=True,
            ):
                plans.append(
                    {
                        "recording": recording,
                        "sequence_no": planned.sequence_no,
                        "timeline_start_sec": milliseconds_to_seconds(planned.timeline_start_ms),
                        "timeline_end_sec": milliseconds_to_seconds(planned.timeline_end_ms),
                        "source_start_sec": milliseconds_to_seconds(planned.source_start_ms),
                        "source_end_sec": milliseconds_to_seconds(planned.source_end_ms),
                        "decision_source": "manual",
                        "merge_confidence": 1.0,
                        "gap_before_sec": milliseconds_to_seconds(planned.gap_before_ms),
                        "merge_reasons": {
                            "manual_reorder": True,
                            "actor": actor,
                        },
                    }
                )

            normalized_existing_geometry = [
                (
                    mapping.recording_id,
                    mapping.sequence_no,
                    round(mapping.timeline_start_sec, 6),
                    round(mapping.timeline_end_sec, 6),
                    round(mapping.source_start_sec, 6),
                    round(
                        (
                            mapping.source_end_sec
                            if mapping.source_end_sec is not None
                            else mapping.source_start_sec
                            + mapping.timeline_end_sec
                            - mapping.timeline_start_sec
                        ),
                        6,
                    ),
                    round(mapping.gap_before_sec, 6),
                )
                for mapping in sorted(
                    existing_rows,
                    key=lambda item: item.sequence_no,
                )
            ]
            normalized_planned_geometry = [
                (
                    int(plan["recording"].id),
                    int(plan["sequence_no"]),
                    round(float(plan["timeline_start_sec"]), 6),
                    round(float(plan["timeline_end_sec"]), 6),
                    round(float(plan["source_start_sec"]), 6),
                    round(float(plan["source_end_sec"]), 6),
                    round(float(plan["gap_before_sec"]), 6),
                )
                for plan in plans
            ]
            timeline_changed = normalized_existing_geometry != normalized_planned_geometry
            if timeline_changed and locked_unit_ids:
                raise ConflictError(
                    "A changed reception timeline cannot replace locked dialogue units",
                    code="LOCKED_DIALOGUE_UNITS_PRESENT",
                    detail={"dialogue_unit_ids": locked_unit_ids},
                )

            if audio_manifest is not None:
                for plan, manifest_input in zip(
                    plans,
                    audio_manifest.inputs,
                    strict=True,
                ):
                    plan["merge_reasons"]["physical_manifest"] = {
                        "source_path": manifest_input.path,
                        "sha256": manifest_input.sha256,
                        "codec": manifest_input.codec,
                        "sample_rate": manifest_input.sample_rate,
                        "channels": manifest_input.channels,
                    }

            before = [_mapping_snapshot(row) for row in existing_rows]
            invalidated_artifacts: dict[str, list[int]] = {
                "dialogue_unit_ids": [],
                "dialogue_tag_assignment_ids": [],
                "dialogue_state_transition_ids": [],
            }
            reception_status = "needs_review" if timeline_changed else reception.status
            if timeline_changed and dialogue_units:
                tag_result = await session.execute(
                    select(DialogueTagAssignment).where(
                        DialogueTagAssignment.tenant_id == tenant_id,
                        DialogueTagAssignment.reception_id == reception.id,
                    )
                )
                tags = list(tag_result.scalars().all())
                transition_result = await session.execute(
                    select(DialogueStateTransition).where(
                        DialogueStateTransition.tenant_id == tenant_id,
                        DialogueStateTransition.reception_id == reception.id,
                    )
                )
                transitions = list(transition_result.scalars().all())
                invalidated_artifacts = {
                    "dialogue_unit_ids": [unit.id for unit in dialogue_units],
                    "dialogue_tag_assignment_ids": [tag.id for tag in tags],
                    "dialogue_state_transition_ids": [transition.id for transition in transitions],
                }
                invalidated_at = datetime.now(UTC)
                common_payload = {
                    "reception_id": reception.id,
                    "reason": "reception_timeline_changed",
                    "before_geometry": normalized_existing_geometry,
                    "after_geometry": normalized_planned_geometry,
                }
                for unit in dialogue_units:
                    session.add(
                        ProvenanceEvent(
                            tenant_id=tenant_id,
                            reception_id=reception.id,
                            object_type="dialogue_unit",
                            object_ref=str(unit.id),
                            event_type="superseded",
                            actor=actor,
                            algorithm_version=None,
                            parent_refs=[
                                {
                                    "type": "reception",
                                    "id": reception.id,
                                    "version": reception.version,
                                }
                            ],
                            evidence_refs=deepcopy(unit.segment_refs),
                            payload={
                                **deepcopy(common_payload),
                                "snapshot": _unit_snapshot(unit),
                            },
                            occurred_at=invalidated_at,
                        )
                    )
                for tag in tags:
                    session.add(
                        ProvenanceEvent(
                            tenant_id=tenant_id,
                            reception_id=reception.id,
                            object_type="dialogue_tag_assignment",
                            object_ref=str(tag.id),
                            event_type="superseded",
                            actor=actor,
                            algorithm_version=None,
                            parent_refs=[
                                {
                                    "type": "dialogue_unit",
                                    "id": tag.dialogue_unit_id,
                                }
                            ],
                            evidence_refs=deepcopy(tag.evidence_refs),
                            payload={
                                **deepcopy(common_payload),
                                "snapshot": _tag_snapshot(tag),
                            },
                            occurred_at=invalidated_at,
                        )
                    )
                for transition in transitions:
                    session.add(
                        ProvenanceEvent(
                            tenant_id=tenant_id,
                            reception_id=reception.id,
                            object_type="dialogue_state_transition",
                            object_ref=str(transition.id),
                            event_type="superseded",
                            actor=actor,
                            algorithm_version=transition.algorithm_version,
                            parent_refs=[
                                {
                                    "type": "dialogue_unit",
                                    "id": transition.dialogue_unit_id,
                                }
                            ],
                            evidence_refs=deepcopy(transition.evidence_refs),
                            payload={
                                **deepcopy(common_payload),
                                "snapshot": {
                                    "id": transition.id,
                                    "sequence_no": transition.sequence_no,
                                    "from_state": transition.from_state,
                                    "to_state": transition.to_state,
                                    "trigger": transition.trigger,
                                    "confidence": transition.confidence,
                                },
                            },
                            occurred_at=invalidated_at,
                        )
                    )
                await session.execute(
                    delete(DialogueStateTransition).where(
                        DialogueStateTransition.tenant_id == tenant_id,
                        DialogueStateTransition.reception_id == reception.id,
                    )
                )
                await session.execute(
                    delete(DialogueTagAssignment).where(
                        DialogueTagAssignment.tenant_id == tenant_id,
                        DialogueTagAssignment.reception_id == reception.id,
                    )
                )
                await invalidate_dialogue_unit_currents_in_session(
                    session,
                    tenant_id=tenant_id,
                    dialogue_unit_ids=[unit.id for unit in dialogue_units],
                )
                await session.execute(
                    delete(DialogueUnit).where(
                        DialogueUnit.tenant_id == tenant_id,
                        DialogueUnit.reception_id == reception.id,
                    )
                )
                await session.flush()

            await session.execute(
                delete(ReceptionRecording).where(
                    ReceptionRecording.tenant_id == tenant_id,
                    ReceptionRecording.reception_id == reception.id,
                )
            )
            await session.flush()
            final_mappings: list[ReceptionRecording] = []
            for plan in plans:
                recording = plan["recording"]
                final_mapping = ReceptionRecording(
                    tenant_id=tenant_id,
                    reception_id=reception.id,
                    recording_id=recording.id,
                    sequence_no=int(plan["sequence_no"]),
                    timeline_start_sec=float(plan["timeline_start_sec"]),
                    timeline_end_sec=float(plan["timeline_end_sec"]),
                    source_start_sec=float(plan["source_start_sec"]),
                    source_end_sec=float(plan["source_end_sec"]),
                    source_start_ms=seconds_to_milliseconds(float(plan["source_start_sec"])),
                    source_end_ms=seconds_to_milliseconds(float(plan["source_end_sec"])),
                    timeline_start_ms=seconds_to_milliseconds(float(plan["timeline_start_sec"])),
                    timeline_end_ms=seconds_to_milliseconds(float(plan["timeline_end_sec"])),
                    gap_before_ms=seconds_to_milliseconds(float(plan["gap_before_sec"])),
                    gap_before_sec=float(plan["gap_before_sec"]),
                    decision_source="manual",
                    merge_confidence=1.0,
                    merge_reasons=dict(plan["merge_reasons"]),
                )
                session.add(final_mapping)
                final_mappings.append(final_mapping)

            next_version = body.expected_version + 1
            cas_result = await session.execute(
                update(Reception)
                .where(
                    Reception.id == reception.id,
                    Reception.tenant_id == tenant_id,
                    Reception.version == body.expected_version,
                )
                .values(
                    version=next_version,
                    status=reception_status,
                    merge_mode=body.mode,
                    merged_audio_path=merged_audio_path,
                )
            )
            if cast(CursorResult[Any], cas_result).rowcount != 1:
                actual_result = await session.execute(
                    select(Reception.version).where(
                        Reception.id == reception.id,
                        Reception.tenant_id == tenant_id,
                    )
                )
                actual_version = actual_result.scalar_one_or_none()
                raise self._version_conflict(
                    object_type="reception",
                    object_id=reception.id,
                    expected=body.expected_version,
                    actual=(
                        int(actual_version) if actual_version is not None else body.expected_version
                    ),
                )
            session.add(
                ProvenanceEvent(
                    tenant_id=tenant_id,
                    reception_id=reception.id,
                    object_type="reception",
                    object_ref=str(reception.id),
                    event_type="merged",
                    actor=actor,
                    algorithm_version=None,
                    parent_refs=[
                        {"type": "recording", "recording_id": recording.id}
                        for recording in recordings
                    ],
                    evidence_refs=[
                        {
                            "kind": "audio",
                            "recording_id": plan["recording"].id,
                            "source_start_sec": plan["source_start_sec"],
                            "source_end_sec": plan["source_end_sec"],
                        }
                        for plan in plans
                    ],
                    payload={
                        "reception_id": reception.id,
                        "before": before,
                        "after_recording_ids": body.recording_ids,
                        "mode": body.mode,
                        "version": next_version,
                        "merged_audio_path": merged_audio_path,
                        "timeline_changed": timeline_changed,
                        "invalidated_artifacts": invalidated_artifacts,
                        "audio_manifest": (
                            {
                                "output_path": audio_manifest.output_path,
                                "output_sha256": audio_manifest.output_sha256,
                                "output_bytes": audio_manifest.output_bytes,
                                "total_duration_sec": audio_manifest.total_duration_sec,
                                "command_mode": audio_manifest.command_mode,
                                "inputs": [
                                    {
                                        "path": item.path,
                                        "sha256": item.sha256,
                                        "size_bytes": item.size_bytes,
                                        "duration_sec": item.duration_sec,
                                        "source_start_sec": item.source_start_sec,
                                        "source_end_sec": item.source_end_sec,
                                        "gap_before_sec": item.gap_before_sec,
                                        "timeline_start_sec": item.timeline_start_sec,
                                        "timeline_end_sec": item.timeline_end_sec,
                                        "codec": item.codec,
                                        "sample_rate": item.sample_rate,
                                        "channels": item.channels,
                                    }
                                    for item in audio_manifest.inputs
                                ],
                            }
                            if audio_manifest is not None
                            else None
                        ),
                    },
                    occurred_at=datetime.now(UTC),
                )
            )
            if previous_merged_audio_path and previous_merged_audio_path != merged_audio_path:
                retired_audio_path = previous_merged_audio_path
            if before_commit is not None:
                # Flush gives the hook stable mapping IDs while keeping every
                # operation/artifact/revision mutation inside this transaction.
                await session.flush()
                await session.refresh(reception)
                await before_commit(
                    session,
                    reception,
                    tuple(final_mappings),
                    prepared,
                    previous_merged_audio_path,
                )
                await session.flush()

        await self._retire_physical_artifact(
            retired_audio_path,
            reception_id=reception_id,
            tenant_id=tenant_id,
        )
        return await self.get_workspace(reception_id, tenant_id)

    async def _segmentation_inputs(
        self,
        session: AsyncSession,
        reception: Reception,
        *,
        lock_recordings: bool = False,
    ) -> _DialogueSegmentationInputs:
        mapping_result = await session.execute(
            select(ReceptionRecording)
            .where(
                ReceptionRecording.tenant_id == reception.tenant_id,
                ReceptionRecording.reception_id == reception.id,
            )
            .order_by(ReceptionRecording.sequence_no)
        )
        mappings = list(mapping_result.scalars().all())
        recording_ids = sorted({mapping.recording_id for mapping in mappings})
        if not recording_ids:
            return _DialogueSegmentationInputs(
                segments=(),
                evidence_by_segment={},
                speaker_by_segment={},
                input_generation={},
                legacy_fallback_recording_ids=(),
                fingerprint=hashlib.sha256(b"[]").hexdigest(),
            )

        recording_statement = select(Recording).where(
            Recording.tenant_id == reception.tenant_id,
            Recording.id.in_(recording_ids),
        )
        if lock_recordings:
            recording_statement = recording_statement.with_for_update()
        recording_result = await session.execute(recording_statement)
        recordings = {item.id: item for item in recording_result.scalars().all()}
        missing_recording_ids = sorted(set(recording_ids) - recordings.keys())
        if missing_recording_ids:
            raise ValidationError(
                "Reception references unavailable recordings",
                code="RECEPTION_RECORDING_INVALID",
                detail={"recording_ids": missing_recording_ids},
            )

        active_run_ids = {
            int(recording.active_pipeline_run_id)
            for recording in recordings.values()
            if recording.active_pipeline_run_id is not None
        }
        active_runs: dict[int, RecordingPipelineRun] = {}
        if active_run_ids:
            run_result = await session.execute(
                select(RecordingPipelineRun).where(
                    RecordingPipelineRun.id.in_(active_run_ids),
                    RecordingPipelineRun.tenant_id == reception.tenant_id,
                )
            )
            active_runs = {item.id: item for item in run_result.scalars().all()}

        segment_scopes = []
        input_generation: dict[str, int | str] = {}
        legacy_fallback_recording_ids: list[int] = []
        generation_by_recording: dict[int, int | str] = {}
        pipeline_run_by_recording: dict[int, int | None] = {}
        for recording_id in recording_ids:
            recording = recordings[recording_id]
            active_run_id = recording.active_pipeline_run_id
            if active_run_id is None:
                input_generation[str(recording_id)] = "legacy"
                generation_by_recording[recording_id] = "legacy"
                pipeline_run_by_recording[recording_id] = None
                legacy_fallback_recording_ids.append(recording_id)
                segment_scopes.append(
                    and_(
                        Segment.recording_id == recording_id,
                        Segment.pipeline_run_id.is_(None),
                        Segment.generation == 0,
                    )
                )
                continue

            run = active_runs.get(active_run_id)
            if (
                run is None
                or run.recording_id != recording_id
                or run.state not in {"ready", "ready_no_speech"}
            ):
                raise ValidationError(
                    "Recording has no valid active pipeline generation",
                    code="ACTIVE_PIPELINE_RUN_INVALID",
                    detail={
                        "recording_id": recording_id,
                        "active_pipeline_run_id": active_run_id,
                        "state": run.state if run is not None else None,
                    },
                )
            input_generation[str(recording_id)] = run.generation
            generation_by_recording[recording_id] = run.generation
            pipeline_run_by_recording[recording_id] = run.id
            segment_scopes.append(
                and_(
                    Segment.recording_id == recording_id,
                    Segment.pipeline_run_id == run.id,
                    Segment.generation == run.generation,
                )
            )

        segment_result = await session.execute(
            select(Segment)
            .where(
                Segment.tenant_id == reception.tenant_id,
                or_(*segment_scopes),
            )
            .order_by(
                Segment.recording_id,
                Segment.start_sec,
                Segment.end_sec,
                Segment.idx,
                Segment.id,
            )
        )
        by_recording: dict[int, list[Segment]] = {}
        for segment in segment_result.scalars().all():
            by_recording.setdefault(segment.recording_id, []).append(segment)

        inputs: list[DialogueSegment] = []
        evidence_by_segment: dict[str, dict[str, Any]] = {}
        speaker_by_segment: dict[str, str | None] = {}
        fingerprint_mappings: list[dict[str, Any]] = []
        fingerprint_segments: list[dict[str, Any]] = []
        for mapping in mappings:
            source_end = mapping.source_end_sec
            if source_end is None:
                source_end = mapping.source_start_sec + (
                    mapping.timeline_end_sec - mapping.timeline_start_sec
                )
            fingerprint_mappings.append(
                {
                    "mapping_id": mapping.id,
                    "recording_id": mapping.recording_id,
                    "sequence_no": mapping.sequence_no,
                    "source_start_sec": round(float(mapping.source_start_sec), 6),
                    "source_end_sec": round(float(source_end), 6),
                    "timeline_start_sec": round(float(mapping.timeline_start_sec), 6),
                    "timeline_end_sec": round(float(mapping.timeline_end_sec), 6),
                }
            )
            for segment in by_recording.get(mapping.recording_id, []):
                transcript = scrubbed_segment_text(
                    segment.text_scrubbed,
                    segment.transcript,
                )
                if not transcript or not transcript.strip():
                    continue
                source_start = max(segment.start_sec, mapping.source_start_sec)
                clipped_source_end = min(segment.end_sec, source_end)
                if clipped_source_end <= source_start:
                    continue
                timeline_start = (
                    mapping.timeline_start_sec + source_start - mapping.source_start_sec
                )
                timeline_end = (
                    mapping.timeline_start_sec + clipped_source_end - mapping.source_start_sec
                )
                segment_ref = str(segment.id)
                vad_confidence = (
                    float(segment.vad_conf)
                    if segment.vad_conf is not None and 0 <= float(segment.vad_conf) <= 1
                    else 1.0
                )
                inputs.append(
                    DialogueSegment(
                        segment_id=segment_ref,
                        recording_id=segment.recording_id,
                        start_sec=timeline_start,
                        end_sec=timeline_end,
                        transcript=transcript,
                        speaker=segment.speaker,
                        vad_conf=vad_confidence,
                    )
                )
                evidence_by_segment[segment_ref] = {
                    "kind": "transcript_segment",
                    "segment_id": segment.id,
                    "recording_id": segment.recording_id,
                    "generation": generation_by_recording[segment.recording_id],
                    "pipeline_run_id": pipeline_run_by_recording[segment.recording_id],
                    "source_start_sec": source_start,
                    "source_end_sec": clipped_source_end,
                    "timeline_start_sec": timeline_start,
                    "timeline_end_sec": timeline_end,
                }
                speaker_by_segment[segment_ref] = segment.speaker
                fingerprint_segments.append(
                    {
                        "segment_id": segment.id,
                        "recording_id": segment.recording_id,
                        "generation": generation_by_recording[segment.recording_id],
                        "pipeline_run_id": pipeline_run_by_recording[segment.recording_id],
                        "idx": segment.idx,
                        "start_sec": round(float(segment.start_sec), 6),
                        "end_sec": round(float(segment.end_sec), 6),
                        "transcript": transcript,
                        "speaker": segment.speaker,
                        "vad_conf": (
                            round(float(segment.vad_conf), 6)
                            if segment.vad_conf is not None
                            else None
                        ),
                    }
                )

        inputs.sort(key=lambda item: (item.start_sec, item.end_sec, item.segment_id))
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "input_generation": input_generation,
                    "mappings": fingerprint_mappings,
                    "segments": fingerprint_segments,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return _DialogueSegmentationInputs(
            segments=tuple(inputs),
            evidence_by_segment=evidence_by_segment,
            speaker_by_segment=speaker_by_segment,
            input_generation=input_generation,
            legacy_fallback_recording_ids=tuple(legacy_fallback_recording_ids),
            fingerprint=fingerprint,
        )

    async def _semantic_segmentation_inputs(
        self,
        inputs: tuple[DialogueSegment, ...],
    ) -> _SemanticSegmentationCapability:
        """Batch semantic vectors outside the write transaction, with honest fallback."""
        if self._embed_adapter is None:
            return _SemanticSegmentationCapability(
                segments=inputs,
                status="not_configured",
            )

        try:
            results = tuple(
                await self._embed_adapter.embed_texts(tuple(item.transcript for item in inputs))
            )
            if len(results) != len(inputs):
                raise ValueError("embedding result count does not match segment count")

            vectors: list[tuple[float, ...]] = []
            result_model: str | None = None
            result_dim: int | None = None
            for result in results:
                vector = tuple(float(value) for value in result.vector)
                if (
                    not vector
                    or result.dim != len(vector)
                    or any(not math.isfinite(value) for value in vector)
                ):
                    raise ValueError("embedding result has invalid dimensions or values")
                if result_model is None:
                    result_model = result.model
                    result_dim = result.dim
                elif result.model != result_model or result.dim != result_dim:
                    raise ValueError("embedding batch used inconsistent models or dimensions")
                vectors.append(vector)

            return _SemanticSegmentationCapability(
                segments=tuple(
                    replace(item, semantic_embedding=vector)
                    for item, vector in zip(inputs, vectors, strict=True)
                ),
                status="enabled",
                model=result_model,
                dim=result_dim,
            )
        except Exception as exc:
            logger.warning(
                "Dialogue semantic embeddings unavailable; using rules-only "
                "(segments=%d, error_type=%s)",
                len(inputs),
                type(exc).__name__,
            )
            return _SemanticSegmentationCapability(
                segments=inputs,
                status="unavailable",
                model=getattr(self._embed_adapter, "model", None),
                dim=getattr(self._embed_adapter, "dim", None),
                error_type=type(exc).__name__,
            )

    @staticmethod
    def _segmentation_run_provenance(
        *,
        segmenter: DialogueSegmenter,
        scenario: SalesScenario,
        snapshot: _DialogueSegmentationInputs,
        semantic: _SemanticSegmentationCapability,
        requested_algorithm_version: str,
    ) -> dict[str, Any]:
        """Build the canonical replay/capability manifest for one v2 run."""
        algorithm_version = str(
            getattr(
                segmenter,
                "ALGORITHM_VERSION",
                DialogueSegmenter.ALGORITHM_VERSION,
            )
        )
        if not algorithm_version:
            algorithm_version = DialogueSegmenter.ALGORITHM_VERSION
        enabled_signals = [
            "pause",
            "long_pause",
            "business_stage_change",
            "topic_change",
            "speaker_change",
        ]
        if semantic.status == "enabled":
            enabled_signals.append("semantic_shift")
        config = {
            "algorithm_version": algorithm_version,
            "scenario": scenario.value,
            "boundary_threshold": float(segmenter.boundary_threshold),
            "medium_pause_sec": float(segmenter.medium_pause_sec),
            "long_pause_sec": float(segmenter.long_pause_sec),
            "summary_max_chars": int(segmenter.summary_max_chars),
            "semantic_status": semantic.status,
            "semantic_model": semantic.model,
            "semantic_dim": semantic.dim,
        }
        config_hash = hashlib.sha256(
            json.dumps(
                config,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return {
            "algorithm_version": algorithm_version,
            "requested_algorithm_version": requested_algorithm_version,
            "config_hash": config_hash,
            "enabled_signals": enabled_signals,
            "capability": ("rules+semantic" if semantic.status == "enabled" else "rules-only"),
            "semantic_embedding": {
                "status": semantic.status,
                "model": semantic.model,
                "dim": semantic.dim,
                "error_type": semantic.error_type,
            },
            "input_generation": dict(snapshot.input_generation),
            "legacy_fallback_recording_ids": list(snapshot.legacy_fallback_recording_ids),
            "input_fingerprint": snapshot.fingerprint,
        }

    async def _automatic_segmentation_existing_units(
        self,
        session: AsyncSession,
        *,
        reception_id: int,
        tenant_id: str,
        body: ReceptionSegmentRequest,
        for_update: bool,
    ) -> list[DialogueUnit]:
        """Validate replacement policy both before provider I/O and under lock."""
        statement = (
            select(DialogueUnit)
            .where(
                DialogueUnit.tenant_id == tenant_id,
                DialogueUnit.reception_id == reception_id,
            )
            .order_by(DialogueUnit.unit_index)
        )
        if for_update:
            statement = statement.with_for_update()
        existing_result = await session.execute(statement)
        existing = list(existing_result.scalars().all())
        locked_ids = [unit.id for unit in existing if unit.edit_status == "locked"]
        if locked_ids:
            raise ConflictError(
                "Locked dialogue units cannot be replaced automatically",
                code="LOCKED_DIALOGUE_UNITS_PRESENT",
                detail={"dialogue_unit_ids": locked_ids},
            )
        manual_ids = [unit.id for unit in existing if unit.edit_status != "auto"]
        if manual_ids:
            raise ConflictError(
                "Manually edited dialogue units cannot be replaced automatically",
                code="MANUAL_DIALOGUE_UNITS_PRESENT",
                detail={"dialogue_unit_ids": manual_ids},
            )
        if existing and not body.replace_auto:
            raise ConflictError(
                "Dialogue units already exist; replacement must be explicit",
                code="DIALOGUE_UNITS_EXIST",
                detail={"dialogue_unit_ids": [unit.id for unit in existing]},
            )
        if existing:
            tag_count_result = await session.execute(
                select(func.count(DialogueTagAssignment.id)).where(
                    DialogueTagAssignment.tenant_id == tenant_id,
                    DialogueTagAssignment.reception_id == reception_id,
                    DialogueTagAssignment.dialogue_unit_id.in_([unit.id for unit in existing]),
                )
            )
            if int(tag_count_result.scalar_one()) > 0:
                raise ConflictError(
                    "Tagged dialogue units require manual review before replacement",
                    code="TAGGED_DIALOGUE_UNITS_PRESENT",
                    detail={"dialogue_unit_ids": [unit.id for unit in existing]},
                )
        return existing

    async def segment_reception(
        self,
        reception_id: int,
        tenant_id: str,
        body: ReceptionSegmentRequest,
        *,
        actor: str,
        segmenter: DialogueSegmenter | None = None,
    ) -> ReceptionWorkspace:
        """Derive dialogue units and state transitions from persisted segments."""
        effective_segmenter = segmenter or DialogueSegmenter()
        async with self._session_factory() as read_session:
            preflight_reception = await self._find_reception(
                read_session,
                reception_id,
                tenant_id,
                for_update=False,
            )
            if preflight_reception.version != body.expected_version:
                raise self._version_conflict(
                    object_type="reception",
                    object_id=preflight_reception.id,
                    expected=body.expected_version,
                    actual=preflight_reception.version,
                )
            await self._automatic_segmentation_existing_units(
                read_session,
                reception_id=preflight_reception.id,
                tenant_id=tenant_id,
                body=body,
                for_update=False,
            )
            snapshot = await self._segmentation_inputs(
                read_session,
                preflight_reception,
            )
            reception_scenario = preflight_reception.scenario

        if not snapshot.segments:
            raise ValidationError(
                "No persisted transcript segments overlap this reception",
                code="NO_SEGMENTS_FOR_RECEPTION",
                detail={"reception_id": reception_id},
            )

        # Provider I/O deliberately occurs after the read session closes and
        # before any reception/recording row is locked for publication.
        semantic = await self._semantic_segmentation_inputs(snapshot.segments)
        scenario = {
            "gold": SalesScenario.GOLD_JEWELRY,
            "automotive": SalesScenario.AUTOMOTIVE,
            "custom": SalesScenario.GENERIC,
        }[reception_scenario]
        derived_units = effective_segmenter.segment(
            semantic.segments,
            scenario=scenario,
        )
        if not derived_units:
            raise ValidationError(
                "Dialogue segmentation produced no units",
                code="DIALOGUE_SEGMENTATION_EMPTY",
                detail={"reception_id": reception_id},
            )
        run_provenance = self._segmentation_run_provenance(
            segmenter=effective_segmenter,
            scenario=scenario,
            snapshot=snapshot,
            semantic=semantic,
            requested_algorithm_version=body.algorithm_version,
        )
        algorithm_version = str(run_provenance["algorithm_version"])
        evidence_by_segment = snapshot.evidence_by_segment
        speaker_by_segment = snapshot.speaker_by_segment

        async with self._session_factory() as session, session.begin():
            reception = await self._find_reception(
                session,
                reception_id,
                tenant_id,
                for_update=True,
            )
            if reception.version != body.expected_version:
                raise self._version_conflict(
                    object_type="reception",
                    object_id=reception.id,
                    expected=body.expected_version,
                    actual=reception.version,
                )

            existing = await self._automatic_segmentation_existing_units(
                session,
                reception_id=reception.id,
                tenant_id=tenant_id,
                body=body,
                for_update=True,
            )

            current_snapshot = await self._segmentation_inputs(
                session,
                reception,
                lock_recordings=True,
            )
            if current_snapshot.fingerprint != snapshot.fingerprint:
                raise ConflictError(
                    "Recording generation or reception timeline changed during segmentation",
                    code="SEGMENTATION_INPUT_CHANGED",
                    detail={
                        "reception_id": reception.id,
                        "expected_input_fingerprint": snapshot.fingerprint,
                        "actual_input_fingerprint": current_snapshot.fingerprint,
                        "expected_input_generation": snapshot.input_generation,
                        "actual_input_generation": current_snapshot.input_generation,
                    },
                )

            before = [_unit_snapshot(unit) for unit in existing]
            previous_generation = max(
                (unit.version for unit in existing),
                default=0,
            )
            unit_generation = previous_generation + 1
            await session.execute(
                delete(DialogueStateTransition).where(
                    DialogueStateTransition.tenant_id == tenant_id,
                    DialogueStateTransition.reception_id == reception.id,
                )
            )
            await session.flush()

            persisted_units: list[DialogueUnit] = []
            refs_by_unit: list[list[dict[str, Any]]] = []
            boundary_reasons_by_unit: list[list[dict[str, Any]]] = []
            for derived in derived_units:
                evidence_refs = [
                    deepcopy(evidence_by_segment[ref.segment_id]) for ref in derived.segment_refs
                ]
                refs_by_unit.append(evidence_refs)
                source_recording_ids = {int(ref["recording_id"]) for ref in evidence_refs}
                speaker_refs = list(
                    dict.fromkeys(
                        speaker
                        for ref in derived.segment_refs
                        if (speaker := speaker_by_segment.get(ref.segment_id))
                    )
                )
                boundary_reasons = [
                    {
                        "code": signal.code,
                        "score": signal.score,
                        "detail": signal.detail,
                    }
                    for signal in derived.boundary_signals
                ]
                if not boundary_reasons:
                    boundary_reasons = [
                        {
                            "code": derived.boundary_reason,
                            "score": derived.boundary_score,
                            "detail": "dialogue segment start",
                        }
                    ]
                boundary_reasons.append(
                    {
                        "code": "stage_inference",
                        "stage": derived.stage,
                        "stage_confidence": derived.stage_confidence,
                    }
                )
                boundary_reasons_by_unit.append(boundary_reasons)
                persisted_units.append(
                    DialogueUnit(
                        tenant_id=tenant_id,
                        reception_id=reception.id,
                        source_recording_id=(
                            next(iter(source_recording_ids))
                            if len(source_recording_ids) == 1
                            else None
                        ),
                        unit_index=derived.unit_index,
                        version=unit_generation,
                        start_sec=derived.start_sec,
                        end_sec=derived.end_sec,
                        topic=derived.topic,
                        business_stage=derived.stage,
                        summary=derived.summary,
                        boundary_confidence=derived.boundary_score,
                        stage_confidence=derived.stage_confidence,
                        boundary_reasons=boundary_reasons,
                        segment_refs=evidence_refs,
                        speaker_refs=speaker_refs,
                        edit_status="auto",
                    )
                )
            session.add_all(persisted_units)
            await session.flush()

            now = datetime.now(UTC)
            for old_unit in existing:
                session.add(
                    ProvenanceEvent(
                        tenant_id=tenant_id,
                        reception_id=reception.id,
                        object_type="dialogue_unit",
                        object_ref=str(old_unit.id),
                        event_type="superseded",
                        actor=actor,
                        algorithm_version=algorithm_version,
                        parent_refs=[
                            {
                                "type": "reception",
                                "id": reception.id,
                                "version": reception.version,
                            }
                        ],
                        evidence_refs=deepcopy(old_unit.segment_refs),
                        payload={
                            "reception_id": reception.id,
                            "before": _unit_snapshot(old_unit),
                            "reason": "replace_auto",
                            **deepcopy(run_provenance),
                        },
                        occurred_at=now,
                    )
                )

            if existing:
                await session.execute(
                    delete(DialogueUnit).where(
                        DialogueUnit.tenant_id == tenant_id,
                        DialogueUnit.id.in_([unit.id for unit in existing]),
                    )
                )
                await session.flush()

            previous_state = "__start__"
            for derived, unit, evidence_refs, boundary_reasons in zip(
                derived_units,
                persisted_units,
                refs_by_unit,
                boundary_reasons_by_unit,
                strict=True,
            ):
                session.add(
                    DialogueStateTransition(
                        tenant_id=tenant_id,
                        reception_id=reception.id,
                        dialogue_unit_id=unit.id,
                        sequence_no=unit.unit_index,
                        from_state=previous_state,
                        to_state=derived.stage,
                        trigger=derived.boundary_reason[:128],
                        confidence=derived.stage_confidence,
                        evidence_refs=deepcopy(evidence_refs),
                        algorithm_version=algorithm_version,
                    )
                )
                session.add(
                    ProvenanceEvent(
                        tenant_id=tenant_id,
                        reception_id=reception.id,
                        object_type="dialogue_unit",
                        object_ref=str(unit.id),
                        event_type="derived",
                        actor=actor,
                        algorithm_version=algorithm_version,
                        parent_refs=[
                            {
                                "type": "segment",
                                "id": ref["segment_id"],
                                "recording_id": ref["recording_id"],
                                "generation": ref["generation"],
                                "pipeline_run_id": ref["pipeline_run_id"],
                            }
                            for ref in evidence_refs
                        ],
                        evidence_refs=deepcopy(evidence_refs),
                        payload={
                            "reception_id": reception.id,
                            "unit_index": unit.unit_index,
                            "unit_version": unit.version,
                            "business_stage": unit.business_stage,
                            "boundary_reasons": deepcopy(boundary_reasons),
                            **deepcopy(run_provenance),
                        },
                        occurred_at=now,
                    )
                )
                previous_state = derived.stage

            reception.version += 1
            await invalidate_dialogue_units_in_session(
                session,
                tenant_id=tenant_id,
                reception_id=reception.id,
                dialogue_unit_ids=[
                    *[unit.id for unit in existing],
                    *[unit.id for unit in persisted_units],
                ],
                recompute_dialogue_unit_ids=[unit.id for unit in persisted_units],
                cause=("automatic_resegmentation" if existing else "automatic_segmentation"),
                reception_version=reception.version,
                actor_user_id=_actor_user_id(actor),
            )
            session.add(
                ProvenanceEvent(
                    tenant_id=tenant_id,
                    reception_id=reception.id,
                    object_type="reception",
                    object_ref=str(reception.id),
                    event_type="derived",
                    actor=actor,
                    algorithm_version=algorithm_version,
                    parent_refs=[
                        {
                            "type": "recording",
                            "id": recording_id,
                            "generation": snapshot.input_generation[str(recording_id)],
                        }
                        for recording_id in sorted(
                            {int(ref["recording_id"]) for refs in refs_by_unit for ref in refs}
                        )
                    ],
                    evidence_refs=[deepcopy(ref) for refs in refs_by_unit for ref in refs],
                    payload={
                        "reception_id": reception.id,
                        "operation": "dialogue_segmentation",
                        "replace_auto": body.replace_auto,
                        "superseded_units": before,
                        "dialogue_unit_ids": [unit.id for unit in persisted_units],
                        "version": reception.version,
                        **deepcopy(run_provenance),
                    },
                    occurred_at=now,
                )
            )

        return await self.get_workspace(reception_id, tenant_id)

    async def split_dialogue_unit(
        self,
        reception_id: int,
        unit_id: int,
        tenant_id: str,
        body: DialogueUnitSplitRequest,
        *,
        actor: str,
    ) -> ReceptionWorkspace:
        """Split one unlocked unit, copy evidence, and append provenance."""
        async with self._session_factory() as session, session.begin():
            reception = await self._find_reception(
                session,
                reception_id,
                tenant_id,
                for_update=True,
            )
            if reception.version != body.expected_reception_version:
                raise self._version_conflict(
                    object_type="reception",
                    object_id=reception.id,
                    expected=body.expected_reception_version,
                    actual=reception.version,
                )

            unit_result = await session.execute(
                select(DialogueUnit)
                .where(
                    DialogueUnit.id == unit_id,
                    DialogueUnit.tenant_id == tenant_id,
                    DialogueUnit.reception_id == reception_id,
                )
                .with_for_update()
            )
            unit = unit_result.scalar_one_or_none()
            if unit is None:
                raise self._unit_not_found(unit_id)
            if unit.version != body.expected_unit_version:
                raise self._version_conflict(
                    object_type="dialogue_unit",
                    object_id=unit.id,
                    expected=body.expected_unit_version,
                    actual=unit.version,
                )
            if unit.edit_status == "locked":
                raise ConflictError(
                    "Locked dialogue units cannot be edited",
                    code="DIALOGUE_UNIT_LOCKED",
                    detail={"dialogue_unit_id": unit.id},
                )
            if not unit.start_sec < body.split_at_sec < unit.end_sec:
                raise ValidationError(
                    "Split point must be strictly inside the dialogue unit",
                    code="INVALID_SPLIT_POINT",
                    detail={
                        "start_sec": unit.start_sec,
                        "end_sec": unit.end_sec,
                        "split_at_sec": body.split_at_sec,
                    },
                )

            tags_result = await session.execute(
                select(DialogueTagAssignment).where(
                    DialogueTagAssignment.tenant_id == tenant_id,
                    DialogueTagAssignment.dialogue_unit_id == unit.id,
                )
            )
            tags = list(tags_result.scalars().all())
            max_index_result = await session.execute(
                select(func.max(DialogueUnit.unit_index)).where(
                    DialogueUnit.tenant_id == tenant_id,
                    DialogueUnit.reception_id == reception_id,
                )
            )
            max_index = max_index_result.scalar_one()
            before = _unit_snapshot(unit)
            tag_snapshots = [_tag_snapshot(tag) for tag in tags]
            state_transitions_before = await self._state_transition_snapshots(
                session,
                tenant_id=tenant_id,
                reception_id=reception_id,
            )
            old_start = unit.start_sec
            old_end = unit.end_sec
            original_segment_refs = deepcopy(unit.segment_refs)
            original_boundary_reasons = deepcopy(unit.boundary_reasons)
            stage_confidence = _unit_stage_confidence(unit)
            left_segment_refs = _clip_evidence_refs(
                original_segment_refs,
                start_sec=old_start,
                end_sec=body.split_at_sec,
            )
            right_segment_refs = _clip_evidence_refs(
                original_segment_refs,
                start_sec=body.split_at_sec,
                end_sec=old_end,
            )
            left_speaker_refs = await self._speaker_refs_for_evidence(
                session,
                tenant_id=tenant_id,
                evidence_refs=left_segment_refs,
            )
            right_speaker_refs = await self._speaker_refs_for_evidence(
                session,
                tenant_id=tenant_id,
                evidence_refs=right_segment_refs,
            )

            unit.end_sec = body.split_at_sec
            unit.version += 1
            unit.edit_status = "manual_edited"
            unit.summary = None
            unit.segment_refs = left_segment_refs
            unit.speaker_refs = left_speaker_refs
            unit.stage_confidence = stage_confidence
            unit.boundary_reasons = original_boundary_reasons
            right = DialogueUnit(
                tenant_id=tenant_id,
                reception_id=reception_id,
                source_recording_id=unit.source_recording_id,
                unit_index=int(max_index or 0) + 1,
                version=1,
                start_sec=body.split_at_sec,
                end_sec=old_end,
                topic=unit.topic,
                business_stage=unit.business_stage,
                summary=None,
                boundary_confidence=1.0,
                stage_confidence=stage_confidence,
                boundary_reasons=[
                    {
                        "code": "manual_split",
                        "at_sec": body.split_at_sec,
                        "reason": body.reason,
                    },
                    {
                        "code": "stage_inference",
                        "stage": unit.business_stage,
                        "stage_confidence": stage_confidence,
                    },
                ],
                segment_refs=right_segment_refs,
                speaker_refs=right_speaker_refs,
                edit_status="manual_edited",
            )
            session.add(right)
            await session.flush()

            left_tag_evidence: list[Any] = []
            right_tag_evidence: list[Any] = []
            for tag in tags:
                was_current = tag.is_current
                applies_left = _tag_applies_to_window(
                    tag,
                    start_sec=unit.start_sec,
                    end_sec=unit.end_sec,
                )
                applies_right = _tag_applies_to_window(
                    tag,
                    start_sec=right.start_sec,
                    end_sec=right.end_sec,
                )
                left_evidence_refs = _clip_evidence_refs(
                    tag.evidence_refs,
                    start_sec=unit.start_sec,
                    end_sec=unit.end_sec,
                )
                right_evidence_refs = _clip_evidence_refs(
                    tag.evidence_refs,
                    start_sec=right.start_sec,
                    end_sec=right.end_sec,
                )
                if applies_left:
                    tag.evidence_refs = left_evidence_refs
                    left_tag_evidence.extend(deepcopy(left_evidence_refs))
                else:
                    await session.delete(tag)
                if applies_right:
                    right_tag_evidence.extend(deepcopy(right_evidence_refs))
                    session.add(
                        DialogueTagAssignment(
                            tenant_id=tenant_id,
                            reception_id=reception_id,
                            dialogue_unit_id=right.id,
                            group_key=tag.group_key,
                            group_version=tag.group_version,
                            label_key=tag.label_key,
                            label_value=tag.label_value,
                            confidence=tag.confidence,
                            source=tag.source,
                            priority=tag.priority,
                            evidence_refs=right_evidence_refs,
                            model_run_id=tag.model_run_id,
                            is_current=was_current,
                            assigned_at=tag.assigned_at,
                        )
                    )

            await self._renumber_dialogue_units(
                session,
                tenant_id=tenant_id,
                reception_id=reception_id,
            )
            await self._rebuild_state_transitions(
                session,
                tenant_id=tenant_id,
                reception_id=reception_id,
                trigger="manual_split",
            )

            reception.version += 1
            await invalidate_dialogue_units_in_session(
                session,
                tenant_id=tenant_id,
                reception_id=reception.id,
                dialogue_unit_ids=[unit.id, right.id],
                cause="manual_split",
                reception_version=reception.version,
                actor_user_id=_actor_user_id(actor),
            )
            left_evidence = _merge_json_values(
                unit.segment_refs,
                left_tag_evidence,
            )
            right_evidence = _merge_json_values(
                right.segment_refs,
                right_tag_evidence,
            )
            session.add(
                ProvenanceEvent(
                    tenant_id=tenant_id,
                    reception_id=reception_id,
                    object_type="dialogue_unit",
                    object_ref=str(unit.id),
                    event_type="split",
                    actor=actor,
                    algorithm_version=None,
                    parent_refs=[
                        {
                            "type": "dialogue_unit",
                            "id": unit.id,
                            "version": body.expected_unit_version,
                        }
                    ],
                    evidence_refs=left_evidence,
                    payload={
                        "reception_id": reception_id,
                        "before": before,
                        "tag_assignments_before": tag_snapshots,
                        "state_transitions_before": state_transitions_before,
                        "left_unit_id": unit.id,
                        "right_unit_id": right.id,
                        "split_at_sec": body.split_at_sec,
                        "reason": body.reason,
                        "reception_version": reception.version,
                    },
                    occurred_at=datetime.now(UTC),
                )
            )
            session.add(
                ProvenanceEvent(
                    tenant_id=tenant_id,
                    reception_id=reception_id,
                    object_type="dialogue_unit",
                    object_ref=str(right.id),
                    event_type="derived",
                    actor=actor,
                    algorithm_version=None,
                    parent_refs=[
                        {
                            "type": "dialogue_unit",
                            "id": unit.id,
                            "version": body.expected_unit_version,
                        }
                    ],
                    evidence_refs=right_evidence,
                    payload={
                        "reception_id": reception_id,
                        "derived_by": "manual_split",
                        "split_from_unit_id": unit.id,
                        "split_at_sec": body.split_at_sec,
                        "reason": body.reason,
                    },
                    occurred_at=datetime.now(UTC),
                )
            )

        return await self.get_workspace(reception_id, tenant_id)

    @staticmethod
    def _tag_rank(tag: DialogueTagAssignment) -> tuple[int, int, float, datetime]:
        return (
            int(tag.is_current),
            tag.priority,
            tag.confidence if tag.confidence is not None else -1.0,
            tag.assigned_at,
        )

    async def _merge_tag_assignments(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        survivor: DialogueUnit,
        removed: DialogueUnit,
    ) -> tuple[
        list[Any],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        result = await session.execute(
            select(DialogueTagAssignment).where(
                DialogueTagAssignment.tenant_id == tenant_id,
                DialogueTagAssignment.dialogue_unit_id.in_([survivor.id, removed.id]),
            )
        )
        assignments = list(result.scalars().all())
        snapshots = [_tag_snapshot(assignment) for assignment in assignments]
        by_key: dict[tuple[str, str, str], list[DialogueTagAssignment]] = {}
        for assignment in assignments:
            key = (
                assignment.group_key,
                assignment.group_version,
                assignment.label_key,
            )
            by_key.setdefault(key, []).append(assignment)

        all_evidence: list[Any] = []
        conflicts: list[dict[str, Any]] = []
        for key, candidates in by_key.items():
            winner = max(candidates, key=self._tag_rank)
            matching_value = [
                candidate for candidate in candidates if candidate.label_value == winner.label_value
            ]
            winner.evidence_refs = _merge_json_values(
                *(candidate.evidence_refs for candidate in matching_value)
            )
            all_evidence.extend(deepcopy(winner.evidence_refs))
            values = sorted({candidate.label_value for candidate in candidates})
            if len(values) > 1:
                conflicts.append(
                    {
                        "group_key": key[0],
                        "group_version": key[1],
                        "label_key": key[2],
                        "values": values,
                        "selected_value": winner.label_value,
                        "selected_assignment_id": winner.id,
                        "resolution": "current_priority_confidence_recency",
                    }
                )
            losers = [candidate for candidate in candidates if candidate is not winner]
            for loser in losers:
                await session.delete(loser)
            if losers:
                await session.flush()
            winner.dialogue_unit_id = survivor.id
        await session.flush()
        return _merge_json_values(all_evidence), snapshots, conflicts

    async def merge_dialogue_units(
        self,
        reception_id: int,
        unit_id: int,
        tenant_id: str,
        body: DialogueUnitMergeRequest,
        *,
        actor: str,
    ) -> ReceptionWorkspace:
        """Merge adjacent unlocked units while preserving the strongest tag evidence."""
        if unit_id == body.other_unit_id:
            raise ValidationError(
                "A dialogue unit cannot be merged with itself",
                code="INVALID_DIALOGUE_MERGE",
            )

        async with self._session_factory() as session, session.begin():
            reception = await self._find_reception(
                session,
                reception_id,
                tenant_id,
                for_update=True,
            )
            if reception.version != body.expected_reception_version:
                raise self._version_conflict(
                    object_type="reception",
                    object_id=reception.id,
                    expected=body.expected_reception_version,
                    actual=reception.version,
                )

            units_result = await session.execute(
                select(DialogueUnit)
                .where(
                    DialogueUnit.tenant_id == tenant_id,
                    DialogueUnit.reception_id == reception_id,
                    DialogueUnit.id.in_([unit_id, body.other_unit_id]),
                )
                .with_for_update()
            )
            by_id = {unit.id: unit for unit in units_result.scalars().all()}
            first_requested = by_id.get(unit_id)
            second_requested = by_id.get(body.other_unit_id)
            if first_requested is None:
                raise self._unit_not_found(unit_id)
            if second_requested is None:
                raise self._unit_not_found(body.other_unit_id)

            expected_versions = {
                unit_id: body.expected_unit_version,
                body.other_unit_id: body.expected_other_unit_version,
            }
            for unit in (first_requested, second_requested):
                expected = expected_versions[unit.id]
                if unit.version != expected:
                    raise self._version_conflict(
                        object_type="dialogue_unit",
                        object_id=unit.id,
                        expected=expected,
                        actual=unit.version,
                    )
                if unit.edit_status == "locked":
                    raise ConflictError(
                        "Locked dialogue units cannot be edited",
                        code="DIALOGUE_UNIT_LOCKED",
                        detail={"dialogue_unit_id": unit.id},
                    )

            order_result = await session.execute(
                select(DialogueUnit.id)
                .where(
                    DialogueUnit.tenant_id == tenant_id,
                    DialogueUnit.reception_id == reception_id,
                )
                .order_by(DialogueUnit.start_sec, DialogueUnit.unit_index)
            )
            ordered_ids = list(order_result.scalars().all())
            left_position = ordered_ids.index(first_requested.id)
            right_position = ordered_ids.index(second_requested.id)
            if abs(left_position - right_position) != 1:
                raise ValidationError(
                    "Only adjacent dialogue units can be merged",
                    code="NON_ADJACENT_DIALOGUE_UNITS",
                    detail={"dialogue_unit_ids": [unit_id, body.other_unit_id]},
                )

            survivor, removed = sorted(
                (first_requested, second_requested),
                key=lambda unit: (unit.start_sec, unit.unit_index),
            )
            before = [_unit_snapshot(survivor), _unit_snapshot(removed)]
            state_transitions_before = await self._state_transition_snapshots(
                session,
                tenant_id=tenant_id,
                reception_id=reception_id,
            )
            removed_segment_refs = deepcopy(removed.segment_refs)
            survivor_boundary_reasons = [
                deepcopy(reason)
                for reason in survivor.boundary_reasons
                if not (isinstance(reason, Mapping) and reason.get("code") == "stage_inference")
            ]
            survivor_stage_confidence = _unit_stage_confidence(survivor)
            removed_stage_confidence = _unit_stage_confidence(removed)
            tag_evidence, tag_snapshots, tag_conflicts = await self._merge_tag_assignments(
                session,
                tenant_id=tenant_id,
                survivor=survivor,
                removed=removed,
            )

            survivor.start_sec = min(survivor.start_sec, removed.start_sec)
            survivor.end_sec = max(survivor.end_sec, removed.end_sec)
            survivor.topic = survivor.topic if survivor.topic == removed.topic else None
            survivor.business_stage = (
                survivor.business_stage
                if survivor.business_stage == removed.business_stage
                else None
            )
            survivor.summary = None
            merged_segment_refs = _merge_json_values(
                survivor.segment_refs,
                removed_segment_refs,
            )
            survivor.segment_refs = merged_segment_refs
            survivor.speaker_refs = await self._speaker_refs_for_evidence(
                session,
                tenant_id=tenant_id,
                evidence_refs=merged_segment_refs,
            )
            merged_stage_confidence = (
                min(survivor_stage_confidence, removed_stage_confidence)
                if survivor.business_stage
                else 0.0
            )
            survivor.stage_confidence = merged_stage_confidence
            survivor.boundary_reasons = _merge_json_values(
                survivor_boundary_reasons,
                [
                    {
                        "code": "stage_inference",
                        "stage": survivor.business_stage,
                        "stage_confidence": merged_stage_confidence,
                    }
                ],
            )
            survivor.version += 1
            survivor.edit_status = "manual_edited"

            await session.delete(removed)
            await session.flush()
            await self._renumber_dialogue_units(
                session,
                tenant_id=tenant_id,
                reception_id=reception_id,
            )
            await self._rebuild_state_transitions(
                session,
                tenant_id=tenant_id,
                reception_id=reception_id,
                trigger="manual_merge",
            )

            reception.version += 1
            await invalidate_dialogue_units_in_session(
                session,
                tenant_id=tenant_id,
                reception_id=reception.id,
                dialogue_unit_ids=[survivor.id, removed.id],
                recompute_dialogue_unit_ids=[survivor.id],
                cause="manual_merge",
                reception_version=reception.version,
                actor_user_id=_actor_user_id(actor),
            )
            evidence = _merge_json_values(
                survivor.segment_refs,
                removed_segment_refs,
                tag_evidence,
            )
            session.add(
                ProvenanceEvent(
                    tenant_id=tenant_id,
                    reception_id=reception_id,
                    object_type="dialogue_unit",
                    object_ref=str(survivor.id),
                    event_type="merged",
                    actor=actor,
                    algorithm_version=None,
                    parent_refs=[
                        {
                            "type": "dialogue_unit",
                            "id": snapshot["id"],
                            "version": snapshot["version"],
                        }
                        for snapshot in before
                    ],
                    evidence_refs=evidence,
                    payload={
                        "reception_id": reception_id,
                        "before": before,
                        "tag_assignments_before": tag_snapshots,
                        "tag_conflicts": tag_conflicts,
                        "state_transitions_before": state_transitions_before,
                        "survivor_unit_id": survivor.id,
                        "removed_unit_id": removed.id,
                        "reason": body.reason,
                        "reception_version": reception.version,
                    },
                    occurred_at=datetime.now(UTC),
                )
            )
            session.add(
                ProvenanceEvent(
                    tenant_id=tenant_id,
                    reception_id=reception_id,
                    object_type="dialogue_unit",
                    object_ref=str(removed.id),
                    event_type="superseded",
                    actor=actor,
                    algorithm_version=None,
                    parent_refs=[
                        {
                            "type": "dialogue_unit",
                            "id": before[1]["id"],
                            "version": before[1]["version"],
                        }
                    ],
                    evidence_refs=deepcopy(evidence),
                    payload={
                        "reception_id": reception_id,
                        "merged_into_unit_id": survivor.id,
                        "merged_into_unit_version": survivor.version,
                        "reason": body.reason,
                        "reception_version": reception.version,
                    },
                    occurred_at=datetime.now(UTC),
                )
            )

        return await self.get_workspace(reception_id, tenant_id)

    async def get_state_transitions(
        self,
        reception_id: int,
        tenant_id: str,
        *,
        page: int,
        page_size: int,
        agent_user_id: int | None = None,
    ) -> tuple[list[DialogueStateTransition], int]:
        """Query one bounded transition page without loading the workspace."""
        async with self._session_factory() as session:
            await self._find_reception(
                session,
                reception_id,
                tenant_id,
                agent_user_id=agent_user_id,
            )
            filters = (
                DialogueStateTransition.tenant_id == tenant_id,
                DialogueStateTransition.reception_id == reception_id,
            )
            total = int(
                (
                    await session.execute(
                        select(func.count(DialogueStateTransition.id)).where(*filters)
                    )
                ).scalar_one()
            )
            result = await session.execute(
                select(DialogueStateTransition)
                .where(*filters)
                .order_by(
                    DialogueStateTransition.sequence_no,
                    DialogueStateTransition.id,
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            return list(result.scalars().all()), total

    async def _ensure_agent_provenance_access(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        agent_user_id: int,
        object_type: str,
        object_ref: str,
    ) -> None:
        try:
            object_id = int(object_ref)
        except ValueError as exc:
            raise NotFoundError(
                "Provenance object not found",
                code="PROVENANCE_NOT_FOUND",
            ) from exc

        if object_type == "reception":
            await self._find_reception(
                session,
                object_id,
                tenant_id,
                agent_user_id=agent_user_id,
            )
            return
        if object_type == "dialogue_unit":
            stmt = (
                select(DialogueUnit.id)
                .join(Reception, Reception.id == DialogueUnit.reception_id)
                .where(
                    DialogueUnit.id == object_id,
                    DialogueUnit.tenant_id == tenant_id,
                    Reception.tenant_id == tenant_id,
                    Reception.agent_user_id == agent_user_id,
                )
            )
        elif object_type == "dialogue_tag_assignment":
            stmt = (
                select(DialogueTagAssignment.id)
                .join(
                    Reception,
                    Reception.id == DialogueTagAssignment.reception_id,
                )
                .where(
                    DialogueTagAssignment.id == object_id,
                    DialogueTagAssignment.tenant_id == tenant_id,
                    Reception.tenant_id == tenant_id,
                    Reception.agent_user_id == agent_user_id,
                )
            )
        elif object_type == "dialogue_state_transition":
            stmt = (
                select(DialogueStateTransition.id)
                .join(
                    Reception,
                    Reception.id == DialogueStateTransition.reception_id,
                )
                .where(
                    DialogueStateTransition.id == object_id,
                    DialogueStateTransition.tenant_id == tenant_id,
                    Reception.tenant_id == tenant_id,
                    Reception.agent_user_id == agent_user_id,
                )
            )
        elif object_type == "recording":
            owner_result = await session.execute(
                select(Reception.agent_user_id)
                .select_from(Recording)
                .join(
                    ReceptionRecording,
                    ReceptionRecording.recording_id == Recording.id,
                )
                .join(
                    Reception,
                    Reception.id == ReceptionRecording.reception_id,
                )
                .where(
                    Recording.id == object_id,
                    Recording.tenant_id == tenant_id,
                    ReceptionRecording.tenant_id == tenant_id,
                    Reception.tenant_id == tenant_id,
                )
                .distinct()
                .limit(2)
            )
            # A source recording may be referenced by multiple receptions.
            # Authorize its global provenance only when every mapped reception
            # has the same stable owner; mixed or unresolved ownership fails
            # closed rather than exposing another agent's lineage.
            owner_ids = set(owner_result.scalars().all())
            if owner_ids == {agent_user_id}:
                return
            raise NotFoundError(
                "Provenance object not found",
                code="PROVENANCE_NOT_FOUND",
            )
        else:
            raise NotFoundError(
                "Provenance object not found",
                code="PROVENANCE_NOT_FOUND",
            )

        result = await session.execute(stmt)
        if result.scalar_one_or_none() is not None:
            return

        # Rebuilding state transitions deletes obsolete ORM rows after writing
        # append-only provenance. Authorize those historical nodes through the
        # event's persisted reception owner so their lineage remains traceable.
        if object_type == "dialogue_state_transition":
            historical_result = await session.execute(
                select(ProvenanceEvent.id)
                .join(
                    Reception,
                    and_(
                        Reception.id == ProvenanceEvent.reception_id,
                        Reception.tenant_id == ProvenanceEvent.tenant_id,
                    ),
                )
                .where(
                    ProvenanceEvent.tenant_id == tenant_id,
                    ProvenanceEvent.object_type == object_type,
                    ProvenanceEvent.object_ref == object_ref,
                    Reception.agent_user_id == agent_user_id,
                )
                .limit(1)
            )
            if historical_result.scalar_one_or_none() is not None:
                return

        # A merged unit no longer has an ORM row, but its append-only events
        # retain reception_id so the owning agent can still follow lineage.
        if object_type == "dialogue_unit":
            payload_result = await session.execute(
                select(ProvenanceEvent.payload)
                .where(
                    ProvenanceEvent.tenant_id == tenant_id,
                    ProvenanceEvent.object_type == object_type,
                    ProvenanceEvent.object_ref == object_ref,
                )
                .order_by(ProvenanceEvent.occurred_at.desc())
                .limit(100)
            )
            reception_ids = {
                int(payload["reception_id"])
                for payload in payload_result.scalars().all()
                if isinstance(payload, dict) and isinstance(payload.get("reception_id"), int)
            }
            if reception_ids:
                owner_result = await session.execute(
                    select(func.count(Reception.id)).where(
                        Reception.id.in_(reception_ids),
                        Reception.tenant_id == tenant_id,
                        Reception.agent_user_id == agent_user_id,
                    )
                )
                if int(owner_result.scalar_one() or 0) == len(reception_ids):
                    return

        raise NotFoundError(
            "Provenance object not found",
            code="PROVENANCE_NOT_FOUND",
        )

    async def get_provenance(
        self,
        object_type: str,
        object_ref: str,
        tenant_id: str,
        *,
        page: int,
        page_size: int,
        agent_user_id: int | None = None,
    ) -> tuple[list[ProvenanceEvent], int]:
        """Return one bounded tenant-scoped chronological provenance page."""
        if not _OBJECT_TYPE_PATTERN.fullmatch(object_type) or len(object_ref) > 128:
            raise ValidationError(
                "Invalid provenance object reference",
                code="INVALID_PROVENANCE_REFERENCE",
            )

        async with self._session_factory() as session:
            if agent_user_id is not None:
                await self._ensure_agent_provenance_access(
                    session,
                    tenant_id=tenant_id,
                    agent_user_id=agent_user_id,
                    object_type=object_type,
                    object_ref=object_ref,
                )
            filters = (
                ProvenanceEvent.tenant_id == tenant_id,
                ProvenanceEvent.object_type == object_type,
                ProvenanceEvent.object_ref == object_ref,
            )
            total = int(
                (
                    await session.execute(select(func.count(ProvenanceEvent.id)).where(*filters))
                ).scalar_one()
            )
            if total == 0:
                raise NotFoundError(
                    "Provenance chain not found",
                    code="PROVENANCE_NOT_FOUND",
                    detail={
                        "object_type": object_type,
                        "object_ref": object_ref,
                    },
                )
            result = await session.execute(
                select(ProvenanceEvent)
                .where(*filters)
                .order_by(ProvenanceEvent.occurred_at, ProvenanceEvent.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            events = list(result.scalars().all())
            return events, total


__all__ = [
    "AudioAssembler",
    "PlaybackGrantClaims",
    "ReceptionAudioAsset",
    "ReceptionMergeProposalResult",
    "ReceptionPhysicalPrepareHook",
    "ReceptionPhysicalStageHook",
    "ReceptionPlaybackGeometry",
    "ReceptionService",
    "ReceptionTranscriptItem",
    "ReceptionWorkspace",
    "create_playback_grant",
    "reception_mapping_playback_geometry",
    "reception_physical_generation_relative_path",
    "resolve_confined_audio_file",
    "resolve_safe_audio_output",
    "verify_playback_grant",
]
