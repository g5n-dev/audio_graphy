"""Indexing service — pipeline orchestration (VAD/ASR/chunk → extract → graph merge → tag).

Called by the APScheduler pipeline worker. Advances the recording's
PipelineState through each stage.

See: docs/m3-architecture.md §10.1.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.adapters.bundle import AdapterBundle
from audio_graphy.core.chunker import Chunker, ChunkerOutput
from audio_graphy.core.extractor import EntityExtractor
from audio_graphy.core.graph import GraphBuilder
from audio_graphy.core.pii import PIIScrubber
from audio_graphy.core.types import DEFAULT_ENTITY_TYPES
from audio_graphy.models.chunk import Chunk
from audio_graphy.models.enums import PipelineState, RecordingStatus
from audio_graphy.models.pipeline import (
    DEFAULT_REQUIRED_PROJECTIONS,
    ProjectionOutbox,
    RecordingPipelineRun,
    pipeline_run_transition_allowed,
)
from audio_graphy.models.recording import Recording
from audio_graphy.storage.file_index import FileIndex
from audio_graphy.storage.graph_networkx import NetworkXGraphStore
from audio_graphy.storage.mysql_vector import MySQLVectorStore

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Default extraction prompt template
_DEFAULT_PROMPT_TEMPLATE = (
    "你是门店录音质检领域的实体关系抽取专家。\n"
    "请从以下录音转写文本中抽取实体和关系。\n\n"
    "实体类型: {entity_types}\n\n"
    "输入文本:\n{input_text}\n\n"
    "请按以下格式输出:\n"
    '("实体"{tuple_delimiter}名称{tuple_delimiter}类型{tuple_delimiter}描述)'
    "{record_delimiter}"
    '("关系"{tuple_delimiter}源实体{tuple_delimiter}关系{tuple_delimiter}'
    "目标实体{tuple_delimiter}描述)"
    "{completion_delimiter}"
)


class PipelineIncompleteError(RuntimeError):
    """A required AI stage produced no publishable result."""


class PipelineLeaseLostError(RuntimeError):
    """The worker no longer owns the generation it was processing."""


@dataclass(frozen=True, slots=True)
class _PipelineLeaseFence:
    owner: str
    attempt_count: int


class IndexingService:
    """Pipeline orchestration service.

    Runs VAD → ASR → chunk → extract → graph merge → tag for a single recording.

    Args:
        session_factory: async session maker for DB operations.
        bundle: AdapterBundle (VAD/ASR/LLM/embed).
        vector_store: Global MySQLVectorStore.
        graph_store: Per-tenant NetworkXGraphStore.
        file_index: Per-tenant FileIndex.
        enable_adaptive_gleaning: Opt-in multi-round early-stop extraction.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        bundle: AdapterBundle,
        vector_store: MySQLVectorStore,
        graph_store: NetworkXGraphStore,
        file_index: FileIndex,
        *,
        pii_scrubber: PIIScrubber | None = None,
        enable_adaptive_gleaning: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._bundle = bundle
        self._vector_store = vector_store
        self._graph_store = graph_store
        self._file_index = file_index
        # Persistence is fail-safe by default: raw ASR text must not reach
        # chunks, embeddings, extraction prompts, graphs, or file indexes.
        self._pii_scrubber = pii_scrubber or PIIScrubber()
        self._enable_adaptive_gleaning = enable_adaptive_gleaning

    async def run_pipeline(
        self,
        recording: Recording,
        *,
        pipeline_run_id: int | None = None,
        lease_owner: str | None = None,
        lease_seconds: int = 60,
    ) -> None:
        """Execute the full indexing pipeline for a recording.

        Advances the recording through PipelineState stages. On any failure,
        sets status=failed and pipeline_state=error.

        Args:
            recording: The Recording ORM object to process.
        """
        recording_id = int(recording.id)
        tenant_id = str(recording.tenant_id)
        run = await self._resolve_run(
            recording,
            pipeline_run_id=pipeline_run_id,
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
        )
        if run.state in {"ready", "ready_no_speech"}:
            return
        if run.lease_owner is None:
            raise RuntimeError("pipeline run was claimed without a lease owner")
        fence = _PipelineLeaseFence(
            owner=run.lease_owner,
            attempt_count=int(run.attempt_count),
        )
        heartbeat = asyncio.create_task(
            self._heartbeat_lease(
                run.id,
                lease_owner=fence.owner,
                attempt_count=fence.attempt_count,
                lease_seconds=lease_seconds,
            )
        )

        logger.info(
            "Starting pipeline for recording %d generation %d (tenant=%s)",
            recording_id,
            run.generation,
            tenant_id,
        )

        try:
            await self._transition_run(
                run.id,
                state="vad",
                pipeline_state=PipelineState.VAD.value,
                fence=fence,
            )
            await self._transition_run(
                run.id,
                state="asr",
                pipeline_state=PipelineState.ASR.value,
                fence=fence,
            )
            chunker_output = await self._stage_vad_asr_chunk(recording, run)

            if not chunker_output.segments:
                await self._set_required_projections(
                    run.id,
                    ["file_index"],
                    fence=fence,
                )
                await self._ensure_projection_outboxes(run.id, fence=fence)
                await self._file_index.flush()
                await self._complete_projection(
                    run.id,
                    "file_index",
                    fence=fence,
                )
                await self._transition_run(
                    run.id,
                    state="verifying",
                    pipeline_state=PipelineState.EMBEDDING.value,
                    fence=fence,
                )
                await self._activate_run(
                    run.id,
                    ready_state="ready_no_speech",
                    fence=fence,
                )
                return
            if not chunker_output.chunks:
                raise PipelineIncompleteError(
                    "VAD detected speech but ASR produced no publishable transcript"
                )

            await self._transition_run(
                run.id,
                state="segments",
                pipeline_state=PipelineState.CHUNKING.value,
                fence=fence,
            )
            await self._transition_run(
                run.id,
                state="chunks",
                pipeline_state=PipelineState.CHUNKING.value,
                fence=fence,
            )
            await self._ensure_projection_outboxes(run.id, fence=fence)
            await self._file_index.flush()
            await self._complete_projection(
                run.id,
                "file_index",
                fence=fence,
            )

            await self._transition_run(
                run.id,
                state="projections",
                pipeline_state=PipelineState.EMBEDDING.value,
                fence=fence,
            )
            extractions = await self._stage_extract(recording, run)
            await self._complete_projection(run.id, "vector", fence=fence)
            await self._stage_graph_merge(recording, extractions)
            await self._complete_projection(run.id, "graph", fence=fence)

            await self._transition_run(
                run.id,
                state="verifying",
                pipeline_state=PipelineState.GRAPH_MERGE.value,
                fence=fence,
            )
            await self._activate_run(
                run.id,
                ready_state="ready",
                fence=fence,
            )

            logger.info("Pipeline completed for recording %d", recording_id)

        except Exception as exc:
            logger.error("Pipeline failed for recording %d: %s", recording_id, exc, exc_info=True)
            await self._fail_run(run.id, exc, fence=fence)
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _resolve_run(
        self,
        recording: Recording,
        *,
        pipeline_run_id: int | None,
        lease_owner: str | None,
        lease_seconds: int,
    ) -> RecordingPipelineRun:
        """Load/claim one immutable generation for direct or worker execution."""
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            run: RecordingPipelineRun | None
            persistent_recording = (
                await session.execute(
                    select(Recording)
                    .where(
                        Recording.id == recording.id,
                        Recording.tenant_id == recording.tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one()
            if pipeline_run_id is not None:
                run = (
                    await session.execute(
                        select(RecordingPipelineRun)
                        .where(
                            RecordingPipelineRun.id == pipeline_run_id,
                            RecordingPipelineRun.recording_id == recording.id,
                            RecordingPipelineRun.tenant_id == recording.tenant_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one()
            else:
                run = (
                    await session.execute(
                        select(RecordingPipelineRun)
                        .where(
                            RecordingPipelineRun.recording_id == recording.id,
                            RecordingPipelineRun.tenant_id == recording.tenant_id,
                            RecordingPipelineRun.state.in_(
                                ("queued", "claimed", "failed_retryable")
                            ),
                        )
                        .order_by(RecordingPipelineRun.generation.desc())
                        .limit(1)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if run is None:
                    latest = (
                        await session.execute(
                            select(func.max(RecordingPipelineRun.generation)).where(
                                RecordingPipelineRun.recording_id == recording.id
                            )
                        )
                    ).scalar_one_or_none()
                    generation = int(latest or 0) + 1
                    source_fingerprint = (
                        persistent_recording.audio_sha256
                        or hashlib.sha256(
                            (
                                f"{persistent_recording.path}:"
                                f"{persistent_recording.source_revision}"
                            ).encode()
                        ).hexdigest()
                    )
                    run = RecordingPipelineRun(
                        tenant_id=str(recording.tenant_id),
                        recording_id=recording.id,
                        generation=generation,
                        idempotency_key=(
                            f"pipeline:{recording.tenant_id}:{recording.id}:{generation}"
                        ),
                        source_fingerprint=source_fingerprint,
                        config_fingerprint=hashlib.sha256(
                            (
                                f"recording-generation-v1:"
                                f"{persistent_recording.prompt_version or ''}"
                            ).encode()
                        ).hexdigest(),
                        state="queued",
                        required_projections=list(DEFAULT_REQUIRED_PROJECTIONS),
                        completed_projections=[],
                    )
                    session.add(run)
                    await session.flush()

            assert run is not None
            if run.state in {"ready", "ready_no_speech"}:
                return run
            lease_expires_at = run.lease_expires_at
            if lease_expires_at is not None and lease_expires_at.tzinfo is None:
                lease_expires_at = lease_expires_at.replace(tzinfo=UTC)
            already_claimed_by_caller = (
                run.state == "claimed" and run.lease_owner == lease_owner
            )
            if (
                run.state == "claimed"
                and run.lease_owner not in {None, lease_owner}
                and lease_expires_at is not None
                and lease_expires_at > now
            ):
                raise RuntimeError("pipeline run is leased by another worker")
            if not pipeline_run_transition_allowed(run.state, "claimed"):
                raise RuntimeError(
                    f"pipeline run cannot be claimed from state: {run.state}"
                )
            run.state = "claimed"
            run.lease_owner = lease_owner or f"direct:{recording.id}"
            run.lease_expires_at = now + timedelta(seconds=max(lease_seconds, 10))
            if not already_claimed_by_caller:
                run.attempt_count += 1
            run.started_at = run.started_at or now
            run.error_code = None
            run.error_message = None
            if persistent_recording.active_pipeline_run_id is None:
                persistent_recording.status = RecordingStatus.PROCESSING.value
                persistent_recording.pipeline_state = PipelineState.VAD.value
        return run

    async def _heartbeat_lease(
        self,
        run_id: int,
        *,
        lease_owner: str,
        attempt_count: int,
        lease_seconds: int,
    ) -> None:
        interval = max(2, lease_seconds // 3)
        while True:
            await asyncio.sleep(interval)
            async with self._session_factory() as session, session.begin():
                run = await session.get(RecordingPipelineRun, run_id)
                if (
                    run is None
                    or run.lease_owner != lease_owner
                    or int(run.attempt_count) != attempt_count
                    or run.state
                    in {
                        "ready",
                        "ready_no_speech",
                        "partial",
                        "failed_retryable",
                        "failed_terminal",
                        "superseded",
                    }
                ):
                    return
                run.lease_expires_at = datetime.now(UTC) + timedelta(
                    seconds=max(lease_seconds, 10)
                )

    @staticmethod
    def _require_lease_fence(
        run: RecordingPipelineRun,
        fence: _PipelineLeaseFence,
    ) -> None:
        if (
            run.lease_owner != fence.owner
            or int(run.attempt_count) != fence.attempt_count
        ):
            raise PipelineLeaseLostError(
                "pipeline lease was reassigned to another worker attempt"
            )

    async def _transition_run(
        self,
        run_id: int,
        *,
        state: str,
        pipeline_state: str,
        fence: _PipelineLeaseFence,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            run = await session.get(RecordingPipelineRun, run_id, with_for_update=True)
            if run is None:
                raise RuntimeError("pipeline run disappeared")
            self._require_lease_fence(run, fence)
            if not pipeline_run_transition_allowed(run.state, state):
                raise RuntimeError(
                    f"illegal pipeline transition: {run.state} -> {state}"
                )
            run.state = state
            recording = await session.get(Recording, run.recording_id, with_for_update=True)
            if recording is None:
                raise RuntimeError("pipeline recording disappeared")
            if recording.active_pipeline_run_id is None:
                recording.status = RecordingStatus.PROCESSING.value
                recording.pipeline_state = pipeline_state

    async def _set_required_projections(
        self,
        run_id: int,
        projection_types: list[str],
        *,
        fence: _PipelineLeaseFence,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            run = await session.get(RecordingPipelineRun, run_id, with_for_update=True)
            if run is None:
                raise RuntimeError("pipeline run disappeared")
            self._require_lease_fence(run, fence)
            run.required_projections = list(projection_types)
            run.completed_projections = [
                projection
                for projection in run.completed_projections
                if projection in projection_types
            ]

    async def _ensure_projection_outboxes(
        self,
        run_id: int,
        *,
        fence: _PipelineLeaseFence,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            run = await session.get(RecordingPipelineRun, run_id, with_for_update=True)
            if run is None:
                raise RuntimeError("pipeline run disappeared")
            self._require_lease_fence(run, fence)
            existing_types = set(
                (
                    await session.execute(
                        select(ProjectionOutbox.projection_type).where(
                            ProjectionOutbox.pipeline_run_id == run_id
                        )
                    )
                ).scalars()
            )
            for projection_type in run.required_projections:
                if projection_type in existing_types:
                    continue
                session.add(
                    ProjectionOutbox(
                        tenant_id=str(run.tenant_id),
                        recording_id=run.recording_id,
                        pipeline_run_id=run.id,
                        generation=run.generation,
                        projection_type=projection_type,
                        aggregate_type="pipeline_run",
                        aggregate_id=str(run.id),
                        payload={
                            "recording_id": run.recording_id,
                            "generation": run.generation,
                        },
                        idempotency_key=f"pipeline:{run.id}:{projection_type}",
                    )
                )

    async def _complete_projection(
        self,
        run_id: int,
        projection_type: str,
        *,
        fence: _PipelineLeaseFence,
    ) -> None:
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            run = await session.get(RecordingPipelineRun, run_id, with_for_update=True)
            if run is None or projection_type not in run.required_projections:
                raise RuntimeError("unknown pipeline projection")
            self._require_lease_fence(run, fence)
            outbox = (
                await session.execute(
                    select(ProjectionOutbox)
                    .where(
                        ProjectionOutbox.pipeline_run_id == run_id,
                        ProjectionOutbox.projection_type == projection_type,
                    )
                    .with_for_update()
                )
            ).scalar_one()
            outbox.status = "succeeded"
            outbox.attempts += 1
            outbox.available_at = now
            outbox.lease_owner = None
            outbox.lease_expires_at = None
            outbox.error_message = None
            run.completed_projections = sorted(
                {*run.completed_projections, projection_type}
            )

    async def _activate_run(
        self,
        run_id: int,
        *,
        ready_state: str,
        fence: _PipelineLeaseFence,
    ) -> bool:
        if ready_state not in {"ready", "ready_no_speech"}:
            raise ValueError(f"invalid ready pipeline state: {ready_state}")
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            run = await session.get(RecordingPipelineRun, run_id, with_for_update=True)
            if run is None:
                raise RuntimeError("pipeline run disappeared")
            self._require_lease_fence(run, fence)
            if not pipeline_run_transition_allowed(run.state, ready_state):
                raise RuntimeError(
                    f"illegal pipeline transition: {run.state} -> {ready_state}"
                )
            if not run.projections_complete():
                raise PipelineIncompleteError("required projections are incomplete")
            incomplete_outboxes = (
                await session.execute(
                    select(func.count(ProjectionOutbox.id)).where(
                        ProjectionOutbox.pipeline_run_id == run_id,
                        ProjectionOutbox.status != "succeeded",
                    )
                )
            ).scalar_one()
            if incomplete_outboxes:
                raise PipelineIncompleteError("durable projections are not acknowledged")
            latest_generation = (
                await session.execute(
                    select(func.max(RecordingPipelineRun.generation)).where(
                        RecordingPipelineRun.recording_id == run.recording_id
                    )
                )
            ).scalar_one()
            if int(latest_generation) != run.generation:
                run.state = "superseded"
                run.finished_at = now
                run.lease_owner = None
                run.lease_expires_at = None
                return False

            recording = await session.get(Recording, run.recording_id, with_for_update=True)
            if recording is None:
                raise RuntimeError("pipeline recording disappeared")
            if (
                recording.audio_sha256 is not None
                and recording.audio_sha256 != run.source_fingerprint
            ):
                raise PipelineIncompleteError("recording source fingerprint changed")
            if (
                recording.active_pipeline_run_id is not None
                and recording.active_pipeline_run_id != run.id
            ):
                previous = await session.get(
                    RecordingPipelineRun,
                    recording.active_pipeline_run_id,
                    with_for_update=True,
                )
                if previous is not None:
                    previous.state = "superseded"
                    previous.finished_at = previous.finished_at or now

            run.state = ready_state
            run.finished_at = now
            run.activated_at = now
            run.lease_owner = None
            run.lease_expires_at = None
            recording.active_pipeline_run_id = run.id
            recording.status = (
                RecordingStatus.INDEXED.value
                if ready_state == "ready"
                else RecordingStatus.READY_NO_SPEECH.value
            )
            recording.pipeline_state = PipelineState.DONE.value
            recording.indexed_at = now if ready_state == "ready" else None
            return True

    async def _fail_run(
        self,
        run_id: int,
        exc: Exception,
        *,
        fence: _PipelineLeaseFence,
    ) -> None:
        now = datetime.now(UTC)
        target_state = (
            "partial" if isinstance(exc, PipelineIncompleteError) else "failed_retryable"
        )
        async with self._session_factory() as session, session.begin():
            run = await session.get(RecordingPipelineRun, run_id, with_for_update=True)
            if run is None:
                return
            try:
                self._require_lease_fence(run, fence)
            except PipelineLeaseLostError:
                logger.warning(
                    "Ignoring stale worker failure for reassigned pipeline run %d",
                    run_id,
                )
                return
            if not pipeline_run_transition_allowed(run.state, target_state):
                logger.error(
                    "Ignoring illegal pipeline failure transition %s -> %s for run %d",
                    run.state,
                    target_state,
                    run_id,
                )
                return
            run.state = target_state
            run.error_code = type(exc).__name__[:64]
            run.error_message = str(exc)[:8000]
            run.finished_at = now
            run.lease_owner = None
            run.lease_expires_at = None
            recording = await session.get(Recording, run.recording_id, with_for_update=True)
            if recording is not None and recording.active_pipeline_run_id is None:
                recording.status = RecordingStatus.FAILED.value
                recording.pipeline_state = PipelineState.ERROR.value
                recording.indexed_at = None

    async def _stage_vad_asr_chunk(
        self,
        recording: Recording,
        run: RecordingPipelineRun,
    ) -> ChunkerOutput:
        """VAD → ASR → token-budget chunking + MySQL persistence."""
        chunker = Chunker(
            self._bundle,
            session_factory=self._session_factory,
            file_index=self._file_index,
            pii_scrubber=self._pii_scrubber,
        )
        return await chunker.process_recording(
            recording.id,
            str(recording.path),
            recording.recorded_at,
            tenant_id=str(recording.tenant_id),
            pipeline_run_id=run.id,
            generation=run.generation,
        )

    async def _stage_extract(
        self,
        recording: Recording,
        run: RecordingPipelineRun | None = None,
    ) -> list[Any]:
        """Entity extraction from all chunks."""
        # Load chunks for this recording
        async with self._session_factory() as session:
            result = await session.execute(
                select(Chunk)
                .where(
                    Chunk.recording_id == recording.id,
                    Chunk.tenant_id == recording.tenant_id,
                    *(
                        (Chunk.pipeline_run_id == run.id,)
                        if run is not None
                        else ()
                    ),
                )
                .order_by(Chunk.id)
            )
            chunks = list(result.scalars().all())

        if not chunks:
            logger.warning("No chunks found for recording %d, skipping extraction", recording.id)
            return []

        # Build extractor
        extractor = EntityExtractor(
            self._bundle,
            prompt_template=_DEFAULT_PROMPT_TEMPLATE,
            gleaning_rounds=1,
            adaptive_gleaning=self._enable_adaptive_gleaning,
            entity_types=DEFAULT_ENTITY_TYPES,
            file_index=self._file_index,
        )

        chunk_tuples = [(c.id, c.text, recording.id) for c in chunks]
        extractions = await extractor.extract_from_chunks(
            chunk_tuples,
            tenant_id=str(recording.tenant_id),
        )

        # Embed chunks into vector store
        for chunk in chunks:
            try:
                embeddings = await self._bundle.embed.embed_texts([chunk.text])
                if embeddings:
                    await self._vector_store.upsert_chunk_vector(
                        str(recording.tenant_id),
                        chunk.id,
                        embeddings[0].vector,
                    )
            except Exception as exc:
                raise PipelineIncompleteError(
                    f"chunk embedding failed for chunk {chunk.id}: {exc}"
                ) from exc

        return extractions

    async def _stage_graph_merge(self, recording: Recording, extractions: list[Any]) -> None:
        """Merge extraction results into the knowledge graph."""
        if not extractions:
            return

        builder = GraphBuilder(
            self._graph_store,
            bundle=self._bundle,
            vector_store=self._vector_store,
            strict_persistence=True,
        )
        await builder.build_from_extractions(extractions, tenant_id=str(recording.tenant_id))

        # Reload graph store cache (O3 decision)
        await self._graph_store.load()

    async def _stage_tag(self, recording: Recording) -> None:
        """Defer tagging until a recording belongs to an accepted reception.

        Recording-level legacy facts are read-only.  Reception acceptance
        creates the canonical extraction job transactionally, once dialogue
        units and evidence windows exist.
        """

        logger.info(
            "Deferred canonical tagging for recording %d until reception acceptance",
            recording.id,
        )

    async def _update_state(
        self,
        recording_id: int,
        status: str,
        pipeline_state: str,
        *,
        indexed_at: datetime | None = None,
        error_message: str | None = None,
    ) -> None:
        """Update recording status and pipeline_state in the DB."""
        async with self._session_factory() as session:
            result = await session.execute(select(Recording).where(Recording.id == recording_id))
            rec = result.scalar_one_or_none()
            if rec is None:
                return
            rec.status = status
            rec.pipeline_state = pipeline_state
            if indexed_at is not None:
                rec.indexed_at = indexed_at
            await session.commit()
