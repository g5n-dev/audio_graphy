"""RetentionEnforcer — daily cron: hard-delete recordings past retention.

PIPL §14.3 implementation. Triggered by APScheduler at 03:00 daily (CronTrigger
hour=3 minute=0) — see ``main.py`` lifespan wiring.

Deletion scope per recording (hard delete, M6):
    1. Audio file on disk (``audio_encrypted_path`` if set, else ``path``).
    2. DB rows (explicit deletes for auditability):
       - tag_facts, tag_currents
       - chunks (cascade-clears vector_chunks via FK)
       - segments
       - vectors_chunk rows whose chunk_id belongs to this recording
       - the recordings row itself
    3. GraphML cleanup: cold-load the tenant graph, remove nodes/edges that
       reference this recording, and persist before the irreversible DB
       deletion. A persistence failure is reported and leaves the recording
       retryable.

M9 R1 T14 addition:
    Before removing a node from the graph, the retention cascade now
    invokes ``BiTemporalEdgeService.retention_cascade`` on all edges
    touching that node. The resulting ``EdgeEvent`` rows are buffered
    for atomic DB commit alongside the hard-delete batch. This preserves
    bi-temporal audit trail for any edge that disappeared due to PIPL.

For each recording deleted, one ``retention_delete`` audit_log row is
written. Failures produce ``retention_delete_failed`` entries.

Args (constructor):
    session_factory: async session maker.
    crypto: AudioCrypto (currently unused at delete-time; reserved for M7+
        verification of encrypted_path before unlink).
    audit: AuditWriter (fire-and-forget audit append).
    graph_store_factory: callable ``(tenant_id) -> NetworkXGraphStore | None``;
        synchronous and asynchronous factories are supported. Production uses
        a lazy-loading factory so cold tenants are not skipped.
    retention_days: Override ``Settings.recording_retention_days`` (testing).
    batch_size: Max recordings processed per sweep (default 500).
    enable_advanced_graph: M9 R1 T14 — when True, fire bi-temporal cascade
        on edges before removing nodes (L9 zero-regression flag).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.core.audit import AuditWriter
from audio_graphy.core.crypto import AudioCrypto
from audio_graphy.models.chunk import Chunk
from audio_graphy.models.recording import Recording
from audio_graphy.models.segment import Segment
from audio_graphy.models.tag_fact import TagFact
from audio_graphy.services.reception_erasure import (
    erase_reception_artifacts,
    invalidate_receptions_for_recording,
)
from audio_graphy.storage.graph_bitemporal import GraphCompressionSink, all_graph_nodes

if TYPE_CHECKING:
    from audio_graphy.storage.file_index import FileIndex
    from audio_graphy.storage.graph_networkx import NetworkXGraphStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RetentionReport:
    """Output of RetentionEnforcer.run_sweep.

    Attributes:
        total_scanned: Number of candidate recordings evaluated.
        deleted: Number actually deleted.
        errors: Human-readable error strings (one per failure).
        duration_sec: Wall-clock duration of the sweep.
    """

    total_scanned: int
    deleted: int
    errors: list[str] = field(default_factory=list)
    duration_sec: float = 0.0


class RetentionEnforcer:
    """Daily cron: hard-delete recordings older than recording_retention_days."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        crypto: AudioCrypto | None,
        audit: AuditWriter,
        graph_store_factory: Callable[
            [str],
            NetworkXGraphStore | Awaitable[NetworkXGraphStore | None] | None,
        ],
        *,
        retention_days: int | None = None,
        batch_size: int = 500,
        cascade_voiceprint: bool = True,
        enable_advanced_graph: bool = False,
        working_dir: Path | None = None,
        file_index_factory: Callable[
            [str],
            FileIndex | Awaitable[FileIndex | None] | None,
        ]
        | None = None,
        llm_cache: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._crypto = crypto
        self._audit = audit
        self._graph_store_factory = graph_store_factory
        self._retention_days_override = retention_days
        self._batch_size = batch_size
        self._cascade_voiceprint = cascade_voiceprint
        self._working_dir = working_dir
        self._file_index_factory = file_index_factory
        self._llm_cache = llm_cache
        # M9 R1 T14 — flag-gated bi-temporal cascade (L9 zero-regression).
        self._enable_advanced_graph: bool = enable_advanced_graph
        # Buffer of EdgeEvent rows pending atomic commit (filled by the
        # bi-temporal cascade; flushed in _delete_one after the hard-delete
        # batch). Kept on the instance so multiple recordings in one sweep
        # share a single commit at the end.
        self._m9_edge_events_buffer: list[Any] = []

    async def run_sweep(self) -> RetentionReport:
        """Run one retention sweep. Called by APScheduler daily cron."""
        started = datetime.now(UTC)
        started_perf = started.timestamp()

        retention_days = self._retention_days_override
        if retention_days is None:
            # Late import to keep this module importable without settings.
            from audio_graphy.config import get_settings

            retention_days = get_settings().recording_retention_days

        cutoff = started - timedelta(days=retention_days)

        async with self._session_factory() as session:
            stmt = (
                select(Recording)
                .where(
                    # Match the two "done" lifecycle states in the recordings
                    # enum: 'indexed' (pipeline finished) and 'archived'
                    # (manually tombstoned but not yet retention-deleted).
                    Recording.status.in_(("indexed", "archived")),
                    Recording.recorded_at.is_not(None),
                    Recording.recorded_at < cutoff,
                )
                .order_by(Recording.recorded_at)
                .limit(self._batch_size)
            )
            result = await session.execute(stmt)
            candidates: list[Recording] = list(result.scalars().all())

        if not candidates:
            return RetentionReport(
                total_scanned=0,
                deleted=0,
                duration_sec=datetime.now(UTC).timestamp() - started_perf,
            )

        deleted = 0
        errors: list[str] = []
        for rec in candidates:
            try:
                await self._delete_one(rec)
                deleted += 1
                await self._audit.record(
                    tenant_id=str(rec.tenant_id),
                    user_id=None,
                    action="retention_delete",
                    target=f"recording:{rec.id}",
                    before={
                        "path": str(rec.path),
                        "audio_encrypted_path": rec.audio_encrypted_path,
                        "recorded_at": rec.recorded_at.isoformat() if rec.recorded_at else None,
                    },
                    after={},
                )
            except Exception as exc:
                msg = f"recording {rec.id}: {exc!r}"
                logger.error("Retention delete failed: %s", msg, exc_info=True)
                errors.append(msg)
                await self._audit.record(
                    tenant_id=str(rec.tenant_id),
                    user_id=None,
                    action="retention_delete_failed",
                    target=f"recording:{rec.id}",
                    before={"path": str(rec.path)},
                    after={"error": repr(exc)},
                )

        return RetentionReport(
            total_scanned=len(candidates),
            deleted=deleted,
            errors=errors,
            duration_sec=datetime.now(UTC).timestamp() - started_perf,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _delete_one(self, rec: Recording) -> None:
        """Delete one recording + all dependent rows + on-disk file.

        M6 uses explicit deletes (not ORM cascade) for auditability:
        every removed table is named in code so reviewers can reason
        about what was deleted.

        M7 adds an optional voiceprint cascade when ``cascade_voiceprint``
        is enabled (default True). The cascade:
            1. Looks up ``vectors_voiceprint`` rows for this recording.
            2. For each row's speaker_entity_id:
                a. Drops the recording from speaker_node.recordings_list.
                b. If this was the only recording for the speaker, hard-deletes
                   the speaker_node (FK cascade handles speaker_links).
            3. Hard-deletes the vectors_voiceprint rows.
        """
        # 0. GraphML is a durable PII-bearing store, not a best-effort cache.
        # Scrub and persist it before irreversible DB deletion so a failure
        # enters run_sweep's existing failure/retry reporting path.
        store_result = self._graph_store_factory(str(rec.tenant_id))
        graph_store = await store_result if isinstance(store_result, Awaitable) else store_result
        if graph_store is not None:
            self._remove_graph_refs(
                graph_store,
                rec.id,
                tenant_id=str(rec.tenant_id),
            )
            await graph_store.save()

        # File-backed transcript/chunk/LLM-cache copies are part of the
        # erasure boundary. Persist their removal before irreversible DB work.
        file_index_result = (
            self._file_index_factory(str(rec.tenant_id))
            if self._file_index_factory is not None
            else None
        )
        file_index = (
            await file_index_result
            if isinstance(file_index_result, Awaitable)
            else file_index_result
        )
        if file_index is None and self._working_dir is not None:
            from audio_graphy.storage.file_index import FileIndex

            file_index = FileIndex(
                self._working_dir,
                tenant_id=str(rec.tenant_id),
            )
        if file_index is not None:
            await file_index.erase_recording(rec.id)

        # Remove encrypted LLM outputs linked to this recording before the
        # source row/file becomes irreversible. A failure is intentionally
        # surfaced so the retention sweep reports and retries this recording.
        if self._llm_cache is not None:
            await self._llm_cache.delete_by_provenance(
                str(rec.tenant_id),
                [{"source_type": "recording", "source_id": str(rec.id)}],
            )

        # 1. (M7) voiceprint cascade — must run BEFORE recordings row is
        # deleted so we can still query speaker_node aggregations cleanly.
        if self._cascade_voiceprint:
            try:
                await self._cascade_voiceprint_for_recording(rec)
            except Exception as exc:
                logger.warning(
                    "Retention: voiceprint cascade failed for recording %d: %s",
                    rec.id,
                    exc,
                )

        # 2. During processing encrypted and plaintext paths can coexist.
        # Retention must remove both distinct artifacts.
        audio_paths = {Path(path) for path in (str(rec.path), rec.audio_encrypted_path) if path}
        for audio_path in audio_paths:
            if audio_path.exists():
                try:
                    audio_path.unlink()
                    logger.info(
                        "Retention: deleted audio file %s for recording %d",
                        audio_path,
                        rec.id,
                    )
                except OSError as exc:
                    raise OSError(f"failed to unlink retained audio {audio_path}") from exc
            else:
                # The no-op is deliberate and load-bearing — the unlink runs before
                # the DB transaction, so a transient DB failure leaves audio-gone/
                # row-present and only converges on retry because a missing file
                # is not an error. But silent tolerance also swallows the case
                # where the file lives on ANOTHER deployment's working_dir volume
                # (two stacks sharing one database): the row is deleted, the sweep
                # reports success, and the audio survives where nothing can find
                # it by. Record the absence so an operator can tell the two apart.
                logger.warning(
                    "Retention: audio path %s for recording %d was already absent "
                    "(expected on retry; on a multi-deployment database this can "
                    "mean the file lives on another stack's volume)",
                    audio_path,
                    rec.id,
                )

        # 3. DB rows — explicit deletes for auditability.
        async with self._session_factory() as session:
            reception_artifacts = await invalidate_receptions_for_recording(
                session,
                tenant_id=str(rec.tenant_id),
                recording_id=rec.id,
                actor="retention",
            )
            # TagFact
            await session.execute(
                delete(TagFact).where(
                    TagFact.tenant_id == rec.tenant_id,
                    TagFact.recording_id == rec.id,
                )
            )
            # TagCurrent — same tenant/recording scope.
            await self._delete_tag_current(session, rec.id, str(rec.tenant_id))
            # Chunks (cascades to vector_chunks via FK ON DELETE CASCADE)
            await session.execute(
                delete(Chunk).where(
                    Chunk.tenant_id == rec.tenant_id,
                    Chunk.recording_id == rec.id,
                )
            )
            # Segments
            await session.execute(
                delete(Segment).where(
                    Segment.tenant_id == rec.tenant_id,
                    Segment.recording_id == rec.id,
                )
            )
            # Recording itself
            await session.execute(
                delete(Recording).where(
                    Recording.tenant_id == rec.tenant_id,
                    Recording.id == rec.id,
                )
            )
            if reception_artifacts:
                if self._working_dir is None:
                    raise RuntimeError("working_dir is required to erase reception artifacts")
                await asyncio.to_thread(
                    erase_reception_artifacts,
                    reception_artifacts,
                    allowed_root=self._working_dir,
                )
            await session.commit()

    async def _cascade_voiceprint_for_recording(self, rec: Recording) -> None:
        """M7 PIPL §14.3 cascade: voiceprint_vectors + speaker_nodes cleanup.

        See class docstring for the full decision tree. Failure-tolerant:
        logs warnings + writes audit entries; does not abort the main delete.
        """
        # Local imports to keep this module importable without M7 models.
        from audio_graphy.models.speaker_link import SpeakerLink
        from audio_graphy.models.speaker_node import SpeakerNode
        from audio_graphy.models.voiceprint_vector import VoiceprintVector

        async with self._session_factory() as session:
            # 1. Find all voiceprint rows for this recording.
            vp_rows = list(
                (
                    await session.execute(
                        select(VoiceprintVector).where(
                            VoiceprintVector.recording_id == rec.id,
                            VoiceprintVector.tenant_id == str(rec.tenant_id),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not vp_rows:
                return

            affected_speaker_ids: set[int] = set()
            for vp in vp_rows:
                affected_speaker_ids.add(vp.speaker_entity_id)

            # 2. For each affected speaker_node, decrement / delete.
            for speaker_id in affected_speaker_ids:
                node = await session.get(SpeakerNode, speaker_id)
                if node is None:
                    continue
                recordings_list = list(node.recordings_list or [])
                if rec.id in recordings_list:
                    recordings_list.remove(rec.id)
                if not recordings_list:
                    # Hard delete — speaker_links cascade via FK.
                    await session.delete(node)
                else:
                    node.recordings_list = recordings_list
                    node.recordings_count = len(recordings_list)

            # 3. Hard delete the voiceprint rows.
            await session.execute(
                delete(VoiceprintVector).where(VoiceprintVector.recording_id == rec.id)
            )
            # 4. Drop speaker_links rows referencing this recording.
            await session.execute(delete(SpeakerLink).where(SpeakerLink.recording_id == rec.id))

            await session.commit()

        # Audit the cascade.
        await self._audit.record(
            tenant_id=str(rec.tenant_id),
            user_id=None,
            action="recording_voiceprint_cascade",
            target=f"recording:{rec.id}",
            before={"voiceprint_rows": len(vp_rows)},
            after={},
        )

    @staticmethod
    async def _delete_tag_current(session: AsyncSession, recording_id: int, tenant_id: str) -> None:
        """Delete TagCurrent rows for this recording (no direct FK to Recording)."""
        # Local import to avoid loading the model at module import time
        # in case the table is missing in tests.
        from audio_graphy.models.tag_current import TagCurrent

        await session.execute(
            delete(TagCurrent).where(
                TagCurrent.recording_id == recording_id,
                TagCurrent.tenant_id == tenant_id,
            )
        )

    def _remove_graph_refs(
        self,
        graph_store: NetworkXGraphStore,
        recording_id: int,
        *,
        tenant_id: str = "",
    ) -> None:
        """Drop graph nodes/edges that mention this recording id.

        M6 strategy: iterate node attrs (``recording_ids`` JSON-serialized
        list) and remove nodes whose only reference is this recording.
        Edges are dropped alongside their endpoints by NetworkX.

        M9 R1 T14 addition: when ``self._enable_advanced_graph`` is True,
        every edge touching a to-be-removed node first passes through
        ``BiTemporalEdgeService.retention_cascade`` so that Q1/Q3
        bi-temporal semantics are preserved (edges get ``invalid_at=now()``
        and an ``EdgeEvent`` audit row is queued for commit).
        """
        from audio_graphy.core.types import _str_to_list

        graph = graph_store.graph
        rid_str = str(recording_id)
        to_remove: list[str] = []
        for node_id, attrs in list(graph.nodes(data=True)):
            rec_ids_raw = attrs.get("recording_ids")
            if rec_ids_raw is None:
                continue
            rec_ids = _str_to_list(str(rec_ids_raw))
            if rid_str in rec_ids:
                # If this recording is the only source, drop the node.
                if len(rec_ids) <= 1:
                    to_remove.append(node_id)
                else:
                    # Otherwise just strip this recording id from the list.
                    remaining = [r for r in rec_ids if r != rid_str]
                    from audio_graphy.core.types import _list_to_str

                    graph.nodes[node_id]["recording_ids"] = _list_to_str(remaining)

        # M9 R1 T14 — bi-temporal cascade before hard node removal.
        if self._enable_advanced_graph and to_remove:
            self._bitemporal_cascade_for_nodes(
                graph_store=graph_store,
                node_ids=to_remove,
                tenant_id=tenant_id or "default",
            )

        for node_id in to_remove:
            graph.remove_node(node_id)
        graph_store.invalidate_path_projection()

    def _bitemporal_cascade_for_nodes(
        self,
        *,
        graph_store: NetworkXGraphStore,
        node_ids: list[str],
        tenant_id: str,
    ) -> None:
        """Q3 soft-delete every edge touching any node in ``node_ids``.

        Per architecture §9 + Q3 ruling: edges get ``invalid_at=now()``;
        EdgeEvent audit rows are buffered for commit. Nodes themselves
        are still hard-removed (the M6 path) because retention IS the
        regulatory hard-delete path; only the EDGES get the soft-delete
        treatment so the audit trail records their disappearance.
        """
        # Lazy imports to keep retention.py importable without M9 modules.
        from audio_graphy.core.bi_temporal import BiTemporalEdgeService
        from audio_graphy.core.types import GraphEdge

        bt = BiTemporalEdgeService(tenant_id=tenant_id)
        graph = graph_store.graph

        # Collect every edge that touches any to-be-removed node.
        node_set = set(node_ids)
        edges_to_invalidate: list[GraphEdge] = []
        for src, tgt, data in list(graph.edges(data=True)):
            if src in node_set or tgt in node_set:
                # Reconstruct a GraphEdge from the GraphML attrs.
                # We only need the bi-temporal fields; weight defaults
                # to 1.0 because retention_cascade ignores it.
                try:
                    edge = GraphEdge(
                        source=src,
                        target=tgt,
                        relation=str(data.get("relation", "")),
                        weight=float(data.get("weight", 1.0)),
                        confidence="EXTRACTED",
                        confidence_score=1.0,
                        source_ids=[],
                    )
                    edges_to_invalidate.append(edge)
                except (TypeError, ValueError):
                    # Skip malformed edges — graph cleanup is best-effort.
                    continue

        if not edges_to_invalidate:
            return

        # Produce (invalidated_edge, event) tuples and write back.
        pairs = bt.retention_cascade(
            edges_on_node=edges_to_invalidate,
            actor="retention",
        )
        for invalidated_edge, event in pairs:
            # Update the in-memory edge attrs (GraphML).
            u, v = invalidated_edge.source, invalidated_edge.target
            if graph.has_edge(u, v) and invalidated_edge.invalid_at is not None:
                graph[u][v]["invalid_at"] = invalidated_edge.invalid_at.isoformat()
            # Buffer the EdgeEvent for atomic DB commit.
            self._m9_edge_events_buffer.append(event)


# ============================================================
# M9 R2 T10 — weekly compression cron entrypoint
# ============================================================


class _DryRunSink:
    """Read-through sink that discards writes.

    The sweep had never run for any tenant but "default", so the first real run
    across a populated database is also the first time compression touches
    those graphs. This lets an operator see the candidate counts first.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def fetch_node(self, entity_id: str) -> Any:
        return self._inner.fetch_node(entity_id)

    def fetch_edges_on_node(self, entity_id: str) -> Any:
        return self._inner.fetch_edges_on_node(entity_id)

    def write_node(self, node: Any) -> None:
        return None

    def write_edge(self, edge: Any) -> None:
        return None

    def commit(self) -> None:
        # Nothing was written, but rolling the inner sink back keeps any state
        # it accumulated from leaking into the next tenant.
        self._inner.rollback()

    def rollback(self) -> None:
        self._inner.rollback()


async def _registered_tenant_codes(session_factory: Any) -> list[str]:
    """Every tenant code in the database, ordered for a reproducible sweep."""
    from sqlalchemy import select

    from audio_graphy.models.tenant import Tenant

    async with session_factory() as session:
        result = await session.execute(select(Tenant.code).order_by(Tenant.code))
        return [str(code) for code in result.scalars().all()]


async def run_weekly_compression_sweep(
    *,
    session_factory: Any,
    graph_store_factory: Callable[[str], Awaitable[NetworkXGraphStore | None]],
    settings: Any,
) -> dict[str, Any]:
    """Invoke ``CompressionService.run`` for every tenant that has a graph.

    Designed to be invoked by APScheduler (Sunday 03:00 weekly — the
    cron registration lives in ``main.py``). This function is async so
    that callers can also invoke it directly from admin tooling.

    Returns a summary dict keyed by tenant_id. Failures are logged and
    surfaced in the per-tenant report (best-effort — the sweep does not
    abort on the first tenant error).

    Tenants come from the database. This used to read
    ``settings._compression_tenant_index``, an attribute Settings has never
    defined, so the list was always empty and the sweep silently degraded to
    the single hardcoded "default" tenant — every other tenant's graph went
    uncompressed indefinitely, with nothing logged to say so.

    Args:
        session_factory: async_sessionmaker, used to enumerate tenants.
        graph_store_factory: awaitable returning the per-tenant graph store or
            None. Must be able to cold-load: a tenant whose graph is not
            already resident in the process cache still needs sweeping.
        settings: app settings (god_node_degree_threshold + stale_days
            come from here). ``compression_dry_run`` reports what would change
            without writing.
    """
    from audio_graphy.core.bi_temporal import BiTemporalEdgeService
    from audio_graphy.core.compression import CompressionService

    try:
        tenants = await _registered_tenant_codes(session_factory)
    except Exception as exc:
        logger.error("Compression sweep could not enumerate tenants: %s", exc)
        return {}

    if not tenants:
        logger.info("Compression sweep found no tenants; nothing to do.")
        return {}

    dry_run = bool(getattr(settings, "compression_dry_run", False))
    if dry_run:
        logger.info("Compression sweep running in dry-run mode over %d tenants", len(tenants))

    summary: dict[str, Any] = {}
    for tenant_id in tenants:
        store = await graph_store_factory(tenant_id)
        if store is None:
            continue
        try:
            graph_sink = GraphCompressionSink(store, tenant_id)
            sink: Any = _DryRunSink(graph_sink) if dry_run else graph_sink
            bt = BiTemporalEdgeService(tenant_id=tenant_id)
            service = CompressionService(
                sink=sink,
                bt_service=bt,
                god_node_degree_threshold=int(getattr(settings, "compression_god_node_degree", 50)),
                stale_days=int(getattr(settings, "compression_stale_days", 180)),
                tenant_id=tenant_id,
            )
            nodes = all_graph_nodes(store)
            report = service.run(
                nodes,
                max_candidates=int(getattr(settings, "compression_max_candidates_per_run", 100)),
            )
            summary[tenant_id] = {
                "candidates": len(report.candidates),
                "soft_deleted_nodes": len(report.soft_deleted_nodes),
                "soft_deleted_edges": len(report.soft_deleted_edges),
                "rolled_back": report.rolled_back,
                "error": str(report.error) if report.error else None,
            }
        except Exception as exc:
            logger.error(
                "Weekly compression sweep failed for tenant %s: %s",
                tenant_id,
                exc,
                exc_info=True,
            )
            summary[tenant_id] = {"error": str(exc)}

    return summary


__all__ = ["RetentionEnforcer", "RetentionReport", "run_weekly_compression_sweep"]
