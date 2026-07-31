"""Standalone MySQL-leased tag worker.

Run with ``python -m audio_graphy.tag_worker``.  It does not import or start
FastAPI and can therefore scale independently from request-serving processes.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import signal
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.models.reception import DialogueUnit
from audio_graphy.models.tag_governance import (
    TagDeployment,
    TagEvaluationRun,
    TagExtractionJob,
    TaggerVersion,
)
from audio_graphy.schemas.reception_pipeline import ReceptionAutomationRequest
from audio_graphy.services.reception_pipeline import ReceptionAutomationPipeline
from audio_graphy.services.tag_deployment_monitor import TagDeploymentMonitor
from audio_graphy.services.tag_evaluator import TagEvaluationService
from audio_graphy.services.tag_extractor import (
    ExtractionResult,
    TagExtractor,
    TagExtractorHarnessTrialExecutor,
)
from audio_graphy.services.tag_governance import (
    GovernanceConflictError,
    GovernanceError,
    TagGovernanceService,
    TagJobBudgetExhaustedError,
    TagJobBudgetReservation,
    resolve_serving_tagger_route,
    stable_canary_bucket,
)

logger = logging.getLogger(__name__)
EvaluationProcessor = Callable[[TagExtractionJob], Awaitable[None]]
OptimizationProcessor = Callable[[TagExtractionJob], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _ProviderUsage:
    tokens: int = 0
    calls: int = 0
    cost_microunits: int = 0

    def add(self, other: _ProviderUsage) -> _ProviderUsage:
        return _ProviderUsage(
            tokens=self.tokens + other.tokens,
            calls=self.calls + other.calls,
            cost_microunits=self.cost_microunits + other.cost_microunits,
        )


class DeploymentMonitorRunner(Protocol):
    async def run_forever(
        self,
        *,
        stop: asyncio.Event,
        poll_seconds: float,
    ) -> None: ...


def _deployment_route_decision(
    *,
    status: str,
    bucket: int,
    traffic_percent: int,
    shadow_sample_percent: int,
    shadow_sampling_complete: bool = False,
) -> tuple[bool, bool]:
    """Return ``(execute, publish_current)`` for one deterministic subject bucket."""

    if status == "shadow":
        if shadow_sampling_complete:
            return False, False
        return bucket < shadow_sample_percent, False
    if status == "canary_5":
        selected = bucket < 5
        return selected, selected
    if status == "canary_25":
        selected = bucket < 25
        return selected, selected
    if status == "awaiting_admin":
        selected = bucket < traffic_percent
        return selected, selected
    if status == "production":
        return True, True
    return False, False


class TagJobWorker:
    """Claim and execute durable TagJobs with lease heartbeat and truthful progress."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        worker_id: str,
        actor_user_id: int = 0,
        extractor: TagExtractor | None = None,
        reception_pipeline: ReceptionAutomationPipeline | None = None,
        evaluation_processor: EvaluationProcessor | None = None,
        optimization_processor: OptimizationProcessor | None = None,
        deployment_monitor: DeploymentMonitorRunner | None = None,
        lease_ttl: timedelta = timedelta(minutes=15),
        poll_seconds: float = 2.0,
        monitor_poll_seconds: float = 30.0,
        optimization_check_seconds: float = 60 * 60,
        shadow_sample_percent: int = 10,
    ) -> None:
        if lease_ttl.total_seconds() <= 3:
            raise ValueError("lease_ttl must be longer than three seconds")
        if monitor_poll_seconds <= 0:
            raise ValueError("monitor_poll_seconds must be positive")
        if optimization_check_seconds <= 0:
            raise ValueError("optimization_check_seconds must be positive")
        if not 0 <= shadow_sample_percent <= 100:
            raise ValueError("shadow_sample_percent must be between 0 and 100")
        self._factory = session_factory
        self._worker_id = worker_id
        self._actor_user_id = actor_user_id
        self._extractor = extractor or TagExtractor(session_factory)
        self._service = TagGovernanceService(
            session_factory,
            optimization_trial_executor=TagExtractorHarnessTrialExecutor(
                self._extractor,
            ),
        )
        self._reception_pipeline = reception_pipeline
        self._evaluation_processor = evaluation_processor
        self._optimization_processor = optimization_processor
        self._evaluation_service = TagEvaluationService(
            session_factory,
            predictor=self._extractor,
        )
        self._deployment_monitor = deployment_monitor or TagDeploymentMonitor(
            session_factory,
            actor_user_id=actor_user_id,
        )
        self._lease_ttl = lease_ttl
        self._poll_seconds = max(0.1, poll_seconds)
        self._monitor_poll_seconds = monitor_poll_seconds
        self._optimization_check_seconds = optimization_check_seconds
        self._shadow_sample_percent = shadow_sample_percent
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def _resolve_route(self, job: TagExtractionJob) -> tuple[int, int | None]:
        scoped_deployment = job.scope.get("deployment_id")
        async with self._factory() as session:
            if scoped_deployment is not None:
                deployment = (
                    await session.execute(
                        select(TagDeployment).where(
                            TagDeployment.id == int(scoped_deployment),
                            TagDeployment.tenant_id == job.tenant_id,
                        )
                    )
                ).scalar_one_or_none()
                if deployment is None:
                    raise GovernanceError("scoped deployment does not exist")
                if (
                    job.tagger_version_id is not None
                    and deployment.tagger_version_id != job.tagger_version_id
                ):
                    raise GovernanceError("job tagger does not match scoped deployment")
                return int(deployment.tagger_version_id), int(deployment.id)
            if job.tagger_version_id is not None:
                production_deployment = (
                    await session.execute(
                        select(TagDeployment.id)
                        .where(
                            TagDeployment.tenant_id == job.tenant_id,
                            TagDeployment.tagger_version_id == job.tagger_version_id,
                            TagDeployment.status == "production",
                        )
                        .order_by(TagDeployment.approved_at.desc(), TagDeployment.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                return (
                    int(job.tagger_version_id),
                    int(production_deployment) if production_deployment is not None else None,
                )
            tagger_version_id, deployment_id = await resolve_serving_tagger_route(
                session,
                tenant_id=str(job.tenant_id),
            )
            if tagger_version_id is None:
                raise GovernanceError("no production or qualified tagger is available")
            return tagger_version_id, deployment_id

    async def _route_decision(
        self,
        *,
        tenant_id: str,
        dialogue_unit_id: int,
        deployment_id: int | None,
    ) -> tuple[bool, bool]:
        if deployment_id is None:
            return True, True
        async with self._factory() as session:
            deployment = (
                await session.execute(
                    select(TagDeployment).where(
                        TagDeployment.id == deployment_id,
                        TagDeployment.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            unit = (
                await session.execute(
                    select(DialogueUnit).where(
                        DialogueUnit.id == dialogue_unit_id,
                        DialogueUnit.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if deployment is None or unit is None:
                raise GovernanceError("deployment route subject does not exist")
            bucket = stable_canary_bucket(tenant_id, unit.reception_id, deployment.id)
            return _deployment_route_decision(
                status=str(deployment.status),
                bucket=bucket,
                traffic_percent=int(deployment.traffic_percent),
                shadow_sample_percent=self._shadow_sample_percent,
                shadow_sampling_complete=deployment.sampling_complete_at is not None,
            )

    async def _route_current(
        self,
        *,
        tenant_id: str,
        dialogue_unit_id: int,
        deployment_id: int | None,
    ) -> bool:
        """Compatibility wrapper for callers that only need publication state."""

        _execute, publish_current = await self._route_decision(
            tenant_id=tenant_id,
            dialogue_unit_id=dialogue_unit_id,
            deployment_id=deployment_id,
        )
        return publish_current

    async def _active_candidate_routes(
        self,
        *,
        tenant_id: str,
        production_deployment_id: int | None,
        production_tagger_version_id: int,
    ) -> list[tuple[int, int]]:
        async with self._factory() as session:
            production_tagger = (
                await session.execute(
                    select(TaggerVersion).where(
                        TaggerVersion.id == production_tagger_version_id,
                        TaggerVersion.tenant_id == tenant_id,
                    )
                )
            ).scalar_one()
            predicates = [
                TagDeployment.tenant_id == tenant_id,
                TagDeployment.baseline_tagger_version_id == production_tagger_version_id,
                TagDeployment.status.in_(["shadow", "canary_5", "canary_25", "awaiting_admin"]),
                TaggerVersion.tenant_id == tenant_id,
                TaggerVersion.schema_version_id == production_tagger.schema_version_id,
            ]
            if production_deployment_id is not None:
                predicates.append(TagDeployment.id != production_deployment_id)
            rows = (
                await session.execute(
                    select(TagDeployment.id, TagDeployment.tagger_version_id)
                    .join(
                        TaggerVersion,
                        TaggerVersion.id == TagDeployment.tagger_version_id,
                    )
                    .where(*predicates)
                    .order_by(TagDeployment.created_at, TagDeployment.id)
                )
            ).all()
            return [(int(row.tagger_version_id), int(row.id)) for row in rows]

    async def _dialogue_ids_for_reception(
        self,
        *,
        tenant_id: str,
        reception_id: int,
    ) -> list[int]:
        async with self._factory() as session:
            return [
                int(item)
                for item in (
                    await session.execute(
                        select(DialogueUnit.id)
                        .where(
                            DialogueUnit.tenant_id == tenant_id,
                            DialogueUnit.reception_id == reception_id,
                        )
                        .order_by(DialogueUnit.unit_index, DialogueUnit.id)
                    )
                ).scalars()
            ]

    async def _extract_route(
        self,
        *,
        job: TagExtractionJob,
        dialogue_unit_id: int,
        tagger_version_id: int,
        deployment_id: int | None,
        publish_current: bool,
        target_tag_keys: tuple[str, ...] | None,
        defer_current: bool = False,
        budget_policy: dict[str, int] | None = None,
    ) -> ExtractionResult | None:
        try:
            kwargs: dict[str, Any] = {
                "tenant_id": str(job.tenant_id),
                "dialogue_unit_id": dialogue_unit_id,
                "tagger_version_id": tagger_version_id,
                "job_id": job.id,
                "deployment_id": deployment_id,
                "actor_user_id": self._actor_user_id,
                "publish_current": publish_current and not defer_current,
                "run_origin": str(job.origin),
                "served_current": publish_current,
            }
            if target_tag_keys is not None:
                kwargs["target_tag_keys"] = target_tag_keys
            if budget_policy is not None:
                kwargs["budget_policy_override"] = budget_policy
            return await self._extractor.extract_dialogue_unit(**kwargs)
        except Exception as exc:
            with contextlib.suppress(Exception):
                await self._extractor.record_failed_subject(
                    tenant_id=str(job.tenant_id),
                    subject_type="dialogue_unit",
                    subject_id=dialogue_unit_id,
                    tagger_version_id=tagger_version_id,
                    job_id=job.id,
                    deployment_id=deployment_id,
                    error=exc,
                    run_origin=str(job.origin),
                    served_current=publish_current,
                )
            raise

    async def _process_extraction_item(
        self,
        *,
        job: TagExtractionJob,
        item_kind: str,
        item_id: int,
        budget_policy: dict[str, int] | None = None,
    ) -> _ProviderUsage:
        target_tag_keys = self._target_tag_keys(job)
        if target_tag_keys == ():
            return _ProviderUsage()
        tagger_version_id, deployment_id = await self._resolve_route(job)
        dialogue_ids: list[int]
        if item_kind == "reception":
            if self._reception_pipeline is None:
                raise GovernanceError("reception automation pipeline is unavailable")
            try:
                run = await self._reception_pipeline.run(
                    reception_id=item_id,
                    tenant_id=str(job.tenant_id),
                    request=ReceptionAutomationRequest(),
                    actor=f"tag-worker:{self._worker_id}",
                    raise_on_failure=True,
                )
            except Exception as exc:
                with contextlib.suppress(Exception):
                    await self._extractor.record_failed_subject(
                        tenant_id=str(job.tenant_id),
                        subject_type="reception",
                        subject_id=item_id,
                        tagger_version_id=tagger_version_id,
                        job_id=job.id,
                        deployment_id=deployment_id,
                        error=exc,
                        run_origin=str(job.origin),
                        served_current=False,
                    )
                raise
            if run.status != "ready":
                raise GovernanceError(f"reception automation did not reach ready: {run.status}")
            dialogue_ids = await self._dialogue_ids_for_reception(
                tenant_id=str(job.tenant_id),
                reception_id=item_id,
            )
        else:
            dialogue_ids = [item_id]
        if not dialogue_ids:
            raise GovernanceError("no dialogue units are available for extraction")
        candidate_routes = (
            []
            if job.origin != "serving" or job.scope.get("deployment_id") is not None
            else await self._active_candidate_routes(
                tenant_id=str(job.tenant_id),
                production_deployment_id=deployment_id,
                production_tagger_version_id=tagger_version_id,
            )
        )
        usage = _ProviderUsage()
        remaining_budget = dict(budget_policy or {})

        async def execute_route(
            *,
            dialogue_unit_id: int,
            route_tagger_id: int,
            route_deployment_id: int | None,
            route_publish_current: bool,
        ) -> None:
            nonlocal usage
            for key in (
                "max_provider_tokens",
                "max_provider_calls",
                "max_cost_microunits",
                "max_wall_seconds",
            ):
                if key in remaining_budget and remaining_budget[key] <= 0:
                    raise TagJobBudgetExhaustedError(
                        f"tag job budget exhausted before route: {key}"
                    )
            result = await self._extract_route(
                job=job,
                dialogue_unit_id=dialogue_unit_id,
                tagger_version_id=route_tagger_id,
                deployment_id=route_deployment_id,
                publish_current=route_publish_current,
                target_tag_keys=target_tag_keys,
                defer_current=budget_policy is not None,
                budget_policy=(dict(remaining_budget) if budget_policy is not None else None),
            )
            route_usage = _ProviderUsage(
                tokens=int(getattr(result, "provider_tokens", 0)),
                calls=int(getattr(result, "provider_calls", 0)),
                cost_microunits=int(getattr(result, "cost_microunits", 0)),
            )
            usage = usage.add(route_usage)
            for key, consumed in (
                ("max_provider_tokens", route_usage.tokens),
                ("max_provider_calls", route_usage.calls),
                ("max_cost_microunits", route_usage.cost_microunits),
            ):
                if key not in remaining_budget:
                    continue
                remaining_budget[key] -= consumed
                if remaining_budget[key] < 0:
                    raise TagJobBudgetExhaustedError(
                        f"tag job budget exhausted during route settlement: {key}"
                    )

        for dialogue_unit_id in dialogue_ids:
            execute, publish_current = await self._route_decision(
                tenant_id=str(job.tenant_id),
                dialogue_unit_id=dialogue_unit_id,
                deployment_id=deployment_id,
            )
            if execute:
                await execute_route(
                    dialogue_unit_id=dialogue_unit_id,
                    route_tagger_id=tagger_version_id,
                    route_deployment_id=deployment_id,
                    route_publish_current=publish_current,
                )
            for candidate_tagger_id, candidate_deployment_id in candidate_routes:
                candidate_execute, candidate_current = await self._route_decision(
                    tenant_id=str(job.tenant_id),
                    dialogue_unit_id=dialogue_unit_id,
                    deployment_id=candidate_deployment_id,
                )
                if not candidate_execute:
                    continue
                await execute_route(
                    dialogue_unit_id=dialogue_unit_id,
                    route_tagger_id=candidate_tagger_id,
                    route_deployment_id=candidate_deployment_id,
                    route_publish_current=candidate_current,
                )
        return usage

    @staticmethod
    def _target_tag_keys(job: TagExtractionJob) -> tuple[str, ...] | None:
        if "target_tag_keys" not in job.scope:
            return None
        raw = job.scope["target_tag_keys"]
        if not isinstance(raw, list):
            raise GovernanceError("scope.target_tag_keys must be a list")
        if any(
            not isinstance(item, str) or not item.strip() or len(item.strip()) > 128 for item in raw
        ):
            raise GovernanceError("scope.target_tag_keys contains an invalid tag key")
        return tuple(sorted({item.strip() for item in raw}))

    @staticmethod
    def _job_items(job: TagExtractionJob) -> tuple[str, list[Any]]:
        if isinstance(job.scope.get("dialogue_unit_ids"), list):
            return "dialogue_unit", list(job.scope["dialogue_unit_ids"])
        if isinstance(job.scope.get("reception_ids"), list):
            return "reception", list(job.scope["reception_ids"])
        if isinstance(job.scope.get("subjects"), list):
            return "subject", list(job.scope["subjects"])
        return "none", []

    async def _heartbeat_loop(
        self,
        *,
        job: TagExtractionJob,
        revision: list[int],
        lock: asyncio.Lock,
        stop: asyncio.Event,
    ) -> None:
        interval = self._lease_ttl.total_seconds() / 3
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            async with lock:
                now = datetime.now(UTC)
                ok = await self._service.heartbeat_job(
                    job.id,
                    tenant_id=str(job.tenant_id),
                    worker_id=self._worker_id,
                    expected_revision=revision[0],
                    now=now,
                    lease_for=self._lease_ttl,
                )
                if not ok:
                    raise GovernanceError("tag job lease was lost")
                revision[0] += 1

    async def _ensure_optimizer_shadow(
        self,
        *,
        tenant_id: str,
        evaluation_run_id: int,
    ) -> TagDeployment | None:
        """Start the release ladder for a passed optimizer candidate exactly once."""

        async with self._factory() as session:
            existing = (
                await session.execute(
                    select(TagDeployment).where(
                        TagDeployment.tenant_id == tenant_id,
                        TagDeployment.evaluation_run_id == evaluation_run_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            evaluation = (
                await session.execute(
                    select(TagEvaluationRun).where(
                        TagEvaluationRun.id == evaluation_run_id,
                        TagEvaluationRun.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if (
                evaluation is None
                or evaluation.status != "completed"
                or evaluation.passed is not True
                or evaluation.baseline_tagger_version_id is None
            ):
                return None
            candidate = (
                await session.execute(
                    select(TaggerVersion).where(
                        TaggerVersion.id == evaluation.tagger_version_id,
                        TaggerVersion.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if candidate is None or candidate.optimization_run_id is None:
                return None
            candidate_id = int(candidate.id)
            baseline_id = int(evaluation.baseline_tagger_version_id)

        try:
            return await self._service.create_deployment(
                tenant_id=tenant_id,
                tagger_version_id=candidate_id,
                evaluation_run_id=evaluation_run_id,
                baseline_tagger_version_id=baseline_id,
                actor_user_id=self._actor_user_id,
            )
        except GovernanceConflictError:
            # A retry can race with another worker. Return the deployment if
            # that worker won; a different active release remains an explicit
            # queueing condition rather than failing the completed evaluation.
            async with self._factory() as session:
                existing = (
                    await session.execute(
                        select(TagDeployment).where(
                            TagDeployment.tenant_id == tenant_id,
                            TagDeployment.evaluation_run_id == evaluation_run_id,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    return existing
            logger.warning(
                "Optimizer candidate passed but Shadow is waiting for another active "
                "release tenant=%s evaluation=%s",
                tenant_id,
                evaluation_run_id,
            )
            return None

    async def _execute_claimed(self, job: TagExtractionJob) -> None:
        item_kind, items = self._job_items(job)
        already_processed = job.completed_items + job.failed_items
        remaining = list(job.failed_subset) if job.failed_subset else items[already_processed:]
        revision = [job.revision]
        lock = asyncio.Lock()
        heartbeat_stop = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat_loop(
                job=job,
                revision=revision,
                lock=lock,
                stop=heartbeat_stop,
            )
        )
        try:
            if job.job_type == "review_batch":
                if item_kind != "subject":
                    raise GovernanceError("review_batch requires scope.subjects")
                for item in remaining:
                    async with lock:
                        active = await self._service.job_lease_is_active(
                            tenant_id=str(job.tenant_id),
                            job_id=job.id,
                            worker_id=self._worker_id,
                            expected_revision=revision[0],
                            now=datetime.now(UTC),
                        )
                    if not active:
                        raise GovernanceError("tag job was cancelled or its lease was lost")
                    await self._service.create_review_batch(
                        tenant_id=str(job.tenant_id),
                        reason=str(job.scope.get("reason", "random")),
                        subjects=[dict(item)],
                        actor_user_id=self._actor_user_id,
                        batch_id=(
                            "review-"
                            + hashlib.sha256(
                                (
                                    f"{job.tenant_id}:{job.id}:{item['subject_type']}:"
                                    f"{int(item['subject_id'])}:{item['tag_key']!s}"
                                ).encode()
                            ).hexdigest()[:40]
                        )[:64],
                        review_bundle_id=(
                            str(job.scope["review_bundle_id"])
                            if job.scope.get("review_bundle_id") is not None
                            else None
                        ),
                        selection_policy=str(job.scope.get("selection_policy", "legacy")),
                        selection_policy_version=str(
                            job.scope.get("selection_policy_version", "1")
                        ),
                        sampling_probability=(
                            float(job.scope["sampling_probability"])
                            if job.scope.get("sampling_probability") is not None
                            else None
                        ),
                        blind_mode=bool(job.scope.get("blind_mode", False)),
                        trusted_observation_id=(
                            int(job.scope["trusted_observation_id"])
                            if job.scope.get("trusted_observation_id") is not None
                            else None
                        ),
                        trusted_sampling_lineage=True,
                    )
                    async with lock:
                        next_revision = await self._service.advance_job_progress(
                            tenant_id=str(job.tenant_id),
                            job_id=job.id,
                            worker_id=self._worker_id,
                            expected_revision=revision[0],
                            success=True,
                            item_ref=item,
                            now=datetime.now(UTC),
                            lease_for=self._lease_ttl,
                        )
                        if next_revision is None:
                            raise GovernanceError("tag job lease was lost")
                        revision[0] = next_revision
            elif job.job_type == "evaluate":
                async with lock:
                    active = await self._service.job_lease_is_active(
                        tenant_id=str(job.tenant_id),
                        job_id=job.id,
                        worker_id=self._worker_id,
                        expected_revision=revision[0],
                        now=datetime.now(UTC),
                    )
                if not active:
                    raise GovernanceError("tag job was cancelled or its lease was lost")
                if self._evaluation_processor is not None:
                    await self._evaluation_processor(job)
                else:
                    evaluation_run_id = job.scope.get("evaluation_run_id")
                    if evaluation_run_id is None:
                        raise GovernanceError("evaluate job requires scope.evaluation_run_id")
                    evaluation = await self._evaluation_service.execute(
                        tenant_id=str(job.tenant_id),
                        evaluation_run_id=int(evaluation_run_id),
                        worker_id=self._worker_id,
                        manage_job=False,
                    )
                    await self._ensure_optimizer_shadow(
                        tenant_id=str(job.tenant_id),
                        evaluation_run_id=evaluation.id,
                    )
                remaining_count = max(
                    job.total_items - job.completed_items - job.failed_items,
                    0,
                )
                for item_index in range(remaining_count):
                    async with lock:
                        next_revision = await self._service.advance_job_progress(
                            tenant_id=str(job.tenant_id),
                            job_id=job.id,
                            worker_id=self._worker_id,
                            expected_revision=revision[0],
                            success=True,
                            item_ref=item_index,
                            now=datetime.now(UTC),
                            lease_for=self._lease_ttl,
                        )
                        if next_revision is None:
                            raise GovernanceError("tag job lease was lost")
                        revision[0] = next_revision
            elif job.job_type == "optimize":
                async with lock:
                    active = await self._service.job_lease_is_active(
                        tenant_id=str(job.tenant_id),
                        job_id=job.id,
                        worker_id=self._worker_id,
                        expected_revision=revision[0],
                        now=datetime.now(UTC),
                    )
                if not active:
                    raise GovernanceError("tag job was cancelled or its lease was lost")
                optimization_run_id = job.scope.get("optimization_run_id")
                if optimization_run_id is None:
                    raise GovernanceError("optimize job requires scope.optimization_run_id")
                if self._optimization_processor is not None:
                    await self._optimization_processor(job)
                else:
                    await self._service.execute_optimization_run(
                        tenant_id=str(job.tenant_id),
                        optimization_run_id=int(optimization_run_id),
                        actor_user_id=self._actor_user_id,
                        worker_id=self._worker_id,
                    )
                async with lock:
                    next_revision = await self._service.advance_job_progress(
                        tenant_id=str(job.tenant_id),
                        job_id=job.id,
                        worker_id=self._worker_id,
                        expected_revision=revision[0],
                        success=True,
                        item_ref=optimization_run_id,
                        now=datetime.now(UTC),
                        lease_for=self._lease_ttl,
                    )
                    if next_revision is None:
                        raise GovernanceError("tag job lease was lost")
                    revision[0] = next_revision
            elif job.job_type in {"extract", "recompute", "remediate"}:
                if item_kind not in {"dialogue_unit", "reception"}:
                    raise GovernanceError(
                        "extraction job requires dialogue_unit_ids or reception_ids"
                    )
                hard_budget_job = any(
                    limit is not None
                    for limit in (
                        job.budget_max_provider_tokens,
                        job.budget_max_provider_calls,
                        job.budget_max_cost_microunits,
                        job.budget_max_wall_seconds,
                    )
                )
                all_items_succeeded = True
                for item in remaining:
                    async with lock:
                        active = await self._service.job_lease_is_active(
                            tenant_id=str(job.tenant_id),
                            job_id=job.id,
                            worker_id=self._worker_id,
                            expected_revision=revision[0],
                            now=datetime.now(UTC),
                        )
                    if not active:
                        raise GovernanceError("tag job was cancelled or its lease was lost")
                    success = True
                    reservation: TagJobBudgetReservation | None = None
                    try:
                        if hard_budget_job:
                            async with lock:
                                reservation = await self._service.reserve_job_budget(
                                    tenant_id=str(job.tenant_id),
                                    job_id=job.id,
                                    worker_id=self._worker_id,
                                    expected_revision=revision[0],
                                    now=datetime.now(UTC),
                                )
                                if reservation is not None:
                                    revision[0] = reservation.revision
                        usage = await self._process_extraction_item(
                            job=job,
                            item_kind=item_kind,
                            item_id=int(item),
                            budget_policy=(
                                reservation.as_policy()
                                if reservation is not None
                                else None
                            ),
                        )
                        if reservation is not None or not hard_budget_job:
                            async with lock:
                                revision[0] = await self._service.settle_job_budget(
                                    tenant_id=str(job.tenant_id),
                                    job_id=job.id,
                                    worker_id=self._worker_id,
                                    expected_revision=revision[0],
                                    provider_tokens=usage.tokens,
                                    provider_calls=usage.calls,
                                    cost_microunits=usage.cost_microunits,
                                    now=datetime.now(UTC),
                                )
                    except TagJobBudgetExhaustedError:
                        if reservation is not None:
                            try:
                                async with lock:
                                    revision[0] = (
                                        await self._service.settle_job_budget(
                                            tenant_id=str(job.tenant_id),
                                            job_id=job.id,
                                            worker_id=self._worker_id,
                                            expected_revision=revision[0],
                                            provider_tokens=0,
                                            provider_calls=0,
                                            cost_microunits=0,
                                            now=datetime.now(UTC),
                                            consume_reserved=True,
                                        )
                                    )
                            except TagJobBudgetExhaustedError as settlement_error:
                                if settlement_error.revision is not None:
                                    revision[0] = settlement_error.revision
                                raise
                        raise
                    except Exception:
                        logger.exception(
                            "Tag job item failed job=%s item=%s",
                            job.id,
                            item,
                        )
                        if reservation is not None or not hard_budget_job:
                            try:
                                async with lock:
                                    revision[0] = (
                                        await self._service.settle_job_budget(
                                            tenant_id=str(job.tenant_id),
                                            job_id=job.id,
                                            worker_id=self._worker_id,
                                            expected_revision=revision[0],
                                            provider_tokens=0,
                                            provider_calls=0,
                                            cost_microunits=0,
                                            now=datetime.now(UTC),
                                            consume_reserved=reservation is not None,
                                        )
                                    )
                            except TagJobBudgetExhaustedError as settlement_error:
                                if settlement_error.revision is not None:
                                    revision[0] = settlement_error.revision
                                raise
                        success = False
                        all_items_succeeded = False
                    async with lock:
                        next_revision = await self._service.advance_job_progress(
                            tenant_id=str(job.tenant_id),
                            job_id=job.id,
                            worker_id=self._worker_id,
                            expected_revision=revision[0],
                            success=success,
                            item_ref=item,
                            now=datetime.now(UTC),
                            lease_for=self._lease_ttl,
                        )
                        if next_revision is None:
                            raise GovernanceError("tag job lease was lost")
                        revision[0] = next_revision
                if hard_budget_job and all_items_succeeded:
                    async with lock:
                        revision[0] = (
                            await self._service.publish_budgeted_job_current(
                                tenant_id=str(job.tenant_id),
                                job_id=job.id,
                                worker_id=self._worker_id,
                                expected_revision=revision[0],
                                actor_user_id=self._actor_user_id,
                                now=datetime.now(UTC),
                            )
                        )
            else:
                raise GovernanceError(f"unsupported tag job type: {job.job_type}")
            async with lock:
                finished = await self._service.finish_job(
                    tenant_id=str(job.tenant_id),
                    job_id=job.id,
                    worker_id=self._worker_id,
                    expected_revision=revision[0],
                    now=datetime.now(UTC),
                )
                if not finished:
                    raise GovernanceError("tag job lease was lost before completion")
        finally:
            heartbeat_stop.set()
            with contextlib.suppress(Exception):
                await heartbeat

    async def run_once(self, *, now: datetime | None = None) -> bool:
        claimed_at = now or datetime.now(UTC)
        job = await self._service.claim_next_job(
            worker_id=self._worker_id,
            now=claimed_at,
            lease_for=self._lease_ttl,
        )
        if job is None:
            return False
        try:
            await self._execute_claimed(job)
        except Exception as exc:
            logger.exception("Tag job failed job=%s", job.id)
            current = await self._service.get_job(
                tenant_id=str(job.tenant_id),
                job_id=job.id,
            )
            if current.status == "running" and current.lease_owner == self._worker_id:
                await self._service.defer_job_failure(
                    tenant_id=str(job.tenant_id),
                    job_id=job.id,
                    worker_id=self._worker_id,
                    expected_revision=current.revision,
                    error_code=(
                        TagJobBudgetExhaustedError.error_code
                        if isinstance(exc, TagJobBudgetExhaustedError)
                        else exc.__class__.__name__
                    ),
                    error_message=str(exc),
                    now=datetime.now(UTC),
                )
        return True

    async def _optimization_check_loop(self) -> None:
        """Poll cheaply; the service enforces one durable check per ISO week."""

        while not self._stop.is_set():
            try:
                await self._service.run_weekly_optimization_checks(
                    at=datetime.now(UTC),
                    actor_user_id=self._actor_user_id,
                )
            except Exception:
                logger.exception("Weekly tag-Harness optimization check failed")
            if self._stop.is_set():
                return
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._optimization_check_seconds,
                )
            except TimeoutError:
                continue

    async def run_forever(self) -> None:
        monitor_task = asyncio.create_task(
            self._deployment_monitor.run_forever(
                stop=self._stop,
                poll_seconds=self._monitor_poll_seconds,
            )
        )
        optimization_check_task = asyncio.create_task(self._optimization_check_loop())
        try:
            while not self._stop.is_set():
                worked = await self.run_once()
                if worked:
                    continue
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_seconds)
                except TimeoutError:
                    continue
        finally:
            self._stop.set()
            with contextlib.suppress(Exception):
                await monitor_task
            with contextlib.suppress(Exception):
                await optimization_check_task


async def _close_adapter(adapter: Any) -> None:
    close = getattr(adapter, "aclose", None)
    if close is not None:
        await close()


async def _main() -> None:
    from audio_graphy.config import build_adapters, get_settings
    from audio_graphy.db import create_db_engine, create_session_factory
    from audio_graphy.services.reception_tagging import ReceptionTaggingService
    from audio_graphy.services.receptions import ReceptionService

    settings = get_settings()
    engine = create_db_engine(settings)
    factory = create_session_factory(engine)
    adapters = build_adapters(settings)
    from audio_graphy.services.llm_runtime import build_llm_runtime

    llm_runtime = await build_llm_runtime(settings, factory, adapters)
    adapters = llm_runtime.bundle
    reception_service = ReceptionService(
        factory,
        audio_root=Path(settings.working_dir),
        audio_assembler=None,
        audio_crypto=None,
    )
    reception_pipeline = ReceptionAutomationPipeline(
        factory,
        reception_service=reception_service,
        tagging_service=ReceptionTaggingService(factory),
    )
    worker_id = os.getenv("TAG_WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}"
    worker = TagJobWorker(
        factory,
        worker_id=worker_id,
        extractor=TagExtractor(
            factory,
            weak_llm=adapters.weak_llm,
            strong_llm=adapters.strong_llm,
            enable_hybrid_rule_short_circuit=(settings.enable_hybrid_rule_short_circuit),
        ),
        reception_pipeline=reception_pipeline,
        poll_seconds=float(os.getenv("TAG_WORKER_POLL_SECONDS", "2")),
        monitor_poll_seconds=float(os.getenv("TAG_MONITOR_POLL_SECONDS", "30")),
        optimization_check_seconds=float(os.getenv("TAG_OPTIMIZATION_CHECK_SECONDS", "3600")),
        shadow_sample_percent=int(os.getenv("TAG_SHADOW_SAMPLE_PERCENT", "10")),
    )
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, worker.stop)
    try:
        await worker.run_forever()
    finally:
        with contextlib.suppress(Exception):
            await llm_runtime.aclose()
        for adapter in (
            adapters.vad,
            adapters.asr,
            adapters.strong_llm,
            adapters.weak_llm,
            adapters.embed,
            adapters.audio_embed,
            adapters.voiceprint,
        ):
            if adapter is not None:
                with contextlib.suppress(Exception):
                    await _close_adapter(adapter)
        await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())


if __name__ == "__main__":
    main()


__all__ = ["TagJobWorker", "main"]
