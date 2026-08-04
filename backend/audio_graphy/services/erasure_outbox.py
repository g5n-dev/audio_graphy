"""Atomic DSAR erasure staging and recoverable external cleanup."""

from __future__ import annotations

import asyncio
import inspect
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.models.audit_log import AuditLog
from audio_graphy.models.chunk import Chunk
from audio_graphy.models.erasure_outbox import ErasureOutbox
from audio_graphy.models.llm_cache import LLMCacheSourceGuard
from audio_graphy.models.recording import Recording
from audio_graphy.models.segment import Segment
from audio_graphy.models.speaker_link import SpeakerLink
from audio_graphy.models.speaker_node import SpeakerNode
from audio_graphy.models.tag_current import TagCurrent
from audio_graphy.models.tag_fact import TagFact
from audio_graphy.models.voiceprint_vector import VoiceprintVector
from audio_graphy.services.reception_erasure import (
    erase_reception_artifacts,
    invalidate_receptions_for_recording,
)

logger = logging.getLogger(__name__)

_LEASE_SECONDS = 60
_MAX_ERROR_LENGTH = 512

GraphStoreFactory = Callable[[str], Any | Awaitable[Any]]
FileIndexFactory = Callable[[str], Any | Awaitable[Any]]
GraphCleanup = Callable[[Any, int, str], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class ErasureStageResult:
    """Outcome of the transactional database phase."""

    outbox_id: int
    newly_erased: bool


@dataclass(frozen=True, slots=True)
class _Claim:
    id: int
    tenant_id: str
    subject_type: str
    subject_id: str
    payload: dict[str, Any]
    lease_token: str


def _rowcount(result: Any) -> int:
    return int(cast(CursorResult[Any], result).rowcount or 0)


def remove_recording_graph_refs(
    graph_store: Any,
    recording_id: int,
    _tenant_id: str,
) -> None:
    """Idempotently remove one recording's provenance from a GraphML projection."""
    from audio_graphy.core.types import _list_to_str, _str_to_list

    graph = graph_store.graph
    recording_id_text = str(recording_id)
    nodes_to_remove: list[str] = []
    for node_id, attrs in list(graph.nodes(data=True)):
        raw_ids = attrs.get("recording_ids")
        if raw_ids is None:
            continue
        recording_ids = _str_to_list(str(raw_ids))
        if recording_id_text not in recording_ids:
            continue
        if len(recording_ids) <= 1:
            nodes_to_remove.append(str(node_id))
        else:
            graph.nodes[node_id]["recording_ids"] = _list_to_str(
                [item for item in recording_ids if item != recording_id_text]
            )
    graph.remove_nodes_from(nodes_to_remove)
    invalidate = getattr(graph_store, "invalidate_path_projection", None)
    if callable(invalidate):
        invalidate()


async def _mark_cache_source_erased(
    session: AsyncSession,
    *,
    tenant_id: str,
    recording_id: int,
) -> None:
    """Serialize in-flight cache claims with the source deletion transaction."""
    subject_id = str(recording_id)
    guard: LLMCacheSourceGuard | None = None
    for _attempt in range(3):
        guard = (
            await session.execute(
                select(LLMCacheSourceGuard)
                .where(
                    LLMCacheSourceGuard.tenant_id == tenant_id,
                    LLMCacheSourceGuard.source_type == "recording",
                    LLMCacheSourceGuard.source_id == subject_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if guard is not None:
            break
        try:
            async with session.begin_nested():
                guard = LLMCacheSourceGuard(
                    tenant_id=tenant_id,
                    source_type="recording",
                    source_id=subject_id,
                    state="erased",
                    erased_at=datetime.now(UTC),
                )
                session.add(guard)
                await session.flush()
        except IntegrityError:
            # A concurrent cache claim inserted the same serialization row.
            # The savepoint preserves this erasure transaction; lock/re-read.
            guard = None
            continue
        break
    if guard is None:
        raise RuntimeError("LLM source guard creation did not converge")
    guard.state = "erased"
    if guard.erased_at is None:
        guard.erased_at = datetime.now(UTC)


async def _cascade_voiceprint_in_session(
    session: AsyncSession,
    *,
    tenant_id: str,
    recording_id: int,
) -> dict[str, int]:
    """Remove biometric rows and repair canonical speaker aggregates atomically."""
    speaker_ids = set(
        (
            await session.execute(
                select(VoiceprintVector.speaker_entity_id).where(
                    VoiceprintVector.tenant_id == tenant_id,
                    VoiceprintVector.recording_id == recording_id,
                )
            )
        ).scalars()
    )
    deleted_nodes = 0
    updated_nodes = 0
    if speaker_ids:
        nodes = list(
            (
                await session.execute(
                    select(SpeakerNode)
                    .where(
                        SpeakerNode.tenant_id == tenant_id,
                        SpeakerNode.id.in_(speaker_ids),
                    )
                    .with_for_update()
                )
            ).scalars()
        )
        for node in nodes:
            remaining = [
                int(item) for item in list(node.recordings_list or []) if int(item) != recording_id
            ]
            if not remaining:
                await session.delete(node)
                deleted_nodes += 1
            else:
                node.recordings_list = remaining
                node.recordings_count = len(remaining)
                updated_nodes += 1

    voiceprints = _rowcount(
        await session.execute(
            delete(VoiceprintVector).where(
                VoiceprintVector.tenant_id == tenant_id,
                VoiceprintVector.recording_id == recording_id,
            )
        )
    )
    speaker_links = _rowcount(
        await session.execute(
            delete(SpeakerLink).where(
                SpeakerLink.tenant_id == tenant_id,
                SpeakerLink.recording_id == recording_id,
            )
        )
    )
    return {
        "voiceprints": voiceprints,
        "speaker_links": speaker_links,
        "speaker_nodes_deleted": deleted_nodes,
        "speaker_nodes_updated": updated_nodes,
    }


async def stage_recording_erasure(
    session: AsyncSession,
    *,
    tenant_id: str,
    recording_id: int,
    actor_user_id: int,
) -> ErasureStageResult | None:
    """Delete database truth and persist external cleanup intent atomically.

    ``None`` means neither a recording nor a prior outbox exists for the
    tenant-scoped subject. A prior outbox is returned even after the recording
    has gone, making the legacy erase endpoint an idempotent retry trigger.
    """
    existing = (
        await session.execute(
            select(ErasureOutbox)
            .where(
                ErasureOutbox.tenant_id == tenant_id,
                ErasureOutbox.subject_type == "recording",
                ErasureOutbox.subject_id == str(recording_id),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()

    recording = (
        await session.execute(
            select(Recording)
            .where(
                Recording.tenant_id == tenant_id,
                Recording.id == recording_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if recording is None:
        if existing is None:
            return None
        return ErasureStageResult(outbox_id=existing.id, newly_erased=False)

    await _mark_cache_source_erased(
        session,
        tenant_id=tenant_id,
        recording_id=recording_id,
    )
    counts: dict[str, int] = {}
    artifact_paths = await invalidate_receptions_for_recording(
        session,
        tenant_id=tenant_id,
        recording_id=recording_id,
        actor=f"dsar:user:{actor_user_id}",
        counts=counts,
    )
    counts.update(
        await _cascade_voiceprint_in_session(
            session,
            tenant_id=tenant_id,
            recording_id=recording_id,
        )
    )

    counts["tag_current"] = _rowcount(
        await session.execute(
            delete(TagCurrent).where(
                TagCurrent.tenant_id == tenant_id,
                TagCurrent.recording_id == recording_id,
            )
        )
    )
    counts["tag_facts"] = _rowcount(
        await session.execute(
            delete(TagFact).where(
                TagFact.tenant_id == tenant_id,
                TagFact.recording_id == recording_id,
            )
        )
    )
    counts["chunks"] = _rowcount(
        await session.execute(
            delete(Chunk).where(
                Chunk.tenant_id == tenant_id,
                Chunk.recording_id == recording_id,
            )
        )
    )
    counts["segments"] = _rowcount(
        await session.execute(
            delete(Segment).where(
                Segment.tenant_id == tenant_id,
                Segment.recording_id == recording_id,
            )
        )
    )

    # Break the Recording -> active run pointer before deleting the aggregate;
    # the run itself cascades from Recording in the same transaction.
    recording.active_pipeline_run_id = None
    await session.flush()

    payload = {
        "version": 1,
        "recording_id": recording_id,
        "audio_paths": list(
            dict.fromkeys(
                str(path) for path in (recording.path, recording.audio_encrypted_path) if path
            )
        ),
        "reception_artifact_paths": artifact_paths,
        "cleanup": {
            "audio": True,
            "reception_artifacts": True,
            "graph": True,
            "file_index": True,
            "llm_cache": True,
        },
    }
    now = datetime.now(UTC)
    if existing is None:
        existing = ErasureOutbox(
            tenant_id=tenant_id,
            subject_type="recording",
            subject_id=str(recording_id),
            payload=payload,
            status="pending",
            attempts=0,
            available_at=now,
        )
        session.add(existing)
    else:
        # Defensive recovery for an old/manual outbox coexisting with source
        # truth. Preserve attempt history but refresh the immutable manifest.
        existing.payload = payload
        existing.status = "pending"
        existing.available_at = now
        existing.lease_owner = None
        existing.lease_token = None
        existing.lease_expires_at = None
        existing.last_error = None
        existing.completed_at = None
    await session.flush()

    deleted = _rowcount(
        await session.execute(
            delete(Recording).where(
                Recording.tenant_id == tenant_id,
                Recording.id == recording_id,
            )
        )
    )
    counts["recordings"] = deleted
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            user_id=actor_user_id,
            action="dsar.erase",
            target=f"recording:{recording_id}",
            before_value={"recording_id": recording_id},
            after_value={
                "recording_deleted": deleted == 1,
                "database_rows_invalidated": sum(counts.values()),
                "affected_receptions": counts.get("receptions", 0),
                "external_cleanup_queued": True,
                "outbox_id": existing.id,
            },
            occurred_at=now,
        )
    )
    return ErasureStageResult(outbox_id=existing.id, newly_erased=True)


class ErasureOutboxProcessor:
    """Lease-aware, idempotent processor for Graph/FileIndex/cache/file cleanup."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        working_dir: Path,
        graph_store_factory: GraphStoreFactory | None = None,
        file_index_factory: FileIndexFactory | None = None,
        graph_cleanup: GraphCleanup | None = None,
        llm_cache: Any | None = None,
        worker_id: str = "api-inline",
    ) -> None:
        self._factory = session_factory
        self._working_dir = Path(working_dir)
        self._graph_store_factory = graph_store_factory
        self._file_index_factory = file_index_factory
        self._graph_cleanup = graph_cleanup
        self._llm_cache = llm_cache
        self._worker_id = worker_id

    async def process_subject(
        self,
        *,
        tenant_id: str,
        subject_type: str,
        subject_id: str,
        force_retry: bool = False,
    ) -> str | None:
        """Claim and process one subject, returning its persisted status."""
        claim = await self._claim(
            tenant_id=tenant_id,
            subject_type=subject_type,
            subject_id=subject_id,
            force_retry=force_retry,
        )
        if claim is None:
            return await self._status(
                tenant_id=tenant_id,
                subject_type=subject_type,
                subject_id=subject_id,
            )

        try:
            await self._perform_cleanup(claim)
        except Exception as exc:
            logger.warning(
                "Erasure outbox cleanup deferred tenant=%s subject=%s:%s error=%s",
                tenant_id,
                subject_type,
                subject_id,
                type(exc).__name__,
            )
            await self._mark_failed(claim, exc)
        else:
            await self._mark_succeeded(claim)
        return await self._status(
            tenant_id=tenant_id,
            subject_type=subject_type,
            subject_id=subject_id,
        )

    async def drain_pending(self, *, limit: int = 100) -> dict[str, int]:
        """Reconcile pending, retryable and lease-expired work in FIFO order."""
        if not 1 <= limit <= 1_000:
            raise ValueError("limit must be in [1, 1000]")
        now = datetime.now(UTC)
        async with self._factory() as session:
            subjects = list(
                (
                    await session.execute(
                        select(
                            ErasureOutbox.tenant_id,
                            ErasureOutbox.subject_type,
                            ErasureOutbox.subject_id,
                        )
                        .where(
                            ErasureOutbox.available_at <= now,
                            or_(
                                ErasureOutbox.status.in_(("pending", "failed")),
                                and_(
                                    ErasureOutbox.status == "processing",
                                    or_(
                                        ErasureOutbox.lease_expires_at.is_(None),
                                        ErasureOutbox.lease_expires_at <= now,
                                    ),
                                ),
                            ),
                        )
                        .order_by(ErasureOutbox.available_at, ErasureOutbox.id)
                        .limit(limit)
                    )
                ).all()
            )

        report: dict[str, int] = {
            "selected": len(subjects),
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
        }
        for tenant_id, subject_type, subject_id in subjects:
            status = await self.process_subject(
                tenant_id=str(tenant_id),
                subject_type=str(subject_type),
                subject_id=str(subject_id),
            )
            if status == "succeeded":
                report["succeeded"] += 1
            elif status == "failed":
                report["failed"] += 1
            else:
                # Another worker may have claimed or completed it after the
                # non-locking scan. The claim CAS remains authoritative.
                report["skipped"] += 1
        return report

    async def _claim(
        self,
        *,
        tenant_id: str,
        subject_type: str,
        subject_id: str,
        force_retry: bool,
    ) -> _Claim | None:
        now = datetime.now(UTC)
        async with self._factory() as session, session.begin():
            row = (
                await session.execute(
                    select(ErasureOutbox)
                    .where(
                        ErasureOutbox.tenant_id == tenant_id,
                        ErasureOutbox.subject_type == subject_type,
                        ErasureOutbox.subject_id == subject_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.status in {"succeeded", "dead_letter"}:
                return None
            if (
                row.status == "processing"
                and row.lease_expires_at is not None
                and self._aware(row.lease_expires_at) > now
            ):
                return None
            if not force_retry and self._aware(row.available_at) > now:
                return None

            lease_token = secrets.token_hex(32)
            row.status = "processing"
            row.attempts += 1
            row.lease_owner = self._worker_id
            row.lease_token = lease_token
            row.lease_expires_at = now + timedelta(seconds=_LEASE_SECONDS)
            row.last_error = None
            return _Claim(
                id=row.id,
                tenant_id=str(row.tenant_id),
                subject_type=str(row.subject_type),
                subject_id=str(row.subject_id),
                payload=dict(row.payload),
                lease_token=lease_token,
            )

    async def _perform_cleanup(self, claim: _Claim) -> None:
        recording_id = int(claim.payload["recording_id"])
        cleanup = dict(claim.payload.get("cleanup") or {})

        if cleanup.get("graph"):
            if self._graph_store_factory is None or self._graph_cleanup is None:
                raise RuntimeError("graph erasure dependency is not configured")
            graph_store = await self._maybe_await(self._graph_store_factory(claim.tenant_id))
            if graph_store is not None:
                await self._maybe_await(
                    self._graph_cleanup(
                        graph_store,
                        recording_id,
                        claim.tenant_id,
                    )
                )
                await graph_store.save()

        if cleanup.get("file_index"):
            if self._file_index_factory is None:
                raise RuntimeError("file-index erasure dependency is not configured")
            file_index = await self._maybe_await(self._file_index_factory(claim.tenant_id))
            if file_index is not None:
                await file_index.erase_recording(recording_id)

        if cleanup.get("llm_cache") and self._llm_cache is not None:
            await self._llm_cache.delete_by_provenance(
                claim.tenant_id,
                [{"source_type": "recording", "source_id": str(recording_id)}],
            )

        if cleanup.get("audio"):
            await asyncio.to_thread(
                self._erase_audio_paths,
                list(claim.payload.get("audio_paths") or []),
            )
        if cleanup.get("reception_artifacts"):
            paths = list(claim.payload.get("reception_artifact_paths") or [])
            if paths:
                await asyncio.to_thread(
                    erase_reception_artifacts,
                    paths,
                    allowed_root=self._working_dir,
                )

    def _erase_audio_paths(self, raw_paths: list[Any]) -> None:
        for raw_path in dict.fromkeys(str(item) for item in raw_paths if item):
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = self._working_dir / candidate
            if not candidate.exists():
                # Tolerated on purpose: erasure retries after partial failure, and
                # a file removed by the previous attempt must be a no-op or the
                # outbox row can never reach `succeeded`. But the same branch also
                # covers audio sitting on a DIFFERENT deployment's working_dir
                # volume (two stacks, one database) — the DSAR is then recorded as
                # fulfilled while the audio survives elsewhere. Log which worker
                # and which working_dir made the call, so the audit trail can tell
                # a benign retry from an orphaned file.
                logger.warning(
                    "DSAR erasure: audio path %s already absent (worker=%s, "
                    "working_dir=%s). Expected on retry; on a multi-deployment "
                    "database this can mean the file lives on another stack's volume.",
                    candidate,
                    self._worker_id,
                    self._working_dir,
                )
                continue
            if not candidate.is_file():
                raise ValueError("recording audio target is not a regular file")
            candidate.unlink()

    async def _mark_succeeded(self, claim: _Claim) -> None:
        now = datetime.now(UTC)
        async with self._factory() as session, session.begin():
            result = await session.execute(
                update(ErasureOutbox)
                .where(
                    ErasureOutbox.id == claim.id,
                    ErasureOutbox.tenant_id == claim.tenant_id,
                    ErasureOutbox.status == "processing",
                    ErasureOutbox.lease_token == claim.lease_token,
                )
                .values(
                    status="succeeded",
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    last_error=None,
                    completed_at=now,
                )
            )
            if _rowcount(result) != 1:
                raise RuntimeError("erasure outbox lease was lost before completion")

    async def _mark_failed(self, claim: _Claim, exc: Exception) -> None:
        now = datetime.now(UTC)
        error = (f"{type(exc).__name__}: external erasure step failed")[:_MAX_ERROR_LENGTH]
        async with self._factory() as session, session.begin():
            row = (
                await session.execute(
                    select(ErasureOutbox)
                    .where(
                        ErasureOutbox.id == claim.id,
                        ErasureOutbox.tenant_id == claim.tenant_id,
                        ErasureOutbox.status == "processing",
                        ErasureOutbox.lease_token == claim.lease_token,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                logger.error(
                    "Cannot record erasure failure because lease was lost outbox_id=%d",
                    claim.id,
                )
                return
            delay_seconds = min(300, 2 ** min(row.attempts, 8))
            row.status = "failed"
            row.available_at = now + timedelta(seconds=delay_seconds)
            row.lease_owner = None
            row.lease_token = None
            row.lease_expires_at = None
            row.last_error = error
            row.completed_at = None

    async def _status(
        self,
        *,
        tenant_id: str,
        subject_type: str,
        subject_id: str,
    ) -> str | None:
        async with self._factory() as session:
            value = await session.scalar(
                select(ErasureOutbox.status).where(
                    ErasureOutbox.tenant_id == tenant_id,
                    ErasureOutbox.subject_type == subject_type,
                    ErasureOutbox.subject_id == subject_id,
                )
            )
            return str(value) if value is not None else None

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


__all__ = [
    "ErasureOutboxProcessor",
    "ErasureStageResult",
    "remove_recording_graph_refs",
    "stage_recording_erasure",
]
