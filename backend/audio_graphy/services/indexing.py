"""Indexing service — pipeline orchestration (VAD/ASR/chunk → extract → graph merge → tag).

Called by the APScheduler pipeline worker. Advances the recording's
PipelineState through each stage.

See: docs/m3-architecture.md §10.1.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.adapters.bundle import AdapterBundle
from audio_graphy.core.chunker import Chunker
from audio_graphy.core.extractor import EntityExtractor
from audio_graphy.core.graph import GraphBuilder
from audio_graphy.core.pii import PIIScrubber
from audio_graphy.core.types import DEFAULT_ENTITY_TYPES
from audio_graphy.models.chunk import Chunk
from audio_graphy.models.enums import PipelineState, RecordingStatus
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


class IndexingService:
    """Pipeline orchestration service.

    Runs VAD → ASR → chunk → extract → graph merge → tag for a single recording.

    Args:
        session_factory: async session maker for DB operations.
        bundle: AdapterBundle (VAD/ASR/LLM/embed).
        vector_store: Global MySQLVectorStore.
        graph_store: Per-tenant NetworkXGraphStore.
        file_index: Per-tenant FileIndex.
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
    ) -> None:
        self._session_factory = session_factory
        self._bundle = bundle
        self._vector_store = vector_store
        self._graph_store = graph_store
        self._file_index = file_index
        # Persistence is fail-safe by default: raw ASR text must not reach
        # chunks, embeddings, extraction prompts, graphs, or file indexes.
        self._pii_scrubber = pii_scrubber or PIIScrubber()

    async def run_pipeline(self, recording: Recording) -> None:
        """Execute the full indexing pipeline for a recording.

        Advances the recording through PipelineState stages. On any failure,
        sets status=failed and pipeline_state=error.

        Args:
            recording: The Recording ORM object to process.
        """
        recording_id = recording.id
        tenant_id = recording.tenant_id

        logger.info("Starting pipeline for recording %d (tenant=%s)", recording_id, tenant_id)

        try:
            # Set status to processing
            await self._update_state(
                recording_id, RecordingStatus.PROCESSING.value, PipelineState.VAD.value
            )

            # Stage 1: VAD + ASR + Chunking
            await self._stage_vad_asr_chunk(recording)
            await self._update_state(
                recording_id, RecordingStatus.PROCESSING.value, PipelineState.EXTRACTION.value
            )

            # Stage 2: Entity extraction
            extractions = await self._stage_extract(recording)
            await self._update_state(
                recording_id, RecordingStatus.PROCESSING.value, PipelineState.GRAPH_MERGE.value
            )

            # Stage 3: Graph merge
            await self._stage_graph_merge(recording, extractions)
            await self._update_state(
                recording_id, RecordingStatus.PROCESSING.value, PipelineState.TAGGING.value
            )

            # Stage 4: Tagging (auto-tag with active prompt)
            await self._stage_tag(recording)

            # Final: indexed
            await self._update_state(
                recording_id,
                RecordingStatus.INDEXED.value,
                PipelineState.DONE.value,
                indexed_at=datetime.now(UTC),
            )

            logger.info("Pipeline completed for recording %d", recording_id)

        except Exception as exc:
            logger.error("Pipeline failed for recording %d: %s", recording_id, exc, exc_info=True)
            await self._update_state(
                recording_id,
                RecordingStatus.FAILED.value,
                PipelineState.ERROR.value,
                error_message=str(exc),
            )

    async def _stage_vad_asr_chunk(self, recording: Recording) -> None:
        """VAD → ASR → token-budget chunking + MySQL persistence."""
        chunker = Chunker(
            self._bundle,
            session_factory=self._session_factory,
            file_index=self._file_index,
            pii_scrubber=self._pii_scrubber,
        )
        await chunker.process_recording(
            recording.id,
            str(recording.path),
            recording.recorded_at,
            tenant_id=str(recording.tenant_id),
        )

    async def _stage_extract(self, recording: Recording) -> list[Any]:
        """Entity extraction from all chunks."""
        # Load chunks for this recording
        async with self._session_factory() as session:
            result = await session.execute(
                select(Chunk)
                .where(
                    Chunk.recording_id == recording.id,
                    Chunk.tenant_id == recording.tenant_id,
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
            entity_types=DEFAULT_ENTITY_TYPES,
            file_index=self._file_index,
        )

        chunk_tuples = [(c.id, c.text, recording.id) for c in chunks]
        extractions = await extractor.extract_from_chunks(chunk_tuples)

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
                logger.warning("Chunk embedding failed for chunk %d: %s", chunk.id, exc)

        return extractions

    async def _stage_graph_merge(self, recording: Recording, extractions: list[Any]) -> None:
        """Merge extraction results into the knowledge graph."""
        if not extractions:
            return

        builder = GraphBuilder(
            self._graph_store,
            bundle=self._bundle,
            vector_store=self._vector_store,
        )
        await builder.build_from_extractions(extractions, tenant_id=str(recording.tenant_id))

        # Reload graph store cache (O3 decision)
        await self._graph_store.load()

    async def _stage_tag(self, recording: Recording) -> None:
        """Auto-tag the recording with the active prompt.

        This is a best-effort stage — if tagging fails, the recording
        is still marked as indexed (tagging can be retried via POST /tags).
        """
        try:
            from audio_graphy.tags.current_view import TagCurrentService
            from audio_graphy.tags.facts import TagFactsService
            from audio_graphy.tags.stats import TagStatsService

            facts_svc = TagFactsService(self._session_factory)
            current_svc = TagCurrentService(self._session_factory)
            stats_svc = TagStatsService(self._session_factory)

            # Use the recording's prompt_version (or a default)
            prompt_version = recording.prompt_version or "tag_prompt_v1"

            # Simple auto-tag: compute quality tags via LLM
            # For mock mode, this produces deterministic tags
            tag_paths = [
                "quality.greeting",
                "quality.closing",
                "sales.product_mention",
            ]

            for tag_path in tag_paths:
                # Build a simple tag prompt
                import hashlib

                messages: list[dict[str, str]] = [
                    {
                        "role": "user",
                        "content": (
                            f"请对录音进行质检打标。\n"
                            f"标签路径: {tag_path}\n"
                            f"录音ID: {recording.id}\n"
                            f"请返回 pass 或 fail。"
                        ),
                    }
                ]

                cache_key = hashlib.md5(
                    f"{tag_path}:{recording.id}:{prompt_version}".encode()
                ).hexdigest()

                # Check cache
                cached = await self._file_index.get_llm_cache(cache_key)
                if cached is not None:
                    tag_value = cached
                else:
                    response = await self._bundle.weak_llm.complete(
                        messages=messages,
                        cache_key=cache_key,
                    )
                    tag_value = response.text.strip().split("\n")[0][:255]
                    await self._file_index.set_llm_cache(cache_key, tag_value)

                # Write tag facts + current + stats
                tid = str(recording.tenant_id)
                await facts_svc.get_next_version(recording.id, tag_path, tid)
                fact = await facts_svc.append_fact(
                    recording_id=recording.id,
                    tag_path=tag_path,
                    tag_value=tag_value,
                    prompt_version=prompt_version,
                    model_version=self._bundle.weak_llm.model,
                    input_hash=cache_key,
                    confidence=0.95,
                    source="llm",
                    computed_by=None,
                    tenant_id=tid,
                )
                await current_svc.upsert_current(fact, tid)
                # Stats delta
                old_value = await current_svc.get_previous_value(
                    recording.id, tag_path, tid, exclude_version=fact.version
                )
                await stats_svc.apply_delta(
                    tenant_id=tid,
                    store_id=str(recording.store_id),
                    agent_name=str(recording.agent_name),
                    tag_path=tag_path,
                    old_value=old_value,
                    new_value=tag_value,
                )

        except Exception as exc:
            logger.warning(
                "Auto-tagging failed for recording %d (non-blocking): %s", recording.id, exc
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
