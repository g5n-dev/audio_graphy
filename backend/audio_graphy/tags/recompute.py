"""RecomputeService — prompt version switch delta recomputation orchestrator.

When a prompt version is activated, this service:
    1. Finds all recordings tagged with older prompt versions.
    2. Re-tags each with the new prompt (with LLM cache).
    3. Diffs old vs new tag values.
    4. Only commits changed values (incremental delta).

See: docs/m3-architecture.md §3.2, §4.3, docs/m3-prd.md TAG-04/05.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.adapters.bundle import AdapterBundle
from audio_graphy.errors import TaskNotFoundError
from audio_graphy.models.reception import DialogueUnit, Reception
from audio_graphy.models.recompute_task import RecomputeTask
from audio_graphy.models.recording import Recording
from audio_graphy.models.tag_governance import (
    TagAssignmentCurrent,
    TagAssignmentFact,
    TaggerVersion,
)
from audio_graphy.services.legacy_tag_batch import (
    LegacyTagBatcher,
    LegacyTagBatchResult,
    load_recording_transcript,
)
from audio_graphy.services.legacy_tag_compatibility import (
    CanonicalLegacyTarget,
    LegacyTagCompatibilityService,
)
from audio_graphy.services.tag_extractor import TagExtractor
from audio_graphy.services.tag_governance import (
    GovernanceConflictError,
    TagGovernanceService,
    canonical_checksum,
)
from audio_graphy.services.tag_harness_runtime import (
    output_token_budget,
    resolve_harness_spec,
)
from audio_graphy.storage.file_index import FileIndex
from audio_graphy.tags.current_view import TagCurrentService

logger = logging.getLogger(__name__)


class PromptDryRunBudgetError(GovernanceConflictError):
    """A canonical Prompt dry-run cannot reserve its full Provider budget."""

    def __init__(
        self,
        *,
        estimated_provider_tokens: int,
        estimated_provider_calls: int,
        max_provider_tokens: int,
        max_provider_calls: int,
    ) -> None:
        self.estimated_provider_tokens = estimated_provider_tokens
        self.estimated_provider_calls = estimated_provider_calls
        self.max_provider_tokens = max_provider_tokens
        self.max_provider_calls = max_provider_calls
        super().__init__(
            "canonical prompt dry-run budget exhausted before Provider execution: "
            f"estimated_tokens={estimated_provider_tokens}, "
            f"max_provider_tokens={max_provider_tokens}, "
            f"estimated_calls={estimated_provider_calls}, "
            f"max_provider_calls={max_provider_calls}"
        )


def _stratum_part(value: object) -> str:
    return "" if value is None else str(value)


def stratified_dialogue_unit_sample(
    rows: Sequence[tuple[int, object, object, object]],
    *,
    limit: int,
) -> tuple[int, ...]:
    """Deterministically round-robin scenario/store/stage strata."""

    bounded_limit = min(100, max(0, int(limit)))
    if bounded_limit == 0:
        return ()
    groups: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for dialogue_unit_id, scenario, store_id, business_stage in rows:
        groups[
            (
                _stratum_part(scenario),
                _stratum_part(store_id),
                _stratum_part(business_stage),
            )
        ].append(int(dialogue_unit_id))
    queues = {
        key: deque(sorted(set(dialogue_unit_ids)))
        for key, dialogue_unit_ids in groups.items()
    }
    selected: list[int] = []
    while len(selected) < bounded_limit:
        emitted = False
        for key in sorted(queues):
            if not queues[key]:
                continue
            selected.append(queues[key].popleft())
            emitted = True
            if len(selected) >= bounded_limit:
                break
        if not emitted:
            break
    return tuple(selected)


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
        *,
        tag_extractor: TagExtractor | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._bundle = bundle
        self._file_index = file_index
        self._current_svc = TagCurrentService(session_factory)
        self._governance = TagGovernanceService(session_factory)
        self._tag_extractor = tag_extractor or TagExtractor(
            session_factory,
            weak_llm=bundle.weak_llm,
            strong_llm=bundle.strong_llm,
            enable_hybrid_rule_short_circuit=True,
        )

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
        *,
        prompt_content: str | None = None,
        baseline_prompt_content: str | None = None,
        sample_limit: int = 100,
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
        normalized_candidate = (
            prompt_content.replace("\r\n", "\n").strip()
            if prompt_content is not None
            else None
        )
        normalized_baseline = (
            baseline_prompt_content.replace("\r\n", "\n").strip()
            if baseline_prompt_content is not None
            else None
        )
        if (
            normalized_candidate is not None
            and normalized_baseline is not None
            and normalized_candidate == normalized_baseline
        ):
            return {
                "dry_run": True,
                "affected_count": len(recordings),
                "sampled_count": 0,
                "changed_count": 0,
                "unchanged_count": 0,
                "estimated_tokens": 0,
                "provider_calls": 0,
                "changes_preview": [],
            }

        bounded_sample_limit = min(100, max(1, int(sample_limit)))
        sampled_recordings = recordings[:bounded_sample_limit]

        changes: list[dict[str, Any]] = []
        changed_count = 0
        unchanged_count = 0
        estimated_tokens = 0
        provider_calls = 0

        for rec in sampled_recordings:
            batch = await self._compute_tag_batch(
                rec,
                effective_tag_paths,
                prompt_version,
                prompt_content=prompt_content,
            )
            estimated_tokens += int(batch.estimated_input_tokens)
            provider_calls += int(batch.provider_calls)
            for tag_path in effective_tag_paths:
                old_value = await self._get_current_tag_value(rec.id, tag_path, tenant_id)
                new_value = batch.values[tag_path]

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
            "sampled_count": len(sampled_recordings),
            "changed_count": changed_count,
            "unchanged_count": unchanged_count,
            "estimated_tokens": estimated_tokens,
            "provider_calls": provider_calls,
            "changes_preview": changes,
        }

    async def dry_run_prompt_candidate(
        self,
        *,
        tenant_id: str,
        prompt_id: int,
        prompt_version: str,
        prompt_content: str,
        resolved_target: CanonicalLegacyTarget,
        actor_user_id: int,
        sample_limit: int = 100,
        max_provider_tokens: int = 5_000_000,
        max_provider_calls: int = 400,
    ) -> dict[str, Any]:
        """Evaluate an immutable Prompt candidate through the serving Harness.

        The whole sample is materialized and conservatively reserved before the
        first Provider call.  Predictions are pure: this method never writes a
        TagAssignment fact/current projection and never qualifies a candidate.
        """

        if max_provider_tokens < 0 or max_provider_calls < 0:
            raise ValueError("Provider budgets must be non-negative")
        baseline = await self._load_tagger(
            tenant_id=tenant_id,
            tagger_version_id=resolved_target.tagger_version_id,
        )
        normalized_prompt = self._normalize_prompt_content(prompt_content)
        if normalized_prompt == self._normalize_prompt_content(baseline.prompt_content):
            return {
                "dry_run": True,
                "affected_count": len(resolved_target.dialogue_unit_ids),
                "sampled_count": 0,
                "changed_count": 0,
                "unchanged_count": 0,
                "estimated_tokens": 0,
                "estimated_provider_calls": 0,
                "provider_calls": 0,
                "provider_tokens": 0,
                "candidate_tagger_version_id": int(baseline.id),
                "quality_gate_status": "already_active_recipe",
                "changes_preview": [],
            }

        candidate = await self._materialize_prompt_candidate(
            tenant_id=tenant_id,
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            prompt_content=normalized_prompt,
            baseline=baseline,
            actor_user_id=actor_user_id,
        )
        sampled_ids = await self._stratified_dialogue_unit_ids(
            tenant_id=tenant_id,
            dialogue_unit_ids=resolved_target.dialogue_unit_ids,
            sample_limit=sample_limit,
        )
        estimate = await self._estimate_prompt_dry_run_budget(
            tenant_id=tenant_id,
            dialogue_unit_ids=sampled_ids,
            tag_keys=resolved_target.tag_keys,
            candidate=candidate,
        )
        estimated_provider_calls = int(estimate["provider_calls"])
        estimated_provider_tokens = int(estimate["provider_tokens"])
        if (
            estimated_provider_calls > max_provider_calls
            or estimated_provider_tokens > max_provider_tokens
        ):
            raise PromptDryRunBudgetError(
                estimated_provider_tokens=estimated_provider_tokens,
                estimated_provider_calls=estimated_provider_calls,
                max_provider_tokens=max_provider_tokens,
                max_provider_calls=max_provider_calls,
            )

        current_values = await self._current_canonical_values(
            tenant_id=tenant_id,
            dialogue_unit_ids=sampled_ids,
            tag_keys=resolved_target.tag_keys,
        )
        changed_count = 0
        unchanged_count = 0
        changes: list[dict[str, Any]] = []
        provider_calls = 0
        provider_tokens = 0
        for dialogue_unit_id in sampled_ids:
            prediction = await self._tag_extractor.predict_dialogue_unit(
                tenant_id=tenant_id,
                dialogue_unit_id=dialogue_unit_id,
                tagger_version_id=int(candidate.id),
                target_tag_keys=resolved_target.tag_keys,
            )
            provider_calls += int(prediction.provider_calls)
            provider_tokens += int(prediction.provider_input_tokens)
            provider_tokens += int(prediction.provider_output_tokens)
            by_key = {
                str(assignment["tag_key"]): assignment.get("tag_value")
                for assignment in prediction.assignments
            }
            for tag_key in resolved_target.tag_keys:
                old_value = current_values.get((dialogue_unit_id, tag_key))
                new_value = by_key.get(tag_key)
                if old_value == new_value:
                    unchanged_count += 1
                    continue
                changed_count += 1
                if len(changes) < 100:
                    changes.append(
                        {
                            "subject_type": "dialogue_unit",
                            "subject_id": dialogue_unit_id,
                            "tag_key": tag_key,
                            "old_value": old_value,
                            "new_value": new_value,
                        }
                    )

        return {
            "dry_run": True,
            "affected_count": len(resolved_target.dialogue_unit_ids),
            "sampled_count": len(sampled_ids),
            "changed_count": changed_count,
            "unchanged_count": unchanged_count,
            "estimated_tokens": estimated_provider_tokens,
            "estimated_provider_calls": estimated_provider_calls,
            "provider_calls": provider_calls,
            "provider_tokens": provider_tokens,
            "candidate_tagger_version_id": int(candidate.id),
            "quality_gate_status": "requires_evaluation",
            "changes_preview": changes,
        }

    async def execute_task(self, task_id: str) -> None:
        """Retire a legacy synchronous task without writing legacy tag tables."""

        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                select(RecomputeTask).where(RecomputeTask.task_id == task_id).with_for_update()
            )
            task = result.scalar_one_or_none()
            if task is None:
                raise TaskNotFoundError(detail={"task_id": task_id})
            task.status = "failed"
            task.error_message = (
                "legacy synchronous recompute is read-only; create a canonical "
                "/tag-jobs recompute request"
            )
            task.finished_at = datetime.now(UTC)

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

        await LegacyTagCompatibilityService(self._session_factory).enqueue_recordings(
            tenant_id=tenant_id,
            recording_ids=[recording_id],
            legacy_paths=effective_tag_paths,
            actor_user_id=0,
            operation="legacy_streaming_recompute",
            idempotency_key=(
                f"legacy-streaming-{tenant_id}-{recording_id}-"
                f"{prompt_version}-{','.join(map(str, sorted(segment_ids)))}"
            )[:128],
        )

        return SegmentTagBatchResult(
            tenant_id=tenant_id,
            recording_id=recording_id,
            segment_ids=list(segment_ids),
            tags_written=0,
            skipped_existing=0,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_prompt_content(value: str) -> str:
        return value.replace("\r\n", "\n").replace("\r", "\n").strip()

    async def _load_tagger(
        self,
        *,
        tenant_id: str,
        tagger_version_id: int,
    ) -> TaggerVersion:
        async with self._session_factory() as session:
            tagger = (
                await session.execute(
                    select(TaggerVersion).where(
                        TaggerVersion.id == tagger_version_id,
                        TaggerVersion.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
        if tagger is None:
            raise GovernanceConflictError("canonical baseline TaggerVersion does not exist")
        return tagger

    async def _materialize_prompt_candidate(
        self,
        *,
        tenant_id: str,
        prompt_id: int,
        prompt_version: str,
        prompt_content: str,
        baseline: TaggerVersion,
        actor_user_id: int,
    ) -> TaggerVersion:
        """Clone serving config and replace only its versioned Prompt payload."""

        del prompt_id, prompt_version
        if baseline.engine not in {"llm", "hybrid"}:
            raise GovernanceConflictError(
                "the canonical baseline does not consume a Prompt; "
                "legacy Prompt activation cannot be evaluated safely"
            )
        harness_spec = resolve_harness_spec(baseline)
        harness_spec["generation"]["prompt_template"] = prompt_content
        harness_spec["spec_version"] = "2.0"
        rule_bundle = deepcopy(baseline.rule_bundle or {})
        thresholds = {
            str(key): float(value)
            for key, value in (baseline.thresholds or {}).items()
        }
        change_summary = "Canonical Prompt activation candidate"
        config = {
            "schema_version_id": int(baseline.schema_version_id),
            "engine": str(baseline.engine),
            "prompt_content": prompt_content,
            "rule_bundle": rule_bundle,
            "model_version": str(baseline.model_version),
            "thresholds": thresholds,
            "harness_spec_version": "2.0",
            "harness_spec": harness_spec,
            "parent_version_id": int(baseline.id),
            "origin": "manual",
            "optimization_run_id": None,
            "change_summary": change_summary,
        }
        checksum = canonical_checksum(config)
        async with self._session_factory() as session:
            existing = (
                await session.execute(
                    select(TaggerVersion).where(
                        TaggerVersion.tenant_id == tenant_id,
                        TaggerVersion.config_checksum == checksum,
                    )
                )
            ).scalar_one_or_none()
        if existing is not None:
            return existing

        version = f"prompt-{int(baseline.id)}-{checksum[:16]}"
        try:
            return await self._governance.create_tagger_version(
                tenant_id=tenant_id,
                schema_version_id=int(baseline.schema_version_id),
                version=version,
                engine=str(baseline.engine),
                prompt_content=prompt_content,
                rule_bundle=rule_bundle,
                model_version=str(baseline.model_version),
                thresholds=thresholds,
                created_by=actor_user_id,
                harness_spec=harness_spec,
                parent_version_id=int(baseline.id),
                origin="manual",
                change_summary=change_summary,
            )
        except GovernanceConflictError:
            # Concurrent dry-runs converge on the same content-addressed recipe.
            async with self._session_factory() as session:
                raced = (
                    await session.execute(
                        select(TaggerVersion).where(
                            TaggerVersion.tenant_id == tenant_id,
                            TaggerVersion.config_checksum == checksum,
                        )
                    )
                ).scalar_one_or_none()
            if raced is None:
                raise
            return raced

    async def _stratified_dialogue_unit_ids(
        self,
        *,
        tenant_id: str,
        dialogue_unit_ids: Sequence[int],
        sample_limit: int,
    ) -> tuple[int, ...]:
        if not dialogue_unit_ids:
            return ()
        bounded_limit = min(100, max(0, int(sample_limit)))
        if bounded_limit == 0:
            return ()
        unique_ids = tuple(sorted({int(value) for value in dialogue_unit_ids}))
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        DialogueUnit.id,
                        Reception.scenario,
                        Reception.store_id,
                        DialogueUnit.business_stage,
                    )
                    .join(
                        Reception,
                        (Reception.id == DialogueUnit.reception_id)
                        & (Reception.tenant_id == DialogueUnit.tenant_id),
                    )
                    .where(
                        DialogueUnit.tenant_id == tenant_id,
                        DialogueUnit.id.in_(unique_ids),
                    )
                )
            ).all()
        found_ids = {int(row[0]) for row in rows}
        missing_ids = sorted(set(unique_ids) - found_ids)
        if missing_ids:
            raise GovernanceConflictError(
                f"canonical Prompt scope contains missing dialogue units: {missing_ids[:10]}"
            )
        return stratified_dialogue_unit_sample(
            [
                (int(row[0]), row[1], row[2], row[3])
                for row in rows
            ],
            limit=bounded_limit,
        )

    @staticmethod
    def _adapter_epoch(adapter: object) -> str:
        return str(
            getattr(
                adapter,
                "model_epoch",
                getattr(adapter, "model", ""),
            )
            or ""
        )

    async def _estimate_prompt_dry_run_budget(
        self,
        *,
        tenant_id: str,
        dialogue_unit_ids: Sequence[int],
        tag_keys: Sequence[str],
        candidate: TaggerVersion,
    ) -> dict[str, int]:
        """Reserve the serving runtime's worst-case batches, including one repair."""

        harness_spec = resolve_harness_spec(candidate)
        route = str(harness_spec["orchestration"]["route"])
        if route == "rule_only" or not tag_keys:
            return {"provider_calls": 0, "provider_tokens": 0}

        generation = harness_spec["generation"]
        max_input_tokens = int(generation["max_input_tokens"])
        configured_cap = int(generation["max_tokens"])
        total_base_calls = 0
        total_base_tokens = 0
        same_model_epoch = (
            self._adapter_epoch(self._bundle.weak_llm)
            == self._adapter_epoch(self._bundle.strong_llm)
        )
        for dialogue_unit_id in dialogue_unit_ids:
            # This is the exact production input materializer and performs no
            # Provider I/O.  Keeping this preflight on the same code path avoids
            # dry-run/serving token-estimate skew.
            prepared = await self._tag_extractor._prepare_dialogue_unit(
                tenant_id=tenant_id,
                dialogue_unit_id=int(dialogue_unit_id),
                tagger_version_id=int(candidate.id),
                target_tag_keys=tag_keys,
            )
            definitions = prepared.definitions
            if not definitions:
                continue
            weak_batches = self._tag_extractor._segment_batches_for_input_budget(
                segment_texts=prepared.segment_texts,
                definitions=definitions,
                prompt_content=str(generation["prompt_template"]),
                max_input_tokens=max_input_tokens,
            )
            weak_output_tokens = output_token_budget(
                len(definitions),
                configured_cap=configured_cap,
            )
            total_base_calls += len(weak_batches)
            total_base_tokens += len(weak_batches) * (
                max_input_tokens + weak_output_tokens
            )

            if route != "weak_then_strong_critic" or same_model_epoch:
                continue
            evidence_refs = [
                {"segment_id": int(segment.id)}
                for segment, _text in prepared.segment_texts
            ]
            weak_candidates = {
                key: {
                    "tag_key": key,
                    "tag_value": self._budget_placeholder_value(definition),
                    "confidence": 0.5,
                    "evidence_refs": evidence_refs,
                }
                for key, definition in definitions.items()
            }
            critic_batches = self._tag_extractor._segment_batches_for_input_budget(
                segment_texts=prepared.segment_texts,
                definitions=definitions,
                prompt_content=(
                    str(generation["prompt_template"])
                    + "\nReview only the supplied weak candidates and cited evidence."
                ),
                max_input_tokens=max_input_tokens,
                weak_candidates=weak_candidates,
            )
            critic_output_tokens = output_token_budget(
                len(definitions),
                configured_cap=configured_cap,
            )
            total_base_calls += len(critic_batches)
            total_base_tokens += len(critic_batches) * (
                max_input_tokens + critic_output_tokens
            )

        # Structured format repair is allowed once per generation.  Reserve it
        # up front so an over-budget request cannot begin and fail mid-sample.
        return {
            "provider_calls": total_base_calls * 2,
            "provider_tokens": total_base_tokens * 2,
        }

    @staticmethod
    def _budget_placeholder_value(definition: dict[str, Any]) -> Any:
        value_type = str(definition.get("value_type", "string"))
        if value_type == "enum":
            allowed = list(definition.get("allowed_values") or [""])
            return max(allowed, key=lambda value: len(str(value)))
        if value_type == "boolean":
            return True
        if value_type == "number":
            return 0
        return "x" * 64

    async def _current_canonical_values(
        self,
        *,
        tenant_id: str,
        dialogue_unit_ids: Sequence[int],
        tag_keys: Sequence[str],
    ) -> dict[tuple[int, str], Any]:
        if not dialogue_unit_ids or not tag_keys:
            return {}
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        TagAssignmentCurrent.subject_id,
                        TagAssignmentCurrent.tag_key,
                        TagAssignmentFact.tag_value,
                    )
                    .join(
                        TagAssignmentFact,
                        (TagAssignmentFact.id == TagAssignmentCurrent.fact_id)
                        & (
                            TagAssignmentFact.tenant_id
                            == TagAssignmentCurrent.tenant_id
                        ),
                    )
                    .where(
                        TagAssignmentCurrent.tenant_id == tenant_id,
                        TagAssignmentCurrent.subject_type == "dialogue_unit",
                        TagAssignmentCurrent.subject_id.in_(dialogue_unit_ids),
                        TagAssignmentCurrent.tag_key.in_(tag_keys),
                    )
                )
            ).all()
        return {
            (int(subject_id), str(tag_key)): tag_value
            for subject_id, tag_key, tag_value in rows
        }

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
            result = await session.execute(stmt.order_by(Recording.id))
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

    async def _compute_tag_batch(
        self,
        recording: Recording,
        tag_paths: list[str],
        prompt_version: str,
        *,
        prompt_content: str | None = None,
    ) -> LegacyTagBatchResult:
        tenant_id = str(recording.tenant_id)
        transcript = await load_recording_transcript(
            self._session_factory,
            tenant_id=tenant_id,
            recording_id=recording.id,
        )
        return await LegacyTagBatcher(self._bundle.weak_llm).classify(
            tenant_id=tenant_id,
            recording_id=recording.id,
            transcript=transcript,
            tag_paths=tag_paths,
            prompt_version=prompt_version,
            prompt_content=prompt_content,
        )

    async def _compute_tag_value_with_cache(
        self,
        recording: Recording,
        tag_path: str,
        prompt_version: str,
    ) -> tuple[str, bool]:
        """Compatibility helper backed by the unified one-call batch service.

        Returns:
            Tuple of (tag_value, cached_hit).
        """
        batch = await self._compute_tag_batch(recording, [tag_path], prompt_version)
        return batch.values[tag_path], batch.cached
