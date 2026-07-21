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
    3. GraphML cleanup: best-effort removal of nodes/edges that reference
       this recording. Failure here is logged + downgraded (M6 does not
       require atomicity across file + DB + graph).

For each recording deleted, one ``retention_delete`` audit_log row is
written. Failures produce ``retention_delete_failed`` entries.

Args (constructor):
    session_factory: async session maker.
    crypto: AudioCrypto (currently unused at delete-time; reserved for M7+
        verification of encrypted_path before unlink).
    audit: AuditWriter (fire-and-forget audit append).
    graph_store_factory: callable ``(tenant_id) -> NetworkXGraphStore | None``;
        receives the tenant and returns the per-tenant graph store if one
        is cached. Returning ``None`` skips GraphML cleanup for that tenant.
    retention_days: Override ``Settings.recording_retention_days`` (testing).
    batch_size: Max recordings processed per sweep (default 500).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.core.audit import AuditWriter
from audio_graphy.core.crypto import AudioCrypto
from audio_graphy.models.chunk import Chunk
from audio_graphy.models.recording import Recording
from audio_graphy.models.segment import Segment
from audio_graphy.models.tag_fact import TagFact

if TYPE_CHECKING:
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
        crypto: AudioCrypto,
        audit: AuditWriter,
        graph_store_factory: Callable[[str], NetworkXGraphStore | None],
        *,
        retention_days: int | None = None,
        batch_size: int = 500,
    ) -> None:
        self._session_factory = session_factory
        self._crypto = crypto
        self._audit = audit
        self._graph_store_factory = graph_store_factory
        self._retention_days_override = retention_days
        self._batch_size = batch_size

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
                        "recorded_at": rec.recorded_at.isoformat()
                        if rec.recorded_at
                        else None,
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
        """
        # 1. Audio file on disk (encrypted_path preferred; fallback to path).
        cipher_path = (
            rec.audio_encrypted_path if rec.audio_encrypted_path else str(rec.path)
        )
        cipher_path_obj = Path(cipher_path)
        if cipher_path_obj.exists():
            try:
                cipher_path_obj.unlink()
                logger.info(
                    "Retention: deleted audio file %s for recording %d",
                    cipher_path_obj,
                    rec.id,
                )
            except OSError as exc:
                logger.warning(
                    "Retention: failed to unlink %s: %s", cipher_path_obj, exc
                )

        # 2. DB rows — explicit deletes for auditability.
        async with self._session_factory() as session:
            # TagFact
            await session.execute(
                delete(TagFact).where(TagFact.recording_id == rec.id)
            )
            # TagCurrent — same tenant/recording scope.
            await self._delete_tag_current(session, rec.id, str(rec.tenant_id))
            # Chunks (cascades to vector_chunks via FK ON DELETE CASCADE)
            await session.execute(delete(Chunk).where(Chunk.recording_id == rec.id))
            # Segments
            await session.execute(
                delete(Segment).where(Segment.recording_id == rec.id)
            )
            # Recording itself
            await session.execute(delete(Recording).where(Recording.id == rec.id))
            await session.commit()

        # 3. GraphML — best-effort cleanup.
        try:
            graph_store = self._graph_store_factory(str(rec.tenant_id))
            if graph_store is not None:
                self._remove_graph_refs(graph_store, rec.id)
        except Exception as exc:
            logger.warning(
                "GraphML cleanup failed for recording %d: %s", rec.id, exc
            )

    @staticmethod
    async def _delete_tag_current(
        session: AsyncSession, recording_id: int, tenant_id: str
    ) -> None:
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

    @staticmethod
    def _remove_graph_refs(graph_store: NetworkXGraphStore, recording_id: int) -> None:
        """Drop graph nodes/edges that mention this recording id.

        M6 strategy: iterate node attrs (``recording_ids`` JSON-serialized
        list) and remove nodes whose only reference is this recording.
        Edges are dropped alongside their endpoints by NetworkX.
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
        for node_id in to_remove:
            graph.remove_node(node_id)


__all__ = ["RetentionEnforcer", "RetentionReport"]
