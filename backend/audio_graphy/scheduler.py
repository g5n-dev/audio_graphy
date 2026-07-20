"""APScheduler pipeline worker — polls queued recordings and processes them.

Uses in-process ``BackgroundScheduler`` (C3 decision). The worker polls
the DB for ``status=queued`` recordings every ``pipeline_poll_seconds``
and runs the indexing pipeline with ``pipeline_concurrency`` limit.

See: docs/m3-architecture.md §1.1 (C3), §4.2, docs/m3-prd.md TAG-08.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.models.enums import RecordingStatus
from audio_graphy.models.recording import Recording

if TYPE_CHECKING:
    from audio_graphy.adapters.bundle import AdapterBundle
    from audio_graphy.storage.file_index import FileIndex
    from audio_graphy.storage.graph_networkx import NetworkXGraphStore
    from audio_graphy.storage.mysql_vector import MySQLVectorStore

logger = logging.getLogger(__name__)


class PipelineWorker:
    """Background pipeline worker that processes queued recordings.

    Args:
        session_factory: async session maker.
        bundle: AdapterBundle.
        vector_store: Global MySQLVectorStore.
        graph_stores: Dict of tenant_id -> NetworkXGraphStore.
        file_indexes: Dict of tenant_id -> FileIndex.
        poll_seconds: Polling interval.
        concurrency: Max concurrent pipeline executions.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        bundle: AdapterBundle,
        vector_store: MySQLVectorStore,
        graph_stores: dict[str, NetworkXGraphStore],
        file_indexes: dict[str, FileIndex],
        *,
        working_dir: str = "/data/working_dir",
        poll_seconds: int = 5,
        concurrency: int = 1,
    ) -> None:
        self._session_factory = session_factory
        self._bundle = bundle
        self._vector_store = vector_store
        self._graph_stores = graph_stores
        self._file_indexes = file_indexes
        self._working_dir = Path(working_dir)
        self._poll_seconds = poll_seconds
        self._concurrency = concurrency
        self._lock = asyncio.Lock()

    async def poll_once(self) -> int:
        """Process queued recordings once. Returns the number processed.

        Acquires a lock to enforce concurrency=1 (C5 decision).
        """
        async with self._lock:
            async with self._session_factory() as session:
                stmt = (
                    select(Recording)
                    .where(Recording.status == RecordingStatus.QUEUED.value)
                    .order_by(Recording.created_at)
                    .limit(self._concurrency)
                )
                result = await session.execute(stmt)
                recordings = list(result.scalars().all())

            if not recordings:
                return 0

            for recording in recordings:
                # Get per-tenant stores
                tenant_id = str(recording.tenant_id)
                graph_store = self._graph_stores.get(tenant_id)
                file_index = self._file_indexes.get(tenant_id)

                if graph_store is None:
                    from audio_graphy.storage.graph_networkx import NetworkXGraphStore

                    graph_store = NetworkXGraphStore(
                        self._working_dir,
                        tenant_id=tenant_id,
                    )
                    self._graph_stores[tenant_id] = graph_store

                if file_index is None:
                    from audio_graphy.storage.file_index import FileIndex

                    file_index = FileIndex(
                        self._working_dir,
                        tenant_id=tenant_id,
                    )
                    self._file_indexes[tenant_id] = file_index

                from audio_graphy.services.indexing import IndexingService

                svc = IndexingService(
                    self._session_factory,
                    self._bundle,
                    self._vector_store,
                    graph_store,
                    file_index,
                )
                try:
                    await svc.run_pipeline(recording)
                except Exception as exc:
                    logger.error(
                        "Pipeline failed for recording %d: %s", recording.id, exc, exc_info=True
                    )

            return len(recordings)

    async def start_loop(self) -> None:
        """Start the polling loop (runs forever)."""
        logger.info(
            "Pipeline worker started (poll=%ds, concurrency=%d)",
            self._poll_seconds,
            self._concurrency,
        )
        while True:
            try:
                processed = await self.poll_once()
                if processed > 0:
                    logger.info("Pipeline worker processed %d recordings", processed)
            except Exception as exc:
                logger.error("Pipeline worker poll error: %s", exc, exc_info=True)
            await asyncio.sleep(self._poll_seconds)


def create_scheduler(
    worker: PipelineWorker,
    *,
    poll_seconds: int = 5,
) -> BackgroundScheduler:
    """Create and configure an APScheduler BackgroundScheduler.

    The scheduler runs the pipeline worker's ``poll_once`` on a fixed interval.

    Args:
        worker: The PipelineWorker instance.
        poll_seconds: Polling interval in seconds.

    Returns:
        A configured (but not started) BackgroundScheduler.
    """
    scheduler = BackgroundScheduler()

    # We need to run async code from a sync scheduler callback.
    # We use a dedicated event loop for the worker.
    _worker_loop: asyncio.AbstractEventLoop | None = None

    def _run_poll() -> None:
        nonlocal _worker_loop
        if _worker_loop is None or _worker_loop.is_closed():
            _worker_loop = asyncio.new_event_loop()
        try:
            _worker_loop.run_until_complete(worker.poll_once())
        except Exception as exc:
            logger.error("Scheduler poll error: %s", exc, exc_info=True)

    scheduler.add_job(
        _run_poll,
        "interval",
        seconds=poll_seconds,
        id="pipeline_poll",
        replace_existing=True,
    )

    return scheduler
