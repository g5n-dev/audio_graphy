"""Automatic five-minute health observations for active tag deployments.

The monitor derives release health exclusively from persisted extraction,
assignment, current-projection, and human-review rows.  It does not invent
quality labels: ``critical_recall`` is emitted only when a reviewer decision
provides a usable ground-truth value.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
from collections import Counter
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from audio_graphy.models.reception import DialogueUnit, Reception, ReceptionRecording
from audio_graphy.models.segment import Segment
from audio_graphy.models.tag_governance import (
    TagAssignmentCurrent,
    TagAssignmentFact,
    TagDeployment,
    TagDeploymentObservation,
    TagDeploymentObservationSample,
    TagExtractionJob,
    TagExtractionRun,
    TagFeedbackEvent,
    TaggerVersion,
    TagHarnessExecution,
    TagReviewDecision,
    TagReviewTask,
    TagSchemaVersion,
)
from audio_graphy.services.tag_governance import (
    GovernanceConflictError,
    GovernanceStaleObservationError,
    TagGovernanceService,
    review_sampling_manifest_checksum,
)

ACTIVE_DEPLOYMENT_STATES = frozenset(
    {"shadow", "canary_5", "canary_25", "awaiting_admin", "production"}
)
TERMINAL_RUN_STATES = frozenset({"completed", "cached", "failed"})
MONITOR_WINDOW = timedelta(minutes=5)
DRIFT_PAIR_LOOKBACK = MONITOR_WINDOW
DRIFT_MIN_PAIRED_SAMPLES = 30
DRIFT_JSD_THRESHOLD = 0.10
DRIFT_PSI_THRESHOLD = 0.20
EFFICIENCY_SOFT_REGRESSION_THRESHOLD = 0.10
EFFICIENCY_HARD_REGRESSION_THRESHOLD = 0.25
JOB_BUDGET_NEAR_EXHAUSTION_THRESHOLD = 0.90
INPUT_DRIFT_REFERENCE_LOOKBACK = timedelta(days=28)
INPUT_DRIFT_REFERENCE_LIMIT_PER_SUBJECT_TYPE = 10_000
_MISSING_VALUE_BUCKET = "__audio_graphy_missing_assignment__"
_INPUT_PROFILE_CATEGORICAL_FEATURES = ("scenario", "store_id")
_INPUT_PROFILE_NUMERIC_BINS: dict[str, tuple[float, ...]] = {
    "duration_sec": (15.0, 60.0, 180.0, 600.0),
    "segment_count": (1.0, 5.0, 20.0, 50.0),
    "speaker_count": (1.0, 2.0, 3.0),
    "average_vad_confidence": (0.5, 0.7, 0.85, 0.95),
    "transcript_char_count": (1.0, 100.0, 500.0, 2_000.0),
}
_UPSTREAM_FAILURE_STAGES = frozenset(
    {"vad", "asr", "speaker", "boundary", "insufficient_audio", "audio_quality"}
)
_SOURCE_SNAPSHOT_KEYS = (
    "dialogue_unit_id",
    "dialogue_unit_version",
    "reception_id",
    "scenario",
    "store_id",
    "segments",
    "transcript",
    "schema_version_id",
    "schema_checksum",
)
logger = logging.getLogger(__name__)


def _sampling_manifest_valid(task: TagReviewTask) -> bool:
    if (
        task.source_deployment_id is None
        or task.source_extraction_run_id is None
        or task.sampled_deployment_stage is None
        or task.sampled_deployment_revision is None
        or task.sampling_probability is None
        or task.sampling_manifest_checksum is None
    ):
        return False
    expected = review_sampling_manifest_checksum(
        deployment_id=int(task.source_deployment_id),
        deployment_stage=str(task.sampled_deployment_stage),
        deployment_revision=int(task.sampled_deployment_revision),
        extraction_run_id=int(task.source_extraction_run_id),
        subject_type=str(task.subject_type),
        subject_id=int(task.subject_id),
        tag_key=str(task.tag_key),
        selection_policy=str(task.selection_policy),
        selection_policy_version=str(task.selection_policy_version),
        sampling_probability=float(task.sampling_probability),
    )
    return hmac.compare_digest(expected, str(task.sampling_manifest_checksum))


async def _certified_release_truth(
    session: AsyncSession,
    *,
    tenant_id: str,
    task: TagReviewTask,
    decision: TagReviewDecision,
) -> bool:
    if (
        task.reason != "adjudication"
        or not task.blind_mode
        or decision.truth_tier != "t3"
        or not decision.adjudication
        or int(decision.annotator_round) != 3
        or decision.truth_state not in {"present", "absent", "not_applicable"}
    ):
        return False
    try:
        await TagGovernanceService._certified_adjudication_predecessors(
            session,
            tenant_id=tenant_id,
            task=task,
            adjudicator_user_id=int(decision.reviewer_user_id),
        )
    except GovernanceConflictError:
        return False
    return True


@dataclass(frozen=True)
class DeploymentHealth:
    """One derived health snapshot before it is persisted as an observation."""

    deployment_id: int
    sample_count: int
    reception_ids: tuple[int, ...]
    metrics: dict[str, Any]
    breach_codes: tuple[str, ...]
    review_fact_ids: tuple[int, ...]
    served_subject_keys: tuple[tuple[str, int], ...]
    paired_subject_keys: tuple[tuple[str, int], ...]
    audited_subject_keys: tuple[tuple[str, int], ...]
    observed_stage: str
    observed_revision: int
    window_start: datetime
    window_end: datetime


@dataclass(frozen=True)
class DeploymentMonitorResult:
    """Persisted observation plus the post-policy deployment state."""

    observation: TagDeploymentObservation
    deployment: TagDeployment


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def completed_monitor_window(now: datetime) -> tuple[datetime, datetime]:
    """Return the last fully closed UTC five-minute bucket."""

    current = _as_utc(now)
    window_end = current.replace(
        minute=(current.minute // 5) * 5,
        second=0,
        microsecond=0,
    )
    return window_end - MONITOR_WINDOW, window_end


def count_duplicate_current_keys(keys: list[tuple[str, int, str]]) -> int:
    """Count logical subject/tag identities with more than one current row."""

    return sum(1 for count in Counter(keys).values() if count > 1)


def _inverse_probability_summary(probabilities: Mapping[Any, float]) -> dict[str, float]:
    weights = [1.0 / probability for probability in probabilities.values() if 0 < probability <= 1]
    weight_sum = sum(weights)
    squared_weight_sum = sum(weight * weight for weight in weights)
    return {
        "population_estimate": weight_sum,
        "effective_sample_size": (
            weight_sum * weight_sum / squared_weight_sum if squared_weight_sum else 0.0
        ),
    }


def jensen_shannon_divergence(
    left: Mapping[str, int],
    right: Mapping[str, int],
) -> float:
    """Return base-2 Jensen–Shannon divergence in the closed interval [0, 1]."""

    left_total = sum(max(int(value), 0) for value in left.values())
    right_total = sum(max(int(value), 0) for value in right.values())
    if not left_total or not right_total:
        return 0.0
    divergence = 0.0
    for key in left.keys() | right.keys():
        left_probability = max(int(left.get(key, 0)), 0) / left_total
        right_probability = max(int(right.get(key, 0)), 0) / right_total
        midpoint = (left_probability + right_probability) / 2
        if left_probability:
            divergence += 0.5 * left_probability * math.log2(left_probability / midpoint)
        if right_probability:
            divergence += 0.5 * right_probability * math.log2(right_probability / midpoint)
    return min(max(divergence, 0.0), 1.0)


def population_stability_index(
    left: Mapping[str, int],
    right: Mapping[str, int],
    *,
    smoothing: float = 1e-6,
) -> float:
    """Return smoothed categorical PSI; zero means identical distributions."""

    if smoothing <= 0:
        raise ValueError("PSI smoothing must be positive")
    keys = left.keys() | right.keys()
    if not keys:
        return 0.0
    left_total = sum(max(int(value), 0) for value in left.values())
    right_total = sum(max(int(value), 0) for value in right.values())
    if not left_total or not right_total:
        return 0.0
    bucket_count = len(keys)
    left_denominator = left_total + smoothing * bucket_count
    right_denominator = right_total + smoothing * bucket_count
    psi = 0.0
    for key in keys:
        left_probability = (max(int(left.get(key, 0)), 0) + smoothing) / left_denominator
        right_probability = (max(int(right.get(key, 0)), 0) + smoothing) / right_denominator
        psi += (left_probability - right_probability) * math.log(
            left_probability / right_probability
        )
    return max(0.0, psi)


def _source_input_fingerprint(run: TagExtractionRun) -> str | None:
    """Hash only immutable business input, excluding tagger/model configuration."""

    snapshot = run.input_snapshot
    if not isinstance(snapshot, dict) or snapshot.get("input_available") is False:
        return None
    source_snapshot = {key: snapshot[key] for key in _SOURCE_SNAPSHOT_KEYS if key in snapshot}
    if not source_snapshot or not any(
        key in source_snapshot for key in ("dialogue_unit_version", "segments", "transcript")
    ):
        return None
    segments = source_snapshot.get("segments")
    if isinstance(segments, list):
        source_snapshot["segments"] = [
            (
                {str(key): value for key, value in item.items() if key != "text_hash"}
                if isinstance(item, dict)
                else item
            )
            for item in segments
        ]
    payload = {
        "subject_type": run.subject_type,
        "subject_id": run.subject_id,
        "input": source_snapshot,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _terminal_time(run: TagExtractionRun) -> datetime:
    return _as_utc(run.finished_at or run.updated_at)


def _value_bucket(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _distribution_details(counter: Counter[str]) -> list[dict[str, Any]]:
    total = sum(counter.values())
    details: list[dict[str, Any]] = []
    for bucket, count in sorted(counter.items()):
        missing = bucket == _MISSING_VALUE_BUCKET
        details.append(
            {
                "value": None if missing else json.loads(bucket),
                "missing": missing,
                "count": count,
                "share": count / total if total else 0.0,
            }
        )
    return details


def _scene_profile_bucket(feature: str, value: Any) -> str | None:
    """Return a stable categorical bucket for one reliable scene-profile field."""

    if feature in _INPUT_PROFILE_CATEGORICAL_FEATURES:
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            return None
        normalized = str(value).strip()
        return _value_bucket(normalized) if normalized else None
    boundaries = _INPUT_PROFILE_NUMERIC_BINS.get(feature)
    if boundaries is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    for index, boundary in enumerate(boundaries):
        if number < boundary:
            lower = "0" if index == 0 else f"{boundaries[index - 1]:g}"
            return _value_bucket(f"[{lower},{boundary:g})")
    return _value_bucket(f"[{boundaries[-1]:g},inf)")


def _latest_execution_by_subject(
    rows: list[tuple[TagHarnessExecution, TagExtractionRun]],
) -> dict[tuple[str, int], TagHarnessExecution]:
    """Deduplicate retries so one business subject contributes one profile."""

    latest: dict[tuple[str, int], tuple[TagHarnessExecution, TagExtractionRun]] = {}
    for execution, run in rows:
        if (
            execution.extraction_run_id != run.id
            or execution.subject_type != run.subject_type
            or execution.subject_id != run.subject_id
            or execution.input_hash != run.input_hash
        ):
            continue
        key = (str(execution.subject_type), int(execution.subject_id))
        prior = latest.get(key)
        if prior is None or (
            _terminal_time(run),
            int(execution.id),
        ) > (
            _terminal_time(prior[1]),
            int(prior[0].id),
        ):
            latest[key] = (execution, run)
    return {key: execution for key, (execution, _run) in latest.items()}


def _definition_map(schema: TagSchemaVersion) -> dict[str, dict[str, Any]]:
    return {
        str(item["key"]): item
        for item in schema.definitions
        if isinstance(item, dict) and str(item.get("key", "")).strip()
    }


def _applicable_tag_keys(
    definitions: Mapping[str, dict[str, Any]],
    *,
    subject_type: str,
    scenario: str | None,
) -> frozenset[str]:
    """Return the complete schema matrix row for one audited subject."""

    return frozenset(
        tag_key
        for tag_key, definition in definitions.items()
        if subject_type in (definition.get("subject_types") or ())
        and (
            not (definition.get("scenarios") or ())
            or scenario in (definition.get("scenarios") or ())
        )
    )


def _has_schema_violation(
    fact: TagAssignmentFact,
    *,
    definition: dict[str, Any] | None,
    schema_version_id: int,
    tagger_version_id: int,
    deployment_id: int,
    scenario: str | None,
    run: TagExtractionRun | None,
    unit_reception_id: int | None,
) -> bool:
    if fact.tombstone:
        return False
    if (
        fact.schema_version_id != schema_version_id
        or fact.tagger_version_id != tagger_version_id
        or fact.deployment_id != deployment_id
        or definition is None
    ):
        return True
    subject_types = definition.get("subject_types") or []
    scenarios = definition.get("scenarios") or []
    if fact.subject_type not in subject_types:
        return True
    if scenarios and scenario not in scenarios:
        return True
    if definition.get("value_type") == "enum" and fact.tag_value not in (
        definition.get("allowed_values") or []
    ):
        return True
    if fact.subject_type == "dialogue_unit" and (
        fact.dialogue_unit_id != fact.subject_id
        or unit_reception_id is None
        or fact.reception_id != unit_reception_id
    ):
        return True
    if fact.subject_type == "reception" and fact.reception_id != fact.subject_id:
        return True
    if run is None:
        return fact.source in {"rule", "llm"}
    return (
        run.subject_type != fact.subject_type
        or run.subject_id != fact.subject_id
        or run.tagger_version_id != fact.tagger_version_id
        or run.input_hash != fact.input_hash
    )


def _evidence_shape_valid(refs: list[Any]) -> bool:
    for item in refs:
        if not isinstance(item, dict) or item.get("segment_id") is None:
            return False
        try:
            if int(item["segment_id"]) <= 0:
                return False
            start = item.get("start_sec")
            end = item.get("end_sec")
            if start is not None and end is not None and float(end) <= float(start):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _provider_budget_health(
    jobs: list[TagExtractionJob],
    *,
    observed_at: datetime,
    expected_job_ids: set[int],
) -> dict[str, Any]:
    """Summarize hard-budget completion from durable server-side job counters."""

    hard_budget_jobs: list[dict[str, Any]] = []
    exhausted_job_ids: list[int] = []
    near_exhaustion_job_ids: list[int] = []
    max_by_dimension = {
        "provider_tokens": 0.0,
        "provider_calls": 0.0,
        "cost_microunits": 0.0,
        "wall_seconds": 0.0,
    }
    loaded_job_ids = {int(job.id) for job in jobs}
    for job in jobs:
        limits = {
            "provider_tokens": job.budget_max_provider_tokens,
            "provider_calls": job.budget_max_provider_calls,
            "cost_microunits": job.budget_max_cost_microunits,
            "wall_seconds": job.budget_max_wall_seconds,
        }
        if all(limit is None for limit in limits.values()):
            continue
        usage = {
            "provider_tokens": int(job.budget_consumed_provider_tokens)
            + int(job.budget_reserved_provider_tokens),
            "provider_calls": int(job.budget_consumed_provider_calls)
            + int(job.budget_reserved_provider_calls),
            "cost_microunits": int(job.budget_consumed_cost_microunits)
            + int(job.budget_reserved_cost_microunits),
            "wall_seconds": 0,
        }
        if job.budget_started_at is not None:
            effective_end = observed_at
            if job.finished_at is not None:
                effective_end = min(_as_utc(job.finished_at), observed_at)
            usage["wall_seconds"] = max(
                math.ceil((effective_end - _as_utc(job.budget_started_at)).total_seconds()),
                0,
            )
        completion = {
            dimension: usage[dimension] / int(limit)
            for dimension, limit in limits.items()
            if limit is not None
        }
        for dimension, ratio in completion.items():
            max_by_dimension[dimension] = max(max_by_dimension[dimension], ratio)
        explicitly_exhausted = bool(
            job.budget_exhausted_at is not None
            or job.last_error_code == "budget_exhausted"
        )
        successfully_completed = job.status == "completed"
        exhausted = explicitly_exhausted or (
            not successfully_completed
            and any(ratio >= 1 for ratio in completion.values())
        )
        near_exhaustion = (
            not exhausted
            and job.status in {"queued", "running", "retry_wait"}
            and any(
                ratio >= JOB_BUDGET_NEAR_EXHAUSTION_THRESHOLD for ratio in completion.values()
            )
        )
        if exhausted:
            exhausted_job_ids.append(int(job.id))
        elif near_exhaustion:
            near_exhaustion_job_ids.append(int(job.id))
        hard_budget_jobs.append(
            {
                "job_id": int(job.id),
                "budget_source": str(job.budget_source),
                "completion": completion,
                "job_status": str(job.status),
                "successfully_completed": successfully_completed,
                "exhausted": exhausted,
                "near_exhaustion": near_exhaustion,
            }
        )
    return {
        "source": "server_linked_jobs",
        "near_exhaustion_threshold": JOB_BUDGET_NEAR_EXHAUSTION_THRESHOLD,
        "near_exhaustion_policy": "remaining_fraction_lte_0.10",
        "observed_job_count": len(expected_job_ids),
        "hard_budget_job_count": len(hard_budget_jobs),
        "missing_job_ids": sorted(expected_job_ids - loaded_job_ids),
        "exhausted_job_ids": sorted(exhausted_job_ids),
        "near_exhaustion_job_ids": sorted(near_exhaustion_job_ids),
        "max_completion_ratio": max(max_by_dimension.values(), default=0.0),
        "max_completion_by_dimension": max_by_dimension,
        "jobs": hard_budget_jobs,
    }


class TagDeploymentMonitor:
    """Collect and persist trustworthy observations for active releases."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        actor_user_id: int = 0,
        governance_service: TagGovernanceService | None = None,
    ) -> None:
        self._factory = session_factory
        self._actor_user_id = actor_user_id
        self._governance = governance_service or TagGovernanceService(session_factory)

    async def run_once(
        self,
        *,
        now: datetime | None = None,
        tenant_id: str | None = None,
    ) -> list[DeploymentMonitorResult]:
        """Observe every active deployment for the last completed time bucket."""

        window_start, window_end = completed_monitor_window(now or datetime.now(UTC))
        async with self._factory() as session:
            query = (
                select(TagDeployment.tenant_id, TagDeployment.id)
                .where(TagDeployment.status.in_(ACTIVE_DEPLOYMENT_STATES))
                .order_by(TagDeployment.tenant_id, TagDeployment.id)
            )
            if tenant_id is not None:
                query = query.where(TagDeployment.tenant_id == tenant_id)
            deployment_refs = list((await session.execute(query)).all())

        results: list[DeploymentMonitorResult] = []
        for deployment_tenant, deployment_id in deployment_refs:
            health = await self.collect_window(
                tenant_id=str(deployment_tenant),
                deployment_id=int(deployment_id),
                window_start=window_start,
                window_end=window_end,
            )
            if health is None:
                continue
            try:
                observation, deployment = await self._governance.record_deployment_observation(
                    tenant_id=str(deployment_tenant),
                    deployment_id=int(deployment_id),
                    sample_reception_ids=list(health.reception_ids),
                    metrics=health.metrics,
                    breach_codes=list(health.breach_codes),
                    window_start=window_start,
                    window_end=window_end,
                    actor_user_id=self._actor_user_id,
                    review_fact_ids=list(health.review_fact_ids),
                    served_subject_keys=health.served_subject_keys,
                    paired_subject_keys=health.paired_subject_keys,
                    expected_stage=health.observed_stage,
                    expected_revision=health.observed_revision,
                    source="monitor",
                    provenance={
                        "collector": "tag_deployment_monitor",
                        "observed_revision": health.observed_revision,
                        "window_start": window_start.isoformat(),
                        "window_end": window_end.isoformat(),
                    },
                    is_trusted=True,
                    served_count=int(health.metrics.get("trusted_served_count", 0)),
                    paired_count=(
                        int(health.metrics.get("trusted_paired_count", 0))
                        if health.observed_stage == "shadow"
                        else 0
                    ),
                    audited_count=int(health.metrics.get("representative_audit_subject_count", 0)),
                    adjudicated_count=int(health.metrics.get("adjudicated_subject_count", 0)),
                    audited_subject_keys=health.audited_subject_keys,
                )
            except GovernanceStaleObservationError:
                logger.info(
                    "Discarded stale tag deployment observation tenant=%s deployment=%s",
                    deployment_tenant,
                    deployment_id,
                )
                continue
            results.append(
                DeploymentMonitorResult(
                    observation=observation,
                    deployment=deployment,
                )
            )
        return results

    async def run_forever(
        self,
        *,
        stop: asyncio.Event,
        poll_seconds: float = 30.0,
    ) -> None:
        """Continuously observe closed buckets until the owning worker stops.

        More than one worker may run this loop. The deployment-row lock and
        observation window uniqueness make repeated collection harmless.
        """

        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:  # pragma: no cover - exercised by worker integration
                logger.exception("Automatic tag deployment observation failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
            except TimeoutError:
                continue

    async def collect_window(
        self,
        *,
        tenant_id: str,
        deployment_id: int,
        window_start: datetime,
        window_end: datetime,
    ) -> DeploymentHealth | None:
        """Derive one deployment window without mutating release state."""

        window_start = _as_utc(window_start)
        window_end = _as_utc(window_end)
        if window_end <= window_start:
            raise ValueError("window_end must be after window_start")

        async with self._factory() as session:
            deployment = (
                await session.execute(
                    select(TagDeployment).where(
                        TagDeployment.id == deployment_id,
                        TagDeployment.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if deployment is None or deployment.status not in ACTIVE_DEPLOYMENT_STATES:
                return None
            tagger = (
                await session.execute(
                    select(TaggerVersion).where(
                        TaggerVersion.id == deployment.tagger_version_id,
                        TaggerVersion.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if tagger is None:
                return DeploymentHealth(
                    deployment_id=deployment_id,
                    sample_count=0,
                    reception_ids=(),
                    metrics={
                        "run_count": 0.0,
                        "failed_run_count": 0.0,
                        "error_rate": 0.0,
                        "fact_count": 0.0,
                        "schema_violation_count": 1.0,
                        "evidence_violation_count": 0.0,
                        "duplicate_current_count": 0.0,
                        "review_truth_count": 0.0,
                    },
                    breach_codes=("schema_inconsistent",),
                    review_fact_ids=(),
                    served_subject_keys=(),
                    paired_subject_keys=(),
                    audited_subject_keys=(),
                    observed_stage=str(deployment.status),
                    observed_revision=int(deployment.revision),
                    window_start=window_start,
                    window_end=window_end,
                )
            schema = (
                await session.execute(
                    select(TagSchemaVersion).where(
                        TagSchemaVersion.id == tagger.schema_version_id,
                        TagSchemaVersion.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if schema is None:
                return DeploymentHealth(
                    deployment_id=deployment_id,
                    sample_count=0,
                    reception_ids=(),
                    metrics={
                        "run_count": 0.0,
                        "failed_run_count": 0.0,
                        "error_rate": 0.0,
                        "fact_count": 0.0,
                        "schema_violation_count": 1.0,
                        "evidence_violation_count": 0.0,
                        "duplicate_current_count": 0.0,
                        "review_truth_count": 0.0,
                    },
                    breach_codes=("schema_inconsistent",),
                    review_fact_ids=(),
                    served_subject_keys=(),
                    paired_subject_keys=(),
                    audited_subject_keys=(),
                    observed_stage=str(deployment.status),
                    observed_revision=int(deployment.revision),
                    window_start=window_start,
                    window_end=window_end,
                )

            terminal_at = func.coalesce(
                TagExtractionRun.finished_at,
                TagExtractionRun.updated_at,
            )
            all_serving_runs = list(
                (
                    await session.execute(
                        select(TagExtractionRun).where(
                            TagExtractionRun.tenant_id == tenant_id,
                            TagExtractionRun.origin == "serving",
                            TagExtractionRun.status.in_(TERMINAL_RUN_STATES),
                            terminal_at >= window_start,
                            terminal_at < window_end,
                            TagExtractionRun.deployment_id == deployment.id,
                            TagExtractionRun.deployment_stage == deployment.status,
                            TagExtractionRun.deployment_revision == deployment.revision,
                        )
                    )
                )
                .scalars()
                .all()
            )
            runs = (
                all_serving_runs
                if deployment.status == "shadow"
                else [run for run in all_serving_runs if run.served_current]
            )
            serving_job_ids = {int(run.job_id) for run in all_serving_runs}
            serving_jobs = (
                list(
                    (
                        await session.execute(
                            select(TagExtractionJob).where(
                                TagExtractionJob.tenant_id == tenant_id,
                                TagExtractionJob.id.in_(serving_job_ids),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if serving_job_ids
                else []
            )
            provider_budget = _provider_budget_health(
                serving_jobs,
                observed_at=window_end,
                expected_job_ids=serving_job_ids,
            )
            budget_exhausted = bool(provider_budget["exhausted_job_ids"])
            budget_near_exhaustion = bool(provider_budget["near_exhaustion_job_ids"])
            run_by_id = {run.id: run for run in runs}
            dialogue_ids = {
                int(run.subject_id) for run in runs if run.subject_type == "dialogue_unit"
            }
            recording_ids = {int(run.subject_id) for run in runs if run.subject_type == "recording"}
            units = (
                list(
                    (
                        await session.execute(
                            select(DialogueUnit).where(
                                DialogueUnit.tenant_id == tenant_id,
                                DialogueUnit.id.in_(dialogue_ids),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if dialogue_ids
                else []
            )
            units_by_id = {unit.id: unit for unit in units}
            recording_receptions = (
                list(
                    (
                        await session.execute(
                            select(
                                ReceptionRecording.recording_id,
                                ReceptionRecording.reception_id,
                            ).where(
                                ReceptionRecording.tenant_id == tenant_id,
                                ReceptionRecording.recording_id.in_(recording_ids),
                            )
                        )
                    ).all()
                )
                if recording_ids
                else []
            )
            reception_ids = {int(run.subject_id) for run in runs if run.subject_type == "reception"}
            reception_ids.update(unit.reception_id for unit in units if unit.id in dialogue_ids)
            for run in runs:
                snapshot_reception_id = run.input_snapshot.get("reception_id")
                if (
                    run.subject_type == "dialogue_unit"
                    and run.subject_id not in units_by_id
                    and snapshot_reception_id is not None
                ):
                    with suppress(TypeError, ValueError):
                        reception_ids.add(int(snapshot_reception_id))
            reception_ids.update(int(row.reception_id) for row in recording_receptions)

            referenced_fact_ids: set[int] = set()
            for run in runs:
                assignments = run.output_snapshot.get("assignments")
                if not isinstance(assignments, list):
                    continue
                for assignment in assignments:
                    if not isinstance(assignment, dict) or assignment.get("fact_id") is None:
                        continue
                    with suppress(TypeError, ValueError):
                        referenced_fact_ids.add(int(assignment["fact_id"]))
            fact_filter: ColumnElement[bool] = TagAssignmentFact.extraction_run_id.in_(run_by_id)
            if referenced_fact_ids:
                fact_filter = or_(
                    fact_filter,
                    TagAssignmentFact.id.in_(referenced_fact_ids),
                )
            facts = (
                list(
                    (
                        await session.execute(
                            select(TagAssignmentFact).where(
                                TagAssignmentFact.tenant_id == tenant_id,
                                fact_filter,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if run_by_id
                else []
            )
            lineage_run_ids = {
                int(fact.extraction_run_id)
                for fact in facts
                if fact.extraction_run_id is not None and fact.extraction_run_id not in run_by_id
            }
            if lineage_run_ids:
                lineage_runs = (
                    await session.execute(
                        select(TagExtractionRun).where(
                            TagExtractionRun.tenant_id == tenant_id,
                            TagExtractionRun.id.in_(lineage_run_ids),
                        )
                    )
                ).scalars()
                run_by_id.update({run.id: run for run in lineage_runs})
            reception_ids.update(
                int(fact.reception_id) for fact in facts if fact.reception_id is not None
            )
            receptions = (
                list(
                    (
                        await session.execute(
                            select(Reception).where(
                                Reception.tenant_id == tenant_id,
                                Reception.id.in_(reception_ids),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if reception_ids
                else []
            )
            scenario_by_reception = {reception.id: reception.scenario for reception in receptions}
            definitions = _definition_map(schema)

            schema_violations = len(referenced_fact_ids.difference(fact.id for fact in facts))
            evidence_candidates: list[tuple[TagAssignmentFact, dict[str, Any]]] = []
            for fact in facts:
                definition = definitions.get(fact.tag_key)
                unit = units_by_id.get(fact.subject_id)
                if _has_schema_violation(
                    fact,
                    definition=definition,
                    schema_version_id=schema.id,
                    tagger_version_id=tagger.id,
                    deployment_id=deployment.id,
                    scenario=(
                        scenario_by_reception.get(fact.reception_id)
                        if fact.reception_id is not None
                        else None
                    ),
                    run=(
                        run_by_id.get(fact.extraction_run_id)
                        if fact.extraction_run_id is not None
                        else None
                    ),
                    unit_reception_id=unit.reception_id if unit is not None else None,
                ):
                    schema_violations += 1
                if not fact.tombstone and definition is not None:
                    evidence_candidates.append((fact, definition))

            evidence_violations = await self._count_evidence_violations(
                session,
                tenant_id=tenant_id,
                candidates=evidence_candidates,
            )
            duplicate_current = await self._count_duplicate_current(
                session,
                tenant_id=tenant_id,
                deployment_id=deployment.id,
            )
            feedback_metrics = await self._representative_feedback_metrics(
                session,
                tenant_id=tenant_id,
                deployment_id=deployment.id,
                deployment_stage=str(deployment.status),
                deployment_revision=int(deployment.revision),
                schema_version_id=int(schema.id),
                definitions=definitions,
                window_start=window_start,
                window_end=window_end,
            )
            review_metrics = await self._critical_review_metrics(
                session,
                tenant_id=tenant_id,
                deployment_id=deployment.id,
                deployment_stage=str(deployment.status),
                deployment_revision=int(deployment.revision),
                critical_values_by_key={
                    key: (
                        tuple(item.get("critical_values") or ())
                        if item.get("critical_values")
                        else None
                    )
                    for key, item in definitions.items()
                    if bool(item.get("critical")) or bool(item.get("critical_values"))
                },
                eligible_task_ids=frozenset(feedback_metrics["complete_task_ids"]),
                window_start=window_start,
                window_end=window_end,
            )
            (
                drift_metrics,
                drift_review_fact_ids,
                paired_subject_keys,
            ) = await self._distribution_drift_metrics(
                session,
                tenant_id=tenant_id,
                deployment=deployment,
                candidate_runs=all_serving_runs,
                definitions=definitions,
                window_start=window_start,
                window_end=window_end,
            )
            already_counted_reception_ids = (
                set(
                    (
                        await session.execute(
                            select(TagDeploymentObservationSample.reception_id).where(
                                TagDeploymentObservationSample.tenant_id == tenant_id,
                                TagDeploymentObservationSample.deployment_id == deployment.id,
                                TagDeploymentObservationSample.stage == deployment.status,
                                TagDeploymentObservationSample.reception_id.in_(reception_ids),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if reception_ids
                else set()
            )

        run_count = len(runs)
        failed_run_count = sum(1 for run in runs if run.status == "failed")
        error_rate = failed_run_count / run_count if run_count else 0.0
        stage_new_reception_count = len(reception_ids - already_counted_reception_ids)
        trusted_served_count = 0 if deployment.status == "shadow" else stage_new_reception_count
        trusted_paired_count = len(paired_subject_keys)
        served_subject_keys = (
            tuple(
                sorted(
                    {
                        (str(run.subject_type), int(run.subject_id))
                        for run in runs
                        if run.subject_type in {"dialogue_unit", "reception"}
                    }
                )
            )
            if deployment.status != "shadow"
            else ()
        )
        metrics: dict[str, Any] = {
            "run_count": float(run_count),
            "failed_run_count": float(failed_run_count),
            "error_rate": error_rate,
            "fact_count": float(len(facts)),
            "schema_violation_count": float(schema_violations),
            "evidence_violation_count": float(evidence_violations),
            "duplicate_current_count": float(duplicate_current),
            "review_truth_count": float(review_metrics["truth_count"]),
            "representative_audit_subject_count": int(feedback_metrics["audited_subject_count"]),
            "representative_audit_subject_count_by_type": dict(
                feedback_metrics["audited_subject_count_by_type"]
            ),
            "adjudicated_subject_count": int(feedback_metrics["adjudicated_subject_count"]),
            "representative_audit_ipw_population": float(
                feedback_metrics["ipw_population_estimate"]
            ),
            "representative_audit_effective_sample_size": float(
                feedback_metrics["effective_sample_size"]
            ),
            "adjudicated_audit_ipw_population": float(
                feedback_metrics["adjudicated_ipw_population_estimate"]
            ),
            "adjudicated_audit_effective_sample_size": float(
                feedback_metrics["adjudicated_effective_sample_size"]
            ),
            "stage_new_reception_count": stage_new_reception_count,
            "trusted_served_count": trusted_served_count,
            "trusted_paired_count": trusted_paired_count,
            "efficiency_required": tagger.optimization_run_id is not None,
            "provider_budget": provider_budget,
            **drift_metrics,
        }
        critical_recall = review_metrics.get("critical_recall")
        if critical_recall is not None:
            metrics["critical_recall"] = critical_recall
            metrics["critical_recall_ipw"] = critical_recall
            metrics["critical_recall_by_subject_type"] = dict(
                review_metrics["critical_recall_by_subject_type"]
            )
            metrics["critical_recall_effective_sample_size"] = review_metrics[
                "critical_recall_effective_sample_size"
            ]
            metrics["critical_truth_ipw_count"] = review_metrics["critical_truth_ipw_count"]

        breaches: list[str] = []
        if error_rate >= 0.01:
            breaches.append("error_rate")
        if schema_violations:
            breaches.append("schema_inconsistent")
        if evidence_violations:
            breaches.append("evidence_inconsistent")
        if duplicate_current:
            breaches.append("duplicate_current")
        if critical_recall is not None and critical_recall < 0.95:
            breaches.append("critical_recall")
        if drift_metrics["drift_affected_domains"]:
            breaches.append("drift")
        if budget_exhausted:
            breaches.append("budget_exhausted")
        elif budget_near_exhaustion:
            breaches.append("budget_near_exhaustion")
        budget_review_fact_ids = (
            {
                int(fact.id)
                for fact in facts
                if not fact.tombstone and fact.deployment_id == deployment.id
            }
            if budget_near_exhaustion
            else set()
        )
        return DeploymentHealth(
            deployment_id=deployment_id,
            sample_count=len(reception_ids),
            reception_ids=tuple(sorted(reception_ids)),
            metrics=metrics,
            breach_codes=tuple(breaches),
            review_fact_ids=tuple(sorted(set(drift_review_fact_ids).union(budget_review_fact_ids))),
            served_subject_keys=served_subject_keys,
            paired_subject_keys=paired_subject_keys,
            audited_subject_keys=tuple(feedback_metrics["audited_subject_keys"]),
            observed_stage=str(deployment.status),
            observed_revision=int(deployment.revision),
            window_start=window_start,
            window_end=window_end,
        )

    async def _distribution_drift_metrics(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        deployment: TagDeployment,
        candidate_runs: list[TagExtractionRun],
        definitions: Mapping[str, dict[str, Any]],
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[
        dict[str, Any],
        tuple[int, ...],
        tuple[tuple[str, int], ...],
    ]:
        """Measure output JSD and independent scene-profile input PSI."""

        default_metrics: dict[str, Any] = {
            "drift_paired_sample_count": 0,
            "drift_paired_sample_count_by_subject_type": {},
            "drift_min_paired_samples": DRIFT_MIN_PAIRED_SAMPLES,
            "drift_jsd_threshold": DRIFT_JSD_THRESHOLD,
            "drift_psi_threshold": DRIFT_PSI_THRESHOLD,
            "drift_max_jsd": 0.0,
            "drift_max_psi": 0.0,
            "output_jsd": 0.0,
            "input_psi": 0.0,
            "drift_eligible_tag_count": 0,
            "input_drift_eligible_feature_count": 0,
            "drift_affected_tags": [],
            "input_drift_affected_domains": [],
            "drift_affected_domains": [],
            "drift_affected_slices": [],
            "drift_by_tag": {},
            "input_drift_by_feature": {},
            "input_candidate_sample_count_by_subject_type": {},
            "input_reference_sample_count_by_subject_type": {},
            "efficiency_measurement_complete": False,
            "efficiency_paired_subject_count": 0,
            "candidate_provider_tokens": 0,
            "baseline_provider_tokens": 0,
            "candidate_cost_microunits": 0,
            "baseline_cost_microunits": 0,
            "candidate_provider_calls": 0,
            "baseline_provider_calls": 0,
            "provider_token_regression_rate": None,
            "cost_regression_rate": None,
        }
        successful_candidates = [
            run
            for run in candidate_runs
            if run.status in {"completed", "cached"}
            and run.tagger_version_id == deployment.tagger_version_id
            and run.deployment_id == deployment.id
        ]
        if not successful_candidates or not definitions:
            return default_metrics, (), ()

        unique_candidates: dict[tuple[str, int, str], TagExtractionRun] = {}
        for run in successful_candidates:
            fingerprint = _source_input_fingerprint(run)
            if fingerprint is None:
                continue
            key = (str(run.subject_type), int(run.subject_id), fingerprint)
            prior = unique_candidates.get(key)
            if prior is None or (_terminal_time(run), run.id) > (
                _terminal_time(prior),
                prior.id,
            ):
                unique_candidates[key] = run
        if not unique_candidates:
            return default_metrics, (), ()

        subject_ids_by_type: dict[str, set[int]] = {}
        for subject_type, subject_id, _fingerprint in unique_candidates:
            subject_ids_by_type.setdefault(subject_type, set()).add(subject_id)
        subject_predicates = [
            and_(
                TagExtractionRun.subject_type == subject_type,
                TagExtractionRun.subject_id.in_(subject_ids),
            )
            for subject_type, subject_ids in subject_ids_by_type.items()
        ]
        terminal_at = func.coalesce(
            TagExtractionRun.finished_at,
            TagExtractionRun.updated_at,
        )
        baseline_runs = list(
            (
                await session.execute(
                    select(TagExtractionRun).where(
                        TagExtractionRun.tenant_id == tenant_id,
                        TagExtractionRun.origin == "serving",
                        TagExtractionRun.tagger_version_id == deployment.baseline_tagger_version_id,
                        TagExtractionRun.status.in_(("completed", "cached")),
                        terminal_at >= window_start - DRIFT_PAIR_LOOKBACK,
                        terminal_at < window_end,
                        or_(*subject_predicates),
                    )
                )
            )
            .scalars()
            .all()
        )
        baseline_by_input: dict[tuple[str, int, str], list[TagExtractionRun]] = {}
        for run in baseline_runs:
            if run.deployment_id == deployment.id:
                continue
            fingerprint = _source_input_fingerprint(run)
            if fingerprint is None:
                continue
            key = (str(run.subject_type), int(run.subject_id), fingerprint)
            baseline_by_input.setdefault(key, []).append(run)

        pairs: list[tuple[TagExtractionRun, TagExtractionRun]] = []
        for key, candidate_run in unique_candidates.items():
            matches = baseline_by_input.get(key, [])
            if not matches:
                continue
            candidate_time = _terminal_time(candidate_run)
            baseline_run = min(
                matches,
                key=lambda item: (
                    item.job_id != candidate_run.job_id,
                    abs((_terminal_time(item) - candidate_time).total_seconds()),
                    -item.id,
                ),
            )
            pairs.append((candidate_run, baseline_run))
        if not pairs:
            input_metrics = await self._input_scene_profile_drift_metrics(
                session,
                tenant_id=tenant_id,
                deployment=deployment,
                candidate_runs=list(unique_candidates.values()),
                window_start=window_start,
            )
            metrics = {**default_metrics, **input_metrics}
            metrics["drift_affected_domains"] = list(input_metrics["input_drift_affected_domains"])
            metrics["drift_affected_slices"] = list(metrics["drift_affected_domains"])
            return metrics, (), ()

        efficiency_metrics = await self._paired_efficiency_metrics(
            session,
            tenant_id=tenant_id,
            pairs=pairs,
        )
        candidate_fact_maps = await self._fact_maps_for_runs(
            session,
            tenant_id=tenant_id,
            runs=[candidate for candidate, _baseline in pairs],
            tagger_version_id=int(deployment.tagger_version_id),
            deployment_id=int(deployment.id),
        )
        baseline_fact_maps = await self._fact_maps_for_runs(
            session,
            tenant_id=tenant_id,
            runs=[baseline for _candidate, baseline in pairs],
            tagger_version_id=int(deployment.baseline_tagger_version_id),
            deployment_id=None,
        )

        by_tag: dict[str, Any] = {}
        affected_tags: list[str] = []
        affected_fact_ids: set[int] = set()
        max_jsd = 0.0
        eligible_tag_count = 0
        paired_by_subject_type = Counter(
            str(candidate.subject_type) for candidate, _baseline in pairs
        )
        for subject_type in sorted(paired_by_subject_type):
            for tag_key, definition in sorted(definitions.items()):
                if subject_type not in (definition.get("subject_types") or ()):
                    continue
                scenarios = set(definition.get("scenarios") or ())
                domain_pairs = [
                    (candidate_run, baseline_run)
                    for candidate_run, baseline_run in pairs
                    if str(candidate_run.subject_type) == subject_type
                    and (
                        not scenarios
                        or not isinstance(candidate_run.input_snapshot.get("scenario"), str)
                        or candidate_run.input_snapshot.get("scenario") in scenarios
                    )
                ]
                if not domain_pairs:
                    continue
                candidate_distribution: Counter[str] = Counter()
                baseline_distribution: Counter[str] = Counter()
                candidate_facts_for_tag: list[TagAssignmentFact] = []
                for candidate_run, baseline_run in domain_pairs:
                    candidate_fact = candidate_fact_maps.get(candidate_run.id, {}).get(tag_key)
                    baseline_fact = baseline_fact_maps.get(baseline_run.id, {}).get(tag_key)
                    candidate_distribution[
                        (
                            _value_bucket(candidate_fact.tag_value)
                            if candidate_fact is not None
                            else _MISSING_VALUE_BUCKET
                        )
                    ] += 1
                    baseline_distribution[
                        (
                            _value_bucket(baseline_fact.tag_value)
                            if baseline_fact is not None
                            else _MISSING_VALUE_BUCKET
                        )
                    ] += 1
                    if candidate_fact is not None:
                        candidate_facts_for_tag.append(candidate_fact)

                sample_count = len(domain_pairs)
                jsd = jensen_shannon_divergence(
                    candidate_distribution,
                    baseline_distribution,
                )
                max_jsd = max(max_jsd, jsd)
                eligible = sample_count >= DRIFT_MIN_PAIRED_SAMPLES
                breached = eligible and jsd > DRIFT_JSD_THRESHOLD
                if eligible:
                    eligible_tag_count += 1
                domain_tag_key = f"{subject_type}:{tag_key}"
                if breached:
                    affected_tags.append(domain_tag_key)
                    affected_fact_ids.update(fact.id for fact in candidate_facts_for_tag)
                by_tag[domain_tag_key] = {
                    "subject_type": subject_type,
                    "tag_key": tag_key,
                    "jsd": jsd,
                    "sample_count": sample_count,
                    "eligible": eligible,
                    "breached": breached,
                    "candidate_distribution": _distribution_details(candidate_distribution),
                    "baseline_distribution": _distribution_details(baseline_distribution),
                }

        input_metrics = await self._input_scene_profile_drift_metrics(
            session,
            tenant_id=tenant_id,
            deployment=deployment,
            candidate_runs=list(unique_candidates.values()),
            window_start=window_start,
        )
        input_affected_domains = list(input_metrics["input_drift_affected_domains"])
        paired_subject_keys = tuple(
            sorted(
                {
                    (str(candidate.subject_type), int(candidate.subject_id))
                    for candidate, _baseline in pairs
                    if candidate.subject_type in {"dialogue_unit", "reception"}
                }
            )
        )
        return (
            {
                **efficiency_metrics,
                "drift_paired_sample_count": len(pairs),
                "drift_paired_sample_count_by_subject_type": dict(
                    sorted(paired_by_subject_type.items())
                ),
                "drift_min_paired_samples": DRIFT_MIN_PAIRED_SAMPLES,
                "drift_jsd_threshold": DRIFT_JSD_THRESHOLD,
                "drift_psi_threshold": DRIFT_PSI_THRESHOLD,
                "drift_max_jsd": max_jsd,
                # Sample support gates a breach; it must not hide the
                # observed signal from operators.
                "output_jsd": max_jsd,
                "drift_eligible_tag_count": eligible_tag_count,
                "drift_affected_tags": affected_tags,
                "drift_affected_domains": sorted({*affected_tags, *input_affected_domains}),
                "drift_affected_slices": sorted({*affected_tags, *input_affected_domains}),
                "drift_by_tag": by_tag,
                **input_metrics,
            },
            tuple(sorted(affected_fact_ids)),
            paired_subject_keys,
        )

    async def _paired_efficiency_metrics(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        pairs: list[tuple[TagExtractionRun, TagExtractionRun]],
    ) -> dict[str, Any]:
        """Compare actual candidate/baseline usage on identical business inputs."""

        default = {
            "efficiency_measurement_complete": False,
            "efficiency_paired_subject_count": 0,
            "candidate_provider_tokens": 0,
            "baseline_provider_tokens": 0,
            "candidate_cost_microunits": 0,
            "baseline_cost_microunits": 0,
            "candidate_provider_calls": 0,
            "baseline_provider_calls": 0,
            "provider_token_regression_rate": None,
            "cost_regression_rate": None,
        }
        run_ids = {
            int(run.id)
            for candidate, baseline in pairs
            for run in (candidate, baseline)
        }
        if not run_ids:
            return default
        executions = list(
            (
                await session.execute(
                    select(TagHarnessExecution).where(
                        TagHarnessExecution.tenant_id == tenant_id,
                        TagHarnessExecution.extraction_run_id.in_(run_ids),
                        TagHarnessExecution.status == "completed",
                    )
                )
            )
            .scalars()
            .all()
        )
        execution_by_run: dict[int, TagHarnessExecution] = {}
        for execution in executions:
            if execution.extraction_run_id is None:
                continue
            run_id = int(execution.extraction_run_id)
            previous = execution_by_run.get(run_id)
            if previous is None or int(execution.id) > int(previous.id):
                execution_by_run[run_id] = execution

        def usage_value(execution: TagHarnessExecution, key: str) -> int | None:
            output = execution.output_snapshot
            usage = output.get("usage") if isinstance(output, Mapping) else None
            value = usage.get(key) if isinstance(usage, Mapping) else None
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            return value

        measured: list[tuple[int, int, int, int, int, int]] = []
        for candidate_run, baseline_run in pairs:
            candidate = execution_by_run.get(int(candidate_run.id))
            baseline = execution_by_run.get(int(baseline_run.id))
            if candidate is None or baseline is None:
                continue
            candidate_input = usage_value(candidate, "provider_input_tokens")
            candidate_output = usage_value(candidate, "provider_output_tokens")
            baseline_input = usage_value(baseline, "provider_input_tokens")
            baseline_output = usage_value(baseline, "provider_output_tokens")
            candidate_cost = usage_value(candidate, "cost_microunits")
            baseline_cost = usage_value(baseline, "cost_microunits")
            candidate_calls = usage_value(candidate, "provider_calls")
            baseline_calls = usage_value(baseline, "provider_calls")
            candidate_unknown = usage_value(candidate, "unknown_billed_tokens")
            baseline_unknown = usage_value(baseline, "unknown_billed_tokens")
            if (
                candidate_input is None
                or candidate_output is None
                or baseline_input is None
                or baseline_output is None
                or candidate_cost is None
                or baseline_cost is None
                or candidate_calls is None
                or baseline_calls is None
                or candidate_unknown is None
                or baseline_unknown is None
            ):
                continue
            candidate_input_value = int(candidate_input)
            candidate_output_value = int(candidate_output)
            baseline_input_value = int(baseline_input)
            baseline_output_value = int(baseline_output)
            candidate_cost_value = int(candidate_cost)
            baseline_cost_value = int(baseline_cost)
            candidate_calls_value = int(candidate_calls)
            baseline_calls_value = int(baseline_calls)
            candidate_unknown_value = int(candidate_unknown)
            baseline_unknown_value = int(baseline_unknown)
            if candidate_unknown_value or baseline_unknown_value:
                continue
            # A paid provider call without a price snapshot is incomplete, not
            # a zero-cost success. Rule-only/cache-only pairs legitimately cost 0.
            if (candidate_calls_value and not candidate_cost_value) or (
                baseline_calls_value and not baseline_cost_value
            ):
                continue
            measured.append(
                (
                    candidate_input_value + candidate_output_value,
                    baseline_input_value + baseline_output_value,
                    candidate_cost_value,
                    baseline_cost_value,
                    candidate_calls_value,
                    baseline_calls_value,
                )
            )

        if len(measured) != len(pairs):
            return {
                **default,
                "efficiency_paired_subject_count": len(measured),
            }
        candidate_tokens = sum(item[0] for item in measured)
        baseline_tokens = sum(item[1] for item in measured)
        candidate_cost = sum(item[2] for item in measured)
        baseline_cost = sum(item[3] for item in measured)
        candidate_calls = sum(item[4] for item in measured)
        baseline_calls = sum(item[5] for item in measured)

        def regression(candidate_value: int, baseline_value: int) -> float | None:
            if baseline_value > 0:
                return candidate_value / baseline_value - 1.0
            return 0.0 if candidate_value == 0 else None

        return {
            "efficiency_measurement_complete": True,
            "efficiency_paired_subject_count": len(measured),
            "candidate_provider_tokens": candidate_tokens,
            "baseline_provider_tokens": baseline_tokens,
            "candidate_cost_microunits": candidate_cost,
            "baseline_cost_microunits": baseline_cost,
            "candidate_provider_calls": candidate_calls,
            "baseline_provider_calls": baseline_calls,
            "provider_token_regression_rate": regression(
                candidate_tokens,
                baseline_tokens,
            ),
            "cost_regression_rate": regression(candidate_cost, baseline_cost),
            "efficiency_soft_regression_threshold": (
                EFFICIENCY_SOFT_REGRESSION_THRESHOLD
            ),
            "efficiency_hard_regression_threshold": (
                EFFICIENCY_HARD_REGRESSION_THRESHOLD
            ),
        }

    async def _input_scene_profile_drift_metrics(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        deployment: TagDeployment,
        candidate_runs: list[TagExtractionRun],
        window_start: datetime,
    ) -> dict[str, Any]:
        """Compare current inputs with bounded historical baseline serving inputs."""

        empty: dict[str, Any] = {
            "drift_max_psi": 0.0,
            "input_psi": 0.0,
            "input_drift_eligible_feature_count": 0,
            "input_drift_affected_domains": [],
            "input_drift_by_feature": {},
            "input_candidate_sample_count_by_subject_type": {},
            "input_reference_sample_count_by_subject_type": {},
        }
        candidate_run_ids = {int(run.id) for run in candidate_runs}
        if not candidate_run_ids:
            return empty
        candidate_rows = list(
            (
                await session.execute(
                    select(TagHarnessExecution, TagExtractionRun)
                    .join(
                        TagExtractionRun,
                        and_(
                            TagExtractionRun.id == TagHarnessExecution.extraction_run_id,
                            TagExtractionRun.tenant_id == TagHarnessExecution.tenant_id,
                        ),
                    )
                    .where(
                        TagHarnessExecution.tenant_id == tenant_id,
                        TagHarnessExecution.tagger_version_id == deployment.tagger_version_id,
                        TagHarnessExecution.deployment_id == deployment.id,
                        TagHarnessExecution.status == "completed",
                        TagHarnessExecution.extraction_run_id.in_(candidate_run_ids),
                        TagExtractionRun.tenant_id == tenant_id,
                        TagExtractionRun.origin == "serving",
                        TagExtractionRun.status.in_(("completed", "cached")),
                    )
                )
            )
            .tuples()
            .all()
        )
        candidates = _latest_execution_by_subject(candidate_rows)
        subject_types = sorted(
            {
                subject_type
                for subject_type, _subject_id in candidates
                if subject_type in {"dialogue_unit", "reception"}
            }
        )
        if not subject_types:
            return empty

        terminal_at = func.coalesce(
            TagExtractionRun.finished_at,
            TagExtractionRun.updated_at,
        )
        references: dict[tuple[str, int], TagHarnessExecution] = {}
        for subject_type in subject_types:
            reference_rows = list(
                (
                    await session.execute(
                        select(TagHarnessExecution, TagExtractionRun)
                        .join(
                            TagExtractionRun,
                            and_(
                                TagExtractionRun.id == TagHarnessExecution.extraction_run_id,
                                TagExtractionRun.tenant_id == TagHarnessExecution.tenant_id,
                            ),
                        )
                        .where(
                            TagHarnessExecution.tenant_id == tenant_id,
                            TagHarnessExecution.tagger_version_id
                            == deployment.baseline_tagger_version_id,
                            TagHarnessExecution.subject_type == subject_type,
                            TagHarnessExecution.status == "completed",
                            TagExtractionRun.tenant_id == tenant_id,
                            TagExtractionRun.origin == "serving",
                            TagExtractionRun.tagger_version_id
                            == deployment.baseline_tagger_version_id,
                            TagExtractionRun.status.in_(("completed", "cached")),
                            terminal_at >= window_start - INPUT_DRIFT_REFERENCE_LOOKBACK,
                            terminal_at < window_start,
                        )
                        .order_by(terminal_at.desc(), TagHarnessExecution.id.desc())
                        .limit(INPUT_DRIFT_REFERENCE_LIMIT_PER_SUBJECT_TYPE)
                    )
                )
                .tuples()
                .all()
            )
            references.update(_latest_execution_by_subject(reference_rows))

        candidate_count_by_type = Counter(subject_type for subject_type, _subject_id in candidates)
        reference_count_by_type = Counter(subject_type for subject_type, _subject_id in references)
        by_feature: dict[str, dict[str, Any]] = {}
        affected_domains: list[str] = []
        max_psi = 0.0
        eligible_feature_count = 0
        features = (
            *_INPUT_PROFILE_CATEGORICAL_FEATURES,
            *_INPUT_PROFILE_NUMERIC_BINS,
        )
        for subject_type in subject_types:
            candidate_profiles = [
                execution.scene_profile
                for (domain, _subject_id), execution in candidates.items()
                if domain == subject_type
                and isinstance(execution.scene_profile, dict)
                and execution.scene_profile.get("subject_type", subject_type) == subject_type
            ]
            reference_profiles = [
                execution.scene_profile
                for (domain, _subject_id), execution in references.items()
                if domain == subject_type
                and isinstance(execution.scene_profile, dict)
                and execution.scene_profile.get("subject_type", subject_type) == subject_type
            ]
            for feature in features:
                candidate_distribution: Counter[str] = Counter(
                    bucket
                    for profile in candidate_profiles
                    if (
                        bucket := _scene_profile_bucket(
                            feature,
                            profile.get(feature),
                        )
                    )
                    is not None
                )
                reference_distribution: Counter[str] = Counter(
                    bucket
                    for profile in reference_profiles
                    if (
                        bucket := _scene_profile_bucket(
                            feature,
                            profile.get(feature),
                        )
                    )
                    is not None
                )
                candidate_count = sum(candidate_distribution.values())
                reference_count = sum(reference_distribution.values())
                if not candidate_count or not reference_count:
                    continue
                psi = population_stability_index(
                    candidate_distribution,
                    reference_distribution,
                )
                eligible = (
                    candidate_count >= DRIFT_MIN_PAIRED_SAMPLES
                    and reference_count >= DRIFT_MIN_PAIRED_SAMPLES
                )
                breached = eligible and psi > DRIFT_PSI_THRESHOLD
                max_psi = max(max_psi, psi)
                if eligible:
                    eligible_feature_count += 1
                domain_key = f"{subject_type}:@input:{feature}"
                if breached:
                    affected_domains.append(domain_key)
                by_feature[domain_key] = {
                    "subject_type": subject_type,
                    "feature": feature,
                    "psi": psi,
                    "candidate_sample_count": candidate_count,
                    "reference_sample_count": reference_count,
                    "eligible": eligible,
                    "breached": breached,
                    "candidate_distribution": _distribution_details(candidate_distribution),
                    "reference_distribution": _distribution_details(reference_distribution),
                }
        return {
            "drift_max_psi": max_psi,
            # Eligibility controls whether PSI may breach, not whether the
            # raw signal remains observable.
            "input_psi": max_psi,
            "input_drift_eligible_feature_count": eligible_feature_count,
            "input_drift_affected_domains": affected_domains,
            "input_drift_by_feature": by_feature,
            "input_candidate_sample_count_by_subject_type": dict(
                sorted(candidate_count_by_type.items())
            ),
            "input_reference_sample_count_by_subject_type": dict(
                sorted(reference_count_by_type.items())
            ),
        }

    @staticmethod
    async def _fact_maps_for_runs(
        session: AsyncSession,
        *,
        tenant_id: str,
        runs: list[TagExtractionRun],
        tagger_version_id: int,
        deployment_id: int | None,
    ) -> dict[int, dict[str, TagAssignmentFact]]:
        """Resolve direct and cache-referenced facts back to each extraction run."""

        if not runs:
            return {}
        runs_by_id = {int(run.id): run for run in runs}
        run_ids = set(runs_by_id)
        referenced_by_run: dict[int, set[int]] = {}
        referenced_fact_ids: set[int] = set()
        for run in runs:
            assignments = run.output_snapshot.get("assignments")
            if not isinstance(assignments, list):
                continue
            for assignment in assignments:
                if not isinstance(assignment, dict) or assignment.get("fact_id") is None:
                    continue
                with suppress(TypeError, ValueError):
                    fact_id = int(assignment["fact_id"])
                    referenced_fact_ids.add(fact_id)
                    referenced_by_run.setdefault(int(run.id), set()).add(fact_id)

        fact_filter: ColumnElement[bool] = TagAssignmentFact.extraction_run_id.in_(run_ids)
        if referenced_fact_ids:
            fact_filter = or_(
                fact_filter,
                TagAssignmentFact.id.in_(referenced_fact_ids),
            )
        predicates: list[ColumnElement[bool]] = [
            TagAssignmentFact.tenant_id == tenant_id,
            TagAssignmentFact.tagger_version_id == tagger_version_id,
            TagAssignmentFact.tombstone.is_(False),
            fact_filter,
        ]
        if deployment_id is not None:
            predicates.append(TagAssignmentFact.deployment_id == deployment_id)
        facts = list(
            (await session.execute(select(TagAssignmentFact).where(*predicates))).scalars().all()
        )
        facts_by_id = {int(fact.id): fact for fact in facts}
        facts_by_run: dict[int, dict[str, TagAssignmentFact]] = {run_id: {} for run_id in run_ids}

        def attach(run_id: int, fact: TagAssignmentFact) -> None:
            if fact.deployment_id != runs_by_id[run_id].deployment_id:
                return
            existing = facts_by_run[run_id].get(str(fact.tag_key))
            if existing is None or (fact.revision, fact.id) > (
                existing.revision,
                existing.id,
            ):
                facts_by_run[run_id][str(fact.tag_key)] = fact

        for fact in facts:
            if fact.extraction_run_id is not None and int(fact.extraction_run_id) in run_ids:
                attach(int(fact.extraction_run_id), fact)
        for run_id, fact_ids in referenced_by_run.items():
            for fact_id in fact_ids:
                referenced_fact = facts_by_id.get(fact_id)
                if referenced_fact is not None:
                    attach(run_id, referenced_fact)
        return facts_by_run

    @staticmethod
    async def _count_evidence_violations(
        session: AsyncSession,
        *,
        tenant_id: str,
        candidates: list[tuple[TagAssignmentFact, dict[str, Any]]],
    ) -> int:
        segment_ids: set[int] = set()
        reception_ids: set[int] = set()
        for fact, _definition in candidates:
            if fact.reception_id is not None:
                reception_ids.add(int(fact.reception_id))
            for item in fact.evidence_refs:
                if isinstance(item, dict) and item.get("segment_id") is not None:
                    try:
                        segment_ids.add(int(item["segment_id"]))
                    except (TypeError, ValueError):
                        continue
        owned_pairs: set[tuple[int, int]] = set()
        if segment_ids and reception_ids:
            owned_pairs = {
                (int(row.reception_id), int(row.segment_id))
                for row in (
                    await session.execute(
                        select(
                            ReceptionRecording.reception_id,
                            Segment.id.label("segment_id"),
                        )
                        .join(
                            Segment,
                            Segment.recording_id == ReceptionRecording.recording_id,
                        )
                        .where(
                            ReceptionRecording.tenant_id == tenant_id,
                            Segment.tenant_id == tenant_id,
                            ReceptionRecording.reception_id.in_(reception_ids),
                            Segment.id.in_(segment_ids),
                            or_(
                                ReceptionRecording.source_end_sec.is_(None),
                                Segment.start_sec < ReceptionRecording.source_end_sec,
                            ),
                            Segment.end_sec > ReceptionRecording.source_start_sec,
                        )
                    )
                ).all()
            }

        violations = 0
        for fact, definition in candidates:
            refs = list(fact.evidence_refs)
            invalid = bool(definition.get("evidence_required")) and not refs
            invalid = invalid or not _evidence_shape_valid(refs)
            if not invalid and refs:
                invalid = fact.reception_id is None or any(
                    (int(fact.reception_id), int(item["segment_id"])) not in owned_pairs
                    for item in refs
                )
            if invalid:
                violations += 1
        return violations

    @staticmethod
    async def _count_duplicate_current(
        session: AsyncSession,
        *,
        tenant_id: str,
        deployment_id: int,
    ) -> int:
        rows = list(
            (
                await session.execute(
                    select(
                        TagAssignmentCurrent.subject_type,
                        TagAssignmentCurrent.subject_id,
                        TagAssignmentCurrent.tag_key,
                    )
                    .join(
                        TagAssignmentFact,
                        TagAssignmentFact.id == TagAssignmentCurrent.fact_id,
                    )
                    .where(
                        TagAssignmentCurrent.tenant_id == tenant_id,
                        TagAssignmentFact.tenant_id == tenant_id,
                        TagAssignmentFact.deployment_id == deployment_id,
                    )
                )
            ).all()
        )
        keys = [(str(row.subject_type), int(row.subject_id), str(row.tag_key)) for row in rows]
        return count_duplicate_current_keys(keys)

    @staticmethod
    async def _representative_feedback_metrics(
        session: AsyncSession,
        *,
        tenant_id: str,
        deployment_id: int,
        deployment_stage: str,
        deployment_revision: int,
        schema_version_id: int,
        definitions: Mapping[str, dict[str, Any]],
        window_start: datetime,
        window_end: datetime,
    ) -> dict[str, Any]:
        rows = list(
            (
                await session.execute(
                    select(
                        TagFeedbackEvent,
                        TagReviewTask,
                        TagReviewDecision,
                        TagExtractionRun,
                    )
                    .join(
                        TagReviewDecision,
                        TagReviewDecision.id == TagFeedbackEvent.review_decision_id,
                    )
                    .join(
                        TagReviewTask,
                        TagReviewTask.id == TagReviewDecision.task_id,
                    )
                    .join(
                        TagExtractionRun,
                        TagExtractionRun.id == TagReviewTask.source_extraction_run_id,
                    )
                    .where(
                        TagFeedbackEvent.tenant_id == tenant_id,
                        TagFeedbackEvent.deployment_id == deployment_id,
                        TagFeedbackEvent.source == "human",
                        TagFeedbackEvent.selection_policy.in_(
                            (
                                "representative_random",
                                "representative_audit",
                                "random_audit",
                            )
                        ),
                        TagFeedbackEvent.truth_tier == "t3",
                        TagFeedbackEvent.truth_state.in_(
                            ("present", "absent", "not_applicable")
                        ),
                        TagFeedbackEvent.sampling_probability.is_not(None),
                        TagReviewDecision.tenant_id == tenant_id,
                        TagReviewDecision.adjudication.is_(True),
                        TagReviewDecision.truth_tier == "t3",
                        TagReviewDecision.annotator_round == 3,
                        TagReviewDecision.truth_tier == TagFeedbackEvent.truth_tier,
                        TagReviewDecision.truth_state == TagFeedbackEvent.truth_state,
                        TagReviewTask.tenant_id == tenant_id,
                        TagReviewTask.schema_version_id == schema_version_id,
                        TagReviewTask.source_deployment_id == deployment_id,
                        TagReviewTask.subject_type == TagFeedbackEvent.subject_type,
                        TagReviewTask.subject_id == TagFeedbackEvent.subject_id,
                        TagReviewTask.tag_key == TagFeedbackEvent.tag_key,
                        TagReviewTask.selection_policy == TagFeedbackEvent.selection_policy,
                        TagReviewTask.sampling_probability == TagFeedbackEvent.sampling_probability,
                        TagReviewTask.blind_mode.is_(True),
                        TagReviewTask.reason == "adjudication",
                        TagReviewTask.sampled_deployment_stage == deployment_stage,
                        TagReviewTask.sampled_deployment_revision == deployment_revision,
                        TagReviewTask.sampling_manifest_checksum.is_not(None),
                        TagExtractionRun.tenant_id == tenant_id,
                        TagExtractionRun.deployment_id == deployment_id,
                        TagExtractionRun.origin == "serving",
                        TagExtractionRun.subject_type == TagReviewTask.subject_type,
                        TagExtractionRun.subject_id == TagReviewTask.subject_id,
                        TagExtractionRun.deployment_stage == TagReviewTask.sampled_deployment_stage,
                        TagExtractionRun.deployment_revision
                        == TagReviewTask.sampled_deployment_revision,
                        TagFeedbackEvent.occurred_at < window_end,
                    )
                )
            ).all()
        )
        reception_ids = {
            int(task.reception_id)
            for _event, task, _decision, _run in rows
            if task.reception_id is not None
        }
        scenarios_by_reception = (
            {
                int(reception.id): str(reception.scenario)
                for reception in (
                    await session.execute(
                        select(Reception).where(
                            Reception.tenant_id == tenant_id,
                            Reception.id.in_(reception_ids),
                        )
                    )
                )
                .scalars()
                .all()
            }
            if reception_ids
            else {}
        )
        audit_groups: dict[
            tuple[str, int, str, int],
            dict[str, Any],
        ] = {}
        for event, task, decision, run in rows:
            if not _sampling_manifest_valid(task) or not await _certified_release_truth(
                session,
                tenant_id=tenant_id,
                task=task,
                decision=decision,
            ):
                continue
            review_bundle_id = task.review_bundle_id
            if not isinstance(review_bundle_id, str) or not review_bundle_id:
                continue
            group_key = (
                review_bundle_id,
                int(run.id),
                str(event.subject_type),
                int(event.subject_id),
            )
            group = audit_groups.setdefault(
                group_key,
                {
                    "tags": {},
                    "probabilities": set(),
                    "policies": set(),
                    "has_upstream_failure": False,
                    "occurred_at": event.occurred_at,
                    "scenario": (
                        run.input_snapshot.get("scenario")
                        if isinstance(run.input_snapshot.get("scenario"), str)
                        else scenarios_by_reception.get(int(task.reception_id or 0))
                    ),
                },
            )
            group["tags"][str(event.tag_key)] = int(task.id)
            group["probabilities"].add(float(event.sampling_probability))
            group["policies"].add(str(event.selection_policy))
            group["has_upstream_failure"] = bool(group["has_upstream_failure"]) or (
                event.error_stage in _UPSTREAM_FAILURE_STAGES
            )
            if event.occurred_at > group["occurred_at"]:
                group["occurred_at"] = event.occurred_at

        # A release audit is one complete, immutable matrix row. Never assemble
        # it from different runs/bundles, and never equate an omitted label with
        # ``absent``.
        complete_by_subject: dict[tuple[str, int], dict[str, Any]] = {}
        for (
            _bundle_id,
            _run_id,
            subject_type,
            subject_id,
        ), group in audit_groups.items():
            if not (window_start <= _as_utc(group["occurred_at"]) < window_end):
                continue
            required_tag_keys = _applicable_tag_keys(
                definitions,
                subject_type=subject_type,
                scenario=group["scenario"],
            )
            if (
                not required_tag_keys
                or not required_tag_keys.issubset(group["tags"])
                or len(group["probabilities"]) != 1
                or len(group["policies"]) != 1
                or bool(group["has_upstream_failure"])
            ):
                continue
            identity = (subject_type, subject_id)
            candidate = {
                "occurred_at": group["occurred_at"],
                "probability": next(iter(group["probabilities"])),
                "task_ids": frozenset(int(group["tags"][tag_key]) for tag_key in required_tag_keys),
            }
            prior = complete_by_subject.get(identity)
            if prior is None or candidate["occurred_at"] > prior["occurred_at"]:
                complete_by_subject[identity] = candidate

        audited_subjects = {
            identity: float(item["probability"]) for identity, item in complete_by_subject.items()
        }
        complete_task_ids = frozenset(
            task_id for item in complete_by_subject.values() for task_id in item["task_ids"]
        )
        counts_by_type = Counter(subject_type for subject_type, _subject_id in audited_subjects)
        audit_ipw = _inverse_probability_summary(audited_subjects)
        adjudicated_ipw = _inverse_probability_summary(audited_subjects)
        return {
            "audited_subject_count": len(audited_subjects),
            "audited_subject_count_by_type": dict(sorted(counts_by_type.items())),
            "adjudicated_subject_count": len(audited_subjects),
            "ipw_population_estimate": audit_ipw["population_estimate"],
            "effective_sample_size": audit_ipw["effective_sample_size"],
            "adjudicated_ipw_population_estimate": adjudicated_ipw["population_estimate"],
            "adjudicated_effective_sample_size": adjudicated_ipw["effective_sample_size"],
            "audited_subject_keys": tuple(sorted(audited_subjects)),
            "complete_task_ids": tuple(sorted(complete_task_ids)),
        }

    @staticmethod
    async def _critical_review_metrics(
        session: AsyncSession,
        *,
        tenant_id: str,
        deployment_id: int,
        deployment_stage: str,
        deployment_revision: int,
        critical_values_by_key: Mapping[str, tuple[Any, ...] | None],
        eligible_task_ids: frozenset[int],
        window_start: datetime,
        window_end: datetime,
    ) -> dict[str, Any]:
        """Compute positive recall only from blind representative human truth."""

        if not critical_values_by_key or not eligible_task_ids:
            return {"truth_count": 0.0}
        rows = list(
            (
                await session.execute(
                    select(
                        TagReviewTask,
                        TagReviewDecision,
                    )
                    .join(
                        TagReviewDecision,
                        TagReviewDecision.task_id == TagReviewTask.id,
                    )
                    .join(
                        TagExtractionRun,
                        TagExtractionRun.id == TagReviewTask.source_extraction_run_id,
                    )
                    .where(
                        TagReviewTask.tenant_id == tenant_id,
                        TagReviewDecision.tenant_id == tenant_id,
                        TagReviewTask.id.in_(eligible_task_ids),
                        TagReviewTask.source_deployment_id == deployment_id,
                        TagReviewTask.tag_key.in_(critical_values_by_key),
                        TagReviewTask.selection_policy.in_(
                            (
                                "representative_random",
                                "representative_audit",
                                "random_audit",
                            )
                        ),
                        TagReviewTask.sampling_probability.is_not(None),
                        TagReviewTask.blind_mode.is_(True),
                        TagReviewTask.sampled_deployment_stage == deployment_stage,
                        TagReviewTask.sampled_deployment_revision == deployment_revision,
                        TagReviewTask.sampling_manifest_checksum.is_not(None),
                        TagExtractionRun.tenant_id == tenant_id,
                        TagExtractionRun.deployment_id == deployment_id,
                        TagExtractionRun.origin == "serving",
                        TagExtractionRun.subject_type == TagReviewTask.subject_type,
                        TagExtractionRun.subject_id == TagReviewTask.subject_id,
                        TagExtractionRun.deployment_stage == TagReviewTask.sampled_deployment_stage,
                        TagExtractionRun.deployment_revision
                        == TagReviewTask.sampled_deployment_revision,
                        TagReviewDecision.truth_tier.in_(("t2", "t3")),
                        TagReviewDecision.truth_tier == "t3",
                        TagReviewDecision.adjudication.is_(True),
                        TagReviewDecision.annotator_round == 3,
                        TagReviewDecision.truth_state == "present",
                        TagReviewTask.reason == "adjudication",
                        TagReviewDecision.decided_at < window_end,
                    )
                    .order_by(
                        TagReviewDecision.task_id,
                        TagReviewDecision.decided_at,
                        TagReviewDecision.id,
                    )
                )
            ).all()
        )
        latest_by_task: dict[int, tuple[TagReviewTask, TagReviewDecision]] = {
            int(task.id): (task, decision) for task, decision in rows
        }
        per_subject_type: dict[str, dict[str, Any]] = {}
        for task, decision in latest_by_task.values():
            if not _sampling_manifest_valid(task) or not await _certified_release_truth(
                session,
                tenant_id=tenant_id,
                task=task,
                decision=decision,
            ):
                continue
            if decision.action == "accept":
                truth = task.proposed_value
            elif decision.action == "correct":
                truth = decision.corrected_value
            else:
                truth = None
            if truth is None:
                continue
            critical_values = critical_values_by_key.get(str(task.tag_key))
            if critical_values is not None and not any(
                truth == critical_value for critical_value in critical_values
            ):
                continue
            subject_type = str(task.subject_type)
            stats = per_subject_type.setdefault(
                subject_type,
                {
                    "truth_count": 0,
                    "true_positive_count": 0,
                    "weighted_truth_count": 0.0,
                    "weighted_true_positive": 0.0,
                    "probabilities": {},
                },
            )
            stats["truth_count"] += 1
            if task.sampling_probability is None:
                continue
            probability = float(task.sampling_probability)
            weight = 1.0 / probability
            stats["weighted_truth_count"] += weight
            stats["probabilities"][int(task.id)] = probability
            if task.proposed_value == truth:
                stats["true_positive_count"] += 1
                stats["weighted_true_positive"] += weight
        truth_count = sum(int(stats["truth_count"]) for stats in per_subject_type.values())
        if not truth_count:
            return {"truth_count": 0.0}
        recall_by_subject_type = {
            subject_type: (
                float(stats["weighted_true_positive"]) / float(stats["weighted_truth_count"])
            )
            for subject_type, stats in per_subject_type.items()
            if float(stats["weighted_truth_count"]) > 0
        }
        if not recall_by_subject_type:
            return {"truth_count": float(truth_count)}
        all_probabilities = {
            (subject_type, task_id): probability
            for subject_type, stats in per_subject_type.items()
            for task_id, probability in stats["probabilities"].items()
        }
        ipw = _inverse_probability_summary(all_probabilities)
        weighted_truth_count = sum(
            float(stats["weighted_truth_count"]) for stats in per_subject_type.values()
        )
        weighted_true_positive = sum(
            float(stats["weighted_true_positive"]) for stats in per_subject_type.values()
        )
        return {
            "truth_count": float(truth_count),
            "true_positive_count": float(
                sum(int(stats["true_positive_count"]) for stats in per_subject_type.values())
            ),
            "critical_truth_ipw_count": weighted_truth_count,
            "critical_true_positive_ipw_count": weighted_true_positive,
            # Release safety is the worst supported subject domain, never a
            # pooled denominator that lets a large domain hide another.
            "critical_recall": min(recall_by_subject_type.values()),
            "critical_recall_by_subject_type": dict(sorted(recall_by_subject_type.items())),
            "critical_recall_effective_sample_size": ipw["effective_sample_size"],
        }


__all__ = [
    "ACTIVE_DEPLOYMENT_STATES",
    "DRIFT_JSD_THRESHOLD",
    "DRIFT_MIN_PAIRED_SAMPLES",
    "DRIFT_PSI_THRESHOLD",
    "DeploymentHealth",
    "DeploymentMonitorResult",
    "TagDeploymentMonitor",
    "completed_monitor_window",
    "count_duplicate_current_keys",
    "jensen_shannon_divergence",
    "population_stability_index",
]
