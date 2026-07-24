"""RecomputeService — prompt version switch delta recomputation orchestrator.

When a prompt version is activated, this service:
    1. Finds all recordings tagged with older prompt versions.
    2. Re-tags each with the new prompt (with LLM cache).
    3. Diffs old vs new tag values.
    4. Only commits changed values (incremental delta).

See: docs/m3-architecture.md §3.2, §4.3, docs/m3-prd.md TAG-04/05.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.adapters.bundle import AdapterBundle
from audio_graphy.errors import TaskNotFoundError
from audio_graphy.models.recompute_task import RecomputeTask
from audio_graphy.models.recording import Recording
from audio_graphy.storage.file_index import FileIndex
from audio_graphy.tags.current_view import TagCurrentService
from audio_graphy.tags.facts import TagFactsService
from audio_graphy.tags.stats import TagStatsService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SegmentTagBatchResult:
    """Result of one streaming segment-scoped tag recompute (M8 WS-3 / T9).

    Attributes:
        tenant_id: Tenant scope.
        recording_id: Recording the segments belong to.
        segment_ids: Segment ids included in the batch.
        tags_written: Number of tag facts appended.
        skipped_existing: Tag values identical to current — no write.
    """

    tenant_id: str
    recording_id: int
    segment_ids: list[int] = field(default_factory=list)
    tags_written: int = 0
    skipped_existing: int = 0


class RecomputeService:
    """Prompt version switch recomputation orchestrator.

    Args:
        session_factory: async session maker.
        bundle: AdapterBundle (uses weak_llm for tagging).
        file_index: Per-tenant FileIndex (LLM cache).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        bundle: AdapterBundle,
        file_index: FileIndex,
    ) -> None:
        self._session_factory = session_factory
        self._bundle = bundle
        self._file_index = file_index
        self._facts_svc = TagFactsService(session_factory)
        self._current_svc = TagCurrentService(session_factory)
        self._stats_svc = TagStatsService(session_factory)

    async def create_task(
        self,
        tenant_id: str,
        prompt_version: str,
        tag_paths: list[str] | None,
        recording_ids: list[int] | None,
    ) -> RecomputeTask:
        """Create a recompute task for tracking progress.

        Args:
            tenant_id: Tenant scope.
            prompt_version: Target prompt version.
            tag_paths: Optional tag path filter.
            recording_ids: Optional recording ID filter.

        Returns:
            The created RecomputeTask ORM object.
        """
        # Count affected recordings
        affected = await self._count_affected(tenant_id, prompt_version, recording_ids)

        task_id = f"recompute-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"

        task = RecomputeTask(
            tenant_id=tenant_id,
            task_id=task_id,
            prompt_version=prompt_version,
            status="pending",
            total=affected,
        )
        async with self._session_factory() as session:
            session.add(task)
            await session.commit()
            await session.refresh(task)

        return task

    async def dry_run(
        self,
        tenant_id: str,
        prompt_version: str,
        tag_paths: list[str] | None,
        recording_ids: list[int] | None,
    ) -> dict[str, Any]:
        """Execute a dry run: compute diffs without writing.

        Args:
            tenant_id: Tenant scope.
            prompt_version: Target prompt version.
            tag_paths: Optional tag path filter.
            recording_ids: Optional recording ID filter.

        Returns:
            Dict with affected_count, changed_count, unchanged_count, changes_preview.
        """
        recordings = await self._get_affected_recordings(tenant_id, prompt_version, recording_ids)
        effective_tag_paths = tag_paths or [
            "quality.greeting",
            "quality.closing",
            "sales.product_mention",
        ]

        changes: list[dict[str, Any]] = []
        changed_count = 0
        unchanged_count = 0

        for rec in recordings:
            for tag_path in effective_tag_paths:
                old_value = await self._get_current_tag_value(rec.id, tag_path, tenant_id)
                new_value = await self._compute_tag_value(rec, tag_path, prompt_version)

                if old_value != new_value:
                    changed_count += 1
                    changes.append(
                        {
                            "recording_id": rec.id,
                            "tag_path": tag_path,
                            "old_value": old_value,
                            "new_value": new_value,
                        }
                    )
                else:
                    unchanged_count += 1

        return {
            "dry_run": True,
            "affected_count": len(recordings),
            "changed_count": changed_count,
            "unchanged_count": unchanged_count,
            "changes_preview": changes,
        }

    async def execute_task(self, task_id: str) -> None:
        """Execute a recompute task (called by scheduler or inline).

        Args:
            task_id: The task ID to execute.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(RecomputeTask).where(RecomputeTask.task_id == task_id)
            )
            task = result.scalar_one_or_none()
            if task is None:
                raise TaskNotFoundError(detail={"task_id": task_id})

            # Set running
            task.status = "running"
            task.started_at = datetime.now(UTC)
            await session.commit()

        try:
            tid = str(task.tenant_id)
            recordings = await self._get_affected_recordings(tid, str(task.prompt_version), None)

            tag_paths = ["quality.greeting", "quality.closing", "sales.product_mention"]

            cached_hits = 0
            llm_calls = 0
            changed = 0

            for rec in recordings:
                for tag_path in tag_paths:
                    old_value = await self._get_current_tag_value(rec.id, tag_path, tid)
                    new_value, cached = await self._compute_tag_value_with_cache(
                        rec, tag_path, str(task.prompt_version)
                    )

                    if cached:
                        cached_hits += 1
                    else:
                        llm_calls += 1

                    if old_value != new_value:
                        changed += 1
                        # Write tag facts + current + stats delta
                        await self._facts_svc.get_next_version(rec.id, tag_path, tid)
                        input_hash = hashlib.md5(
                            f"{tag_path}:{rec.id}:{task.prompt_version}".encode()
                        ).hexdigest()

                        fact = await self._facts_svc.append_fact(
                            recording_id=rec.id,
                            tag_path=tag_path,
                            tag_value=new_value,
                            prompt_version=str(task.prompt_version),
                            model_version=self._bundle.weak_llm.model,
                            input_hash=input_hash,
                            confidence=0.95,
                            source="llm",
                            computed_by=None,
                            tenant_id=tid,
                        )
                        await self._current_svc.upsert_current(fact, tid)
                        await self._stats_svc.apply_delta(
                            tenant_id=tid,
                            store_id=str(rec.store_id),
                            agent_name=str(rec.agent_name),
                            tag_path=tag_path,
                            old_value=old_value,
                            new_value=new_value,
                        )

                # Update progress
                async with self._session_factory() as session:
                    result = await session.execute(
                        select(RecomputeTask).where(RecomputeTask.task_id == task_id)
                    )
                    t = result.scalar_one()
                    t.processed += 1
                    t.changed = changed
                    t.cached_hits = cached_hits
                    t.llm_calls = llm_calls
                    await session.commit()

            # Mark done
            async with self._session_factory() as session:
                result = await session.execute(
                    select(RecomputeTask).where(RecomputeTask.task_id == task_id)
                )
                t = result.scalar_one()
                t.status = "done"
                t.finished_at = datetime.now(UTC)
                await session.commit()

        except Exception as exc:
            logger.error("Recompute task %s failed: %s", task_id, exc, exc_info=True)
            async with self._session_factory() as session:
                result = await session.execute(
                    select(RecomputeTask).where(RecomputeTask.task_id == task_id)
                )
                t = result.scalar_one()
                t.status = "failed"
                t.error_message = str(exc)
                t.finished_at = datetime.now(UTC)
                await session.commit()

    async def get_task_status(
        self,
        task_id: str,
        tenant_id: str,
    ) -> RecomputeTask:
        """Get a recompute task by ID (tenant-scoped).

        Args:
            task_id: Task ID string.
            tenant_id: Tenant scope.

        Returns:
            RecomputeTask ORM object.

        Raises:
            TaskNotFoundError: If not found or cross-tenant.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(RecomputeTask).where(
                    RecomputeTask.task_id == task_id,
                    RecomputeTask.tenant_id == tenant_id,
                )
            )
            task = result.scalar_one_or_none()
            if task is None:
                raise TaskNotFoundError(detail={"task_id": task_id})
            return task

    # ------------------------------------------------------------------
    # M8 WS-3 / T9 — streaming segment-scoped recompute entry point
    # ------------------------------------------------------------------

    async def recompute_tags_for_segments(
        self,
        tenant_id: str,
        recording_id: int,
        segment_ids: list[int],
        tag_paths: list[str] | None = None,
        prompt_version: str = "streaming/v1",
    ) -> SegmentTagBatchResult:
        """Recompute tags for a batch of streaming confirmed segments.

        M8 Phase 4 (WS-3 / T9) — invoked by ``StreamingTagScheduler`` every
        N confirmed segments. Writes go into the SAME ``tag_facts`` table as
        the batch path (M3 three-layer model), scoped by the recording id.

        The provided ``segment_ids`` are used as the LLM input scope: their
        transcripts are concatenated and tagged as one batch.

        Args:
            tenant_id: Tenant scope.
            recording_id: Recording the segments belong to.
            segment_ids: Segment DB ids confirmed since the last batch.
            tag_paths: Tag paths to compute (default: the three P0 paths).
            prompt_version: Provenance marker (default ``"streaming/v1"``).

        Returns:
            SegmentTagBatchResult with write counts.
        """
        effective_tag_paths = tag_paths or [
            "quality.greeting",
            "quality.closing",
            "sales.product_mention",
        ]

        # Load the recording (tenant-scoped).
        async with self._session_factory() as session:
            result = await session.execute(
                select(Recording).where(
                    Recording.id == recording_id,
                    Recording.tenant_id == tenant_id,
                )
            )
            recording = result.scalar_one_or_none()
        if recording is None:
            raise TaskNotFoundError(detail={"recording_id": recording_id, "tenant_id": tenant_id})

        # Load segment transcripts (tenant-scoped, ordered by idx).
        from audio_graphy.models.segment import Segment

        stmt = (
            select(Segment)
            .where(
                Segment.recording_id == recording_id,
                Segment.tenant_id == tenant_id,
                Segment.id.in_(segment_ids),
            )
            .order_by(Segment.idx)
        )
        async with self._session_factory() as session:
            seg_result = await session.execute(stmt)
            segments = list(seg_result.scalars().all())

        transcripts = "\n".join(s.transcript for s in segments if s.transcript)

        tags_written = 0
        skipped_existing = 0
        for tag_path in effective_tag_paths:
            old_value = await self._get_current_tag_value(recording_id, tag_path, tenant_id)
            new_value = await self._compute_segment_tag_value(
                recording_id=recording_id,
                tag_path=tag_path,
                transcripts=transcripts,
                prompt_version=prompt_version,
                segment_ids=segment_ids,
            )
            if old_value == new_value:
                skipped_existing += 1
                continue

            input_hash = hashlib.md5(
                f"{tag_path}:{recording_id}:streaming:{','.join(map(str, segment_ids))}".encode()
            ).hexdigest()
            fact = await self._facts_svc.append_fact(
                recording_id=recording_id,
                tag_path=tag_path,
                tag_value=new_value,
                prompt_version=prompt_version,
                model_version=self._bundle.weak_llm.model,
                input_hash=input_hash,
                confidence=0.95,
                source="llm",
                computed_by=None,
                tenant_id=tenant_id,
            )
            await self._current_svc.upsert_current(fact, tenant_id)
            await self._stats_svc.apply_delta(
                tenant_id=tenant_id,
                store_id=str(recording.store_id),
                agent_name=str(recording.agent_name),
                tag_path=tag_path,
                old_value=old_value,
                new_value=new_value,
            )
            tags_written += 1

        return SegmentTagBatchResult(
            tenant_id=tenant_id,
            recording_id=recording_id,
            segment_ids=list(segment_ids),
            tags_written=tags_written,
            skipped_existing=skipped_existing,
        )

    async def _compute_segment_tag_value(
        self,
        recording_id: int,
        tag_path: str,
        transcripts: str,
        prompt_version: str,
        segment_ids: list[int],
    ) -> str:
        """Compute one tag value over streaming segment transcripts (cached)."""
        cache_key = hashlib.md5(
            f"streaming:{tag_path}:{recording_id}:{prompt_version}:{','.join(map(str, segment_ids))}".encode()
        ).hexdigest()

        cached = await self._file_index.get_llm_cache(cache_key)
        if cached is not None:
            return cached

        messages: list[dict[str, str]] = [
            {
                "role": "user",
                "content": (
                    "请对以下流式转写片段进行质检打标。\n"
                    f"标签路径: {tag_path}\n"
                    f"录音ID: {recording_id}\n"
                    f"转写片段:\n{transcripts[:4000]}\n"
                    "请返回 pass 或 fail。"
                ),
            }
        ]
        response = await self._bundle.weak_llm.complete(
            messages=messages,
            cache_key=cache_key,
        )
        tag_value = response.text.strip().split("\n")[0][:255]
        await self._file_index.set_llm_cache(cache_key, tag_value)
        return tag_value

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _count_affected(
        self,
        tenant_id: str,
        prompt_version: str,
        recording_ids: list[int] | None,
    ) -> int:
        """Count recordings that would be affected by recompute."""
        recordings = await self._get_affected_recordings(tenant_id, prompt_version, recording_ids)
        return len(recordings)

    async def _get_affected_recordings(
        self,
        tenant_id: str,
        prompt_version: str,
        recording_ids: list[int] | None,
    ) -> list[Recording]:
        """Get recordings tagged with older prompt versions."""
        async with self._session_factory() as session:
            stmt = select(Recording).where(Recording.tenant_id == tenant_id)
            # Find recordings with prompt_version < target (or None)
            stmt = stmt.where(
                (Recording.prompt_version != prompt_version) | (Recording.prompt_version.is_(None))
            )
            if recording_ids is not None:
                stmt = stmt.where(Recording.id.in_(recording_ids))
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def _get_current_tag_value(
        self,
        recording_id: int,
        tag_path: str,
        tenant_id: str,
    ) -> str | None:
        """Get the current tag value for a recording + path."""
        current = await self._current_svc.get_current_value(recording_id, tag_path, tenant_id)
        return current.tag_value if current is not None else None

    async def _compute_tag_value(
        self,
        recording: Recording,
        tag_path: str,
        prompt_version: str,
    ) -> str:
        """Compute a tag value via LLM (no cache write for dry_run)."""
        value, _ = await self._compute_tag_value_with_cache(recording, tag_path, prompt_version)
        return value

    async def _compute_tag_value_with_cache(
        self,
        recording: Recording,
        tag_path: str,
        prompt_version: str,
    ) -> tuple[str, bool]:
        """Compute a tag value via LLM with cache.

        Returns:
            Tuple of (tag_value, cached_hit).
        """
        cache_key = hashlib.md5(f"{tag_path}:{recording.id}:{prompt_version}".encode()).hexdigest()

        # Check LLM cache
        cached = await self._file_index.get_llm_cache(cache_key)
        if cached is not None:
            return cached, True

        # Call LLM
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
        response = await self._bundle.weak_llm.complete(
            messages=messages,
            cache_key=cache_key,
        )
        tag_value = response.text.strip().split("\n")[0][:255]

        # Store in cache
        await self._file_index.set_llm_cache(cache_key, tag_value)

        return tag_value, False
