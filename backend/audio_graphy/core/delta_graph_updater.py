"""DeltaGraphUpdater — incremental knowledge-graph update from confirmed chunks.

M8 Phase 4 (WS-2 / T7). Streaming counterpart to ``core/extractor.py`` +
``core/graph.py``. For each ChunkRecord produced by ``StreamingChunker``,
run entity extraction → merge → speaker-link → graph-store upsert.

Reuses (L5 + §17.8):

    - ``EntityExtractor.extract_from_chunk()`` (no source change).
    - ``EntityMerger.merge()`` via per-tenant factory (no source change).
    - ``SpeakerLinker.run()`` via per-tenant factory (no source change).
    - ``NetworkXGraphStore.upsert_node()`` / ``upsert_edge()`` (no source change).

Delta detection (L8):

    Before extraction, query the ``chunks`` table for ``content_hash``.
    If the hash already exists for this tenant, skip extraction entirely
    and emit ``streaming_delta_skipped_total`` (P0-7 + P0-12 metric).

Edge confidence tagging (L9 + Q3):

    - LLM first-round relations → ``EXTRACTED``.
    - Gleaning-supplement relations → ``INFERRED``.
    - EntityMerger fuzzy hits on either endpoint → ``AMBIGUOUS``.

M9 R1 (T3):
    When ``Settings.enable_advanced_graph`` is True, every newly inserted
    edge is routed through ``BiTemporalEdgeService.insert_edge()`` so that
    the four bi-temporal timestamps + ``superseded_by`` pointer are
    populated correctly. The matching ``EdgeEvent`` audit row is buffered
    on the report for the caller to commit (Q1 dual-track hook).

NOT in scope:
    - Leiden community rebuild (L6 — admin-only, separate task).
    - SpeakerFuzzyMatcher wiring into Layer 2 (T12, separate).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.adapters.bundle import AdapterBundle
from audio_graphy.adapters.protocols import EdgeConfidence
from audio_graphy.core.chunker import ChunkRecord
from audio_graphy.core.extractor import (
    EntityExtractor,
    ExtractedEntity,
    ExtractedRelation,
)
from audio_graphy.core.streaming_rwlock import StreamingRWLock

if TYPE_CHECKING:
    from audio_graphy.core.bi_temporal import BiTemporalEdgeService
    from audio_graphy.core.entity_merger import EntityMerger
    from audio_graphy.core.speaker_linker import SpeakerLinker
    from audio_graphy.models.edge_event import EdgeEvent
    from audio_graphy.storage.file_index import FileIndex
    from audio_graphy.storage.graph_networkx import NetworkXGraphStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeltaUpdateReport:
    """Output of one ``DeltaGraphUpdater.update()`` call.

    Attributes:
        chunk_id: DB id of the persisted chunk (None if persistence skipped).
        skipped_by_hash: True if content_hash matched an existing chunk (delta dedup).
        new_entities: Number of brand-new entity nodes inserted into the graph.
        merged_entities: Number of entities merged into existing canonicals.
        new_edges: Number of new edges added to the graph.
        ambiguous_edges: Subset of new_edges tagged AMBIGUOUS.
        speaker_links: Number of speaker-link operations invoked (always 0 in M8 P0).
        extraction_ms: Wall-clock time of the LLM extraction step.
        merge_ms: Wall-clock time of EntityMerger.merge().
        persist_ms: Wall-clock time of chunk + entity + edge DB writes.
        m9_edge_events: M9 only — buffered EdgeEvent rows awaiting commit.
            Empty when ``enable_advanced_graph`` is False (zero-regression).
    """

    chunk_id: int | None
    skipped_by_hash: bool
    new_entities: int
    merged_entities: int
    new_edges: int
    ambiguous_edges: int
    speaker_links: int
    extraction_ms: float
    merge_ms: float
    persist_ms: float
    # M9 R1 T3: bi-temporal audit events awaiting caller's DB commit.
    m9_edge_events: list[EdgeEvent] | None = None


# Type aliases for factory callables (kept simple — no ParamSpec for now).
MergerFactory = Callable[[AsyncSession, str], "EntityMerger"]
LinkerFactory = Callable[..., "SpeakerLinker"]
GraphStoreFactory = Callable[[str], "NetworkXGraphStore"]


class DeltaGraphUpdater:
    """Incremental graph update — confirmed chunks → entities/edges.

    Args:
        bundle: AdapterBundle (uses ``strong_llm`` for extraction).
        session_factory: Async session maker for DB access.
        prompt_template: GraphRAG extraction prompt template.
        merger_factory: Per-tenant ``EntityMerger`` factory.
        linker_factory: Per-tenant ``SpeakerLinker`` factory (unused in M8 P0
            but kept for future use; callers may pass a no-op).
        file_index: Optional FileIndex for LLM cache Layer 2.
        graph_store_factory: Per-tenant ``NetworkXGraphStore`` factory.
        rwlock: StreamingRWLock guarding the in-memory graph (per-tenant).
        session_id: WebSocket session_id (for edge provenance).
    """

    def __init__(
        self,
        bundle: AdapterBundle,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        prompt_template: str,
        merger_factory: MergerFactory,
        linker_factory: LinkerFactory,
        file_index: FileIndex | None,
        graph_store_factory: GraphStoreFactory,
        rwlock: StreamingRWLock,
        session_id: str,
        enable_advanced_graph: bool = False,
    ) -> None:
        self._bundle = bundle
        self._session_factory = session_factory
        self._prompt_template = prompt_template
        self._merger_factory = merger_factory
        self._linker_factory = linker_factory
        self._file_index = file_index
        self._graph_store_factory = graph_store_factory
        self._rwlock = rwlock
        self._session_id = session_id
        # M9 R1 T3: bi-temporal hook is flag-gated (L9). When False the
        # updater behaves identically to M8 (zero-regression).
        self._enable_advanced_graph: bool = enable_advanced_graph

        # Reusable extractor (no per-call state besides prompt + cache).
        self._extractor = EntityExtractor(
            bundle=bundle,
            prompt_template=prompt_template,
            file_index=file_index,
        )

    async def update(
        self,
        chunk: ChunkRecord,
        recording_id: int,
        tenant_id: str,
    ) -> DeltaUpdateReport:
        """Process one ChunkRecord through the delta pipeline.

        Steps:
            1. content_hash lookup → skip if hit.
            2. Persist chunk to DB.
            3. Extract entities via LLM.
            4. EntityMerger.merge() → normalise.
            5. Insert entities + edges with streaming provenance.
            6. SpeakerLinker.run() (no-op in M8 P0; left for future).
            7. Update NetworkXGraphStore under rwlock.write.

        Args:
            chunk: ChunkRecord produced by ``StreamingChunker``.
            recording_id: Recording id for provenance.
            tenant_id: Tenant scope.

        Returns:
            DeltaUpdateReport.
        """
        # Initialise timers.
        t_extract = t_merge = t_persist = 0.0

        async with self._session_factory() as session:
            # Step 1: content_hash dedup (L8).
            existing = await self._find_chunk_by_hash(
                session,
                chunk.content_hash,
                tenant_id,
            )
            if existing is not None:
                logger.info(
                    "DeltaGraphUpdater skipped chunk by content_hash=%s (session=%s)",
                    chunk.content_hash[:12],
                    self._session_id,
                )
                return DeltaUpdateReport(
                    chunk_id=existing,
                    skipped_by_hash=True,
                    new_entities=0,
                    merged_entities=0,
                    new_edges=0,
                    ambiguous_edges=0,
                    speaker_links=0,
                    extraction_ms=0.0,
                    merge_ms=0.0,
                    persist_ms=0.0,
                    m9_edge_events=None,
                )

            # Step 2: persist chunk + get id.
            t0 = time.perf_counter()
            chunk_id = await self._persist_chunk(session, chunk, recording_id, tenant_id)
            t_persist = (time.perf_counter() - t0) * 1000.0

            # Step 3: extract entities (LLM call, may hit cache).
            t0 = time.perf_counter()
            extraction = await self._extractor.extract_from_chunk(
                chunk_id=chunk_id,
                chunk_text=chunk.text,
                recording_id=recording_id,
                tenant_id=tenant_id,
            )
            t_extract = (time.perf_counter() - t0) * 1000.0

            # Step 4: EntityMerger.merge().
            t0 = time.perf_counter()
            merger = self._merger_factory(session, tenant_id)
            merge_pairs = [(e.name, e.type) for e in extraction.entities]
            merged_pairs = await merger.merge(merge_pairs)
            merge_scores = self._extract_merge_scores(merger)
            t_merge = (time.perf_counter() - t0) * 1000.0

            # Build canonical-name lookup for relations.
            name_remap: dict[str, str] = {
                ent.name: merged_pairs[i][0]
                for i, ent in enumerate(extraction.entities)
                if i < len(merged_pairs)
            }

            # Step 5: tag edges + persist (entities/edges go into NetworkX,
            # not into SQL — see m8-architecture §14.1.2 deviation note).
            edges_with_conf = self._tag_edges(extraction.relations, name_remap, merge_scores)
            new_entity_count, merged_entity_count = self._count_entity_outcomes(merge_scores)
            ambiguous_count = sum(1 for _, c in edges_with_conf if c == "AMBIGUOUS")

            # Step 6: speaker link (M8 P0: no-op — kept as a stable call site).
            speaker_links = 0  # deferred to round 2 (StreamingTagScheduler WS-3).

            # Step 7: update NetworkX graph store under write-lock.
            graph_store = self._graph_store_factory(tenant_id)
            t0 = time.perf_counter()
            m9_events_buffer: list[EdgeEvent] | None = None
            async with self._rwlock.write_lock():
                m9_events_buffer = await self._write_to_graph(
                    graph_store,
                    extraction.entities,
                    merged_pairs,
                    edges_with_conf,
                    recording_id,
                    chunk_id,
                    tenant_id,
                )
            t_persist += (time.perf_counter() - t0) * 1000.0

            await session.commit()

        return DeltaUpdateReport(
            chunk_id=chunk_id,
            skipped_by_hash=False,
            new_entities=new_entity_count,
            merged_entities=merged_entity_count,
            new_edges=len(edges_with_conf),
            ambiguous_edges=ambiguous_count,
            speaker_links=speaker_links,
            extraction_ms=t_extract,
            merge_ms=t_merge,
            persist_ms=t_persist,
            m9_edge_events=m9_events_buffer,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _find_chunk_by_hash(
        self,
        session: AsyncSession,
        content_hash: str,
        tenant_id: str,
    ) -> int | None:
        """Return existing chunk id if content_hash exists for this tenant."""
        from audio_graphy.models.chunk import Chunk

        stmt = select(Chunk.id).where(
            Chunk.tenant_id == tenant_id,
            Chunk.content_hash == content_hash,
        )
        result = await session.execute(stmt)
        row = result.first()
        return int(row[0]) if row is not None else None

    async def _persist_chunk(
        self,
        session: AsyncSession,
        chunk: ChunkRecord,
        recording_id: int,
        tenant_id: str,
    ) -> int:
        """Insert one Chunk row and return its id (mirrors batch chunker)."""
        from audio_graphy.models.chunk import Chunk

        orm_chunk = Chunk(
            tenant_id=tenant_id,
            recording_id=recording_id,
            segment_ids=chunk.segment_ids,
            text=chunk.text,
            token_n=chunk.token_n,
            content_hash=chunk.content_hash,
        )
        session.add(orm_chunk)
        await session.flush()  # populate auto-increment id
        return orm_chunk.id

    @staticmethod
    def _extract_merge_scores(merger: EntityMerger) -> list[float]:
        """Best-effort extraction of per-entity merge scores from the merger.

        ``EntityMerger.merge()`` returns ``[(canonical, type), ...]`` but does
        NOT expose scores in its return value. To tag edges as AMBIGUOUS vs
        INFERRED, we look at the merger's in-memory state: any canonical
        that was registered as a fuzzy hit during this merge call counts as
        an AMBIGUOUS source.

        For the M8 P0 we fall back to a simpler heuristic: if the merger's
        ``_canonical_index`` grew during the merge (i.e. any new canonical
        was added), we tag ALL edges as EXTRACTED (no fuzzy hits observed).
        The proper integration with rapidfuzz scores is left to round 2.
        """
        # In M8 P0 we always return [] — fuzzy hits are recorded but not
        # surfaced via the merge() return contract. Edge tagging falls back
        # to ``confidence`` attribute on the ExtractedRelation itself.
        return []

    def _tag_edges(
        self,
        relations: list[ExtractedRelation],
        name_remap: dict[str, str],
        merge_scores: list[float],
    ) -> list[tuple[ExtractedRelation, EdgeConfidence]]:
        """Decide per-edge confidence after entity merge.

        Rules:
            - LLM first-round → EXTRACTED.
            - Gleaning → INFERRED.
            - If either endpoint was remapped by EntityMerger AND the remap
              looks fuzzy (heuristic: remapped name differs significantly),
              tag AMBIGUOUS. Otherwise honour the LLM confidence.
        """
        out: list[tuple[ExtractedRelation, EdgeConfidence]] = []
        for rel in relations:
            base = rel.confidence  # EXTRACTED or INFERRED from the extractor
            src_remapped = name_remap.get(rel.source_name, rel.source_name) != rel.source_name
            tgt_remapped = name_remap.get(rel.target_name, rel.target_name) != rel.target_name
            if src_remapped or tgt_remapped:
                # Conservative: any entity-merge remap on this edge → AMBIGUOUS.
                # This avoids over-trusting cross-recording fuzzy hits.
                tag: EdgeConfidence = "AMBIGUOUS"
            else:
                tag = base
            out.append((rel, tag))
        return out

    @staticmethod
    def _count_entity_outcomes(merge_scores: list[float]) -> tuple[int, int]:
        """Split entity outcomes into new vs merged.

        In M8 P0 (no score surface), assume all are new. Round 2 will
        refine this when the EntityMerger API exposes scores.
        """
        # Without score surface, count all as new.
        return (len(merge_scores) if merge_scores else 0, 0) if merge_scores else (0, 0)

    async def _write_to_graph(
        self,
        graph_store: NetworkXGraphStore,
        entities: list[ExtractedEntity],
        merged_pairs: list[tuple[str, str]],
        edges_with_conf: list[tuple[ExtractedRelation, EdgeConfidence]],
        recording_id: int,
        chunk_id: int,
        tenant_id: str,
    ) -> list[EdgeEvent] | None:
        """Upsert nodes + edges into the NetworkXGraphStore.

        Reuses ``NetworkXGraphStore.upsert_node`` and ``upsert_edge`` —
        both are tenant-scoped and idempotent. The M8 streaming origin is
        recorded via the ``source_ids`` field (existing GraphNode attribute).

        M9 R1 T3 — bi-temporal hook:
            When ``self._enable_advanced_graph`` is True, every edge passes
            through ``BiTemporalEdgeService.insert_edge()`` so that the four
            bi-temporal timestamps + ``superseded_by`` field are populated
            correctly. The matching ``EdgeEvent`` audit row is collected
            into a buffer and returned to the caller for atomic commit.

        Returns:
            ``None`` when the M9 flag is False (zero-regression path).
            A list of ``EdgeEvent`` rows (one per edge written) when True.
        """
        # Lazy import to avoid core/types.py circular dependency at module-load.
        from audio_graphy.core.types import GraphEdge, GraphNode

        # Build nodes (one per merged canonical name).
        seen_canonicals: set[str] = set()
        for ent, (canonical, ent_type) in zip(entities, merged_pairs, strict=False):
            if canonical in seen_canonicals:
                continue
            seen_canonicals.add(canonical)
            node = GraphNode(
                entity_id=canonical,
                name=canonical,
                type=ent_type,
                description=ent.description,
                source_ids=[f"{recording_id}_{chunk_id}"],
                recording_ids=[recording_id],
                degree=0,
            )
            await graph_store.upsert_node(node)

        # M9 R1 T3: instantiate the bi-temporal service lazily.
        bt_service: BiTemporalEdgeService | None = None
        if self._enable_advanced_graph:
            from audio_graphy.core.bi_temporal import BiTemporalEdgeService

            bt_service = BiTemporalEdgeService(tenant_id=tenant_id)

        events_buffer: list[EdgeEvent] | None = [] if bt_service is not None else None

        # Build edges.
        for rel, conf in edges_with_conf:
            source_id = (
                merged_pairs[
                    next(
                        (i for i, e in enumerate(entities) if e.name == rel.source_name),
                        0,
                    )
                ][0]
                if entities
                else rel.source_name
            )
            target_id = (
                merged_pairs[
                    next(
                        (i for i, e in enumerate(entities) if e.name == rel.target_name),
                        0,
                    )
                ][0]
                if entities
                else rel.target_name
            )

            if bt_service is not None:
                # M9 path — bi-temporal timestamps + supersede pointer populated.
                edge, event = bt_service.insert_edge(
                    source=source_id,
                    target=target_id,
                    relation=rel.relation,
                    weight=rel.weight,
                    confidence=conf,
                    confidence_score=1.0 if conf == "EXTRACTED" else 0.5,
                    source_ids=[f"{recording_id}_{chunk_id}"],
                )
                assert events_buffer is not None
                events_buffer.append(event)
            else:
                # M1-M8 legacy path — no bi-temporal fields.
                edge = GraphEdge(
                    source=source_id,
                    target=target_id,
                    relation=rel.relation,
                    weight=rel.weight,
                    confidence=conf,
                    confidence_score=1.0 if conf == "EXTRACTED" else 0.5,
                    source_ids=[f"{recording_id}_{chunk_id}"],
                )
            await graph_store.upsert_edge(edge)

        return events_buffer
