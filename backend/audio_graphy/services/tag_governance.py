"""Transactional services for the tag-governance feedback loop."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import re
import secrets
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import perf_counter
from types import SimpleNamespace
from typing import Any, ClassVar, Protocol, cast

from sqlalchemy import Select, and_, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from audio_graphy.models.reception import (
    DialogueTagAssignment,
    DialogueUnit,
    ProvenanceEvent,
    Reception,
    ReceptionRecording,
)
from audio_graphy.models.segment import Segment
from audio_graphy.models.tag_governance import (
    TagAssignmentCurrent,
    TagAssignmentFact,
    TagBadcase,
    TagDeployment,
    TagDeploymentAuditSubject,
    TagDeploymentObservation,
    TagDeploymentObservationSample,
    TagEvaluationRun,
    TagExperienceCase,
    TagExtractionJob,
    TagExtractionRun,
    TagFeedbackEvent,
    TagFeedbackLaneAssignment,
    TagGateResult,
    TaggerVersion,
    TagGoldLabel,
    TagGoldSet,
    TagGoldSetVersion,
    TagGovernanceAuditEvent,
    TagHarnessExecution,
    TagHarnessStageTrace,
    TagOptimizationRun,
    TagOptimizationTrial,
    TagReviewDecision,
    TagReviewTask,
    TagSchema,
    TagSchemaVersion,
)
from audio_graphy.models.user import User
from audio_graphy.services.stage_projection import (
    project_stage_change_in_session,
)
from audio_graphy.services.tag_evaluation_policy import (
    CRITICAL_RECALL_LCB_THRESHOLD,
    critical_enum_values,
    minimum_perfect_wilson_support,
)
from audio_graphy.services.tag_harness_runtime import (
    materialize_trial_candidate,
    resolve_harness_spec,
)

_WHITESPACE = re.compile(r"\s+")
_ERROR_RATE_THRESHOLD = 0.01
_ERROR_POLICY_WINDOW = timedelta(minutes=15)
_ERROR_POLICY_REQUIRED_WINDOWS = 2
_EFFICIENCY_POLICY_WINDOW = timedelta(minutes=15)
_EFFICIENCY_POLICY_REQUIRED_WINDOWS = 2
_EFFICIENCY_SOFT_REGRESSION_THRESHOLD = 0.10
_EFFICIENCY_HARD_REGRESSION_THRESHOLD = 0.25
_DRIFT_POLICY_WINDOW = timedelta(hours=1)
_DRIFT_POLICY_REQUIRED_WINDOWS = 2
_DRIFT_MIN_PAIRED_SAMPLES = 30
_DRIFT_JSD_THRESHOLD = 0.10
_DRIFT_PSI_THRESHOLD = 0.20
_MISSING_DISTRIBUTION_BUCKET = "__audio_graphy_missing_assignment__"
_BLIND_REVIEW_REASONS = frozenset({"random", "audit", "critical", "gold", "drift"})
_DOUBLE_BLIND_REVIEW_REASONS = frozenset({"critical", "gold"})
_RELEASE_REVIEW_POLICIES = frozenset(
    {"representative_random", "representative_audit", "random_audit"}
)
_TRUSTED_SAMPLING_POLICIES = frozenset(
    {
        "representative_random",
        "representative_audit",
        "random_audit",
        "drift_audit",
    }
)
_LEARNING_DATASET_SPLITS = frozenset({"train", "validation", "challenge"})
_HIDDEN_DATASET_SPLITS = frozenset({"pending", "holdout"})
_JOB_BUDGET_BASELINE_AGE = timedelta(days=7)
_JOB_BUDGET_BASELINE_MIN_SAMPLES = 100
_JOB_BUDGET_BASELINE_MAX_SAMPLES = 10_000
_SERVER_BUDGET_BREACHES = frozenset({"budget_exhausted", "budget_near_exhaustion"})


def _is_double_blind_review(*, reason: str, selection_policy: str) -> bool:
    return reason in _DOUBLE_BLIND_REVIEW_REASONS or selection_policy in _RELEASE_REVIEW_POLICIES


class GovernanceError(ValueError):
    """Expected governance-domain validation or transition failure."""


class GovernanceNotFoundError(GovernanceError):
    """A tenant-scoped resource was not found."""


class GovernanceConflictError(GovernanceError):
    """A version, state transition or idempotency contract conflicts."""


class GovernanceStaleObservationError(GovernanceConflictError):
    """A deployment changed after a monitor collected its health window."""


class AssignmentValidationError(GovernanceError):
    """A predicted/manual assignment violates its published definition."""


class TagJobBudgetExhaustedError(AssignmentValidationError):
    """A durable tag-job budget cannot safely admit more Provider work."""

    error_code = "budget_exhausted"

    def __init__(self, message: str, *, revision: int | None = None) -> None:
        super().__init__(message)
        self.revision = revision


@dataclass(frozen=True, slots=True)
class TagJobBudgetReservation:
    """Persisted conservative reservation handed to one extraction item."""

    revision: int
    max_provider_tokens: int | None
    max_provider_calls: int | None
    max_cost_microunits: int | None
    max_wall_seconds: int | None

    def as_policy(self) -> dict[str, int]:
        return {
            key: value
            for key, value in (
                ("max_provider_tokens", self.max_provider_tokens),
                ("max_provider_calls", self.max_provider_calls),
                ("max_cost_microunits", self.max_cost_microunits),
                ("max_wall_seconds", self.max_wall_seconds),
            )
            if value is not None
        }


def _bounded_list_limit(limit: int) -> int:
    if not 1 <= limit <= 500:
        raise GovernanceError("list limit must be between 1 and 500")
    return limit


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _job_budget_purpose(*, job_type: str, scope: Mapping[str, Any]) -> str:
    raw = scope.get("purpose")
    if isinstance(raw, str):
        normalized = _WHITESPACE.sub("_", raw.strip())
        if normalized:
            return normalized[:64]
    return job_type


def _nearest_rank_percentile(values: Sequence[int], percentile: float) -> int:
    if not values:
        raise GovernanceError("budget percentile requires at least one value")
    rank = max(1, math.ceil(percentile * len(values)))
    return sorted(values)[rank - 1]


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True)
class _PolicyObservation:
    window_start: datetime
    window_end: datetime
    metrics: Mapping[str, Any]


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _window_boundary_is_aligned(value: datetime, duration: timedelta) -> bool:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    seconds = int((_aware_utc(value) - epoch).total_seconds())
    duration_seconds = int(duration.total_seconds())
    return duration_seconds > 0 and seconds % duration_seconds == 0


def _strict_bucket_samples(
    samples: Sequence[_PolicyObservation],
    *,
    bucket_start: datetime,
    bucket_end: datetime,
) -> tuple[_PolicyObservation, ...] | None:
    """Return observations only when they cover a bucket with no gap or overlap."""

    expected_start = _aware_utc(bucket_start)
    expected_end = _aware_utc(bucket_end)
    ordered = sorted(
        (
            item
            for item in samples
            if _aware_utc(item.window_start) >= expected_start
            and _aware_utc(item.window_end) <= expected_end
        ),
        key=lambda item: (
            _aware_utc(item.window_start),
            _aware_utc(item.window_end),
        ),
    )
    if not ordered:
        return None
    cursor = expected_start
    for item in ordered:
        item_start = _aware_utc(item.window_start)
        item_end = _aware_utc(item.window_end)
        if item_start != cursor or item_end <= item_start:
            return None
        cursor = item_end
    return tuple(ordered) if cursor == expected_end else None


def _sample_error_rate(sample: _PolicyObservation) -> float | None:
    rate = _finite_float(sample.metrics.get("error_rate"))
    if rate is not None and 0 <= rate <= 1:
        return rate
    run_count = _finite_float(sample.metrics.get("run_count"))
    failed_count = _finite_float(sample.metrics.get("failed_run_count"))
    if (
        run_count is None
        or failed_count is None
        or run_count < 0
        or failed_count < 0
        or failed_count > run_count
    ):
        return None
    return failed_count / run_count if run_count else 0.0


def _aggregate_error_bucket(
    samples: Sequence[_PolicyObservation],
) -> tuple[float, float, float] | None:
    run_counts = [_finite_float(item.metrics.get("run_count")) for item in samples]
    failed_counts = [_finite_float(item.metrics.get("failed_run_count")) for item in samples]
    if all(value is not None for value in run_counts) and all(
        value is not None for value in failed_counts
    ):
        total_runs = sum(cast(float, value) for value in run_counts)
        total_failed = sum(cast(float, value) for value in failed_counts)
        if (
            total_runs < 0
            or total_failed < 0
            or total_failed > total_runs
            or any(cast(float, value) < 0 for value in run_counts)
            or any(cast(float, value) < 0 for value in failed_counts)
        ):
            return None
        rate = total_failed / total_runs if total_runs else 0.0
        return rate, total_runs, total_failed

    weighted_rate = 0.0
    total_seconds = 0.0
    for item in samples:
        fallback_rate = _sample_error_rate(item)
        duration_seconds = (
            _aware_utc(item.window_end) - _aware_utc(item.window_start)
        ).total_seconds()
        if fallback_rate is None or duration_seconds <= 0:
            return None
        weighted_rate += fallback_rate * duration_seconds
        total_seconds += duration_seconds
    if not total_seconds:
        return None
    return weighted_rate / total_seconds, 0.0, 0.0


def _evaluate_error_policy(
    samples: Sequence[_PolicyObservation],
    *,
    window_end: datetime,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "complete": False,
        "consecutive_breach": False,
        "threshold": _ERROR_RATE_THRESHOLD,
        "comparison": ">=",
        "window_minutes": int(_ERROR_POLICY_WINDOW.total_seconds() // 60),
        "required_consecutive_windows": _ERROR_POLICY_REQUIRED_WINDOWS,
        "windows": [],
    }
    aligned_end = _aware_utc(window_end)
    if not _window_boundary_is_aligned(aligned_end, _ERROR_POLICY_WINDOW):
        result["reason"] = "window_end_not_aligned"
        return result

    evaluated_windows: list[dict[str, Any]] = []
    for index in reversed(range(_ERROR_POLICY_REQUIRED_WINDOWS)):
        bucket_end = aligned_end - (_ERROR_POLICY_WINDOW * index)
        bucket_start = bucket_end - _ERROR_POLICY_WINDOW
        bucket_samples = _strict_bucket_samples(
            samples,
            bucket_start=bucket_start,
            bucket_end=bucket_end,
        )
        if bucket_samples is None:
            result["reason"] = "incomplete_trusted_coverage"
            result["windows"] = evaluated_windows
            return result
        aggregate = _aggregate_error_bucket(bucket_samples)
        if aggregate is None:
            result["reason"] = "invalid_error_metrics"
            result["windows"] = evaluated_windows
            return result
        error_rate, run_count, failed_run_count = aggregate
        evaluated_windows.append(
            {
                "window_start": bucket_start.isoformat(),
                "window_end": bucket_end.isoformat(),
                "error_rate": error_rate,
                "run_count": run_count,
                "failed_run_count": failed_run_count,
                "breached": error_rate >= _ERROR_RATE_THRESHOLD,
            }
        )
    result["complete"] = True
    result["windows"] = evaluated_windows
    result["consecutive_breach"] = all(bool(item["breached"]) for item in evaluated_windows)
    return result


def _aggregate_efficiency_bucket(
    samples: Sequence[_PolicyObservation],
) -> dict[str, float] | None:
    """Aggregate paired usage without averaging per-window percentages."""

    required_fields = (
        "efficiency_paired_subject_count",
        "candidate_provider_tokens",
        "baseline_provider_tokens",
        "candidate_cost_microunits",
        "baseline_cost_microunits",
    )
    totals = dict.fromkeys(required_fields, 0.0)
    for item in samples:
        if item.metrics.get("efficiency_measurement_complete") is not True:
            return None
        for field in required_fields:
            value = _finite_float(item.metrics.get(field))
            if value is None or value < 0:
                return None
            totals[field] += value
    if totals["efficiency_paired_subject_count"] <= 0:
        return None

    def regression(candidate: float, baseline: float) -> float:
        if baseline > 0:
            return candidate / baseline - 1.0
        return 0.0 if candidate == 0 else 1.0

    return {
        **totals,
        "provider_token_regression_rate": regression(
            totals["candidate_provider_tokens"],
            totals["baseline_provider_tokens"],
        ),
        "cost_regression_rate": regression(
            totals["candidate_cost_microunits"],
            totals["baseline_cost_microunits"],
        ),
    }


def _evaluate_efficiency_policy(
    samples: Sequence[_PolicyObservation],
    *,
    window_end: datetime,
    required: bool,
) -> dict[str, Any]:
    """Require two complete 15-minute regressions before pausing promotion."""

    result: dict[str, Any] = {
        "required": required,
        "complete": not required,
        "consecutive_breach": False,
        "hard_breach": False,
        "soft_threshold": _EFFICIENCY_SOFT_REGRESSION_THRESHOLD,
        "hard_threshold": _EFFICIENCY_HARD_REGRESSION_THRESHOLD,
        "comparison": ">",
        "window_minutes": int(_EFFICIENCY_POLICY_WINDOW.total_seconds() // 60),
        "required_consecutive_windows": _EFFICIENCY_POLICY_REQUIRED_WINDOWS,
        "windows": [],
    }
    if not required:
        result["reason"] = "not_required_for_non_optimizer_deployment"
        return result
    aligned_end = _aware_utc(window_end)
    if not _window_boundary_is_aligned(aligned_end, _EFFICIENCY_POLICY_WINDOW):
        result["reason"] = "window_end_not_aligned"
        return result

    latest_bucket_start = aligned_end - _EFFICIENCY_POLICY_WINDOW
    latest_bucket_samples = _strict_bucket_samples(
        samples,
        bucket_start=latest_bucket_start,
        bucket_end=aligned_end,
    )
    if latest_bucket_samples is not None:
        latest_aggregate = _aggregate_efficiency_bucket(latest_bucket_samples)
        if latest_aggregate is not None:
            latest_maximum = max(
                latest_aggregate["provider_token_regression_rate"],
                latest_aggregate["cost_regression_rate"],
            )
            result["latest_complete_window"] = {
                "window_start": latest_bucket_start.isoformat(),
                "window_end": aligned_end.isoformat(),
                **latest_aggregate,
            }
            # A >25% regression rolls back after one complete window. The
            # softer >10% signal still requires two consecutive windows.
            result["hard_breach"] = (
                latest_maximum > _EFFICIENCY_HARD_REGRESSION_THRESHOLD
            )

    evaluated_windows: list[dict[str, Any]] = []
    for index in reversed(range(_EFFICIENCY_POLICY_REQUIRED_WINDOWS)):
        bucket_end = aligned_end - (_EFFICIENCY_POLICY_WINDOW * index)
        bucket_start = bucket_end - _EFFICIENCY_POLICY_WINDOW
        bucket_samples = _strict_bucket_samples(
            samples,
            bucket_start=bucket_start,
            bucket_end=bucket_end,
        )
        if bucket_samples is None:
            result["reason"] = "incomplete_trusted_coverage"
            result["windows"] = evaluated_windows
            return result
        aggregate = _aggregate_efficiency_bucket(bucket_samples)
        if aggregate is None:
            result["reason"] = "missing_or_unpaired_usage_ledger"
            result["windows"] = evaluated_windows
            return result
        token_regression = aggregate["provider_token_regression_rate"]
        cost_regression = aggregate["cost_regression_rate"]
        maximum_regression = max(token_regression, cost_regression)
        evaluated_windows.append(
            {
                "window_start": bucket_start.isoformat(),
                "window_end": bucket_end.isoformat(),
                **aggregate,
                "breached": maximum_regression
                > _EFFICIENCY_SOFT_REGRESSION_THRESHOLD,
                "hard_breached": maximum_regression
                > _EFFICIENCY_HARD_REGRESSION_THRESHOLD,
            }
        )
    result["complete"] = True
    result["windows"] = evaluated_windows
    result["consecutive_breach"] = all(
        bool(item["breached"]) for item in evaluated_windows
    )
    result["hard_breach"] = bool(result["hard_breach"]) or any(
        bool(item["hard_breached"]) for item in evaluated_windows
    )
    return result


def _distribution_counts(raw: object) -> Counter[str] | None:
    if not isinstance(raw, list):
        return None
    counts: Counter[str] = Counter()
    for item in raw:
        if not isinstance(item, dict):
            return None
        count = _finite_float(item.get("count"))
        if count is None or count < 0 or not count.is_integer():
            return None
        if bool(item.get("missing")):
            bucket = _MISSING_DISTRIBUTION_BUCKET
        else:
            bucket = json.dumps(
                item.get("value"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        counts[bucket] += int(count)
    return counts


def _jensen_shannon_divergence(
    left: Mapping[str, int],
    right: Mapping[str, int],
) -> float:
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


def _population_stability_index(
    left: Mapping[str, int],
    right: Mapping[str, int],
    *,
    smoothing: float = 1e-6,
) -> float:
    keys = left.keys() | right.keys()
    left_total = sum(max(int(value), 0) for value in left.values())
    right_total = sum(max(int(value), 0) for value in right.values())
    if not keys or not left_total or not right_total:
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


def _aggregate_drift_bucket(
    samples: Sequence[_PolicyObservation],
) -> dict[str, Any] | None:
    output_series = [item.metrics.get("drift_by_tag") for item in samples]
    input_series = [item.metrics.get("input_drift_by_feature") for item in samples]
    if all(isinstance(item, dict) for item in output_series) and all(
        isinstance(item, dict) for item in input_series
    ):
        output_sets = [set(cast(dict[str, Any], item)) for item in output_series]
        input_sets = [set(cast(dict[str, Any], item)) for item in input_series]
        common_outputs = set.intersection(*output_sets) if output_sets else set()
        common_inputs = set.intersection(*input_sets) if input_sets else set()
        output_results: dict[str, dict[str, Any]] = {}
        input_results: dict[str, dict[str, Any]] = {}

        for domain_key in sorted(common_outputs):
            candidate_counts: Counter[str] = Counter()
            baseline_counts: Counter[str] = Counter()
            valid = True
            for raw_by_tag in output_series:
                detail = cast(dict[str, Any], raw_by_tag).get(domain_key)
                if not isinstance(detail, dict):
                    valid = False
                    break
                candidate = _distribution_counts(detail.get("candidate_distribution"))
                baseline = _distribution_counts(detail.get("baseline_distribution"))
                if candidate is None or baseline is None:
                    valid = False
                    break
                candidate_counts.update(candidate)
                baseline_counts.update(baseline)
            if not valid:
                continue
            sample_count = min(
                sum(candidate_counts.values()),
                sum(baseline_counts.values()),
            )
            jsd = _jensen_shannon_divergence(
                candidate_counts,
                baseline_counts,
            )
            eligible = sample_count >= _DRIFT_MIN_PAIRED_SAMPLES
            output_results[str(domain_key)] = {
                "signal": "output",
                "sample_count": sample_count,
                "jsd": jsd,
                "eligible": eligible,
                "breached": eligible and jsd > _DRIFT_JSD_THRESHOLD,
            }

        for domain_key in sorted(common_inputs):
            candidate_input_counts: Counter[str] = Counter()
            reference_counts: Counter[str] | None = None
            valid = True
            for raw_by_feature in input_series:
                detail = cast(dict[str, Any], raw_by_feature).get(domain_key)
                if not isinstance(detail, dict):
                    valid = False
                    break
                candidate = _distribution_counts(detail.get("candidate_distribution"))
                reference = _distribution_counts(detail.get("reference_distribution"))
                if candidate is None or reference is None:
                    valid = False
                    break
                candidate_input_counts.update(candidate)
                # Each five-minute observation carries the same rolling
                # historical reference. Use the latest snapshot once instead
                # of multiplying its support twelve-fold.
                reference_counts = reference
            if not valid or reference_counts is None:
                continue
            candidate_count = sum(candidate_input_counts.values())
            reference_count = sum(reference_counts.values())
            psi = _population_stability_index(
                candidate_input_counts,
                reference_counts,
            )
            eligible = (
                candidate_count >= _DRIFT_MIN_PAIRED_SAMPLES
                and reference_count >= _DRIFT_MIN_PAIRED_SAMPLES
            )
            input_results[str(domain_key)] = {
                "signal": "input",
                "sample_count": candidate_count,
                "reference_sample_count": reference_count,
                "psi": psi,
                "eligible": eligible,
                "breached": eligible and psi > _DRIFT_PSI_THRESHOLD,
            }

        if (common_outputs and not output_results) or (common_inputs and not input_results):
            return None
        domains = {**output_results, **input_results}
        return {
            "tags": output_results,
            "inputs": input_results,
            "domains": domains,
            "affected_tags": sorted(
                key for key, value in output_results.items() if value["breached"]
            ),
            "affected_inputs": sorted(
                key for key, value in input_results.items() if value["breached"]
            ),
            "affected_domains": sorted(key for key, value in domains.items() if value["breached"]),
            "max_jsd": max(
                (float(value["jsd"]) for value in output_results.values() if value["eligible"]),
                default=0.0,
            ),
            "max_psi": max(
                (float(value["psi"]) for value in input_results.values() if value["eligible"]),
                default=0.0,
            ),
        }

    if len(samples) != 1:
        return None
    metrics = samples[0].metrics
    scalar_jsd = _finite_float(metrics.get("output_jsd"))
    scalar_psi = _finite_float(metrics.get("input_psi"))
    scalar_sample_count = _finite_float(metrics.get("drift_paired_sample_count"))
    if (
        scalar_jsd is None
        or scalar_psi is None
        or scalar_sample_count is None
        or scalar_sample_count < 0
    ):
        return None
    eligible = scalar_sample_count >= _DRIFT_MIN_PAIRED_SAMPLES
    output_breach = eligible and scalar_jsd > _DRIFT_JSD_THRESHOLD
    input_breach = eligible and scalar_psi > _DRIFT_PSI_THRESHOLD
    global_result = {
        "__global__": {
            "signal": "global",
            "sample_count": scalar_sample_count,
            "jsd": scalar_jsd,
            "psi": scalar_psi,
            "eligible": eligible,
            "breached": output_breach or input_breach,
        }
    }
    return {
        "tags": global_result,
        "inputs": {},
        "domains": global_result,
        "affected_tags": ["__global__"] if output_breach else [],
        "affected_inputs": ["__global__"] if input_breach else [],
        "affected_domains": (["__global__"] if output_breach or input_breach else []),
        "max_jsd": scalar_jsd,
        "max_psi": scalar_psi,
    }


def _evaluate_drift_policy(
    samples: Sequence[_PolicyObservation],
    *,
    window_end: datetime,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "complete": False,
        "consecutive_breach": False,
        "jsd_threshold": _DRIFT_JSD_THRESHOLD,
        "psi_threshold": _DRIFT_PSI_THRESHOLD,
        "comparison": ">",
        "minimum_paired_samples": _DRIFT_MIN_PAIRED_SAMPLES,
        "window_hours": int(_DRIFT_POLICY_WINDOW.total_seconds() // 3600),
        "required_consecutive_windows": _DRIFT_POLICY_REQUIRED_WINDOWS,
        "affected_tags": [],
        "affected_inputs": [],
        "affected_domains": [],
        "windows": [],
    }
    aligned_end = _aware_utc(window_end)
    if not _window_boundary_is_aligned(aligned_end, _DRIFT_POLICY_WINDOW):
        result["reason"] = "window_end_not_aligned"
        return result

    evaluated_windows: list[dict[str, Any]] = []
    breached_domains_by_window: list[set[str]] = []
    for index in reversed(range(_DRIFT_POLICY_REQUIRED_WINDOWS)):
        bucket_end = aligned_end - (_DRIFT_POLICY_WINDOW * index)
        bucket_start = bucket_end - _DRIFT_POLICY_WINDOW
        bucket_samples = _strict_bucket_samples(
            samples,
            bucket_start=bucket_start,
            bucket_end=bucket_end,
        )
        if bucket_samples is None:
            result["reason"] = "incomplete_trusted_coverage"
            result["windows"] = evaluated_windows
            return result
        aggregate = _aggregate_drift_bucket(bucket_samples)
        if aggregate is None:
            result["reason"] = "invalid_drift_metrics"
            result["windows"] = evaluated_windows
            return result
        affected_domains = {str(item) for item in aggregate["affected_domains"]}
        breached_domains_by_window.append(affected_domains)
        evaluated_windows.append(
            {
                "window_start": bucket_start.isoformat(),
                "window_end": bucket_end.isoformat(),
                **aggregate,
            }
        )
    consecutive_domains = set.intersection(*breached_domains_by_window)
    output_domains = {
        domain_key
        for domain_key in consecutive_domains
        if any(domain_key in window.get("tags", {}) for window in evaluated_windows)
    }
    input_domains = {
        domain_key
        for domain_key in consecutive_domains
        if any(domain_key in window.get("inputs", {}) for window in evaluated_windows)
    }
    result["complete"] = True
    result["windows"] = evaluated_windows
    result["consecutive_breach"] = bool(consecutive_domains)
    result["affected_tags"] = sorted(
        domain_key for domain_key in output_domains if domain_key != "__global__"
    )
    result["affected_inputs"] = sorted(
        domain_key for domain_key in input_domains if domain_key != "__global__"
    )
    result["affected_domains"] = sorted(
        domain_key for domain_key in consecutive_domains if domain_key != "__global__"
    )
    result["global_breach"] = "__global__" in consecutive_domains
    return result


def _parse_drift_domain_key(
    domain_key: str,
) -> tuple[str | None, str | None, str]:
    """Parse output and input drift domains without confusing them with tag keys."""

    if domain_key == "__global__":
        return None, None, "global"
    subject_type, separator, remainder = domain_key.partition(":")
    if separator and subject_type in {"dialogue_unit", "reception"} and remainder:
        if remainder.startswith("@input:") and remainder.removeprefix("@input:"):
            return subject_type, None, "input"
        return subject_type, remainder, "output"
    # Compatibility for observations emitted before domains carried a
    # subject-type prefix.
    return None, domain_key, "output"


def _json_normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_normalize(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_json_normalize(item) for item in value]
    if isinstance(value, float):
        return int(value) if value.is_integer() else round(value, 9)
    return value


def canonical_checksum(value: Any) -> str:
    """SHA-256 of normalized JSON, used for immutable version snapshots."""

    payload = json.dumps(
        _json_normalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def review_sampling_manifest_checksum(
    *,
    deployment_id: int,
    deployment_stage: str,
    deployment_revision: int,
    extraction_run_id: int,
    subject_type: str,
    subject_id: int,
    tag_key: str,
    selection_policy: str,
    selection_policy_version: str,
    sampling_probability: float,
) -> str:
    """Checksum the immutable facts that make one audit sample trustworthy."""

    return canonical_checksum(
        {
            "deployment_id": deployment_id,
            "deployment_stage": deployment_stage,
            "deployment_revision": deployment_revision,
            "extraction_run_id": extraction_run_id,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "tag_key": tag_key,
            "selection_policy": selection_policy,
            "selection_policy_version": selection_policy_version,
            "sampling_probability": sampling_probability,
        }
    )


def compute_gold_dataset_snapshot_hash(
    labels: Sequence[Mapping[str, Any]],
) -> str:
    """Hash every release-relevant truth and frozen-input field deterministically."""

    normalized: list[dict[str, Any]] = [
        {
            "review_decision_id": item.get("review_decision_id"),
            "reception_id": item.get("reception_id"),
            "subject_type": item.get("subject_type"),
            "subject_id": item.get("subject_id"),
            "tag_key": item.get("tag_key"),
            "tag_value": item.get("tag_value"),
            "truth_state": item.get("truth_state"),
            "truth_tier": item.get("truth_tier"),
            "evidence_refs": item.get("evidence_refs") or [],
            "input_hash": item.get("input_hash"),
            "input_snapshot": item.get("input_snapshot") or {},
            "annotation_quality": item.get("annotation_quality") or {},
            "cohort": item.get("cohort"),
            "completeness_manifest": item.get("completeness_manifest") or {},
            "split": item.get("split"),
        }
        for item in labels
    ]
    normalized.sort(
        key=lambda item: (
            str(item["subject_type"]),
            int(item["subject_id"] or 0),
            str(item["tag_key"]),
            int(item["review_decision_id"] or 0),
        )
    )
    return canonical_checksum(normalized)


_PROVENANCE_EVIDENCE_KEYS = {
    "ref_id",
    "kind",
    "segment_id",
    "recording_id",
    "coordinate_space",
    "start_sec",
    "end_sec",
    "start_ms",
    "end_ms",
    "source_start_sec",
    "source_end_sec",
    "timeline_start_sec",
    "timeline_end_sec",
}


def _safe_provenance_evidence(raw_refs: object) -> list[dict[str, Any]]:
    if not isinstance(raw_refs, list):
        return []
    result: list[dict[str, Any]] = []
    for raw in raw_refs[:16]:
        if not isinstance(raw, dict):
            continue
        clean = {
            str(key): value
            for key, value in raw.items()
            if str(key) in _PROVENANCE_EVIDENCE_KEYS and isinstance(value, (str, int, float))
        }
        if clean:
            result.append(clean)
    return result


def _fact_parent_refs(fact: TagAssignmentFact) -> list[dict[str, Any]]:
    parents: list[dict[str, Any]] = []
    if fact.dialogue_unit_id is not None:
        parents.append({"type": "dialogue_unit", "id": fact.dialogue_unit_id})
    seen_segments: set[int] = set()
    for evidence in _safe_provenance_evidence(fact.evidence_refs):
        segment_id = evidence.get("segment_id")
        if isinstance(segment_id, int) and segment_id > 0 and segment_id not in seen_segments:
            parents.append({"type": "segment", "id": segment_id})
            seen_segments.add(segment_id)
    return parents


def _fact_snapshot(fact: TagAssignmentFact) -> dict[str, Any]:
    return {
        "id": fact.id,
        "subject_type": fact.subject_type,
        "subject_id": fact.subject_id,
        "reception_id": fact.reception_id,
        "dialogue_unit_id": fact.dialogue_unit_id,
        "tag_key": fact.tag_key,
        "tag_value": deepcopy(fact.tag_value),
        "confidence": fact.confidence,
        "evidence_refs": _safe_provenance_evidence(fact.evidence_refs),
        "source": fact.source,
        "schema_version_id": fact.schema_version_id,
        "tagger_version_id": fact.tagger_version_id,
        "revision": fact.revision,
        "tombstone": fact.tombstone,
        "assigned_at": fact.assigned_at.isoformat(),
    }


def compute_input_hash(
    *,
    transcript: str,
    segment_snapshot: list[dict[str, Any]],
    dialogue_unit_version: int,
    schema_checksum: str,
    tagger_checksum: str,
    model_version: str,
    context_snapshot: Mapping[str, Any] | None = None,
) -> str:
    """Build a cache key from the complete normalized extraction recipe."""

    normalized_segments = sorted(
        (_json_normalize(item) for item in segment_snapshot),
        key=lambda item: (
            int(item.get("segment_id", 0)),
            float(item.get("start_sec", 0)),
            float(item.get("end_sec", 0)),
        ),
    )
    payload: dict[str, Any] = {
        "transcript": _WHITESPACE.sub(" ", transcript).strip(),
        "segments": normalized_segments,
        "dialogue_unit_version": dialogue_unit_version,
        "schema_checksum": schema_checksum,
        "tagger_checksum": tagger_checksum,
        "model_version": model_version,
    }
    if context_snapshot is not None:
        payload["context"] = _json_normalize(dict(context_snapshot))
    return canonical_checksum(payload)


def stable_canary_bucket(
    tenant_id: str,
    reception_id: int,
    deployment_id: int,
) -> int:
    """Return a process-independent rollout bucket in the inclusive range 0..99."""

    digest = hashlib.sha256(f"{tenant_id}\x1f{reception_id}\x1f{deployment_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % 100


def stable_job_idempotency_key(
    *,
    tenant_id: str,
    operation: str,
    scope: Mapping[str, Any],
    tagger_version_id: int | None,
) -> str:
    """Bind a default job key to its tenant, operation, canonical scope and Tagger."""

    return (
        "stable-"
        + canonical_checksum(
            {
                "tenant_id": tenant_id,
                "operation": operation,
                "scope": dict(scope),
                "tagger_version_id": tagger_version_id,
            }
        )[:48]
    )


async def resolve_serving_tagger_route(
    session: AsyncSession,
    *,
    tenant_id: str,
) -> tuple[int | None, int | None]:
    """Resolve the durable serving Tagger and optional production deployment."""

    production = (
        await session.execute(
            select(TagDeployment.tagger_version_id, TagDeployment.id)
            .where(
                TagDeployment.tenant_id == tenant_id,
                TagDeployment.status == "production",
            )
            .order_by(TagDeployment.approved_at.desc(), TagDeployment.id.desc())
            .limit(1)
        )
    ).one_or_none()
    if production is not None:
        return int(production.tagger_version_id), int(production.id)

    active_baseline = (
        await session.execute(
            select(TagDeployment.baseline_tagger_version_id)
            .where(
                TagDeployment.tenant_id == tenant_id,
                TagDeployment.status.in_(
                    ["shadow", "canary_5", "canary_25", "awaiting_admin"]
                ),
            )
            .order_by(TagDeployment.created_at.desc(), TagDeployment.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if active_baseline is not None:
        return int(active_baseline), None

    rollback_baseline = (
        await session.execute(
            select(TagDeployment.baseline_tagger_version_id)
            .where(
                TagDeployment.tenant_id == tenant_id,
                TagDeployment.status == "rolled_back",
            )
            .order_by(
                TagDeployment.rolled_back_at.desc(),
                TagDeployment.id.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if rollback_baseline is not None:
        return int(rollback_baseline), None

    qualified = (
        await session.execute(
            select(TaggerVersion.id)
            .where(
                TaggerVersion.tenant_id == tenant_id,
                TaggerVersion.status == "qualified",
            )
            .order_by(TaggerVersion.created_at.desc(), TaggerVersion.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return (int(qualified), None) if qualified is not None else (None, None)


def deterministic_gold_split(
    tenant_id: str,
    reception_id: int,
    *,
    truth_tier: str = "t3",
) -> str:
    """Assign a reception to one stable lane without exposing T2 to release holdout."""

    digest = hashlib.sha256(f"{tenant_id}\x1f{reception_id}".encode()).digest()
    bucket = int.from_bytes(digest[:8], "big") % 10
    if bucket < 6:
        return "train"
    if bucket < 8:
        return "validation"
    return "holdout" if truth_tier == "t3" else "challenge"


def validate_assignment(
    *,
    definition: dict[str, Any],
    label_value: Any,
    confidence: float | None,
    evidence_refs: list[dict[str, Any]],
) -> None:
    """Validate one assignment against a published tag definition."""

    if confidence is not None and not 0 <= confidence <= 1:
        raise AssignmentValidationError("confidence must be between 0 and 1")
    if bool(definition.get("evidence_required")) and not evidence_refs:
        raise AssignmentValidationError("evidence is required for this tag")
    if definition.get("value_type") == "enum":
        allowed = definition.get("allowed_values") or []
        if label_value not in allowed:
            raise AssignmentValidationError("tag value is outside allowed_values")
    for evidence in evidence_refs:
        if not isinstance(evidence, dict) or not evidence.get("segment_id"):
            raise AssignmentValidationError("evidence must reference a segment_id")
        start = evidence.get("start_sec")
        end = evidence.get("end_sec")
        if start is not None and end is not None and float(end) <= float(start):
            raise AssignmentValidationError("evidence end_sec must be after start_sec")


def validate_rule_bundle(
    rule_bundle: dict[str, Any],
    *,
    engine: str,
    definitions: list[dict[str, Any]] | None = None,
) -> None:
    """Accept only the bounded, data-only V1 rule DSL."""

    allowed_bundle_fields = {"dsl_version", "rules", "candidate_error_patterns"}
    unknown_bundle = set(rule_bundle) - allowed_bundle_fields
    if unknown_bundle:
        raise GovernanceError(f"unknown rule bundle fields: {sorted(unknown_bundle)}")
    if str(rule_bundle.get("dsl_version", "1")) != "1":
        raise GovernanceError("rule_bundle.dsl_version must be '1'")
    rules = rule_bundle.get("rules", [])
    if not isinstance(rules, list):
        raise GovernanceError("rule_bundle.rules must be a list")
    if engine == "rule" and not rules:
        raise GovernanceError("rule engine requires at least one rule")
    if len(rules) > 500:
        raise GovernanceError("rule bundle exceeds the 500-rule limit")
    allowed_rule_fields = {
        "tag_key",
        "value",
        "contains_any",
        "contains_all",
        "not_contains",
        "confidence",
        "priority",
        "subject_types",
    }
    definitions_by_key = (
        {str(definition["key"]): definition for definition in definitions}
        if definitions is not None
        else None
    )
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise GovernanceError(f"rule {index} must be an object")
        unknown = set(rule) - allowed_rule_fields
        if unknown:
            raise GovernanceError(f"rule {index} has unknown fields: {sorted(unknown)}")
        if not str(rule.get("tag_key", "")).strip() or rule.get("value") is None:
            raise GovernanceError(f"rule {index} requires tag_key and value")
        if definitions_by_key is not None:
            tag_key = str(rule["tag_key"])
            definition = definitions_by_key.get(tag_key)
            if definition is None:
                raise GovernanceError(f"rule {index} references unknown tag_key {tag_key!r}")
            if definition.get("value_type") == "enum" and rule["value"] not in (
                definition.get("allowed_values") or []
            ):
                raise GovernanceError(f"rule {index}.value is outside {tag_key!r} allowed_values")
            subject_types = rule.get("subject_types")
            if subject_types is not None:
                if (
                    not isinstance(subject_types, list)
                    or not subject_types
                    or any(value not in {"dialogue_unit", "reception"} for value in subject_types)
                    or len(subject_types) != len(set(subject_types))
                ):
                    raise GovernanceError(
                        f"rule {index}.subject_types must be a unique bounded subject list"
                    )
                if not set(subject_types).issubset(set(definition.get("subject_types") or [])):
                    raise GovernanceError(
                        f"rule {index}.subject_types exceed {tag_key!r} applicability"
                    )
        for operator in ("contains_any", "contains_all", "not_contains"):
            tokens = rule.get(operator, [])
            if not isinstance(tokens, list) or len(tokens) > 128:
                raise GovernanceError(f"rule {index}.{operator} must be a bounded list")
            if any(not isinstance(token, str) or not token or len(token) > 128 for token in tokens):
                raise GovernanceError(f"rule {index}.{operator} contains an invalid token")
        confidence = float(rule.get("confidence", 1))
        if not 0 <= confidence <= 1:
            raise GovernanceError(f"rule {index}.confidence must be between 0 and 1")


def normalize_schema_definitions(
    definitions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate cross-tag references and return a deterministic immutable snapshot."""

    normalized = [_json_normalize(definition) for definition in definitions]
    keys = [str(definition.get("key", "")) for definition in normalized]
    if not keys or any(not key for key in keys) or len(keys) != len(set(keys)):
        raise GovernanceError("definitions require unique non-empty keys")
    known = set(keys)
    by_key = {str(definition["key"]): definition for definition in normalized}
    for key, definition in by_key.items():
        if definition.get("value_type") == "enum" and not definition.get("allowed_values"):
            raise GovernanceError("enum definitions require allowed_values")
        for field in ("mutually_exclusive_with", "depends_on"):
            raw = definition.get(field, [])
            if not isinstance(raw, list) or len(raw) > 64:
                raise GovernanceError(f"{key}.{field} must be a bounded list")
            refs = [str(value) for value in raw]
            if len(refs) != len(set(refs)):
                raise GovernanceError(f"{key}.{field} must contain unique tag keys")
            if key in refs:
                raise GovernanceError(f"{key}.{field} cannot reference itself")
            unknown = set(refs) - known
            if unknown:
                raise GovernanceError(f"{key}.{field} references unknown tags: {sorted(unknown)}")
            definition[field] = sorted(refs)
    # Mutual exclusion is a symmetric relation. Normalize the reverse edge so
    # downstream extraction and manual edits use one unambiguous contract.
    for key, definition in by_key.items():
        for other in definition["mutually_exclusive_with"]:
            reverse = set(by_key[other]["mutually_exclusive_with"])
            reverse.add(key)
            by_key[other]["mutually_exclusive_with"] = sorted(reverse)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise GovernanceError("tag dependency graph must be acyclic")
        if key in visited:
            return
        visiting.add(key)
        for dependency in by_key[key]["depends_on"]:
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for key in keys:
        visit(key)
    return [by_key[key] for key in keys]


def schema_subject_tag_pairs(
    definitions: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]],
    *,
    label_keys: Sequence[str] | set[str] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Enumerate every explicit subject-domain/tag pair in a frozen Schema."""

    requested = (
        {str(value) for value in label_keys if isinstance(value, str) and value}
        if label_keys
        else None
    )
    definition_items = definitions.values() if isinstance(definitions, Mapping) else definitions
    pairs: set[tuple[str, str]] = set()
    for definition in definition_items:
        if not isinstance(definition, Mapping):
            continue
        tag_key = definition.get("key")
        if not isinstance(tag_key, str) or not tag_key:
            continue
        if requested is not None and tag_key not in requested:
            continue
        raw_subject_types = definition.get("subject_types")
        if not isinstance(raw_subject_types, Sequence) or isinstance(
            raw_subject_types,
            (str, bytes),
        ):
            continue
        for subject_type in raw_subject_types:
            if subject_type in {"dialogue_unit", "reception"}:
                pairs.add((str(subject_type), tag_key))
    return tuple(sorted(pairs))


@dataclass(frozen=True, slots=True)
class Gate:
    code: str
    passed: bool
    actual: float | None
    threshold: float | None
    message: str


@dataclass(frozen=True, slots=True)
class GateEvaluation:
    passed: bool
    gates: tuple[Gate, ...]


@dataclass(frozen=True, slots=True)
class OptimizationReward:
    """Bounded reward vector; selection policy is applied by the optimizer."""

    feasible: bool
    quality_delta: float
    review_rate_delta: float
    p95_latency_delta: float
    cost_delta: float

    @property
    def rank_key(self) -> tuple[int, float, float, float, float]:
        return (
            int(self.feasible),
            self.quality_delta,
            -self.review_rate_delta,
            -self.p95_latency_delta,
            -self.cost_delta,
        )


@dataclass(frozen=True, slots=True)
class HarnessOptimizationTrial:
    index: int
    mutation: str
    config: dict[str, Any]
    reward: OptimizationReward
    metrics: dict[str, Any]


@dataclass(frozen=True, slots=True)
class HarnessSearchResult:
    winner: HarnessOptimizationTrial
    trials: tuple[HarnessOptimizationTrial, ...]
    eligible_sample_count: int
    excluded_upstream_count: int


@dataclass(frozen=True, slots=True)
class PromotionReadiness:
    passed: bool
    stage: str
    elapsed_hours: float
    requirements: dict[str, int]
    observed: dict[str, int]
    unmet: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OptimizationFeedbackCoverage:
    total: int
    by_tag: dict[str, int]
    by_subject_tag: dict[str, int]
    cohort_key: str
    since: datetime | None
    after_event_id: int
    max_event_id: int
    passed: bool
    blockers: tuple[str, ...]


_UPSTREAM_FAILURE_STAGES = frozenset(
    {"vad", "asr", "speaker", "boundary", "insufficient_audio", "audio_quality"}
)

_PROMOTION_REQUIREMENTS: dict[str, dict[str, int]] = {
    "shadow": {
        "duration_hours": 24,
        "served_count": 0,
        "paired_count": 500,
        "audited_count": 100,
    },
    "canary_5": {
        "duration_hours": 24,
        "served_count": 1_000,
        "paired_count": 0,
        "audited_count": 200,
    },
    "canary_25": {
        "duration_hours": 48,
        "served_count": 5_000,
        "paired_count": 0,
        "audited_count": 500,
    },
}


def evaluate_promotion_readiness(
    *,
    stage: str,
    elapsed: timedelta,
    served_count: int,
    paired_count: int,
    audited_count: int,
) -> PromotionReadiness:
    """Require both stage duration and unbiased, trusted observation support."""

    requirements = _PROMOTION_REQUIREMENTS.get(stage)
    if requirements is None:
        raise GovernanceError(f"deployment stage {stage!r} has no promotion gate")
    observed = {
        "served_count": max(0, int(served_count)),
        "paired_count": max(0, int(paired_count)),
        "audited_count": max(0, int(audited_count)),
    }
    elapsed_hours = max(0.0, elapsed.total_seconds() / 3600)
    unmet: list[str] = []
    if elapsed_hours < requirements["duration_hours"]:
        unmet.append("duration_hours")
    for metric in ("served_count", "paired_count", "audited_count"):
        if observed[metric] < requirements[metric]:
            unmet.append(metric)
    return PromotionReadiness(
        passed=not unmet,
        stage=stage,
        elapsed_hours=elapsed_hours,
        requirements=dict(requirements),
        observed=observed,
        unmet=tuple(unmet),
    )


def _shadow_sampling_requirements_met(readiness: PromotionReadiness) -> bool:
    sample_metrics = {
        key
        for key in readiness.requirements
        if key in {"paired_count", "audited_count"}
        or key.startswith(("paired_count:", "audited_count:"))
    }
    return bool(sample_metrics) and all(
        readiness.observed.get(metric, 0) >= readiness.requirements[metric]
        for metric in sample_metrics
    )


def _gold_manifest_is_complete(manifest: Mapping[str, Any] | None) -> bool:
    if not manifest or bool(manifest.get("legacy_sparse")):
        return False
    return bool(
        manifest.get("complete")
        or manifest.get("matrix_complete")
        or manifest.get("status") in {"complete", "qualified"}
    )


def _safe_optimizer_ratio(
    numerator: int | float,
    denominator: int | float,
    *,
    empty: float = 0.0,
) -> float:
    return numerator / denominator if denominator else empty


def reject_client_error_samples(error_samples: Sequence[Mapping[str, Any]] | None) -> None:
    """Make the trust boundary explicit: optimization facts always come from storage."""

    if error_samples is not None:
        raise GovernanceError(
            "client-supplied error_samples are not accepted; use persisted feedback"
        )


def enforce_sealed_holdout_access(
    *,
    requested_candidate_id: int,
    requested_baseline_id: int,
    consumed_candidate_id: int | None,
    bound_baseline_id: int,
) -> None:
    """Allow idempotent replay, but never probe a sealed set with another candidate."""

    if requested_baseline_id != bound_baseline_id:
        raise GovernanceConflictError(
            "sealed holdout baseline differs from the optimization baseline"
        )
    if consumed_candidate_id is not None and requested_candidate_id != consumed_candidate_id:
        raise GovernanceConflictError(
            "sealed holdout was already consumed by a different candidate"
        )


@dataclass(frozen=True, slots=True)
class InjectedCandidate:
    """A candidate authored outside the mechanical sweep, e.g. by a prompt compiler.

    The sweep in :func:`_bounded_candidate_configs` stays a pure function of the
    baseline: injected configs arrive already materialized, carry their own mutation
    label, and record where they came from so a search manifest remains reproducible.
    """

    mutation: str
    config: Mapping[str, Any]
    provenance: Mapping[str, Any]


def _search_manifest_payload(
    *,
    dataset_snapshot_hash: str,
    baseline_tagger_version_id: int,
    baseline_config_checksum: str,
    schema_checksum: str,
    candidate_checksums: Sequence[str],
    gold_inputs: Sequence[Mapping[str, Any]],
    extra_candidates: Sequence[InjectedCandidate] = (),
) -> dict[str, Any]:
    """Describe everything a search replay must reproduce, byte for byte.

    ``injected_candidates`` is recorded only when injection is actually used, so a
    run created before the prompt compiler existed keeps the manifest checksum it was
    already admitted and resumed against.
    """

    payload: dict[str, Any] = {
        "dataset_snapshot_hash": dataset_snapshot_hash,
        "baseline_tagger_version_id": baseline_tagger_version_id,
        "baseline_config_checksum": baseline_config_checksum,
        "schema_checksum": schema_checksum,
        "candidate_checksums": list(candidate_checksums),
        "gold_inputs": [dict(item) for item in gold_inputs],
        "parser_version": "tag-assignment-parser-v2",
        "postprocessor_version": "tag-policy-v2",
        "cache_recipe_version": "llm-recipe-v2",
    }
    if extra_candidates:
        payload["injected_candidates"] = [
            {
                "mutation": injected.mutation,
                "checksum": canonical_checksum(dict(injected.config)),
                "provenance": dict(injected.provenance),
            }
            for injected in sorted(
                extra_candidates,
                key=lambda item: (
                    item.mutation,
                    canonical_checksum(dict(item.config)),
                ),
            )
        ]
    return payload


def _bounded_candidate_configs(
    baseline_config: Mapping[str, Any],
    *,
    materialized_dimensions: frozenset[str] | None = None,
    extra_candidates: Sequence[InjectedCandidate] = (),
) -> list[tuple[str, dict[str, Any]]]:
    raw = deepcopy(dict(baseline_config))
    defaults: dict[str, dict[str, Any]] = {
        "context": {
            "neighbor_units": 0,
            "example_policy": "none",
            "example_top_k": 0,
        },
        "tools": {
            "registered_tools": ["rule_engine", "weak_llm", "strong_llm"],
            "primary_model": "weak",
            "critic_model": None,
        },
        "generation": {
            "temperature": 0,
            "max_input_tokens": 12_000,
            "max_tokens": 2048,
            "response_format": "strict_json",
            "prompt_template": "",
            "budget_policy": {
                "max_provider_tokens": None,
                "max_provider_calls": None,
                "max_cost_microunits": None,
                "max_wall_seconds": None,
            },
        },
        "orchestration": {
            "route": "rule_llm_fusion",
            "fusion_policy": "score_priority",
            "critic_enabled": False,
            "rule_bundle": {},
            "rule_min_confidence": 0.95,
            "critic_confidence_margin": 0.10,
            "critic_max_noncritical_rate": 0.20,
        },
        "memory": {"policy": "none", "top_k": 0},
        "output": {
            "thresholds": {},
            "fallback": "review",
            "schema_validation": True,
            "evidence_validation": True,
            "abstain_threshold": 0.0,
            "review_threshold": 0.7,
        },
    }
    baseline = deepcopy(defaults)
    for section, defaults_for_section in defaults.items():
        supplied = raw.get(section)
        if not isinstance(supplied, Mapping):
            continue
        recognized = {
            key: deepcopy(value) for key, value in supplied.items() if key in defaults_for_section
        }
        baseline[section].update(recognized)

    # One-release compatibility for draft candidates generated by the old optimizer.
    legacy_memory = raw.get("memory")
    if isinstance(legacy_memory, Mapping) and "example_count" in legacy_memory:
        count = int(legacy_memory.get("example_count", 0))
        strategy = str(legacy_memory.get("strategy", "similar"))
        baseline["context"].update(
            {
                "example_policy": "none" if count == 0 else strategy,
                "example_top_k": count if count in {0, 3, 6} else 0,
            }
        )
    legacy_orchestration = raw.get("orchestration")
    if isinstance(legacy_orchestration, Mapping):
        if "fusion" in legacy_orchestration:
            baseline["orchestration"]["fusion_policy"] = legacy_orchestration["fusion"]
        if legacy_orchestration.get("route") == "weak_strong_critic":
            baseline["orchestration"]["route"] = "weak_then_strong_critic"
            baseline["orchestration"]["critic_enabled"] = True
            baseline["tools"]["critic_model"] = "strong"
    if baseline["context"]["example_policy"] == "none":
        baseline["context"]["example_top_k"] = 0
    if baseline["memory"]["policy"] == "none":
        baseline["memory"]["top_k"] = 0
    if baseline["orchestration"]["route"] == "weak_then_strong_critic":
        baseline["orchestration"]["critic_enabled"] = True
        baseline["tools"]["critic_model"] = "strong"
    baseline = materialize_trial_candidate(baseline)

    candidates: list[tuple[str, dict[str, Any]]] = [("baseline", baseline)]
    seen = {canonical_checksum(baseline)}

    def add(mutation: str, candidate: dict[str, Any]) -> None:
        dimension = mutation.split("=", 1)[0].split(".", 1)[0]
        if materialized_dimensions is not None and dimension not in materialized_dimensions:
            return
        checksum = canonical_checksum(candidate)
        if checksum not in seen:
            seen.add(checksum)
            candidates.append((mutation, materialize_trial_candidate(candidate)))

    # Injected candidates are enumerated immediately after the baseline so the
    # max_candidates ceiling truncates the mechanical sweep rather than the compiled
    # prompts that a run was started to evaluate. Sorting keeps the order deterministic
    # regardless of how the caller assembled the list.
    for injected in sorted(
        extra_candidates,
        key=lambda item: (item.mutation, canonical_checksum(dict(item.config))),
    ):
        add(injected.mutation, deepcopy(dict(injected.config)))

    # Context examples/neighbours and semantic memory are intentionally absent:
    # the serving path records those sections but does not materialize retrieval.
    for max_tokens in (256, 512, 1024, 2048):
        candidate = deepcopy(baseline)
        candidate["generation"]["max_tokens"] = max_tokens
        add(f"generation.max_tokens={max_tokens}", candidate)
    for route in (
        "rule_only",
        "weak_llm",
        "weak_then_strong_critic",
        "rule_llm_fusion",
    ):
        candidate = deepcopy(baseline)
        candidate["orchestration"]["route"] = route
        candidate["orchestration"]["critic_enabled"] = route == "weak_then_strong_critic"
        candidate["tools"]["critic_model"] = (
            "strong" if route == "weak_then_strong_critic" else None
        )
        add(f"orchestration.route={route}", candidate)
    for fusion_policy in (
        "rule_priority",
        "score_priority",
        "conflict_to_review",
    ):
        candidate = deepcopy(baseline)
        candidate["orchestration"]["fusion_policy"] = fusion_policy
        add(f"orchestration.fusion_policy={fusion_policy}", candidate)
    for review_threshold in (0.6, 0.7, 0.8):
        candidate = deepcopy(baseline)
        candidate["output"]["review_threshold"] = review_threshold
        candidate["output"]["abstain_threshold"] = min(
            float(candidate["output"]["abstain_threshold"]),
            review_threshold,
        )
        add(f"output.review_threshold={review_threshold}", candidate)
    for abstain_threshold in (0.0, 0.3, 0.5):
        candidate = deepcopy(baseline)
        candidate["output"]["abstain_threshold"] = min(
            abstain_threshold,
            float(candidate["output"]["review_threshold"]),
        )
        add(
            f"output.abstain_threshold={candidate['output']['abstain_threshold']}",
            candidate,
        )
    baseline_thresholds = baseline["output"].get("thresholds", {})
    if isinstance(baseline_thresholds, Mapping) and baseline_thresholds:
        for delta in (-0.05, 0.05):
            candidate = deepcopy(baseline)
            candidate["output"]["thresholds"] = {
                str(key): round(min(1.0, max(0.0, float(value) + delta)), 6)
                for key, value in baseline_thresholds.items()
            }
            add(f"output.thresholds_delta={delta:+.2f}", candidate)
    return candidates


class HarnessTrialExecutor(Protocol):
    """Asynchronous boundary for real PredictionBatch/TagExtractor trial replay."""

    @property
    def materialized_dimensions(self) -> frozenset[str]:
        """Harness dimensions that this executor actually evaluates."""

    async def execute_trial(
        self,
        candidate: dict[str, Any],
        samples: list[dict[str, Any]],
        *,
        optimization_run_id: int | None = None,
        optimization_trial_id: int | None = None,
    ) -> Mapping[str, Any]:
        """Execute one materialized candidate and return measured metrics."""


TrialBudgetReserve = Callable[
    [int, str, str, Mapping[str, int | None]],
    Awaitable[Mapping[str, Any]],
]
TrialBudgetSettle = Callable[
    [Mapping[str, Any], Mapping[str, int]],
    Awaitable[Mapping[str, Any]],
]


@dataclass(frozen=True, slots=True)
class PersistedPredictionTrialExecutor:
    """Replay local output policy over persisted predictions and measured usage.

    This executor deliberately advertises only ``output``. It never assigns
    hard-coded route/model costs to a candidate that was not actually run.
    """

    baseline_thresholds: Mapping[str, Any]
    materialized_dimensions: ClassVar[frozenset[str]] = frozenset({"output"})

    async def execute_trial(
        self,
        candidate: dict[str, Any],
        samples: list[dict[str, Any]],
        *,
        optimization_run_id: int | None = None,
        optimization_trial_id: int | None = None,
    ) -> Mapping[str, Any]:
        del optimization_run_id, optimization_trial_id
        validation = [item for item in samples if item.get("split") == "validation"]
        if not validation:
            validation = samples
        output_policy = candidate.get("output", {})
        candidate_thresholds = output_policy.get("thresholds", {})
        review_threshold = float(output_policy.get("review_threshold", 0.7))

        def accepted_by(
            item: Mapping[str, Any],
            thresholds: Mapping[str, Any],
            default_threshold: float,
        ) -> bool:
            return float(item.get("score", 0)) >= float(
                thresholds.get(
                    str(item.get("subject_tag_key", "")),
                    thresholds.get(
                        str(item.get("tag_key", "")),
                        thresholds.get("default", default_threshold),
                    ),
                )
            )

        accepted = [
            item for item in validation if accepted_by(item, candidate_thresholds, review_threshold)
        ]
        baseline_accepted = [
            item for item in validation if accepted_by(item, self.baseline_thresholds, 0.5)
        ]
        quality = _safe_optimizer_ratio(
            sum(bool(item.get("is_correct")) for item in accepted),
            len(accepted),
        )
        baseline_quality = _safe_optimizer_ratio(
            sum(bool(item.get("is_correct")) for item in baseline_accepted),
            len(baseline_accepted),
        )
        accepted_critical = [item for item in accepted if item.get("is_critical")]
        critical_recall = _safe_optimizer_ratio(
            sum(bool(item.get("is_correct")) for item in accepted_critical),
            sum(bool(item.get("is_critical")) for item in validation),
            empty=1.0,
        )

        measured_executions: dict[str, tuple[int, int | None, float | None, int]] = {}
        usage_complete = True
        for item in validation:
            execution_id = item.get("harness_execution_id")
            provider_tokens = item.get("provider_tokens")
            cost_microunits = item.get("provider_cost_microunits")
            legacy_cost_units = item.get("provider_cost_units")
            latency_ms = item.get("provider_latency_ms")
            if (
                execution_id is None
                or isinstance(provider_tokens, bool)
                or not isinstance(provider_tokens, int)
                or provider_tokens < 0
                or isinstance(latency_ms, bool)
                or not isinstance(latency_ms, int)
                or latency_ms < 0
            ):
                usage_complete = False
                continue
            real_cost: int | None = None
            legacy_cost: float | None = None
            if (
                not isinstance(cost_microunits, bool)
                and isinstance(cost_microunits, int)
                and cost_microunits >= 0
            ):
                real_cost = cost_microunits
            elif (
                not isinstance(legacy_cost_units, bool)
                and isinstance(legacy_cost_units, int | float)
                and float(legacy_cost_units) >= 0
            ):
                # Compatibility only: old Harness rows predate immutable price
                # snapshots. Never convert this proxy into monetary microunits.
                legacy_cost = float(legacy_cost_units)
            else:
                usage_complete = False
                continue
            measured_executions[str(execution_id)] = (
                provider_tokens,
                real_cost,
                legacy_cost,
                latency_ms,
            )
        latencies = sorted(value[3] for value in measured_executions.values())
        p95_latency_ms = (
            latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)] if latencies else None
        )
        provider_tokens = sum(value[0] for value in measured_executions.values())
        real_costs: list[int] = [
            cost
            for _tokens, cost, _legacy_cost, _latency in measured_executions.values()
            if cost is not None
        ]
        real_cost_complete = len(real_costs) == len(measured_executions)
        provider_cost_microunits = (
            sum(real_costs) if real_cost_complete else None
        )
        provider_cost_units = sum(
            float(value[2] or 0.0) for value in measured_executions.values()
        )
        return {
            "measurement_source": "persisted_harness_execution",
            "measurement_complete": usage_complete and bool(measured_executions or not validation),
            "provider_tokens": provider_tokens,
            "provider_cost_microunits": provider_cost_microunits,
            "provider_cost_units": provider_cost_units,
            "cost_measurement_source": (
                "price_snapshot_microunits"
                if real_cost_complete
                else "legacy_cost_units_compatibility"
            ),
            "p95_latency_ms": p95_latency_ms,
            "feasible": critical_recall >= 0.95,
            "quality_delta": quality - baseline_quality,
            "review_rate_delta": (
                _safe_optimizer_ratio(
                    len(validation) - len(accepted),
                    len(validation),
                )
                - _safe_optimizer_ratio(
                    len(validation) - len(baseline_accepted),
                    len(validation),
                )
            ),
            # Output-policy replay performs no new generation. Usage is real
            # absolute baseline usage and therefore has zero generation delta.
            "p95_latency_delta": 0.0,
            "cost_delta": 0.0,
        }


def _eligible_harness_samples(
    feedback_samples: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    eligible = [
        dict(sample)
        for sample in feedback_samples
        if str(sample.get("primary_failure_stage") or "tag_reasoning")
        not in _UPSTREAM_FAILURE_STAGES
    ]
    eligible.sort(
        key=lambda sample: (
            str(sample.get("gold_label_id", "")),
            str(sample.get("subject_type", "")),
            str(sample.get("subject_id", "")),
            str(sample.get("tag_key", "")),
            str(sample.get("predicted_value", "")),
        )
    )
    return eligible, len(feedback_samples) - len(eligible)


def _select_harness_winner(
    trials: Sequence[HarnessOptimizationTrial],
    *,
    objective_policy: str,
) -> HarnessOptimizationTrial:
    if objective_policy not in {"balanced", "quality_first", "efficiency_guarded"}:
        raise GovernanceError("unsupported optimization objective policy")
    feasible = [trial for trial in trials if trial.reward.feasible]
    if not feasible:
        return trials[0]

    def common_tail(trial: HarnessOptimizationTrial) -> tuple[float, float, int]:
        return (
            -trial.reward.review_rate_delta,
            -trial.reward.p95_latency_delta,
            -trial.index,
        )

    def provider_token_delta(trial: HarnessOptimizationTrial) -> float:
        value = trial.metrics.get("provider_token_delta")
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
        return trial.reward.cost_delta

    if objective_policy == "quality_first":
        return max(
            feasible,
            key=lambda trial: (
                trial.reward.quality_delta,
                -provider_token_delta(trial),
                *common_tail(trial),
            ),
        )
    if objective_policy == "efficiency_guarded":
        return max(
            feasible,
            key=lambda trial: (
                -provider_token_delta(trial),
                trial.reward.quality_delta,
                *common_tail(trial),
            ),
        )

    pareto = [
        trial
        for trial in feasible
        if not any(
            (
                other.reward.quality_delta >= trial.reward.quality_delta
                and provider_token_delta(other) <= provider_token_delta(trial)
                and (
                    other.reward.quality_delta > trial.reward.quality_delta
                    or provider_token_delta(other) < provider_token_delta(trial)
                )
            )
            for other in feasible
            if other is not trial
        )
    ]
    quality_values = [trial.reward.quality_delta for trial in pareto]
    cost_values = [provider_token_delta(trial) for trial in pareto]
    quality_min, quality_max = min(quality_values), max(quality_values)
    cost_min, cost_max = min(cost_values), max(cost_values)

    def balanced_key(trial: HarnessOptimizationTrial) -> tuple[float, ...]:
        quality_score = (
            (trial.reward.quality_delta - quality_min) / (quality_max - quality_min)
            if quality_max > quality_min
            else 1.0
        )
        cost_score = (
            (cost_max - provider_token_delta(trial)) / (cost_max - cost_min)
            if cost_max > cost_min
            else 1.0
        )
        return (
            (quality_score + cost_score) / 2,
            trial.reward.quality_delta,
            -provider_token_delta(trial),
            *common_tail(trial),
        )

    return max(pareto, key=balanced_key)


def bounded_harness_search(
    *,
    baseline_config: Mapping[str, Any],
    feedback_samples: Sequence[Mapping[str, Any]],
    evaluator: Callable[
        [dict[str, Any], list[dict[str, Any]]],
        Mapping[str, float | bool],
    ],
    max_candidates: int = 32,
    objective_policy: str = "balanced",
    extra_candidates: Sequence[InjectedCandidate] = (),
) -> HarnessSearchResult:
    """Run a deterministic, one-dimension-at-a-time search with a hard 32-trial cap."""

    if not 1 <= max_candidates <= 32:
        raise GovernanceError("optimizer max_candidates must be between 1 and 32")
    eligible, excluded_count = _eligible_harness_samples(feedback_samples)
    trials: list[HarnessOptimizationTrial] = []
    for index, (mutation, config) in enumerate(
        _bounded_candidate_configs(
            baseline_config,
            extra_candidates=extra_candidates,
        )[:max_candidates]
    ):
        raw_metrics = dict(evaluator(deepcopy(config), deepcopy(eligible)))
        reward = OptimizationReward(
            feasible=(
                bool(raw_metrics.get("feasible", False))
                and raw_metrics.get("measurement_complete") is not False
            ),
            quality_delta=float(raw_metrics.get("quality_delta", 0.0)),
            review_rate_delta=float(raw_metrics.get("review_rate_delta", 0.0)),
            p95_latency_delta=float(raw_metrics.get("p95_latency_delta", 0.0)),
            cost_delta=float(raw_metrics.get("cost_delta", 0.0)),
        )
        trials.append(
            HarnessOptimizationTrial(
                index=index,
                mutation=mutation,
                config=config,
                reward=reward,
                metrics=raw_metrics,
            )
        )
    winner = _select_harness_winner(trials, objective_policy=objective_policy)
    return HarnessSearchResult(
        winner=winner,
        trials=tuple(trials),
        eligible_sample_count=len(eligible),
        excluded_upstream_count=excluded_count,
    )


async def execute_harness_trials(
    *,
    baseline_config: Mapping[str, Any],
    feedback_samples: Sequence[Mapping[str, Any]],
    trial_executor: HarnessTrialExecutor,
    max_candidates: int = 32,
    objective_policy: str = "balanced",
    budget: Mapping[str, Any] | None = None,
    reserve_budget: TrialBudgetReserve | None = None,
    settle_budget: TrialBudgetSettle | None = None,
    optimization_run_id: int | None = None,
    optimization_trial_ids: Sequence[int] | None = None,
    extra_candidates: Sequence[InjectedCandidate] = (),
) -> HarnessSearchResult:
    """Execute materialized candidates through the worker's real trial boundary."""

    if not 1 <= max_candidates <= 32:
        raise GovernanceError("optimizer max_candidates must be between 1 and 32")
    eligible, excluded_count = _eligible_harness_samples(feedback_samples)
    trials: list[HarnessOptimizationTrial] = []
    configs = _bounded_candidate_configs(
        baseline_config,
        materialized_dimensions=trial_executor.materialized_dimensions,
        extra_candidates=extra_candidates,
    )[:max_candidates]
    budget_limits = dict(budget or {})
    max_provider_tokens = budget_limits.get("max_provider_tokens")
    max_provider_calls = budget_limits.get("max_provider_calls")
    max_cost_microunits = budget_limits.get("max_cost_microunits")
    max_wall_seconds = budget_limits.get("max_wall_seconds")
    consumed_provider_tokens = 0
    consumed_provider_calls = 0
    consumed_cost_microunits = 0
    search_started_at = perf_counter()

    def checked_limit(name: str, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise GovernanceError(f"optimizer {name} budget must be a positive integer")
        return value

    max_provider_tokens = checked_limit("max_provider_tokens", max_provider_tokens)
    max_provider_calls = checked_limit("max_provider_calls", max_provider_calls)
    max_cost_microunits = checked_limit(
        "max_cost_microunits",
        max_cost_microunits,
    )
    max_wall_seconds = checked_limit("max_wall_seconds", max_wall_seconds)
    if (reserve_budget is None) != (settle_budget is None):
        raise GovernanceError(
            "optimizer durable budget reserve and settle callbacks must be configured together"
        )
    if optimization_trial_ids is not None and (
        optimization_run_id is None
        or len(optimization_trial_ids) < len(configs)
        or any(
            isinstance(trial_id, bool)
            or not isinstance(trial_id, int)
            or trial_id <= 0
            for trial_id in optimization_trial_ids[: len(configs)]
        )
    ):
        raise GovernanceError(
            "optimizer trial correlation IDs do not match the candidate envelope"
        )

    def estimate_value(
        estimate: Mapping[str, Any],
        name: str,
    ) -> int | None:
        value = estimate.get(name)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise GovernanceError(
                f"trial budget estimator returned invalid {name}"
            )
        return value

    for index, (mutation, config) in enumerate(configs):
        if (
            max_wall_seconds is not None
            and perf_counter() - search_started_at >= max_wall_seconds
        ):
            raise GovernanceError("optimizer budget_exhausted: max_wall_seconds")
        estimate_method = getattr(trial_executor, "estimate_trial_budget", None)
        estimate: Mapping[str, Any] = {}
        if callable(estimate_method):
            raw_estimate = estimate_method(deepcopy(config), deepcopy(eligible))
            if inspect.isawaitable(raw_estimate):
                raw_estimate = await raw_estimate
            if isinstance(raw_estimate, Mapping):
                estimate = raw_estimate

        estimated_tokens = estimate_value(estimate, "provider_tokens")
        estimated_calls = estimate_value(estimate, "provider_calls")
        estimated_cost = estimate_value(estimate, "cost_microunits")
        reservation: Mapping[str, Any] | None = None
        if reserve_budget is not None:
            reservation = await reserve_budget(
                index,
                mutation,
                canonical_checksum(config),
                {
                    "provider_tokens": estimated_tokens,
                    "provider_calls": estimated_calls,
                    "cost_microunits": estimated_cost,
                },
            )
        else:
            if max_provider_tokens is not None and (
                estimated_tokens is None
                or consumed_provider_tokens + estimated_tokens > max_provider_tokens
            ):
                raise GovernanceError("optimizer budget_exhausted: max_provider_tokens")
            if max_provider_calls is not None and (
                estimated_calls is None
                or consumed_provider_calls + estimated_calls > max_provider_calls
            ):
                raise GovernanceError("optimizer budget_exhausted: max_provider_calls")
            if max_cost_microunits is not None and (
                estimated_cost is None
                or consumed_cost_microunits + estimated_cost > max_cost_microunits
            ):
                raise GovernanceError("optimizer budget_exhausted: max_cost_microunits")

        execute_method = trial_executor.execute_trial
        execute_parameters = inspect.signature(execute_method).parameters
        accepts_keywords = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in execute_parameters.values()
        )
        correlation_kwargs: dict[str, int | None] = {}
        if (
            optimization_run_id is not None
            and optimization_trial_ids is not None
            and (
                accepts_keywords
                or {
                    "optimization_run_id",
                    "optimization_trial_id",
                }.issubset(execute_parameters)
            )
        ):
            correlation_kwargs = {
                "optimization_run_id": optimization_run_id,
                "optimization_trial_id": optimization_trial_ids[index],
            }
        raw_metrics = dict(
            await execute_method(
                deepcopy(config),
                deepcopy(eligible),
                **correlation_kwargs,
            )
        )
        measurement_source = raw_metrics.get("measurement_source")
        if not isinstance(measurement_source, str) or not measurement_source:
            raise GovernanceError("trial executor must report a non-empty measurement_source")
        if raw_metrics.get("measurement_complete") is not True:
            # Do not release the durable reservation when any subject failed,
            # usage is unknown, or the ledger is incomplete. The next valid
            # optimizer lease will conservatively promote the outstanding
            # envelope to consumed usage before admitting another call.
            raise GovernanceError(
                "trial measurement is incomplete; provider budget reservation retained"
            )
        actual_tokens = int(raw_metrics.get("provider_input_tokens", 0)) + int(
            raw_metrics.get("provider_output_tokens", 0)
        )
        actual_calls = int(raw_metrics.get("provider_calls", 0))
        actual_cost = int(raw_metrics.get("cost_microunits", 0))
        if min(actual_tokens, actual_calls, actual_cost) < 0:
            raise GovernanceError("trial executor returned negative provider usage")
        if settle_budget is not None:
            assert reservation is not None
            aggregate_budget = await settle_budget(
                reservation,
                {
                    "provider_tokens": actual_tokens,
                    "provider_calls": actual_calls,
                    "cost_microunits": actual_cost,
                },
            )
            raw_metrics["aggregate_budget"] = dict(aggregate_budget)
        else:
            consumed_provider_tokens += actual_tokens
            consumed_provider_calls += actual_calls
            consumed_cost_microunits += actual_cost
            raw_metrics["aggregate_budget"] = {
                "provider_tokens": consumed_provider_tokens,
                "provider_calls": consumed_provider_calls,
                "cost_microunits": consumed_cost_microunits,
                "wall_seconds": round(perf_counter() - search_started_at, 6),
            }
            if (
                (
                    max_provider_tokens is not None
                    and consumed_provider_tokens > max_provider_tokens
                )
                or (
                    max_provider_calls is not None
                    and consumed_provider_calls > max_provider_calls
                )
                or (
                    max_cost_microunits is not None
                    and consumed_cost_microunits > max_cost_microunits
                )
                or (
                    max_wall_seconds is not None
                    and perf_counter() - search_started_at > max_wall_seconds
                )
            ):
                raise GovernanceError("optimizer budget_exhausted during trial settlement")
        reward = OptimizationReward(
            feasible=(
                bool(raw_metrics.get("feasible", False))
                and raw_metrics.get("measurement_complete") is not False
            ),
            quality_delta=float(raw_metrics.get("quality_delta", 0.0)),
            review_rate_delta=float(raw_metrics.get("review_rate_delta", 0.0)),
            p95_latency_delta=float(raw_metrics.get("p95_latency_delta", 0.0)),
            cost_delta=float(raw_metrics.get("cost_delta", 0.0)),
        )
        trials.append(
            HarnessOptimizationTrial(
                index=index,
                mutation=mutation,
                config=config,
                reward=reward,
                metrics=raw_metrics,
            )
        )
    if not any(trial.reward.feasible for trial in trials):
        raise GovernanceError(
            "optimizer produced no candidate that passed measured quality and efficiency gates"
        )
    winner = _select_harness_winner(trials, objective_policy=objective_policy)
    return HarnessSearchResult(
        winner=winner,
        trials=tuple(trials),
        eligible_sample_count=len(eligible),
        excluded_upstream_count=excluded_count,
    )


_HARNESS_DIMENSIONS = (
    "context",
    "tools",
    "generation",
    "orchestration",
    "memory",
    "output",
)


def _numeric_deltas(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> dict[str, float]:
    """Return stable ``right - left`` deltas for shared finite numeric fields."""

    deltas: dict[str, float] = {}
    for key in sorted(set(left).intersection(right)):
        left_value = left[key]
        right_value = right[key]
        if (
            isinstance(left_value, bool)
            or isinstance(right_value, bool)
            or not isinstance(left_value, int | float)
            or not isinstance(right_value, int | float)
        ):
            continue
        left_number = float(left_value)
        right_number = float(right_value)
        if not math.isfinite(left_number) or not math.isfinite(right_number):
            continue
        deltas[str(key)] = round(right_number - left_number, 12)
    return deltas


def _optimizer_reward_from_mapping(
    value: Mapping[str, Any],
) -> OptimizationReward | None:
    if "feasible" not in value:
        return None
    try:
        reward = OptimizationReward(
            feasible=bool(value["feasible"]),
            quality_delta=float(value.get("quality_delta", 0.0)),
            review_rate_delta=float(value.get("review_rate_delta", 0.0)),
            p95_latency_delta=float(value.get("p95_latency_delta", 0.0)),
            cost_delta=float(value.get("cost_delta", 0.0)),
        )
    except (TypeError, ValueError):
        return None
    if not all(
        math.isfinite(item)
        for item in (
            reward.quality_delta,
            reward.review_rate_delta,
            reward.p95_latency_delta,
            reward.cost_delta,
        )
    ):
        return None
    return reward


def build_candidate_comparison(
    *,
    left_trial_id: int,
    right_trial_id: int,
    left_spec: Mapping[str, Any],
    right_spec: Mapping[str, Any],
    left_metrics: Mapping[str, Any],
    right_metrics: Mapping[str, Any],
    left_reward: Mapping[str, Any],
    right_reward: Mapping[str, Any],
    left_badcase_count: int = 0,
    right_badcase_count: int = 0,
) -> dict[str, Any]:
    """Build the deterministic six-dimension comparison consumed by API and UI."""

    if left_trial_id == right_trial_id:
        raise GovernanceError("candidate comparison requires two different trials")
    dimensions = [
        {
            "dimension": dimension,
            "before": deepcopy(left_spec.get(dimension, {})),
            "after": deepcopy(right_spec.get(dimension, {})),
        }
        for dimension in _HARNESS_DIMENSIONS
    ]
    left_count = max(0, int(left_badcase_count))
    right_count = max(0, int(right_badcase_count))
    left_rank = _optimizer_reward_from_mapping(left_reward)
    right_rank = _optimizer_reward_from_mapping(right_reward)
    recommended_trial_id: int | None = None
    recommendation_basis = "insufficient_completed_reward"
    if left_rank is not None and right_rank is not None:
        if right_rank.rank_key > left_rank.rank_key:
            recommended_trial_id = right_trial_id
        elif left_rank.rank_key > right_rank.rank_key:
            recommended_trial_id = left_trial_id
        else:
            recommended_trial_id = min(left_trial_id, right_trial_id)
        recommendation_basis = "feasibility_then_quality_review_latency_cost"
    return {
        "dimensions": dimensions,
        "metric_deltas": _numeric_deltas(left_metrics, right_metrics),
        "reward_deltas": _numeric_deltas(left_reward, right_reward),
        "improved_badcase_count": max(0, left_count - right_count),
        "regressed_badcase_count": max(0, right_count - left_count),
        "recommendation": {
            "trial_id": recommended_trial_id,
            "basis": recommendation_basis,
        },
        "status": "success" if recommended_trial_id is not None else "warning",
        "summary": (
            f"trial {recommended_trial_id} ranks first"
            if recommended_trial_id is not None
            else "candidate rewards are not complete enough to recommend a winner"
        ),
        "next_actions": (
            ["evaluate_recommended_candidate"]
            if recommended_trial_id is not None
            else ["complete_candidate_evaluation"]
        ),
        "artifacts": [
            f"tag_optimization_trial:{left_trial_id}",
            f"tag_optimization_trial:{right_trial_id}",
        ],
    }


def evaluate_quality_gates(
    *,
    metrics: dict[str, float],
    baseline: dict[str, float],
    supported_label_f1: dict[str, float],
    baseline_label_f1: dict[str, float],
) -> GateEvaluation:
    """Evaluate the fixed V1 release gates without consulting mutable state."""

    macro_f1 = float(metrics.get("macro_f1", 0))
    baseline_macro = float(baseline.get("macro_f1", macro_f1))
    critical_recall = float(metrics.get("critical_recall", 0))
    baseline_critical = float(baseline.get("critical_recall", critical_recall))
    enforce_critical_lcb = bool(metrics.get("critical_lcb_enforced", 0))
    critical_gate_value = (
        float(metrics.get("critical_recall_lcb", 0)) if enforce_critical_lcb else critical_recall
    )
    evidence_coverage = float(metrics.get("evidence_coverage", 0))
    error_rate = float(metrics.get("error_rate", 1))
    schema_violations = float(metrics.get("schema_violation_count", 0))
    evidence_violations = float(metrics.get("evidence_violation_count", 0))
    lineage_violations = float(metrics.get("lineage_violation_count", 0))
    gates: list[Gate] = [
        Gate(
            "schema_integrity",
            schema_violations == 0,
            schema_violations,
            0.0,
            "schema violations must be zero",
        ),
        Gate(
            "evidence_integrity",
            evidence_violations == 0,
            evidence_violations,
            0.0,
            "evidence constraint violations must be zero",
        ),
        Gate(
            "lineage_integrity",
            lineage_violations == 0,
            lineage_violations,
            0.0,
            "lineage violations must be zero",
        ),
        Gate(
            "macro_f1",
            macro_f1 >= 0.80 and macro_f1 >= baseline_macro - 0.01,
            macro_f1,
            max(0.80, baseline_macro - 0.01),
            "macro-F1 must meet the absolute and non-regression thresholds",
        ),
        Gate(
            "critical_recall",
            critical_gate_value >= CRITICAL_RECALL_LCB_THRESHOLD
            and critical_recall >= baseline_critical - 0.01,
            critical_gate_value,
            max(CRITICAL_RECALL_LCB_THRESHOLD, baseline_critical - 0.01),
            (
                "critical-value recall Wilson lower bound must be at least 95%"
                if enforce_critical_lcb
                else "critical recall must meet the absolute and non-regression thresholds"
            ),
        ),
        Gate(
            "evidence_coverage",
            evidence_coverage >= 0.98,
            evidence_coverage,
            0.98,
            "required-evidence coverage must be at least 98%",
        ),
        Gate(
            "error_rate",
            error_rate < 0.01,
            error_rate,
            0.01,
            "extraction error rate must stay below 1%",
        ),
    ]
    if "paired_accuracy_delta_lcb" in metrics:
        paired_lcb = float(metrics["paired_accuracy_delta_lcb"])
        gates.append(
            Gate(
                "paired_non_regression",
                paired_lcb >= -0.01,
                paired_lcb,
                -0.01,
                "paired candidate-baseline quality lower bound must be at least -1pp",
            )
        )
    for label_key, value in sorted(supported_label_f1.items()):
        baseline_value = baseline_label_f1.get(label_key)
        if baseline_value is None:
            continue
        threshold = float(baseline_value) - 0.01
        gates.append(
            Gate(
                f"label_f1:{label_key}",
                float(value) >= threshold,
                float(value),
                threshold,
                f"{label_key} F1 must not regress by more than one percentage point",
            )
        )
    return GateEvaluation(all(gate.passed for gate in gates), tuple(gates))


class TagGovernanceService:
    """Application service; every mutation is tenant-scoped and transactional."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        optimization_trial_executor: HarnessTrialExecutor | None = None,
    ) -> None:
        self._factory = session_factory
        self._optimization_trial_executor = optimization_trial_executor

    def _optimization_materialized_dimensions(self) -> frozenset[str]:
        if self._optimization_trial_executor is not None:
            return self._optimization_trial_executor.materialized_dimensions
        # Run creation can happen in the API process, while execution belongs
        # to the worker that owns TagExtractor. Persist every serving dimension
        # up front so the worker never receives an output-only trial envelope.
        return frozenset({"generation", "orchestration", "output"})

    @staticmethod
    def _optimization_budget_counter(
        budget: Mapping[str, Any],
        name: str,
    ) -> int:
        value = budget.get(name, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise GovernanceConflictError(
                f"optimization run contains an invalid {name} counter"
            )
        return value

    @staticmethod
    def _validate_optimization_budget_scope(
        *,
        run: TagOptimizationRun | None,
        job: TagExtractionJob | None,
        tenant_id: str,
        optimization_run_id: int,
        optimization_job_id: int,
        gold_set_version_id: int,
        production_tagger_version_id: int,
        search_manifest_checksum: str,
        lease_owner: str,
        lease_token: str,
        worker_id: str | None,
        now: datetime,
    ) -> tuple[TagOptimizationRun, TagExtractionJob]:
        if (
            run is None
            or run.status != "running"
            or run.phase != "search"
            or run.gold_set_version_id != gold_set_version_id
            or run.baseline_tagger_version_id != production_tagger_version_id
            or run.summary.get("search_manifest_checksum")
            != search_manifest_checksum
        ):
            raise GovernanceConflictError(
                "optimization run changed while trials were executing"
            )
        if (
            job is None
            or job.id != optimization_job_id
            or job.tenant_id != tenant_id
            or job.status != "running"
            or job.job_type != "optimize"
            or job.scope.get("optimization_run_id") != optimization_run_id
            or job.lease_owner != lease_owner
            or job.lease_token != lease_token
            or (worker_id is not None and job.lease_owner != worker_id)
            or job.lease_expires_at is None
            or _aware_utc(job.lease_expires_at) < now
        ):
            raise GovernanceConflictError(
                "optimization job lease changed while trials were executing"
            )
        return run, job

    async def _reserve_optimization_trial_budget(
        self,
        *,
        tenant_id: str,
        optimization_run_id: int,
        optimization_job_id: int,
        gold_set_version_id: int,
        production_tagger_version_id: int,
        search_manifest_checksum: str,
        lease_owner: str,
        lease_token: str,
        worker_id: str | None,
        trial_index: int,
        mutation: str,
        candidate_checksum: str,
        estimate: Mapping[str, int | None],
    ) -> dict[str, Any]:
        """Atomically reserve one Provider-backed optimizer trial.

        An outstanding reservation means the previous process may have reached
        the Provider before crashing.  The next valid lease therefore promotes
        the entire conservative reservation to consumed usage before it tries
        to reserve more.
        """

        now = _utcnow()
        async with self._factory() as session:
            run = (
                await session.execute(
                    select(TagOptimizationRun)
                    .where(
                        TagOptimizationRun.id == optimization_run_id,
                        TagOptimizationRun.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            job = (
                await session.execute(
                    select(TagExtractionJob)
                    .where(
                        TagExtractionJob.id == optimization_job_id,
                        TagExtractionJob.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            run, _job = self._validate_optimization_budget_scope(
                run=run,
                job=job,
                tenant_id=tenant_id,
                optimization_run_id=optimization_run_id,
                optimization_job_id=optimization_job_id,
                gold_set_version_id=gold_set_version_id,
                production_tagger_version_id=production_tagger_version_id,
                search_manifest_checksum=search_manifest_checksum,
                lease_owner=lease_owner,
                lease_token=lease_token,
                worker_id=worker_id,
                now=now,
            )
            budget = deepcopy(dict(run.search_budget))
            dimensions = (
                ("provider_tokens", "max_provider_tokens"),
                ("provider_calls", "max_provider_calls"),
                ("cost_microunits", "max_cost_microunits"),
            )
            consumed = {
                name: self._optimization_budget_counter(
                    budget,
                    f"consumed_{name}",
                )
                for name, _limit_name in dimensions
            }
            reserved = {
                name: self._optimization_budget_counter(
                    budget,
                    f"reserved_{name}",
                )
                for name, _limit_name in dimensions
            }
            previous_reservation = budget.get("reservation")
            if previous_reservation is not None:
                if not isinstance(previous_reservation, Mapping):
                    raise GovernanceConflictError(
                        "optimization run contains an invalid trial reservation"
                    )
                for name, _limit_name in dimensions:
                    consumed[name] += reserved[name]
                    reserved[name] = 0
                budget["last_abandoned_reservation"] = deepcopy(
                    dict(previous_reservation)
                )
                budget["abandoned_reservation_count"] = (
                    self._optimization_budget_counter(
                        budget,
                        "abandoned_reservation_count",
                    )
                    + 1
                )
                budget["reservation"] = None

            started_at_value = budget.get("started_at")
            if started_at_value is None:
                started_at = now
                budget["started_at"] = now.isoformat()
            elif isinstance(started_at_value, str):
                try:
                    started_at = datetime.fromisoformat(
                        started_at_value.replace("Z", "+00:00")
                    )
                except ValueError as exc:
                    raise GovernanceConflictError(
                        "optimization run contains an invalid started_at budget value"
                    ) from exc
                if started_at.tzinfo is None:
                    raise GovernanceConflictError(
                        "optimization run budget started_at must include a timezone"
                    )
                started_at = started_at.astimezone(UTC)
            else:
                raise GovernanceConflictError(
                    "optimization run contains an invalid started_at budget value"
                )

            exhausted_reason: str | None = None
            max_wall_seconds = budget.get("max_wall_seconds")
            if max_wall_seconds is not None:
                if (
                    isinstance(max_wall_seconds, bool)
                    or not isinstance(max_wall_seconds, int)
                    or max_wall_seconds <= 0
                ):
                    raise GovernanceConflictError(
                        "optimization run contains an invalid max_wall_seconds budget"
                    )
                if (now - started_at).total_seconds() >= max_wall_seconds:
                    exhausted_reason = "max_wall_seconds"

            normalized_estimate: dict[str, int] = {}
            for name, limit_name in dimensions:
                value = estimate.get(name)
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                ):
                    raise GovernanceError(
                        f"trial budget estimator returned invalid {name}"
                    )
                limit = budget.get(limit_name)
                if limit is not None and (
                    isinstance(limit, bool)
                    or not isinstance(limit, int)
                    or limit <= 0
                ):
                    raise GovernanceConflictError(
                        f"optimization run contains an invalid {limit_name} budget"
                    )
                if limit is not None and value is None:
                    exhausted_reason = exhausted_reason or limit_name
                normalized_estimate[name] = int(value or 0)
                if (
                    limit is not None
                    and consumed[name] + normalized_estimate[name] > limit
                ):
                    exhausted_reason = exhausted_reason or limit_name

            for name, _limit_name in dimensions:
                budget[f"consumed_{name}"] = consumed[name]
                budget[f"reserved_{name}"] = 0
            if exhausted_reason is not None:
                budget["budget_exhausted_at"] = now.isoformat()
                budget["budget_exhausted_reason"] = exhausted_reason
                run.search_budget = budget
                await session.commit()
                raise GovernanceError(
                    f"optimizer budget_exhausted: {exhausted_reason}"
                )

            sequence = self._optimization_budget_counter(
                budget,
                "reservation_sequence",
            ) + 1
            reservation_id = canonical_checksum(
                {
                    "optimization_run_id": optimization_run_id,
                    "search_manifest_checksum": search_manifest_checksum,
                    "sequence": sequence,
                    "trial_index": trial_index,
                    "candidate_checksum": candidate_checksum,
                    "lease_token": lease_token,
                }
            )
            reservation = {
                "id": reservation_id,
                "sequence": sequence,
                "trial_index": trial_index,
                "mutation": mutation,
                "candidate_checksum": candidate_checksum,
                "search_manifest_checksum": search_manifest_checksum,
                "provider_tokens": normalized_estimate["provider_tokens"],
                "provider_calls": normalized_estimate["provider_calls"],
                "cost_microunits": normalized_estimate["cost_microunits"],
                "reserved_at": now.isoformat(),
            }
            for name, _limit_name in dimensions:
                budget[f"reserved_{name}"] = normalized_estimate[name]
            budget["reservation_sequence"] = sequence
            budget["reservation"] = reservation
            budget.pop("budget_exhausted_at", None)
            budget.pop("budget_exhausted_reason", None)
            run.search_budget = budget
            await session.commit()
            return deepcopy(reservation)

    async def _settle_optimization_trial_budget(
        self,
        *,
        tenant_id: str,
        optimization_run_id: int,
        optimization_job_id: int,
        gold_set_version_id: int,
        production_tagger_version_id: int,
        search_manifest_checksum: str,
        lease_owner: str,
        lease_token: str,
        worker_id: str | None,
        reservation: Mapping[str, Any],
        actual: Mapping[str, int],
    ) -> dict[str, Any]:
        """Atomically replace one conservative reservation with actual usage."""

        now = _utcnow()
        async with self._factory() as session:
            run = (
                await session.execute(
                    select(TagOptimizationRun)
                    .where(
                        TagOptimizationRun.id == optimization_run_id,
                        TagOptimizationRun.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            job = (
                await session.execute(
                    select(TagExtractionJob)
                    .where(
                        TagExtractionJob.id == optimization_job_id,
                        TagExtractionJob.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            run, _job = self._validate_optimization_budget_scope(
                run=run,
                job=job,
                tenant_id=tenant_id,
                optimization_run_id=optimization_run_id,
                optimization_job_id=optimization_job_id,
                gold_set_version_id=gold_set_version_id,
                production_tagger_version_id=production_tagger_version_id,
                search_manifest_checksum=search_manifest_checksum,
                lease_owner=lease_owner,
                lease_token=lease_token,
                worker_id=worker_id,
                now=now,
            )
            budget = deepcopy(dict(run.search_budget))
            persisted_reservation = budget.get("reservation")
            if (
                not isinstance(persisted_reservation, Mapping)
                or persisted_reservation.get("id") != reservation.get("id")
                or persisted_reservation.get("candidate_checksum")
                != reservation.get("candidate_checksum")
            ):
                raise GovernanceConflictError(
                    "optimization trial budget reservation changed before settlement"
                )

            dimensions = (
                ("provider_tokens", "max_provider_tokens"),
                ("provider_calls", "max_provider_calls"),
                ("cost_microunits", "max_cost_microunits"),
            )
            consumed: dict[str, int] = {}
            exhausted_reason: str | None = None
            for name, limit_name in dimensions:
                value = actual.get(name)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise GovernanceError(
                        f"trial executor returned invalid {name} usage"
                    )
                consumed[name] = (
                    self._optimization_budget_counter(
                        budget,
                        f"consumed_{name}",
                    )
                    + value
                )
                limit = budget.get(limit_name)
                if limit is not None and consumed[name] > limit:
                    exhausted_reason = exhausted_reason or limit_name
                budget[f"consumed_{name}"] = consumed[name]
                budget[f"reserved_{name}"] = 0

            started_at_value = budget.get("started_at")
            if not isinstance(started_at_value, str):
                raise GovernanceConflictError(
                    "optimization run contains an invalid started_at budget value"
                )
            try:
                started_at = datetime.fromisoformat(
                    started_at_value.replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise GovernanceConflictError(
                    "optimization run contains an invalid started_at budget value"
                ) from exc
            if started_at.tzinfo is None:
                raise GovernanceConflictError(
                    "optimization run budget started_at must include a timezone"
                )
            wall_seconds = max(
                0.0,
                (now - started_at.astimezone(UTC)).total_seconds(),
            )
            max_wall_seconds = budget.get("max_wall_seconds")
            if max_wall_seconds is not None and wall_seconds > max_wall_seconds:
                exhausted_reason = exhausted_reason or "max_wall_seconds"

            budget["reservation"] = None
            budget["last_settled_reservation"] = deepcopy(
                dict(persisted_reservation)
            )
            budget["last_settled_at"] = now.isoformat()
            if exhausted_reason is not None:
                budget["budget_exhausted_at"] = now.isoformat()
                budget["budget_exhausted_reason"] = exhausted_reason
            else:
                budget.pop("budget_exhausted_at", None)
                budget.pop("budget_exhausted_reason", None)
            run.search_budget = budget
            await session.commit()
            if exhausted_reason is not None:
                raise GovernanceError(
                    "optimizer budget_exhausted during trial settlement: "
                    f"{exhausted_reason}"
                )
            return {
                "provider_tokens": consumed["provider_tokens"],
                "provider_calls": consumed["provider_calls"],
                "cost_microunits": consumed["cost_microunits"],
                "wall_seconds": round(wall_seconds, 6),
            }

    async def _audit(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: int,
        action: str,
        actor_user_id: int,
        payload: dict[str, Any] | None = None,
    ) -> None:
        session.add(
            TagGovernanceAuditEvent(
                tenant_id=tenant_id,
                resource_type=resource_type,
                resource_id=resource_id,
                action=action,
                actor_user_id=actor_user_id,
                payload=payload or {},
                occurred_at=_utcnow(),
            )
        )

    @staticmethod
    async def _lock_review_serialization_scope(
        session: AsyncSession,
        *,
        tenant_id: str,
        task: TagReviewTask,
    ) -> None:
        """Lock one stable parent row before reading sibling review state.

        Sibling tasks are separate rows, so locking only the requested task
        cannot serialize two reviewers claiming or resolving both blind
        rounds concurrently.  Every supported review subject belongs to one
        reception; that reception is the stable, deadlock-free lock key.
        """

        if task.reception_id is not None:
            reception_id = (
                await session.execute(
                    select(Reception.id)
                    .where(
                        Reception.id == task.reception_id,
                        Reception.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if reception_id is None:
                raise GovernanceNotFoundError("review reception not found")
            return
        if task.schema_version_id is not None:
            schema_version_id = (
                await session.execute(
                    select(TagSchemaVersion.id)
                    .where(
                        TagSchemaVersion.id == task.schema_version_id,
                        TagSchemaVersion.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if schema_version_id is None:
                raise GovernanceNotFoundError("review schema version not found")
            return
        await session.execute(
            select(TagReviewTask.id)
            .where(
                TagReviewTask.id == task.id,
                TagReviewTask.tenant_id == tenant_id,
            )
            .with_for_update()
        )

    @staticmethod
    async def _certified_adjudication_predecessors(
        session: AsyncSession,
        *,
        tenant_id: str,
        task: TagReviewTask,
        adjudicator_user_id: int | None = None,
    ) -> list[tuple[TagReviewDecision, TagReviewTask]]:
        """Return the exact two certified blind-T2 rounds behind a T3 task."""

        if task.reason != "adjudication" or not task.blind_mode or not task.review_bundle_id:
            raise GovernanceConflictError(
                "T3 adjudication requires two blind T2 independent rounds"
            )
        rows = list(
            (
                await session.execute(
                    select(TagReviewDecision, TagReviewTask)
                    .join(
                        TagReviewTask,
                        TagReviewTask.id == TagReviewDecision.task_id,
                    )
                    .where(
                        TagReviewDecision.tenant_id == tenant_id,
                        TagReviewTask.tenant_id == tenant_id,
                        TagReviewTask.review_bundle_id == task.review_bundle_id,
                        TagReviewTask.subject_type == task.subject_type,
                        TagReviewTask.subject_id == task.subject_id,
                        TagReviewTask.tag_key == task.tag_key,
                        TagReviewTask.id != task.id,
                        TagReviewDecision.adjudication.is_(False),
                    )
                    .order_by(TagReviewDecision.id)
                )
            ).all()
        )
        predecessors = [(row[0], row[1]) for row in rows]
        reviewers = {decision.reviewer_user_id for decision, _prior_task in predecessors}
        rounds = {int(decision.annotator_round) for decision, _prior_task in predecessors}
        task_ids = {prior_task.id for _decision, prior_task in predecessors}
        valid = (
            len(predecessors) == 2
            and len(reviewers) == 2
            and len(task_ids) == 2
            and rounds == {1, 2}
            and all(
                prior_task.blind_mode
                and _is_double_blind_review(
                    reason=str(prior_task.reason),
                    selection_policy=str(prior_task.selection_policy),
                )
                and prior_task.status == "resolved"
                and decision.truth_tier == "t2"
                and decision.truth_state in {"present", "absent", "not_applicable"}
                for decision, prior_task in predecessors
            )
        )
        if not valid:
            raise GovernanceConflictError(
                "T3 adjudication requires two blind T2 independent rounds"
            )
        if adjudicator_user_id is not None and adjudicator_user_id in reviewers:
            raise GovernanceConflictError("adjudicator must differ from both prior reviewers")
        source_lineages = {
            (
                prior_task.source_deployment_id,
                prior_task.source_extraction_run_id,
                prior_task.source_harness_execution_id,
                prior_task.sampled_deployment_stage,
                prior_task.sampled_deployment_revision,
                prior_task.sampling_manifest_checksum,
                prior_task.selection_policy,
                prior_task.selection_policy_version,
                prior_task.sampling_probability,
            )
            for _decision, prior_task in predecessors
        }
        adjudication_lineage = (
            task.source_deployment_id,
            task.source_extraction_run_id,
            task.source_harness_execution_id,
            task.sampled_deployment_stage,
            task.sampled_deployment_revision,
            task.sampling_manifest_checksum,
            task.selection_policy,
            task.selection_policy_version,
            task.sampling_probability,
        )
        if len(source_lineages) != 1 or adjudication_lineage not in source_lineages:
            raise GovernanceConflictError(
                "T3 adjudication predecessor source lineage is inconsistent"
            )
        return predecessors

    @staticmethod
    async def _linked_feedback_badcases(
        session: AsyncSession,
        *,
        event: TagFeedbackEvent,
    ) -> list[TagBadcase]:
        """Find direct and legacy-clustered materializations of one feedback event."""

        candidates = list(
            (
                await session.execute(
                    select(TagBadcase)
                    .where(
                        TagBadcase.tenant_id == event.tenant_id,
                        TagBadcase.tag_key == event.tag_key,
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        return [
            badcase
            for badcase in candidates
            if badcase.source_feedback_event_id == event.id
            or (
                isinstance(badcase.root_cause, dict)
                and badcase.root_cause.get("latest_feedback_event_id") == event.id
            )
        ]

    async def _isolate_feedback_learning(
        self,
        session: AsyncSession,
        *,
        event: TagFeedbackEvent,
        dataset_split: str,
    ) -> None:
        """Make any legacy T3 materialization unusable without mutating its source event."""

        badcases = await self._linked_feedback_badcases(session, event=event)
        badcase_ids = [badcase.id for badcase in badcases]
        for badcase in badcases:
            badcase.dataset_split = dataset_split
            badcase.status = "ignored"
            badcase.fix_candidate_tagger_version_id = None
            badcase.root_cause = {
                **dict(badcase.root_cause),
                "dataset_split": dataset_split,
                "learning_isolated": True,
            }
        experience_predicate = TagExperienceCase.source_feedback_event_id == event.id
        if badcase_ids:
            experience_predicate = or_(
                experience_predicate,
                TagExperienceCase.source_badcase_id.in_(badcase_ids),
            )
        experiences = list(
            (
                await session.execute(
                    select(TagExperienceCase)
                    .where(
                        TagExperienceCase.tenant_id == event.tenant_id,
                        experience_predicate,
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for experience in experiences:
            experience.dataset_split = dataset_split
            experience.eligible = False

    @staticmethod
    async def _ensure_feedback_remediation(
        session: AsyncSession,
        *,
        event: TagFeedbackEvent,
        task: TagReviewTask,
        actor_user_id: int,
    ) -> TagExtractionJob | None:
        """Route upstream failures independently of semantic-learning visibility."""

        failure_stage = str(event.error_stage or "tag_reasoning")
        if failure_stage not in _UPSTREAM_FAILURE_STAGES:
            return None
        if event.id is None:
            await session.flush()
        idempotency_key = f"feedback-remediation:{event.id}"
        existing = (
            await session.execute(
                select(TagExtractionJob)
                .where(
                    TagExtractionJob.tenant_id == event.tenant_id,
                    TagExtractionJob.idempotency_key == idempotency_key,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        remediation_reception_id = (
            int(task.reception_id)
            if task.reception_id is not None
            else (event.subject_id if event.subject_type == "reception" else None)
        )
        scope_key = "reception_ids" if remediation_reception_id is not None else "dialogue_unit_ids"
        scope_subject_id = (
            remediation_reception_id if remediation_reception_id is not None else event.subject_id
        )
        remediation_job = TagExtractionJob(
            tenant_id=str(event.tenant_id),
            job_type="remediate",
            origin="system",
            status="queued",
            scope={
                scope_key: [scope_subject_id],
                "source_feedback_event_id": event.id,
                "failure_stage": failure_stage,
            },
            tagger_version_id=task.tagger_version_id,
            idempotency_key=idempotency_key,
            total_items=1,
            completed_items=0,
            failed_items=0,
            failed_subset=[],
            attempt_count=0,
            max_attempts=3,
            revision=1,
            created_by=actor_user_id,
        )
        session.add(remediation_job)
        await session.flush()
        return remediation_job

    async def _assign_feedback_lane(
        self,
        session: AsyncSession,
        *,
        decision: TagReviewDecision,
        task: TagReviewTask,
        gold_label: TagGoldLabel,
        gold_set_version_id: int,
        dataset_split: str,
        actor_user_id: int,
    ) -> None:
        """Bind immutable feedback to its frozen lane, then materialize only visible lanes."""

        event = (
            await session.execute(
                select(TagFeedbackEvent)
                .where(
                    TagFeedbackEvent.tenant_id == decision.tenant_id,
                    TagFeedbackEvent.review_decision_id == decision.id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if event is None:
            return
        assignment = (
            await session.execute(
                select(TagFeedbackLaneAssignment)
                .where(
                    TagFeedbackLaneAssignment.tenant_id == event.tenant_id,
                    TagFeedbackLaneAssignment.feedback_event_id == event.id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if assignment is None:
            assignment = TagFeedbackLaneAssignment(
                tenant_id=str(event.tenant_id),
                feedback_event_id=event.id,
                source_gold_label_id=gold_label.id,
                gold_set_version_id=gold_set_version_id,
                split=dataset_split,
                assigned_by=actor_user_id,
                assigned_at=_utcnow(),
            )
            session.add(assignment)
            await session.flush()
        elif assignment.split != dataset_split:
            raise GovernanceConflictError(
                "feedback event cannot be assigned to multiple dataset lanes"
            )

        if event.truth_tier != "t3":
            return
        # Upstream remediation is operational routing, not Harness learning. Run it
        # before a holdout lane is isolated; the helper is idempotent when review
        # submission already queued the job.
        await self._materialize_feedback_learning(
            session,
            event=event,
            task=task,
            actor_user_id=actor_user_id,
        )
        if dataset_split not in _LEARNING_DATASET_SPLITS:
            await self._isolate_feedback_learning(
                session,
                event=event,
                dataset_split=dataset_split,
            )

    async def _materialize_feedback_learning(
        self,
        session: AsyncSession,
        *,
        event: TagFeedbackEvent,
        task: TagReviewTask,
        actor_user_id: int,
    ) -> tuple[TagBadcase | None, TagExperienceCase | None, TagExtractionJob | None]:
        """Persist the durable failure record and its strictly routed follow-up."""

        if event.id is None:
            await session.flush()
        remediation_job = await self._ensure_feedback_remediation(
            session,
            event=event,
            task=task,
            actor_user_id=actor_user_id,
        )
        dataset_split = "operational"
        if event.truth_tier == "t3":
            assigned_split = (
                await session.execute(
                    select(TagFeedbackLaneAssignment.split).where(
                        TagFeedbackLaneAssignment.tenant_id == event.tenant_id,
                        TagFeedbackLaneAssignment.feedback_event_id == event.id,
                    )
                )
            ).scalar_one_or_none()
            if assigned_split not in _LEARNING_DATASET_SPLITS:
                return None, None, remediation_job
            dataset_split = str(assigned_split)
        failure_stage = str(event.error_stage or "tag_reasoning")
        correction = event.correction if isinstance(event.correction, dict) else {}
        reason_code = str(correction.get("reason_code") or "review_feedback")
        action = str(correction.get("action") or "unknown")
        failure_mode = f"{action}:{reason_code}"[:96]
        signature_hash = canonical_checksum(
            {
                "subject_type": event.subject_type,
                "tag_key": event.tag_key,
                "failure_stage": failure_stage,
                "failure_mode": failure_mode,
                "truth_state": event.truth_state,
            }
        )
        now = event.occurred_at
        badcase_predicates: list[Any] = [
            TagBadcase.tenant_id == event.tenant_id,
            TagBadcase.status.in_(
                ["open", "candidate_fix", "verified", "resolved", "reopened", "ignored"]
            ),
        ]
        if event.truth_tier == "t3":
            badcase_predicates.append(TagBadcase.source_feedback_event_id == event.id)
        else:
            badcase_predicates.extend(
                [
                    TagBadcase.signature_hash == signature_hash,
                    TagBadcase.dataset_split == dataset_split,
                ]
            )
        badcase = (
            await session.execute(
                select(TagBadcase)
                .where(*badcase_predicates)
                .order_by(TagBadcase.id)
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        root_cause = {
            "latest_feedback_event_id": event.id,
            "primary_failure_stage": failure_stage,
            "reason_code": reason_code,
            "reason_codes": list(correction.get("reason_codes") or []),
            "truth_state": event.truth_state,
            "source_tagger_version_id": task.tagger_version_id,
            "upstream_routed": failure_stage in _UPSTREAM_FAILURE_STAGES,
        }
        if badcase is None:
            badcase = TagBadcase(
                tenant_id=str(event.tenant_id),
                source_feedback_event_id=event.id,
                subject_type=event.subject_type,
                subject_id=event.subject_id,
                tag_key=event.tag_key,
                failure_stage=failure_stage,
                failure_mode=failure_mode,
                signature_hash=signature_hash,
                cluster_key=f"{failure_stage}:{event.tag_key}:{reason_code}"[:128],
                dataset_split=dataset_split,
                root_cause=root_cause,
                status="open",
                regression_result={},
                occurrence_count=1,
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(badcase)
            await session.flush()
        else:
            already_materialized = (
                event.truth_tier == "t3" and badcase.source_feedback_event_id == event.id
            )
            if not already_materialized:
                badcase.occurrence_count += 1
            badcase.last_seen_at = now
            badcase.dataset_split = dataset_split
            badcase.root_cause = {
                **dict(badcase.root_cause),
                **root_cause,
            }
            if badcase.status in {"verified", "resolved", "ignored"}:
                badcase.status = "reopened"
                badcase.resolved_at = None

        if failure_stage in _UPSTREAM_FAILURE_STAGES:
            # Upstream-labelled feedback remains diagnosable but is never
            # materialized into semantic-Harness memory or coverage.
            badcase.root_cause = {
                **dict(badcase.root_cause),
                "remediation_job_id": (remediation_job.id if remediation_job is not None else None),
            }
            return badcase, None, remediation_job

        experience: TagExperienceCase | None = None
        learning_eligible = event.training_eligible or (
            event.truth_tier == "t3" and dataset_split in _LEARNING_DATASET_SPLITS
        )
        if (
            learning_eligible
            and event.truth_tier in {"t2", "t3"}
            and event.truth_state in {"present", "absent"}
        ):
            execution = (
                (
                    await session.execute(
                        select(TagHarnessExecution).where(
                            TagHarnessExecution.id == event.harness_execution_id,
                            TagHarnessExecution.tenant_id == event.tenant_id,
                        )
                    )
                ).scalar_one_or_none()
                if event.harness_execution_id is not None
                else None
            )
            tagger = (
                (
                    await session.execute(
                        select(TaggerVersion).where(
                            TaggerVersion.id == task.tagger_version_id,
                            TaggerVersion.tenant_id == event.tenant_id,
                        )
                    )
                ).scalar_one_or_none()
                if task.tagger_version_id is not None
                else None
            )
            harness_spec = (
                deepcopy(execution.resolved_harness_spec)
                if execution is not None
                else (
                    deepcopy(tagger.harness_spec)
                    if tagger is not None and isinstance(tagger.harness_spec, dict)
                    else {}
                )
            )
            scene_signature = (
                deepcopy(execution.scene_profile)
                if execution is not None
                else {
                    "subject_type": event.subject_type,
                    "selection_policy": event.selection_policy,
                }
            )
            experience_checksum = canonical_checksum(
                {
                    "feedback_event_id": event.id,
                    "badcase_id": badcase.id,
                    "harness_spec": harness_spec,
                    "truth_state": event.truth_state,
                }
            )
            experience = (
                await session.execute(
                    select(TagExperienceCase)
                    .where(
                        TagExperienceCase.tenant_id == event.tenant_id,
                        TagExperienceCase.source_feedback_event_id == event.id,
                    )
                    .limit(1)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if experience is None:
                experience = TagExperienceCase(
                    tenant_id=str(event.tenant_id),
                    source_badcase_id=badcase.id,
                    source_feedback_event_id=event.id,
                    scene_signature=scene_signature,
                    failure_signature={
                        "signature_hash": signature_hash,
                        "failure_stage": failure_stage,
                        "failure_mode": failure_mode,
                        "tag_key": event.tag_key,
                    },
                    harness_spec=harness_spec,
                    reward_vector={
                        "truth_state": event.truth_state,
                        "action": action,
                        "certified": True,
                    },
                    outcome="successful",
                    quality_tier=event.truth_tier,
                    dataset_split=dataset_split,
                    eligible=True,
                    checksum=experience_checksum,
                    materialized_at=now,
                )
                session.add(experience)
                await session.flush()
            else:
                experience.dataset_split = dataset_split
                experience.eligible = True
        return badcase, experience, remediation_job

    async def create_schema(
        self,
        *,
        tenant_id: str,
        key: str,
        name: str,
        description: str | None,
        created_by: int,
    ) -> TagSchema:
        async with self._factory() as session, session.begin():
            schema = TagSchema(
                tenant_id=tenant_id,
                key=key,
                name=name,
                description=description,
                status="draft",
                created_by=created_by,
            )
            session.add(schema)
            try:
                await session.flush()
            except IntegrityError as exc:
                raise GovernanceConflictError("schema key already exists") from exc
            await self._audit(
                session,
                tenant_id=tenant_id,
                resource_type="tag_schema",
                resource_id=schema.id,
                action="created",
                actor_user_id=created_by,
            )
            return schema

    async def get_schema(self, *, tenant_id: str, schema_id: int) -> TagSchema:
        async with self._factory() as session:
            schema = (
                await session.execute(
                    select(TagSchema).where(
                        TagSchema.id == schema_id,
                        TagSchema.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if schema is None:
                raise GovernanceNotFoundError("tag schema not found")
            return schema

    async def list_schemas(
        self,
        *,
        tenant_id: str,
        limit: int = 200,
    ) -> list[TagSchema]:
        async with self._factory() as session:
            return list(
                (
                    await session.execute(
                        select(TagSchema)
                        .where(TagSchema.tenant_id == tenant_id)
                        .order_by(TagSchema.created_at.desc(), TagSchema.id.desc())
                        .limit(_bounded_list_limit(limit))
                    )
                )
                .scalars()
                .all()
            )

    async def create_schema_version(
        self,
        *,
        tenant_id: str,
        schema_id: int,
        version: str,
        definitions: list[dict[str, Any]],
        created_by: int,
    ) -> TagSchemaVersion:
        definitions = normalize_schema_definitions(definitions)
        checksum = canonical_checksum(definitions)
        async with self._factory() as session, session.begin():
            schema = (
                await session.execute(
                    select(TagSchema).where(
                        TagSchema.id == schema_id,
                        TagSchema.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if schema is None:
                raise GovernanceNotFoundError("tag schema not found")
            snapshot = TagSchemaVersion(
                tenant_id=tenant_id,
                schema_id=schema_id,
                version=version,
                definitions=definitions,
                checksum=checksum,
                status="draft",
                created_by=created_by,
            )
            session.add(snapshot)
            try:
                await session.flush()
            except IntegrityError as exc:
                raise GovernanceConflictError("schema version or checksum already exists") from exc
            await self._audit(
                session,
                tenant_id=tenant_id,
                resource_type="tag_schema_version",
                resource_id=snapshot.id,
                action="created",
                actor_user_id=created_by,
                payload={"checksum": checksum},
            )
            return snapshot

    async def publish_schema_version(
        self,
        *,
        tenant_id: str,
        schema_id: int,
        version_id: int,
        actor_user_id: int,
    ) -> TagSchemaVersion:
        now = _utcnow()
        async with self._factory() as session, session.begin():
            schema = (
                await session.execute(
                    select(TagSchema)
                    .where(TagSchema.id == schema_id, TagSchema.tenant_id == tenant_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            version = (
                await session.execute(
                    select(TagSchemaVersion)
                    .where(
                        TagSchemaVersion.id == version_id,
                        TagSchemaVersion.schema_id == schema_id,
                        TagSchemaVersion.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if schema is None or version is None:
                raise GovernanceNotFoundError("tag schema version not found")
            if version.status not in {"draft", "validated", "published"}:
                raise GovernanceConflictError("schema version cannot be published")
            if version.status != "published":
                previous = (
                    await session.execute(
                        select(TagSchemaVersion).where(
                            TagSchemaVersion.schema_id == schema_id,
                            TagSchemaVersion.tenant_id == tenant_id,
                            TagSchemaVersion.status == "published",
                        )
                    )
                ).scalars()
                for item in previous:
                    item.status = "deprecated"
                version.status = "published"
                version.published_by = actor_user_id
                version.published_at = now
                schema.status = "published"
                schema.active_version_id = version.id
                await self._audit(
                    session,
                    tenant_id=tenant_id,
                    resource_type="tag_schema_version",
                    resource_id=version.id,
                    action="published",
                    actor_user_id=actor_user_id,
                )
            return version

    async def create_tagger_version(
        self,
        *,
        tenant_id: str,
        schema_version_id: int,
        version: str,
        engine: str,
        prompt_content: str,
        rule_bundle: dict[str, Any],
        model_version: str,
        thresholds: dict[str, Any],
        created_by: int,
        harness_spec: dict[str, Any] | None = None,
        parent_version_id: int | None = None,
        origin: str = "manual",
        optimization_run_id: int | None = None,
        change_summary: str | None = None,
    ) -> TaggerVersion:
        async with self._factory() as session, session.begin():
            schema_version = (
                await session.execute(
                    select(TagSchemaVersion).where(
                        TagSchemaVersion.id == schema_version_id,
                        TagSchemaVersion.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if schema_version is None:
                raise GovernanceNotFoundError("tag schema version not found")
            if schema_version.status != "published":
                raise GovernanceConflictError("tagger versions require a published schema version")
            if origin not in {"manual", "optimizer", "bootstrap", "migration"}:
                raise GovernanceError("unsupported tagger origin")
            if parent_version_id is not None:
                parent = (
                    await session.execute(
                        select(TaggerVersion).where(
                            TaggerVersion.id == parent_version_id,
                            TaggerVersion.tenant_id == tenant_id,
                        )
                    )
                ).scalar_one_or_none()
                if parent is None or parent.schema_version_id != schema_version_id:
                    raise GovernanceConflictError(
                        "parent tagger must exist in the same tenant and schema"
                    )
            if optimization_run_id is not None:
                optimization_run = (
                    await session.execute(
                        select(TagOptimizationRun).where(
                            TagOptimizationRun.id == optimization_run_id,
                            TagOptimizationRun.tenant_id == tenant_id,
                        )
                    )
                ).scalar_one_or_none()
                if optimization_run is None:
                    raise GovernanceNotFoundError("optimization run not found")
                if parent_version_id != optimization_run.baseline_tagger_version_id:
                    raise GovernanceConflictError(
                        "optimizer candidate parent must match its server-bound baseline"
                    )
                if origin != "optimizer":
                    raise GovernanceConflictError(
                        "taggers linked to optimization runs must use optimizer origin"
                    )
            definitions = cast(list[dict[str, Any]], schema_version.definitions)
            if engine in {"llm", "hybrid"} and len(prompt_content.strip()) < 8:
                raise GovernanceError("llm and hybrid taggers require a non-empty versioned prompt")
            validate_rule_bundle(
                rule_bundle,
                engine=engine,
                definitions=definitions,
            )
            definition_keys = {str(definition["key"]) for definition in definitions}
            allowed_threshold_keys = (
                definition_keys
                | {"default"}
                | {
                    f"{subject_type}:{definition['key']}"
                    for definition in definitions
                    for subject_type in definition.get("subject_types", [])
                    if subject_type in {"dialogue_unit", "reception"}
                }
            )
            unknown_thresholds = set(thresholds) - allowed_threshold_keys
            if unknown_thresholds:
                raise GovernanceError(
                    f"thresholds reference unknown tags: {sorted(unknown_thresholds)}"
                )
            normalized_thresholds: dict[str, float] = {}
            for key, value in thresholds.items():
                if isinstance(value, bool):
                    raise GovernanceError(f"threshold {key!r} must be numeric")
                try:
                    normalized = float(value)
                except (TypeError, ValueError) as exc:
                    raise GovernanceError(f"threshold {key!r} must be numeric") from exc
                if not math.isfinite(normalized) or not 0 <= normalized <= 1:
                    raise GovernanceError(f"threshold {key!r} must be finite and between 0 and 1")
                normalized_thresholds[key] = normalized
            harness_spec_version = (
                str(harness_spec.get("spec_version", "1.0"))
                if isinstance(harness_spec, Mapping)
                else "1.0"
            )
            if harness_spec_version not in {"1.0", "2.0"}:
                raise GovernanceError("unsupported Harness spec version")
            config = {
                "schema_version_id": schema_version_id,
                "engine": engine,
                "prompt_content": prompt_content,
                "rule_bundle": rule_bundle,
                "model_version": model_version,
                "thresholds": normalized_thresholds,
                "harness_spec_version": harness_spec_version,
                "harness_spec": harness_spec,
                "parent_version_id": parent_version_id,
                "origin": origin,
                "optimization_run_id": optimization_run_id,
                "change_summary": change_summary,
            }
            checksum = canonical_checksum(config)
            tagger = TaggerVersion(
                tenant_id=tenant_id,
                schema_version_id=schema_version_id,
                version=version,
                engine=engine,
                prompt_content=prompt_content,
                rule_bundle=rule_bundle,
                model_version=model_version,
                thresholds=normalized_thresholds,
                harness_spec_version=harness_spec_version,
                harness_spec=deepcopy(harness_spec),
                parent_version_id=parent_version_id,
                origin=origin,
                optimization_run_id=optimization_run_id,
                change_summary=change_summary,
                config_checksum=checksum,
                status="draft",
                created_by=created_by,
            )
            session.add(tagger)
            try:
                await session.flush()
            except IntegrityError as exc:
                raise GovernanceConflictError("tagger version already exists") from exc
            await self._audit(
                session,
                tenant_id=tenant_id,
                resource_type="tagger_version",
                resource_id=tagger.id,
                action="created",
                actor_user_id=created_by,
                payload={"config_checksum": checksum},
            )
            return tagger

    async def list_schema_versions(
        self,
        *,
        tenant_id: str,
        schema_id: int | None = None,
        limit: int = 200,
    ) -> list[TagSchemaVersion]:
        async with self._factory() as session:
            stmt = select(TagSchemaVersion).where(TagSchemaVersion.tenant_id == tenant_id)
            if schema_id is not None:
                stmt = stmt.where(TagSchemaVersion.schema_id == schema_id)
            return list(
                (
                    await session.execute(
                        stmt.order_by(
                            TagSchemaVersion.created_at.desc(),
                            TagSchemaVersion.id.desc(),
                        ).limit(_bounded_list_limit(limit))
                    )
                )
                .scalars()
                .all()
            )

    async def list_tagger_versions(
        self,
        *,
        tenant_id: str,
        limit: int = 200,
    ) -> list[TaggerVersion]:
        async with self._factory() as session:
            return list(
                (
                    await session.execute(
                        select(TaggerVersion)
                        .where(TaggerVersion.tenant_id == tenant_id)
                        .order_by(TaggerVersion.created_at.desc(), TaggerVersion.id.desc())
                        .limit(_bounded_list_limit(limit))
                    )
                )
                .scalars()
                .all()
            )

    @staticmethod
    async def _resolve_subject(
        session: AsyncSession,
        *,
        tenant_id: str,
        subject_type: str,
        subject_id: int,
    ) -> tuple[int, str]:
        if subject_type == "dialogue_unit":
            row = (
                await session.execute(
                    select(DialogueUnit.reception_id, Reception.scenario)
                    .join(
                        Reception,
                        Reception.id == DialogueUnit.reception_id,
                    )
                    .where(
                        DialogueUnit.id == subject_id,
                        DialogueUnit.tenant_id == tenant_id,
                        Reception.tenant_id == tenant_id,
                    )
                )
            ).one_or_none()
            if row is None:
                raise AssignmentValidationError(
                    "dialogue unit does not exist in the current tenant"
                )
            return int(row.reception_id), str(row.scenario)
        if subject_type == "reception":
            row = (
                await session.execute(
                    select(Reception.id, Reception.scenario).where(
                        Reception.id == subject_id,
                        Reception.tenant_id == tenant_id,
                    )
                )
            ).one_or_none()
            if row is None:
                raise AssignmentValidationError("reception does not exist in the current tenant")
            return int(row.id), str(row.scenario)
        raise AssignmentValidationError(
            "canonical assignments only support dialogue_unit or reception"
        )

    @staticmethod
    async def _validate_evidence_ownership(
        session: AsyncSession,
        *,
        tenant_id: str,
        reception_id: int,
        evidence_refs: list[dict[str, Any]],
    ) -> None:
        segment_ids = {
            int(item["segment_id"])
            for item in evidence_refs
            if isinstance(item, dict) and item.get("segment_id") is not None
        }
        if not segment_ids:
            return
        rows = (
            await session.execute(
                select(
                    Segment.id,
                    Segment.start_sec,
                    Segment.end_sec,
                    ReceptionRecording.source_start_sec,
                    ReceptionRecording.source_end_sec,
                )
                .join(
                    ReceptionRecording,
                    ReceptionRecording.recording_id == Segment.recording_id,
                )
                .where(
                    Segment.tenant_id == tenant_id,
                    Segment.id.in_(segment_ids),
                    ReceptionRecording.tenant_id == tenant_id,
                    ReceptionRecording.reception_id == reception_id,
                    Segment.start_sec < ReceptionRecording.source_end_sec,
                    Segment.end_sec > ReceptionRecording.source_start_sec,
                )
            )
        ).all()
        spans_by_segment: dict[int, list[tuple[float, float]]] = {}
        for row in rows:
            spans_by_segment.setdefault(int(row.id), []).append(
                (
                    max(float(row.start_sec), float(row.source_start_sec)),
                    min(float(row.end_sec), float(row.source_end_sec)),
                )
            )
        if set(spans_by_segment) != segment_ids:
            raise AssignmentValidationError(
                "evidence contains missing, cross-tenant, or unrelated segments"
            )
        for evidence in evidence_refs:
            segment_id = int(evidence["segment_id"])
            start = evidence.get("start_sec")
            end = evidence.get("end_sec")
            if start is None or end is None:
                raise AssignmentValidationError(
                    "evidence must include its source start_sec and end_sec"
                )
            start_value = float(start)
            end_value = float(end)
            if end_value <= start_value or not any(
                start_value >= span_start - 1e-6 and end_value <= span_end + 1e-6
                for span_start, span_end in spans_by_segment[segment_id]
            ):
                raise AssignmentValidationError(
                    "evidence window is outside the reception recording span"
                )

    async def _append_assignment_in_session(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        subject_type: str,
        subject_id: int,
        tag_key: str,
        tag_value: Any,
        confidence: float | None,
        evidence_refs: list[dict[str, Any]],
        source: str,
        schema_version_id: int | None,
        tagger_version_id: int | None,
        extraction_run_id: int | None,
        deployment_id: int | None,
        input_hash: str,
        actor_user_id: int | None,
        tombstone: bool = False,
        publish_current: bool = True,
        expected_current_fact_id: int | None = None,
        expected_current_absent: bool = False,
        superseded_fact_id_override: int | None = None,
    ) -> TagAssignmentFact:
        reception_id, scenario = await self._resolve_subject(
            session,
            tenant_id=tenant_id,
            subject_type=subject_type,
            subject_id=subject_id,
        )
        dialogue_unit_id = subject_id if subject_type == "dialogue_unit" else None
        if schema_version_id is None:
            raise AssignmentValidationError("assignments require a published schema version")
        schema_version: TagSchemaVersion | None = None
        if schema_version_id is not None:
            schema_version = (
                await session.execute(
                    select(TagSchemaVersion).where(
                        TagSchemaVersion.id == schema_version_id,
                        TagSchemaVersion.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if schema_version is None or schema_version.status not in {
                "published",
                "deprecated",
            }:
                raise AssignmentValidationError(
                    "assignments require an immutable published schema version"
                )
            definition = next(
                (
                    item
                    for item in schema_version.definitions
                    if isinstance(item, dict) and item.get("key") == tag_key
                ),
                None,
            )
            if definition is None:
                raise AssignmentValidationError(
                    "tag key is not defined by the published schema version"
                )
            subject_types = definition.get("subject_types") or []
            if subject_type not in subject_types:
                raise AssignmentValidationError(
                    "tag definition does not apply to this subject type"
                )
            scenarios = definition.get("scenarios") or []
            if scenarios and scenario not in scenarios:
                raise AssignmentValidationError(
                    "tag definition does not apply to the reception scenario"
                )
            if not tombstone:
                validate_assignment(
                    definition=definition,
                    label_value=tag_value,
                    confidence=confidence,
                    evidence_refs=evidence_refs,
                )
        if source in {"rule", "llm"} and tagger_version_id is None:
            raise AssignmentValidationError("automatic assignments require a tagger version")
        tagger: TaggerVersion | None = None
        if tagger_version_id is not None:
            tagger = (
                await session.execute(
                    select(TaggerVersion).where(
                        TaggerVersion.id == tagger_version_id,
                        TaggerVersion.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if tagger is None:
                raise AssignmentValidationError(
                    "tagger version does not exist in the current tenant"
                )
            if schema_version_id is not None and tagger.schema_version_id != schema_version_id:
                raise AssignmentValidationError(
                    "tagger version does not reference the assignment schema"
                )
        extraction_run: TagExtractionRun | None = None
        if extraction_run_id is not None:
            extraction_run = (
                await session.execute(
                    select(TagExtractionRun)
                    .where(
                        TagExtractionRun.id == extraction_run_id,
                        TagExtractionRun.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if extraction_run is None:
                raise AssignmentValidationError(
                    "extraction run does not exist in the current tenant"
                )
            if (
                extraction_run.subject_type != subject_type
                or extraction_run.subject_id != subject_id
                or extraction_run.tagger_version_id != tagger_version_id
                or extraction_run.deployment_id != deployment_id
            ):
                raise AssignmentValidationError(
                    "extraction run lineage does not match the assignment"
                )
        elif source in {"rule", "llm"}:
            raise AssignmentValidationError("automatic assignments require an extraction run")
        deployment: TagDeployment | None = None
        if deployment_id is not None:
            deployment = (
                await session.execute(
                    select(TagDeployment)
                    .where(
                        TagDeployment.id == deployment_id,
                        TagDeployment.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if deployment is None:
                raise AssignmentValidationError("deployment does not exist in the current tenant")
            if deployment.tagger_version_id != tagger_version_id:
                raise AssignmentValidationError("deployment tagger does not match the assignment")
            if publish_current and (
                extraction_run is None
                or not extraction_run.served_current
                or extraction_run.deployment_stage != deployment.status
                or extraction_run.deployment_revision != deployment.revision
                or deployment.status
                not in {"canary_5", "canary_25", "awaiting_admin", "production"}
            ):
                # A route decision is only a proposal until this transaction
                # locks and revalidates the deployment snapshot.  If promotion,
                # retirement, or rollback won the race, retain the append-only
                # fact for diagnosis but never resurrect it as current.
                publish_current = False
        await self._validate_evidence_ownership(
            session,
            tenant_id=tenant_id,
            reception_id=reception_id,
            evidence_refs=evidence_refs,
        )
        current = (
            await session.execute(
                select(TagAssignmentCurrent)
                .where(
                    TagAssignmentCurrent.tenant_id == tenant_id,
                    TagAssignmentCurrent.subject_type == subject_type,
                    TagAssignmentCurrent.subject_id == subject_id,
                    TagAssignmentCurrent.tag_key == tag_key,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        current_fact: TagAssignmentFact | None = None
        if current is not None:
            current_fact = (
                await session.execute(
                    select(TagAssignmentFact).where(
                        TagAssignmentFact.id == current.fact_id,
                        TagAssignmentFact.tenant_id == tenant_id,
                    )
                )
            ).scalar_one()
        if expected_current_fact_id is not None and (
            current is None or current.fact_id != expected_current_fact_id
        ):
            raise GovernanceConflictError(
                "current assignment fact changed; reload before correcting"
            )
        if expected_current_absent and current is not None:
            raise GovernanceConflictError(
                "current assignment appeared after review creation; reload before correcting"
            )
        if publish_current:
            current_subject_facts = list(
                (
                    await session.execute(
                        select(TagAssignmentFact)
                        .join(
                            TagAssignmentCurrent,
                            TagAssignmentCurrent.fact_id == TagAssignmentFact.id,
                        )
                        .where(
                            TagAssignmentCurrent.tenant_id == tenant_id,
                            TagAssignmentCurrent.subject_type == subject_type,
                            TagAssignmentCurrent.subject_id == subject_id,
                            TagAssignmentFact.tenant_id == tenant_id,
                            TagAssignmentFact.tombstone.is_(False),
                        )
                    )
                )
                .scalars()
                .all()
            )
            current_keys = {
                item.tag_key for item in current_subject_facts if item.tag_key != tag_key
            }
            definitions_by_key = {
                str(item["key"]): item
                for item in cast(list[dict[str, Any]], schema_version.definitions)
            }
            if not tombstone:
                conflicting = current_keys.intersection(
                    set(cast(list[str], definition.get("mutually_exclusive_with", [])))
                )
                if conflicting:
                    raise AssignmentValidationError(
                        "assignment conflicts with mutually exclusive current tags: "
                        + ", ".join(sorted(conflicting))
                    )
                missing_dependencies = (
                    set(cast(list[str], definition.get("depends_on", []))) - current_keys
                )
                if missing_dependencies:
                    raise AssignmentValidationError(
                        "assignment is missing required current tags: "
                        + ", ".join(sorted(missing_dependencies))
                    )
            else:
                blocking_dependents = {
                    key
                    for key in current_keys
                    if tag_key
                    in set(
                        cast(
                            list[str],
                            definitions_by_key.get(key, {}).get("depends_on", []),
                        )
                    )
                }
                if blocking_dependents:
                    raise AssignmentValidationError(
                        "cannot remove a tag while current tags depend on it: "
                        + ", ".join(sorted(blocking_dependents))
                    )
        previous_id = (
            superseded_fact_id_override
            if superseded_fact_id_override is not None
            else current.fact_id
            if current is not None
            else None
        )
        max_revision = (
            await session.execute(
                select(func.max(TagAssignmentFact.revision)).where(
                    TagAssignmentFact.tenant_id == tenant_id,
                    TagAssignmentFact.subject_type == subject_type,
                    TagAssignmentFact.subject_id == subject_id,
                    TagAssignmentFact.tag_key == tag_key,
                )
            )
        ).scalar_one()
        revision = int(max_revision or 0) + 1
        now = _utcnow()
        fact = TagAssignmentFact(
            tenant_id=tenant_id,
            subject_type=subject_type,
            subject_id=subject_id,
            reception_id=reception_id,
            dialogue_unit_id=dialogue_unit_id,
            tag_key=tag_key,
            tag_value=tag_value,
            confidence=confidence,
            evidence_refs=evidence_refs,
            source=source,
            schema_version_id=schema_version_id,
            tagger_version_id=tagger_version_id,
            extraction_run_id=extraction_run_id,
            deployment_id=deployment_id,
            input_hash=input_hash,
            superseded_fact_id=previous_id,
            revision=revision,
            tombstone=tombstone,
            actor_user_id=actor_user_id,
            assigned_at=now,
        )
        try:
            async with session.begin_nested():
                session.add(fact)
                await session.flush()
        except IntegrityError as exc:
            existing_fact = (
                await session.execute(
                    select(TagAssignmentFact).where(
                        TagAssignmentFact.tenant_id == tenant_id,
                        TagAssignmentFact.subject_type == subject_type,
                        TagAssignmentFact.subject_id == subject_id,
                        TagAssignmentFact.tag_key == tag_key,
                        TagAssignmentFact.tagger_version_id == tagger_version_id,
                        TagAssignmentFact.deployment_id == deployment_id,
                        TagAssignmentFact.input_hash == input_hash,
                    )
                )
            ).scalar_one_or_none()
            if existing_fact is None:
                raise GovernanceConflictError(
                    "assignment revision changed concurrently; retry the operation"
                ) from exc
            fact = existing_fact
            current = (
                await session.execute(
                    select(TagAssignmentCurrent)
                    .where(
                        TagAssignmentCurrent.tenant_id == tenant_id,
                        TagAssignmentCurrent.subject_type == subject_type,
                        TagAssignmentCurrent.subject_id == subject_id,
                        TagAssignmentCurrent.tag_key == tag_key,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            revision = fact.revision
        if not publish_current:
            return fact
        if (
            current_fact is not None
            and current_fact.source == "manual"
            and source in {"rule", "llm"}
        ):
            review_exists = (
                await session.execute(
                    select(TagReviewTask.id).where(
                        TagReviewTask.tenant_id == tenant_id,
                        TagReviewTask.proposed_fact_id == fact.id,
                        TagReviewTask.status.in_(["pending", "claimed"]),
                    )
                )
            ).scalar_one_or_none()
            if review_exists is None:
                session.add(
                    TagReviewTask(
                        tenant_id=tenant_id,
                        batch_id=f"manual-conflict-{fact.id}",
                        subject_type=subject_type,
                        subject_id=subject_id,
                        reception_id=reception_id,
                        tag_key=tag_key,
                        proposed_value=tag_value,
                        confidence=confidence,
                        evidence_refs=evidence_refs,
                        proposed_fact_id=fact.id,
                        schema_version_id=schema_version_id,
                        tagger_version_id=tagger_version_id,
                        source_deployment_id=deployment_id,
                        source_extraction_run_id=extraction_run_id,
                        reason="conflict",
                        status="pending",
                        priority=100,
                        created_by=actor_user_id or 0,
                    )
                )
            return fact
        if current is None:
            session.add(
                TagAssignmentCurrent(
                    tenant_id=tenant_id,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    tag_key=tag_key,
                    fact_id=fact.id,
                    revision=revision,
                )
            )
        elif current.revision < revision:
            current.fact_id = fact.id
            current.revision = revision
        return fact

    async def append_assignment(
        self,
        *,
        tenant_id: str,
        subject_type: str,
        subject_id: int,
        tag_key: str,
        tag_value: Any,
        confidence: float | None,
        evidence_refs: list[dict[str, Any]],
        source: str,
        schema_version_id: int | None,
        tagger_version_id: int | None,
        extraction_run_id: int | None,
        deployment_id: int | None,
        input_hash: str,
        actor_user_id: int | None,
        tombstone: bool = False,
        publish_current: bool = True,
    ) -> TagAssignmentFact:
        if len(input_hash) != 64:
            raise AssignmentValidationError("input_hash must be a SHA-256 hex digest")
        if confidence is not None and not 0 <= confidence <= 1:
            raise AssignmentValidationError("confidence must be between 0 and 1")
        async with self._factory() as session, session.begin():
            fact = await self._append_assignment_in_session(
                session,
                tenant_id=tenant_id,
                subject_type=subject_type,
                subject_id=subject_id,
                tag_key=tag_key,
                tag_value=tag_value,
                confidence=confidence,
                evidence_refs=evidence_refs,
                source=source,
                schema_version_id=schema_version_id,
                tagger_version_id=tagger_version_id,
                extraction_run_id=extraction_run_id,
                deployment_id=deployment_id,
                input_hash=input_hash,
                actor_user_id=actor_user_id,
                tombstone=tombstone,
                publish_current=publish_current,
            )
            await self._audit(
                session,
                tenant_id=tenant_id,
                resource_type="tag_assignment_fact",
                resource_id=fact.id,
                action="appended",
                actor_user_id=actor_user_id or 0,
                payload={"subject_type": subject_type, "subject_id": subject_id},
            )
            return fact

    async def append_manual_correction_in_session(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        expected_fact_id: int,
        tag_value: Any,
        evidence_refs: list[dict[str, Any]],
        reason: str,
        actor_user_id: int,
    ) -> tuple[TagAssignmentFact, TagAssignmentFact]:
        """Append a manual fact and CAS the current projection in a caller transaction."""

        current_fact = (
            await session.execute(
                select(TagAssignmentFact)
                .join(
                    TagAssignmentCurrent,
                    and_(
                        TagAssignmentCurrent.tenant_id == TagAssignmentFact.tenant_id,
                        TagAssignmentCurrent.subject_type == TagAssignmentFact.subject_type,
                        TagAssignmentCurrent.subject_id == TagAssignmentFact.subject_id,
                        TagAssignmentCurrent.tag_key == TagAssignmentFact.tag_key,
                        TagAssignmentCurrent.fact_id == TagAssignmentFact.id,
                    ),
                )
                .where(
                    TagAssignmentFact.id == expected_fact_id,
                    TagAssignmentFact.tenant_id == tenant_id,
                    TagAssignmentFact.tombstone.is_(False),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if current_fact is None:
            raise GovernanceConflictError(
                "current assignment fact changed; reload before correcting"
            )
        if not reason.strip():
            raise AssignmentValidationError("manual corrections require a reason")
        if not evidence_refs:
            raise AssignmentValidationError("manual corrections require evidence")
        if current_fact.reception_id is None:
            raise AssignmentValidationError(
                "manual correction fact is missing its reception lineage"
            )
        await self._validate_evidence_ownership(
            session,
            tenant_id=tenant_id,
            reception_id=current_fact.reception_id,
            evidence_refs=evidence_refs,
        )
        input_hash = canonical_checksum(
            {
                "operation": "manual_correction",
                "expected_fact_id": expected_fact_id,
                "tag_value": tag_value,
                "evidence_refs": evidence_refs,
                "reason": reason,
                "actor_user_id": actor_user_id,
            }
        )
        corrected = await self._append_assignment_in_session(
            session,
            tenant_id=tenant_id,
            subject_type=current_fact.subject_type,
            subject_id=current_fact.subject_id,
            tag_key=current_fact.tag_key,
            tag_value=tag_value,
            confidence=1.0,
            evidence_refs=evidence_refs,
            source="manual",
            schema_version_id=current_fact.schema_version_id,
            tagger_version_id=None,
            extraction_run_id=None,
            deployment_id=None,
            input_hash=input_hash,
            actor_user_id=actor_user_id,
            expected_current_fact_id=current_fact.id,
        )
        await self._audit(
            session,
            tenant_id=tenant_id,
            resource_type="tag_assignment_fact",
            resource_id=corrected.id,
            action="manual_corrected",
            actor_user_id=actor_user_id,
            payload={
                "reason": reason,
                "subject_type": current_fact.subject_type,
                "subject_id": current_fact.subject_id,
                "tag_key": current_fact.tag_key,
                "superseded_fact_id": current_fact.id,
            },
        )
        return current_fact, corrected

    async def project_manual_correction_in_session(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        reception: Reception,
        superseded_fact: TagAssignmentFact | None,
        corrected_fact: TagAssignmentFact,
        reason: str,
        actor_user_id: int,
        stage_change: dict[str, Any] | None,
    ) -> int:
        """Advance reception projections and provenance for one canonical fact."""

        if (
            reception.tenant_id != tenant_id
            or corrected_fact.tenant_id != tenant_id
            or corrected_fact.reception_id != reception.id
        ):
            raise AssignmentValidationError(
                "manual correction does not belong to the locked reception"
            )
        if superseded_fact is not None and (
            superseded_fact.tenant_id != tenant_id or superseded_fact.reception_id != reception.id
        ):
            raise AssignmentValidationError(
                "superseded fact does not belong to the locked reception"
            )

        reception.version += 1
        now = _utcnow()
        actor = f"user:{actor_user_id}"
        algorithm_version = (
            f"schema:{corrected_fact.schema_version_id}|"
            f"tagger:{corrected_fact.tagger_version_id or 'manual'}"
        )[:64]
        before = _fact_snapshot(superseded_fact) if superseded_fact is not None else None
        after = _fact_snapshot(corrected_fact)
        events: list[ProvenanceEvent] = []
        if superseded_fact is not None:
            events.append(
                ProvenanceEvent(
                    tenant_id=tenant_id,
                    reception_id=reception.id,
                    object_type="tag_assignment_fact",
                    object_ref=str(superseded_fact.id),
                    event_type="superseded",
                    actor=actor,
                    algorithm_version=algorithm_version,
                    parent_refs=_fact_parent_refs(superseded_fact),
                    evidence_refs=_safe_provenance_evidence(superseded_fact.evidence_refs),
                    payload={
                        "reason": reason,
                        "before": before,
                        "superseded_by_fact_id": corrected_fact.id,
                        "reception_version": reception.version,
                    },
                    occurred_at=now,
                )
            )
        parent_refs = _fact_parent_refs(corrected_fact)
        if superseded_fact is not None:
            parent_refs.insert(
                0,
                {
                    "type": "tag_assignment_fact",
                    "id": superseded_fact.id,
                },
            )
        events.append(
            ProvenanceEvent(
                tenant_id=tenant_id,
                reception_id=reception.id,
                object_type="tag_assignment_fact",
                object_ref=str(corrected_fact.id),
                event_type="edited",
                actor=actor,
                algorithm_version=algorithm_version,
                parent_refs=parent_refs,
                evidence_refs=_safe_provenance_evidence(corrected_fact.evidence_refs),
                payload={
                    "reason": reason,
                    "before": before,
                    "after": after,
                    "reception_version": reception.version,
                },
                occurred_at=now,
            )
        )
        if stage_change is not None:
            events.append(
                ProvenanceEvent(
                    tenant_id=tenant_id,
                    reception_id=reception.id,
                    object_type="dialogue_unit",
                    object_ref=str(corrected_fact.dialogue_unit_id),
                    event_type="edited",
                    actor=actor,
                    algorithm_version="manual-tag-edit-v1",
                    parent_refs=[
                        {
                            "type": "tag_assignment_fact",
                            "id": corrected_fact.id,
                        }
                    ],
                    evidence_refs=_safe_provenance_evidence(corrected_fact.evidence_refs),
                    payload={
                        "reason": reason,
                        **stage_change,
                        "reception_version": reception.version,
                    },
                    occurred_at=now,
                )
            )
        session.add_all(events)
        return int(reception.version)

    async def append_assignment_batch(
        self,
        *,
        tenant_id: str,
        subject_type: str,
        subject_id: int,
        assignments: list[dict[str, Any]],
        schema_version_id: int,
        tagger_version_id: int,
        extraction_run_id: int,
        deployment_id: int | None,
        input_hash: str,
        actor_user_id: int,
        publish_current: bool,
        publish_current_tag_keys: set[str] | None = None,
        replace_current_tag_keys: set[str] | None = None,
    ) -> list[TagAssignmentFact]:
        """Append facts and atomically replace the requested current projection."""

        if len(input_hash) != 64:
            raise AssignmentValidationError("input_hash must be a SHA-256 hex digest")
        async with self._factory() as session, session.begin():
            facts: list[TagAssignmentFact] = []
            for assignment in assignments:
                tag_key = str(assignment["tag_key"])
                assignment_publish_current = publish_current and (
                    publish_current_tag_keys is None or tag_key in publish_current_tag_keys
                )
                fact = await self._append_assignment_in_session(
                    session,
                    tenant_id=tenant_id,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    tag_key=tag_key,
                    tag_value=assignment.get("tag_value"),
                    confidence=(
                        float(assignment["confidence"])
                        if assignment.get("confidence") is not None
                        else None
                    ),
                    evidence_refs=list(assignment.get("evidence_refs") or []),
                    source=str(assignment["source"]),
                    schema_version_id=schema_version_id,
                    tagger_version_id=tagger_version_id,
                    extraction_run_id=extraction_run_id,
                    deployment_id=deployment_id,
                    input_hash=input_hash,
                    actor_user_id=actor_user_id,
                    publish_current=assignment_publish_current,
                )
                facts.append(fact)
                await self._audit(
                    session,
                    tenant_id=tenant_id,
                    resource_type="tag_assignment_fact",
                    resource_id=fact.id,
                    action="appended",
                    actor_user_id=actor_user_id,
                    payload={"subject_type": subject_type, "subject_id": subject_id},
                )
            if publish_current and replace_current_tag_keys:
                selected_keys = (
                    set(publish_current_tag_keys)
                    if publish_current_tag_keys is not None
                    else {str(item["tag_key"]) for item in assignments}
                )
                stale_keys = set(
                    (
                        await session.execute(
                            select(TagAssignmentCurrent.tag_key).where(
                                TagAssignmentCurrent.tenant_id == tenant_id,
                                TagAssignmentCurrent.subject_type == subject_type,
                                TagAssignmentCurrent.subject_id == subject_id,
                                TagAssignmentCurrent.tag_key.in_(
                                    sorted(replace_current_tag_keys - selected_keys)
                                ),
                            )
                        )
                    ).scalars()
                )
                for stale_key in sorted(stale_keys):
                    tombstone = await self._append_assignment_in_session(
                        session,
                        tenant_id=tenant_id,
                        subject_type=subject_type,
                        subject_id=subject_id,
                        tag_key=stale_key,
                        tag_value=None,
                        confidence=None,
                        evidence_refs=[],
                        source="llm",
                        schema_version_id=schema_version_id,
                        tagger_version_id=tagger_version_id,
                        extraction_run_id=extraction_run_id,
                        deployment_id=deployment_id,
                        input_hash=input_hash,
                        actor_user_id=actor_user_id,
                        tombstone=True,
                        publish_current=True,
                    )
                    await self._audit(
                        session,
                        tenant_id=tenant_id,
                        resource_type="tag_assignment_fact",
                        resource_id=tombstone.id,
                        action="projection_retracted",
                        actor_user_id=actor_user_id,
                        payload={
                            "subject_type": subject_type,
                            "subject_id": subject_id,
                            "tag_key": stale_key,
                            "extraction_run_id": extraction_run_id,
                        },
                    )
            return facts

    async def publish_budgeted_job_current(
        self,
        *,
        tenant_id: str,
        job_id: int,
        worker_id: str,
        expected_revision: int,
        actor_user_id: int,
        now: datetime,
    ) -> int:
        """Atomically publish every staged current pointer for a budgeted job.

        Extraction stores immutable facts with ``publish_current=False``.  This
        method is the sole visibility boundary: if any run, lineage check,
        budget check, or tombstone fails, the transaction rolls back and the
        pre-job projection remains byte-for-byte unchanged.
        """

        async with self._factory() as session, session.begin():
            job = (
                await session.execute(
                    select(TagExtractionJob)
                    .where(
                        TagExtractionJob.id == job_id,
                        TagExtractionJob.tenant_id == tenant_id,
                        TagExtractionJob.status == "running",
                        TagExtractionJob.lease_owner == worker_id,
                        TagExtractionJob.revision == expected_revision,
                        TagExtractionJob.lease_expires_at >= now,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if job is None:
                raise GovernanceConflictError(
                    "tag job lease was lost before atomic current publication"
                )
            if not any(
                limit is not None
                for limit in (
                    job.budget_max_provider_tokens,
                    job.budget_max_provider_calls,
                    job.budget_max_cost_microunits,
                    job.budget_max_wall_seconds,
                )
            ):
                raise GovernanceConflictError(
                    "atomic budget publication requires a persisted job budget"
                )
            if job.current_published_at is not None:
                return int(job.revision)
            if job.failed_items or job.completed_items != job.total_items:
                raise GovernanceConflictError(
                    "budgeted current cannot publish before every item succeeds"
                )
            if (
                job.budget_reserved_provider_tokens
                or job.budget_reserved_provider_calls
                or job.budget_reserved_cost_microunits
            ):
                raise GovernanceConflictError(
                    "budgeted current cannot publish with an unsettled reservation"
                )
            if job.budget_exhausted_at is not None:
                raise TagJobBudgetExhaustedError(
                    "budgeted current cannot publish after budget exhaustion",
                    revision=int(job.revision),
                )
            runs = list(
                (
                    await session.execute(
                        select(TagExtractionRun)
                        .where(
                            TagExtractionRun.tenant_id == tenant_id,
                            TagExtractionRun.job_id == job.id,
                        )
                        .order_by(
                            TagExtractionRun.subject_type,
                            TagExtractionRun.subject_id,
                            TagExtractionRun.id,
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            if any(run.status not in {"completed", "cached"} for run in runs):
                raise GovernanceConflictError(
                    "budgeted current cannot publish incomplete extraction runs"
                )
            for run in runs:
                if not run.served_current:
                    continue
                if run.deployment_id is not None:
                    deployment = (
                        await session.execute(
                            select(TagDeployment)
                            .where(
                                TagDeployment.id == run.deployment_id,
                                TagDeployment.tenant_id == tenant_id,
                            )
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if (
                        deployment is None
                        or deployment.tagger_version_id != run.tagger_version_id
                        or run.deployment_stage != deployment.status
                        or run.deployment_revision != deployment.revision
                        or deployment.status
                        not in {
                            "canary_5",
                            "canary_25",
                            "awaiting_admin",
                            "production",
                        }
                    ):
                        # Preserve staged facts for diagnosis, while refusing
                        # to revive a route that changed after extraction.
                        continue
                raw_assignments = run.output_snapshot.get("assignments", [])
                if not isinstance(raw_assignments, list):
                    raise GovernanceConflictError(
                        "budgeted extraction run has malformed assignments"
                    )
                selected_facts: list[TagAssignmentFact] = []
                selected_keys: set[str] = set()
                for raw_assignment in raw_assignments:
                    if not isinstance(raw_assignment, dict):
                        raise GovernanceConflictError(
                            "budgeted extraction run has malformed assignment"
                        )
                    fact_id = raw_assignment.get("fact_id")
                    tag_key = raw_assignment.get("tag_key")
                    if (
                        isinstance(fact_id, bool)
                        or not isinstance(fact_id, int)
                        or fact_id <= 0
                        or not isinstance(tag_key, str)
                        or not tag_key
                    ):
                        raise GovernanceConflictError(
                            "budgeted extraction assignment is missing immutable lineage"
                        )
                    fact = (
                        await session.execute(
                            select(TagAssignmentFact).where(
                                TagAssignmentFact.id == fact_id,
                                TagAssignmentFact.tenant_id == tenant_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if (
                        fact is None
                        or fact.subject_type != run.subject_type
                        or fact.subject_id != run.subject_id
                        or fact.tag_key != tag_key
                        or fact.tagger_version_id != run.tagger_version_id
                        or fact.deployment_id != run.deployment_id
                        or fact.input_hash != run.input_hash
                        or fact.tombstone
                    ):
                        raise GovernanceConflictError(
                            "budgeted extraction assignment lineage does not match its run"
                        )
                    selected_facts.append(fact)
                    selected_keys.add(tag_key)
                for fact in selected_facts:
                    current = (
                        await session.execute(
                            select(TagAssignmentCurrent)
                            .where(
                                TagAssignmentCurrent.tenant_id == tenant_id,
                                TagAssignmentCurrent.subject_type == fact.subject_type,
                                TagAssignmentCurrent.subject_id == fact.subject_id,
                                TagAssignmentCurrent.tag_key == fact.tag_key,
                            )
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    current_fact = (
                        (
                            await session.execute(
                                select(TagAssignmentFact).where(
                                    TagAssignmentFact.id == current.fact_id,
                                    TagAssignmentFact.tenant_id == tenant_id,
                                )
                            )
                        ).scalar_one()
                        if current is not None
                        else None
                    )
                    if (
                        current_fact is not None
                        and current_fact.source == "manual"
                        and fact.source in {"rule", "llm"}
                    ):
                        continue
                    if current is None:
                        session.add(
                            TagAssignmentCurrent(
                                tenant_id=tenant_id,
                                subject_type=fact.subject_type,
                                subject_id=fact.subject_id,
                                tag_key=fact.tag_key,
                                fact_id=fact.id,
                                revision=fact.revision,
                            )
                        )
                    elif current.revision < fact.revision:
                        current.fact_id = fact.id
                        current.revision = fact.revision
                raw_target_keys = run.input_snapshot.get("target_tag_keys", [])
                if not isinstance(raw_target_keys, list) or any(
                    not isinstance(value, str) or not value
                    for value in raw_target_keys
                ):
                    raise GovernanceConflictError(
                        "budgeted extraction run has malformed target tag scope"
                    )
                for stale_key in sorted(set(raw_target_keys) - selected_keys):
                    stale_current = (
                        await session.execute(
                            select(TagAssignmentCurrent.id).where(
                                TagAssignmentCurrent.tenant_id == tenant_id,
                                TagAssignmentCurrent.subject_type == run.subject_type,
                                TagAssignmentCurrent.subject_id == run.subject_id,
                                TagAssignmentCurrent.tag_key == stale_key,
                            )
                        )
                    ).scalar_one_or_none()
                    if stale_current is None:
                        continue
                    await self._append_assignment_in_session(
                        session,
                        tenant_id=tenant_id,
                        subject_type=run.subject_type,
                        subject_id=run.subject_id,
                        tag_key=stale_key,
                        tag_value=None,
                        confidence=None,
                        evidence_refs=[],
                        source="llm",
                        schema_version_id=int(run.input_snapshot["schema_version_id"]),
                        tagger_version_id=run.tagger_version_id,
                        extraction_run_id=run.id,
                        deployment_id=run.deployment_id,
                        input_hash=run.input_hash,
                        actor_user_id=actor_user_id,
                        tombstone=True,
                        publish_current=True,
                    )
                run.output_snapshot = {
                    **dict(run.output_snapshot),
                    "publish_current": True,
                    "published_by_budget_job": True,
                }
            job.current_published_at = now
            job.revision += 1
            await self._audit(
                session,
                tenant_id=tenant_id,
                resource_type="tag_job",
                resource_id=job.id,
                action="current_published_atomically",
                actor_user_id=actor_user_id,
                payload={
                    "completed_items": job.completed_items,
                    "provider_tokens": job.budget_consumed_provider_tokens,
                    "provider_calls": job.budget_consumed_provider_calls,
                    "cost_microunits": job.budget_consumed_cost_microunits,
                },
            )
            return int(job.revision)

    async def ensure_current_fact(
        self,
        *,
        tenant_id: str,
        fact_id: int,
        extraction_run_id: int,
    ) -> None:
        """Repair current projection after a worker retry found an existing fact."""

        async with self._factory() as session, session.begin():
            fact = (
                await session.execute(
                    select(TagAssignmentFact).where(
                        TagAssignmentFact.id == fact_id,
                        TagAssignmentFact.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if fact is None:
                raise GovernanceNotFoundError("tag assignment fact not found")
            run = (
                await session.execute(
                    select(TagExtractionRun)
                    .where(
                        TagExtractionRun.id == extraction_run_id,
                        TagExtractionRun.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if run is None:
                raise AssignmentValidationError("extraction run does not exist")
            if (
                run.subject_type != fact.subject_type
                or run.subject_id != fact.subject_id
                or run.tagger_version_id != fact.tagger_version_id
                or run.deployment_id != fact.deployment_id
            ):
                raise AssignmentValidationError(
                    "cached extraction run lineage does not match the assignment"
                )
            if run.deployment_id is not None:
                deployment = (
                    await session.execute(
                        select(TagDeployment)
                        .where(
                            TagDeployment.id == run.deployment_id,
                            TagDeployment.tenant_id == tenant_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if (
                    deployment is None
                    or deployment.tagger_version_id != run.tagger_version_id
                    or not run.served_current
                    or run.deployment_stage != deployment.status
                    or run.deployment_revision != deployment.revision
                    or deployment.status
                    not in {"canary_5", "canary_25", "awaiting_admin", "production"}
                ):
                    return
            current = (
                await session.execute(
                    select(TagAssignmentCurrent)
                    .where(
                        TagAssignmentCurrent.tenant_id == tenant_id,
                        TagAssignmentCurrent.subject_type == fact.subject_type,
                        TagAssignmentCurrent.subject_id == fact.subject_id,
                        TagAssignmentCurrent.tag_key == fact.tag_key,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if current is None:
                session.add(
                    TagAssignmentCurrent(
                        tenant_id=tenant_id,
                        subject_type=fact.subject_type,
                        subject_id=fact.subject_id,
                        tag_key=fact.tag_key,
                        fact_id=fact.id,
                        revision=fact.revision,
                    )
                )
            elif current.fact_id != fact.id and current.revision < fact.revision:
                current_fact = (
                    await session.execute(
                        select(TagAssignmentFact).where(
                            TagAssignmentFact.id == current.fact_id,
                            TagAssignmentFact.tenant_id == tenant_id,
                        )
                    )
                ).scalar_one()
                if not (current_fact.source == "manual" and fact.source in {"rule", "llm"}):
                    current.fact_id = fact.id
                    current.revision = fact.revision

    async def get_fact_lineage(
        self,
        *,
        tenant_id: str,
        fact_id: int,
        actor_user_id: int,
        actor_role: str,
    ) -> dict[str, Any]:
        """Return a complete, tenant-safe provenance bundle for one fact."""

        async with self._factory() as session:
            fact = (
                await session.execute(
                    select(TagAssignmentFact).where(
                        TagAssignmentFact.id == fact_id,
                        TagAssignmentFact.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if fact is None:
                raise GovernanceNotFoundError("tag assignment fact not found")
            if actor_role == "agent":
                allowed = (
                    await session.execute(
                        select(Reception.id).where(
                            Reception.id == fact.reception_id,
                            Reception.tenant_id == tenant_id,
                            Reception.agent_user_id == actor_user_id,
                        )
                    )
                ).scalar_one_or_none()
                if allowed is None:
                    raise GovernanceNotFoundError("tag assignment fact not found")
            is_current = (
                await session.execute(
                    select(TagAssignmentCurrent.id).where(
                        TagAssignmentCurrent.tenant_id == tenant_id,
                        TagAssignmentCurrent.fact_id == fact.id,
                    )
                )
            ).scalar_one_or_none() is not None
            schema = (
                await session.execute(
                    select(TagSchemaVersion).where(
                        TagSchemaVersion.id == fact.schema_version_id,
                        TagSchemaVersion.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            tagger = (
                await session.execute(
                    select(TaggerVersion).where(
                        TaggerVersion.id == fact.tagger_version_id,
                        TaggerVersion.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            extraction_run = (
                await session.execute(
                    select(TagExtractionRun).where(
                        TagExtractionRun.id == fact.extraction_run_id,
                        TagExtractionRun.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            job = None
            if extraction_run is not None:
                job = (
                    await session.execute(
                        select(TagExtractionJob).where(
                            TagExtractionJob.id == extraction_run.job_id,
                            TagExtractionJob.tenant_id == tenant_id,
                        )
                    )
                ).scalar_one_or_none()
            deployment = (
                await session.execute(
                    select(TagDeployment).where(
                        TagDeployment.id == fact.deployment_id,
                        TagDeployment.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            return {
                "fact": fact,
                "is_current": is_current,
                "schema_version": schema,
                "tagger_version": tagger,
                "model_version": tagger.model_version if tagger is not None else None,
                "extraction_run": extraction_run,
                "job": job,
                "deployment": deployment,
            }

    @staticmethod
    async def _default_job_budget_from_baseline(
        session: AsyncSession,
        *,
        tenant_id: str,
        job_type: str,
        purpose: str,
        now: datetime,
    ) -> tuple[dict[str, int], int]:
        """Return a hard P99×1.2 budget only after a complete 7-day baseline."""

        baseline_predicates = (
            TagExtractionJob.tenant_id == tenant_id,
            TagExtractionJob.job_type == job_type,
            TagExtractionJob.budget_purpose == purpose,
            TagExtractionJob.status == "completed",
            TagExtractionJob.failed_items == 0,
            TagExtractionJob.completed_items == TagExtractionJob.total_items,
            TagExtractionJob.budget_usage_complete.is_(True),
            TagExtractionJob.finished_at.is_not(None),
            TagExtractionJob.budget_started_at.is_not(None),
        )
        sample_count, oldest_finished = (
            await session.execute(
                select(
                    func.count(TagExtractionJob.id),
                    func.min(TagExtractionJob.finished_at),
                ).where(*baseline_predicates)
            )
        ).one()
        sample_count = int(sample_count or 0)
        if sample_count < _JOB_BUDGET_BASELINE_MIN_SAMPLES or oldest_finished is None:
            return {}, sample_count
        if oldest_finished.tzinfo is None:
            oldest_finished = oldest_finished.replace(tzinfo=UTC)
        if now - oldest_finished < _JOB_BUDGET_BASELINE_AGE:
            return {}, sample_count

        # Age coverage must be measured over the complete history, while the
        # percentile itself stays bounded to the most recent observations.
        # Combining both concerns under LIMIT would make high-throughput
        # purposes remain in alert-only mode forever.
        samples = list(
            (
                await session.execute(
                    select(TagExtractionJob)
                    .where(*baseline_predicates)
                    .order_by(
                        TagExtractionJob.finished_at.desc(),
                        TagExtractionJob.id.desc(),
                    )
                    .limit(_JOB_BUDGET_BASELINE_MAX_SAMPLES)
                )
            )
            .scalars()
            .all()
        )
        provider_tokens = [
            int(sample.budget_consumed_provider_tokens) for sample in samples
        ]
        provider_calls = [
            int(sample.budget_consumed_provider_calls) for sample in samples
        ]
        costs = [
            int(sample.budget_consumed_cost_microunits) for sample in samples
        ]
        wall_seconds: list[int] = []
        for sample in samples:
            assert sample.finished_at is not None
            assert sample.budget_started_at is not None
            finished_at = sample.finished_at
            started_at = sample.budget_started_at
            if finished_at.tzinfo is None:
                finished_at = finished_at.replace(tzinfo=UTC)
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=UTC)
            wall_seconds.append(
                max(0, math.ceil((finished_at - started_at).total_seconds()))
            )

        def hard_limit(values: Sequence[int]) -> int:
            p99 = _nearest_rank_percentile(values, 0.99)
            return max(1, math.ceil(p99 * 1.2))

        return (
            {
                "max_provider_tokens": hard_limit(provider_tokens),
                "max_provider_calls": hard_limit(provider_calls),
                "max_cost_microunits": hard_limit(costs),
                "max_wall_seconds": hard_limit(wall_seconds),
            },
            sample_count,
        )

    async def enqueue_job(
        self,
        *,
        tenant_id: str,
        job_type: str,
        scope: dict[str, Any],
        idempotency_key: str,
        created_by: int,
        tagger_version_id: int | None = None,
        origin: str = "system",
    ) -> TagExtractionJob:
        if origin not in {"manual", "serving", "backfill", "monitor", "system"}:
            raise GovernanceError("unsupported tag job origin")
        budget_limits: dict[str, int] = {}
        if "budget" in scope:
            raw_budget = scope["budget"]
            if not isinstance(raw_budget, dict) or not raw_budget:
                raise GovernanceError("scope.budget must be a non-empty object")
            supported_budget_keys = {
                "max_provider_tokens",
                "max_provider_calls",
                "max_cost_microunits",
                "max_wall_seconds",
            }
            unknown_budget_keys = sorted(set(raw_budget) - supported_budget_keys)
            if unknown_budget_keys:
                raise GovernanceError(
                    "scope.budget contains unsupported limits: "
                    + ", ".join(unknown_budget_keys)
                )
            for key in sorted(supported_budget_keys.intersection(raw_budget)):
                value = raw_budget[key]
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    raise GovernanceError(f"scope.budget.{key} must be a positive integer")
                budget_limits[key] = value
        if origin == "manual":
            reserved_scope_keys = {
                "blind_mode",
                "deployment_id",
                "evaluation_run_id",
                "holdout_only",
                "optimization_run_id",
                "reason",
                "release_service",
                "review_bundle_id",
                "sampling_probability",
                "sealed_holdout_query",
                "selection_policy",
                "selection_policy_version",
                "source_deployment_id",
                "source_extraction_run_id",
                "source_harness_execution_id",
                "subjects",
                "tagger_version_id",
                "trusted_observation_id",
            }
            attempted = sorted(reserved_scope_keys.intersection(scope))
            if attempted:
                raise GovernanceError(
                    "manual tag jobs cannot set service-owned scope fields: " + ", ".join(attempted)
                )
        if job_type == "evaluate":
            raise GovernanceError("evaluation jobs must be created through /tag-evaluations")
        ids: list[Any]
        if job_type in {"extract", "recompute"}:
            dialogue_ids = scope.get("dialogue_unit_ids")
            reception_ids = scope.get("reception_ids")
            populated = [
                value
                for value in (dialogue_ids, reception_ids)
                if isinstance(value, list) and value
            ]
            if len(populated) != 1:
                raise GovernanceError(
                    f"{job_type} requires exactly one non-empty "
                    "dialogue_unit_ids or reception_ids list"
                )
            ids = populated[0]
        elif job_type == "review_batch":
            subjects = scope.get("subjects")
            if not isinstance(subjects, list) or not subjects:
                raise GovernanceError("review_batch requires a non-empty scope.subjects list")
            if any(
                not isinstance(item, dict)
                or item.get("subject_type") not in {"dialogue_unit", "reception"}
                or not isinstance(item.get("subject_id"), int)
                or int(item["subject_id"]) <= 0
                or not str(item.get("tag_key", "")).strip()
                for item in subjects
            ):
                raise GovernanceError("review_batch contains an invalid subject")
            ids = subjects
        elif job_type == "remediate":
            dialogue_ids = scope.get("dialogue_unit_ids")
            if not isinstance(dialogue_ids, list) or not dialogue_ids:
                raise GovernanceError("remediate requires a non-empty scope.dialogue_unit_ids list")
            ids = dialogue_ids
        else:
            raise GovernanceError("unsupported tag job type")
        if any(
            not isinstance(item, int) or item <= 0 for item in ids if not isinstance(item, dict)
        ):
            raise GovernanceError("job scope IDs must be positive integers")
        budget_purpose = _job_budget_purpose(job_type=job_type, scope=scope)
        budget_source = "explicit" if budget_limits else "alert_only"
        budget_baseline_sample_count = 0
        async with self._factory() as session, session.begin():
            existing = (
                await session.execute(
                    select(TagExtractionJob).where(
                        TagExtractionJob.tenant_id == tenant_id,
                        TagExtractionJob.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                if (
                    existing.job_type != job_type
                    or existing.scope != scope
                    or existing.tagger_version_id != tagger_version_id
                    or existing.origin != origin
                ):
                    raise GovernanceConflictError(
                        "idempotency key was already used for a different request"
                    )
                return existing
            if tagger_version_id is not None:
                tagger = (
                    await session.execute(
                        select(TaggerVersion).where(
                            TaggerVersion.id == tagger_version_id,
                            TaggerVersion.tenant_id == tenant_id,
                        )
                    )
                ).scalar_one_or_none()
                if tagger is None:
                    raise GovernanceNotFoundError("tagger version not found")
                if tagger.status != "qualified":
                    raise GovernanceConflictError(
                        "manual tag jobs require a qualified tagger version"
                    )
                scoped_schema_version_id = scope.get("schema_version_id")
                if scoped_schema_version_id is not None:
                    if (
                        not isinstance(scoped_schema_version_id, int)
                        or isinstance(scoped_schema_version_id, bool)
                        or scoped_schema_version_id <= 0
                    ):
                        raise GovernanceError("scope.schema_version_id must be a positive integer")
                    if tagger.schema_version_id != scoped_schema_version_id:
                        raise GovernanceConflictError(
                            "tagger version does not belong to scope.schema_version_id"
                        )
            if (
                not budget_limits
                and job_type in {"extract", "recompute", "remediate"}
            ):
                (
                    derived_limits,
                    budget_baseline_sample_count,
                ) = await self._default_job_budget_from_baseline(
                    session,
                    tenant_id=tenant_id,
                    job_type=job_type,
                    purpose=budget_purpose,
                    now=_utcnow(),
                )
                if derived_limits:
                    budget_limits = derived_limits
                    budget_source = "default_p99"
            job = TagExtractionJob(
                tenant_id=tenant_id,
                job_type=job_type,
                origin=origin,
                status="queued",
                scope=scope,
                tagger_version_id=tagger_version_id,
                idempotency_key=idempotency_key,
                total_items=len(ids),
                completed_items=0,
                failed_items=0,
                failed_subset=[],
                budget_max_provider_tokens=budget_limits.get("max_provider_tokens"),
                budget_max_provider_calls=budget_limits.get("max_provider_calls"),
                budget_max_cost_microunits=budget_limits.get("max_cost_microunits"),
                budget_max_wall_seconds=budget_limits.get("max_wall_seconds"),
                budget_reserved_provider_tokens=0,
                budget_reserved_provider_calls=0,
                budget_reserved_cost_microunits=0,
                budget_consumed_provider_tokens=0,
                budget_consumed_provider_calls=0,
                budget_consumed_cost_microunits=0,
                budget_source=budget_source,
                budget_purpose=budget_purpose,
                budget_baseline_sample_count=budget_baseline_sample_count,
                budget_accounted_items=0,
                budget_usage_complete=False,
                attempt_count=0,
                max_attempts=3,
                revision=1,
                created_by=created_by,
            )
            try:
                async with session.begin_nested():
                    session.add(job)
                    await session.flush()
            except IntegrityError as exc:
                existing = (
                    await session.execute(
                        select(TagExtractionJob).where(
                            TagExtractionJob.tenant_id == tenant_id,
                            TagExtractionJob.idempotency_key == idempotency_key,
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:
                    raise GovernanceConflictError(
                        "job enqueue conflicted; retry with the same idempotency key"
                    ) from exc
                if (
                    existing.job_type != job_type
                    or existing.scope != scope
                    or existing.tagger_version_id != tagger_version_id
                    or existing.origin != origin
                ):
                    raise GovernanceConflictError(
                        "idempotency key was already used for a different request"
                    ) from exc
                return existing
            await self._audit(
                session,
                tenant_id=tenant_id,
                resource_type="tag_job",
                resource_id=job.id,
                action="queued",
                actor_user_id=created_by,
                payload={
                    "job_type": job_type,
                    "scope": scope,
                    "budget_source": budget_source,
                    "budget_purpose": budget_purpose,
                    "budget_baseline_sample_count": budget_baseline_sample_count,
                },
            )
            if budget_source == "alert_only":
                await self._audit(
                    session,
                    tenant_id=tenant_id,
                    resource_type="tag_job",
                    resource_id=job.id,
                    action="budget_alert_only",
                    actor_user_id=created_by,
                    payload={
                        "job_type": job_type,
                        "budget_purpose": budget_purpose,
                        "budget_baseline_sample_count": (
                            budget_baseline_sample_count
                        ),
                        "minimum_samples": _JOB_BUDGET_BASELINE_MIN_SAMPLES,
                        "minimum_age_seconds": int(
                            _JOB_BUDGET_BASELINE_AGE.total_seconds()
                        ),
                    },
                )
            return job

    async def get_job(self, *, tenant_id: str, job_id: int) -> TagExtractionJob:
        async with self._factory() as session:
            job = (
                await session.execute(
                    select(TagExtractionJob).where(
                        TagExtractionJob.id == job_id,
                        TagExtractionJob.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if job is None:
                raise GovernanceNotFoundError("tag job not found")
            return job

    async def list_jobs(
        self,
        *,
        tenant_id: str,
        limit: int = 200,
    ) -> list[TagExtractionJob]:
        async with self._factory() as session:
            return list(
                (
                    await session.execute(
                        select(TagExtractionJob)
                        .where(TagExtractionJob.tenant_id == tenant_id)
                        .order_by(
                            TagExtractionJob.created_at.desc(),
                            TagExtractionJob.id.desc(),
                        )
                        .limit(_bounded_list_limit(limit))
                    )
                )
                .scalars()
                .all()
            )

    def _claimable_job_query(self, now: datetime) -> Select[tuple[TagExtractionJob]]:
        return (
            select(TagExtractionJob)
            .where(
                or_(
                    TagExtractionJob.status == "queued",
                    (
                        (TagExtractionJob.status == "retry_wait")
                        & or_(
                            TagExtractionJob.next_attempt_at.is_(None),
                            TagExtractionJob.next_attempt_at <= now,
                        )
                    ),
                    (
                        (TagExtractionJob.status == "running")
                        & (TagExtractionJob.lease_expires_at < now)
                    ),
                )
            )
            .order_by(TagExtractionJob.created_at, TagExtractionJob.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )

    @staticmethod
    async def _sync_evaluation_job_state(
        session: AsyncSession,
        *,
        job: TagExtractionJob,
        state: str,
        now: datetime,
        error_message: str | None = None,
    ) -> None:
        """Keep an evaluate job, its run and candidate tagger in one state machine."""

        if job.job_type != "evaluate":
            return
        evaluation_run_id = job.scope.get("evaluation_run_id")
        if not isinstance(evaluation_run_id, int):
            return
        run = (
            await session.execute(
                select(TagEvaluationRun)
                .where(
                    TagEvaluationRun.id == evaluation_run_id,
                    TagEvaluationRun.tenant_id == job.tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if run is None or run.status == "completed":
            return
        tagger = (
            await session.execute(
                select(TaggerVersion)
                .where(
                    TaggerVersion.id == run.tagger_version_id,
                    TaggerVersion.tenant_id == job.tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if state == "queued":
            run.status = "queued"
            run.finished_at = None
            run.passed = False
            if tagger is not None:
                tagger.status = "validating"
                tagger.qualified_at = None
            return
        if state not in {"failed", "cancelled"}:
            return
        run.status = "failed"
        run.finished_at = now
        run.passed = False
        run.metrics = {
            **dict(run.metrics),
            "terminal_reason": state,
            "error_message": (error_message or "")[:4_000],
        }
        if tagger is not None:
            tagger.status = "rejected"
            tagger.qualified_at = None

    async def claim_next_job(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_for: timedelta,
    ) -> TagExtractionJob | None:
        async with self._factory() as session, session.begin():
            job = (await session.execute(self._claimable_job_query(now))).scalar_one_or_none()
            if job is None:
                return None
            if job.attempt_count >= job.max_attempts:
                job.status = "failed"
                job.finished_at = now
                await self._sync_evaluation_job_state(
                    session,
                    job=job,
                    state="failed",
                    now=now,
                    error_message="maximum attempts exhausted before claim",
                )
                return None
            # A process may die after the durable reservation and before
            # settlement.  Treat that reservation as fully consumed when a
            # later lease reclaims the job; resetting it would create an
            # unbounded spend loophole across retries.
            if (
                job.budget_reserved_provider_tokens
                or job.budget_reserved_provider_calls
                or job.budget_reserved_cost_microunits
            ):
                job.budget_consumed_provider_tokens += (
                    job.budget_reserved_provider_tokens
                )
                job.budget_consumed_provider_calls += (
                    job.budget_reserved_provider_calls
                )
                job.budget_consumed_cost_microunits += (
                    job.budget_reserved_cost_microunits
                )
                job.budget_reserved_provider_tokens = 0
                job.budget_reserved_provider_calls = 0
                job.budget_reserved_cost_microunits = 0
            job.status = "running"
            job.attempt_count += 1
            job.lease_owner = worker_id
            job.lease_token = secrets.token_hex(16)
            job.lease_expires_at = now + lease_for
            job.next_attempt_at = None
            if (
                job.job_type in {"extract", "recompute", "remediate"}
                and job.budget_source == "alert_only"
                and job.budget_accounted_items == 0
            ):
                job.budget_purpose = _job_budget_purpose(
                    job_type=job.job_type,
                    scope=job.scope,
                )
            if (
                job.budget_started_at is None
                and (
                    job.job_type in {"extract", "recompute", "remediate"}
                    or any(
                        limit is not None
                        for limit in (
                            job.budget_max_provider_tokens,
                            job.budget_max_provider_calls,
                            job.budget_max_cost_microunits,
                            job.budget_max_wall_seconds,
                        )
                    )
                )
            ):
                job.budget_started_at = now
            job.revision += 1
            await session.flush()
            return job

    async def heartbeat_job(
        self,
        job_id: int,
        *,
        tenant_id: str,
        worker_id: str,
        expected_revision: int,
        now: datetime,
        lease_for: timedelta,
    ) -> bool:
        async with self._factory() as session, session.begin():
            result = await session.execute(
                update(TagExtractionJob)
                .where(
                    TagExtractionJob.id == job_id,
                    TagExtractionJob.tenant_id == tenant_id,
                    TagExtractionJob.status == "running",
                    TagExtractionJob.lease_owner == worker_id,
                    TagExtractionJob.revision == expected_revision,
                    TagExtractionJob.lease_expires_at >= now,
                )
                .values(
                    lease_expires_at=now + lease_for,
                    revision=TagExtractionJob.revision + 1,
                )
            )
            return bool(cast(CursorResult[Any], result).rowcount)

    async def reserve_job_budget(
        self,
        *,
        tenant_id: str,
        job_id: int,
        worker_id: str,
        expected_revision: int,
        now: datetime,
    ) -> TagJobBudgetReservation | None:
        """Atomically reserve all remaining bounded capacity for one item.

        Reserving the full remainder is deliberately conservative.  The
        extractor receives the same limits and settlement releases unused
        capacity, so a concurrent or restarted worker can never spend capacity
        that was only held in another process's memory.
        """

        exhausted_message: str | None = None
        exhausted_revision: int | None = None
        reservation: TagJobBudgetReservation | None = None
        async with self._factory() as session, session.begin():
            job = (
                await session.execute(
                    select(TagExtractionJob)
                    .where(
                        TagExtractionJob.id == job_id,
                        TagExtractionJob.tenant_id == tenant_id,
                        TagExtractionJob.status == "running",
                        TagExtractionJob.lease_owner == worker_id,
                        TagExtractionJob.revision == expected_revision,
                        TagExtractionJob.lease_expires_at >= now,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if job is None:
                raise GovernanceConflictError("tag job lease was lost before budget reservation")
            limits = {
                "provider_tokens": job.budget_max_provider_tokens,
                "provider_calls": job.budget_max_provider_calls,
                "cost_microunits": job.budget_max_cost_microunits,
            }
            if not any(
                value is not None
                for value in (*limits.values(), job.budget_max_wall_seconds)
            ):
                return None
            if (
                job.budget_reserved_provider_tokens
                or job.budget_reserved_provider_calls
                or job.budget_reserved_cost_microunits
            ):
                raise GovernanceConflictError(
                    "tag job already has an unsettled provider budget reservation"
                )
            started_at = job.budget_started_at or now
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=UTC)
            job.budget_started_at = started_at
            elapsed_seconds = max(0.0, (now - started_at).total_seconds())
            remaining_wall_seconds: int | None = None
            if job.budget_max_wall_seconds is not None:
                wall_remainder = job.budget_max_wall_seconds - elapsed_seconds
                if wall_remainder <= 0:
                    exhausted_message = "tag job budget exhausted: max_wall_seconds"
                else:
                    remaining_wall_seconds = max(1, math.ceil(wall_remainder))
            remaining = {
                "provider_tokens": (
                    job.budget_max_provider_tokens
                    - job.budget_consumed_provider_tokens
                    if job.budget_max_provider_tokens is not None
                    else None
                ),
                "provider_calls": (
                    job.budget_max_provider_calls
                    - job.budget_consumed_provider_calls
                    if job.budget_max_provider_calls is not None
                    else None
                ),
                "cost_microunits": (
                    job.budget_max_cost_microunits
                    - job.budget_consumed_cost_microunits
                    if job.budget_max_cost_microunits is not None
                    else None
                ),
            }
            if exhausted_message is None:
                for dimension, value in remaining.items():
                    if value is not None and value <= 0:
                        exhausted_message = (
                            f"tag job budget exhausted: max_{dimension}"
                        )
                        break
            if job.budget_exhausted_at is not None and exhausted_message is None:
                exhausted_message = "tag job budget was already exhausted"
            if exhausted_message is not None:
                job.budget_exhausted_at = now
                job.last_error_code = TagJobBudgetExhaustedError.error_code
                job.last_error_message = exhausted_message
                job.revision += 1
                exhausted_revision = int(job.revision)
            else:
                job.budget_reserved_provider_tokens = int(
                    remaining["provider_tokens"] or 0
                )
                job.budget_reserved_provider_calls = int(
                    remaining["provider_calls"] or 0
                )
                job.budget_reserved_cost_microunits = int(
                    remaining["cost_microunits"] or 0
                )
                job.revision += 1
                reservation = TagJobBudgetReservation(
                    revision=int(job.revision),
                    max_provider_tokens=remaining["provider_tokens"],
                    max_provider_calls=remaining["provider_calls"],
                    max_cost_microunits=remaining["cost_microunits"],
                    max_wall_seconds=remaining_wall_seconds,
                )
        if exhausted_message is not None:
            raise TagJobBudgetExhaustedError(
                exhausted_message,
                revision=exhausted_revision,
            )
        return reservation

    async def settle_job_budget(
        self,
        *,
        tenant_id: str,
        job_id: int,
        worker_id: str,
        expected_revision: int,
        provider_tokens: int,
        provider_calls: int,
        cost_microunits: int,
        now: datetime,
        consume_reserved: bool = False,
    ) -> int:
        """Settle actual usage and release unused reservation capacity."""

        actual = {
            "provider_tokens": provider_tokens,
            "provider_calls": provider_calls,
            "cost_microunits": cost_microunits,
        }
        if any(isinstance(value, bool) or value < 0 for value in actual.values()):
            raise GovernanceError("tag job budget settlement usage must be non-negative")
        exhausted_message: str | None = None
        settled_revision: int
        async with self._factory() as session, session.begin():
            job = (
                await session.execute(
                    select(TagExtractionJob)
                    .where(
                        TagExtractionJob.id == job_id,
                        TagExtractionJob.tenant_id == tenant_id,
                        TagExtractionJob.status == "running",
                        TagExtractionJob.lease_owner == worker_id,
                        TagExtractionJob.revision == expected_revision,
                        TagExtractionJob.lease_expires_at >= now,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if job is None:
                raise GovernanceConflictError("tag job lease was lost before budget settlement")
            reserved = {
                "provider_tokens": job.budget_reserved_provider_tokens,
                "provider_calls": job.budget_reserved_provider_calls,
                "cost_microunits": job.budget_reserved_cost_microunits,
            }
            limits = {
                "provider_tokens": job.budget_max_provider_tokens,
                "provider_calls": job.budget_max_provider_calls,
                "cost_microunits": job.budget_max_cost_microunits,
            }
            settled = {
                key: reserved[key] if consume_reserved else actual[key]
                for key in actual
            }
            if not consume_reserved:
                for dimension, limit in limits.items():
                    if (
                        limit is not None
                        and actual[dimension] > reserved[dimension]
                        and exhausted_message is None
                    ):
                        exhausted_message = (
                            f"tag job budget settlement exceeded reserved {dimension}"
                        )
            job.budget_consumed_provider_tokens += settled["provider_tokens"]
            job.budget_consumed_provider_calls += settled["provider_calls"]
            job.budget_consumed_cost_microunits += settled["cost_microunits"]
            job.budget_accounted_items += 1
            job.budget_reserved_provider_tokens = 0
            job.budget_reserved_provider_calls = 0
            job.budget_reserved_cost_microunits = 0
            consumed = {
                "provider_tokens": job.budget_consumed_provider_tokens,
                "provider_calls": job.budget_consumed_provider_calls,
                "cost_microunits": job.budget_consumed_cost_microunits,
            }
            for dimension, limit in limits.items():
                if (
                    limit is not None
                    and consumed[dimension] > limit
                    and exhausted_message is None
                ):
                    exhausted_message = (
                        f"tag job budget exhausted during {dimension} settlement"
                    )
            if (
                job.budget_max_wall_seconds is not None
                and job.budget_started_at is not None
            ):
                started_at = job.budget_started_at
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=UTC)
                if (
                    now - started_at
                ).total_seconds() > job.budget_max_wall_seconds:
                    exhausted_message = (
                        exhausted_message
                        or "tag job budget exhausted during max_wall_seconds settlement"
                    )
            if exhausted_message is not None:
                job.budget_exhausted_at = now
                job.last_error_code = TagJobBudgetExhaustedError.error_code
                job.last_error_message = exhausted_message
            job.revision += 1
            settled_revision = int(job.revision)
        if exhausted_message is not None:
            raise TagJobBudgetExhaustedError(
                exhausted_message,
                revision=settled_revision,
            )
        return settled_revision

    async def advance_job_progress(
        self,
        *,
        tenant_id: str,
        job_id: int,
        worker_id: str,
        expected_revision: int,
        success: bool,
        item_ref: Any,
        now: datetime,
        lease_for: timedelta,
    ) -> int | None:
        """Atomically record one real item result and renew the worker lease."""

        async with self._factory() as session, session.begin():
            job = (
                await session.execute(
                    select(TagExtractionJob)
                    .where(
                        TagExtractionJob.id == job_id,
                        TagExtractionJob.tenant_id == tenant_id,
                        TagExtractionJob.status == "running",
                        TagExtractionJob.lease_owner == worker_id,
                        TagExtractionJob.revision == expected_revision,
                        TagExtractionJob.lease_expires_at >= now,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if job is None:
                return None
            failed_subset = list(job.failed_subset or [])
            if success:
                job.completed_items += 1
                failed_subset = [value for value in failed_subset if value != item_ref]
            else:
                job.failed_items += 1
                if item_ref not in failed_subset:
                    failed_subset.append(item_ref)
            job.failed_subset = failed_subset
            job.lease_expires_at = now + lease_for
            job.revision += 1
            return int(job.revision)

    async def job_lease_is_active(
        self,
        *,
        tenant_id: str,
        job_id: int,
        worker_id: str,
        expected_revision: int,
        now: datetime,
    ) -> bool:
        """Check cancellation/CAS state immediately before one scoped item."""

        async with self._factory() as session:
            return (
                await session.execute(
                    select(TagExtractionJob.id).where(
                        TagExtractionJob.id == job_id,
                        TagExtractionJob.tenant_id == tenant_id,
                        TagExtractionJob.status == "running",
                        TagExtractionJob.lease_owner == worker_id,
                        TagExtractionJob.revision == expected_revision,
                        TagExtractionJob.lease_expires_at >= now,
                    )
                )
            ).scalar_one_or_none() is not None

    async def finish_job(
        self,
        *,
        tenant_id: str,
        job_id: int,
        worker_id: str,
        expected_revision: int,
        now: datetime,
    ) -> bool:
        """Move a fully processed lease to its truthful terminal state."""

        async with self._factory() as session, session.begin():
            job = (
                await session.execute(
                    select(TagExtractionJob)
                    .where(
                        TagExtractionJob.id == job_id,
                        TagExtractionJob.tenant_id == tenant_id,
                        TagExtractionJob.status == "running",
                        TagExtractionJob.lease_owner == worker_id,
                        TagExtractionJob.revision == expected_revision,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if job is None:
                return False
            processed = job.completed_items + job.failed_items
            if processed < job.total_items:
                raise GovernanceConflictError(
                    "job cannot finish before every scoped item is accounted for"
                )
            job.status = "completed" if job.failed_items == 0 else "failed"
            job.budget_usage_complete = bool(
                job.status == "completed"
                and job.job_type in {"extract", "recompute", "remediate"}
                and job.budget_accounted_items >= job.total_items
                and job.budget_reserved_provider_tokens == 0
                and job.budget_reserved_provider_calls == 0
                and job.budget_reserved_cost_microunits == 0
            )
            job.finished_at = now
            job.lease_owner = None
            job.lease_token = None
            job.lease_expires_at = None
            job.revision += 1
            if job.status == "failed":
                await self._sync_evaluation_job_state(
                    session,
                    job=job,
                    state="failed",
                    now=now,
                    error_message=job.last_error_message,
                )
            return True

    async def defer_job_failure(
        self,
        *,
        tenant_id: str,
        job_id: int,
        worker_id: str,
        expected_revision: int,
        error_code: str,
        error_message: str,
        now: datetime,
    ) -> bool:
        """Release a failed lease with exponential retry or a final failure."""

        async with self._factory() as session, session.begin():
            job = (
                await session.execute(
                    select(TagExtractionJob)
                    .where(
                        TagExtractionJob.id == job_id,
                        TagExtractionJob.tenant_id == tenant_id,
                        TagExtractionJob.status == "running",
                        TagExtractionJob.lease_owner == worker_id,
                        TagExtractionJob.revision == expected_revision,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if job is None:
                return False
            retryable = (
                error_code != TagJobBudgetExhaustedError.error_code
                and job.attempt_count < job.max_attempts
            )
            job.status = "retry_wait" if retryable else "failed"
            job.next_attempt_at = (
                now + timedelta(seconds=min(300, 2 ** max(0, job.attempt_count - 1)))
                if retryable
                else None
            )
            job.finished_at = None if retryable else now
            job.last_error_code = error_code[:64]
            job.last_error_message = error_message[:4_000]
            job.lease_owner = None
            job.lease_token = None
            job.lease_expires_at = None
            job.revision += 1
            await self._sync_evaluation_job_state(
                session,
                job=job,
                state="queued" if retryable else "failed",
                now=now,
                error_message=error_message,
            )
            return True

    async def retry_job(
        self, *, tenant_id: str, job_id: int, actor_user_id: int
    ) -> TagExtractionJob:
        async with self._factory() as session, session.begin():
            job = (
                await session.execute(
                    select(TagExtractionJob)
                    .where(
                        TagExtractionJob.id == job_id,
                        TagExtractionJob.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if job is None:
                raise GovernanceNotFoundError("tag job not found")
            if job.status not in {"failed", "retry_wait"}:
                raise GovernanceConflictError("only failed/retry_wait jobs can be retried")
            if job.job_type == "evaluate":
                evaluation_run_id = job.scope.get("evaluation_run_id")
                if isinstance(evaluation_run_id, int):
                    evaluation = (
                        await session.execute(
                            select(TagEvaluationRun)
                            .where(
                                TagEvaluationRun.id == evaluation_run_id,
                                TagEvaluationRun.tenant_id == tenant_id,
                            )
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if evaluation is not None:
                        candidate = (
                            await session.execute(
                                select(TaggerVersion)
                                .where(
                                    TaggerVersion.id == evaluation.tagger_version_id,
                                    TaggerVersion.tenant_id == tenant_id,
                                )
                                .with_for_update()
                            )
                        ).scalar_one_or_none()
                        if candidate is not None and candidate.status == "rejected":
                            raise GovernanceConflictError(
                                "evaluation jobs for rejected candidates cannot be retried"
                            )
            job.status = "queued"
            job.failed_items = 0
            job.attempt_count = 0
            job.lease_owner = None
            job.lease_token = None
            job.lease_expires_at = None
            job.next_attempt_at = None
            job.last_error_code = None
            job.last_error_message = None
            job.finished_at = None
            job.revision += 1
            await self._sync_evaluation_job_state(
                session,
                job=job,
                state="queued",
                now=_utcnow(),
            )
            await self._audit(
                session,
                tenant_id=tenant_id,
                resource_type="tag_job",
                resource_id=job.id,
                action="retried",
                actor_user_id=actor_user_id,
            )
            return job

    async def cancel_job(
        self, *, tenant_id: str, job_id: int, actor_user_id: int
    ) -> TagExtractionJob:
        async with self._factory() as session, session.begin():
            job = (
                await session.execute(
                    select(TagExtractionJob)
                    .where(
                        TagExtractionJob.id == job_id,
                        TagExtractionJob.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if job is None:
                raise GovernanceNotFoundError("tag job not found")
            if (
                job.job_type in {"optimize", "evaluate"}
                and job.scope.get("optimization_run_id") is not None
            ):
                raise GovernanceConflictError(
                    "optimizer-owned jobs must be cancelled through "
                    "the optimization-run cancel endpoint"
                )
            if job.status in {"completed", "cancelled"}:
                return job
            job.status = "cancelled"
            job.finished_at = _utcnow()
            job.lease_owner = None
            job.lease_token = None
            job.lease_expires_at = None
            job.next_attempt_at = None
            job.revision += 1
            await self._sync_evaluation_job_state(
                session,
                job=job,
                state="cancelled",
                now=job.finished_at,
                error_message="evaluation job cancelled",
            )
            await self._audit(
                session,
                tenant_id=tenant_id,
                resource_type="tag_job",
                resource_id=job.id,
                action="cancelled",
                actor_user_id=actor_user_id,
            )
            return job

    async def create_review_batch(
        self,
        *,
        tenant_id: str,
        reason: str,
        subjects: list[dict[str, Any]],
        actor_user_id: int,
        batch_id: str | None = None,
        review_bundle_id: str | None = None,
        selection_policy: str = "legacy",
        selection_policy_version: str = "1",
        sampling_probability: float | None = None,
        blind_mode: bool = False,
        trusted_sampling_lineage: bool = False,
        trusted_observation_id: int | None = None,
    ) -> list[TagReviewTask]:
        if reason == "adjudication":
            raise GovernanceError(
                "adjudication review tasks are system-created after two blind T2 rounds"
            )
        resolved_batch_id = batch_id or f"review-{secrets.token_hex(12)}"
        effective_blind_mode = bool(blind_mode or reason in _BLIND_REVIEW_REASONS)
        resolved_review_bundle_id = review_bundle_id or (
            f"{resolved_batch_id}-blind" if effective_blind_mode else None
        )
        async with self._factory() as session, session.begin():
            if actor_user_id > 0:
                actor_lock = (
                    await session.execute(
                        select(User.id)
                        .where(
                            User.id == actor_user_id,
                            User.tenant_id == tenant_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if actor_lock is not None:
                    active_blind = (
                        await session.execute(
                            select(TagReviewTask.id)
                            .where(
                                TagReviewTask.tenant_id == tenant_id,
                                TagReviewTask.blind_mode.is_(True),
                                TagReviewTask.status == "claimed",
                                TagReviewTask.claimed_by == actor_user_id,
                            )
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if active_blind is not None:
                        raise GovernanceConflictError(
                            "active blind reviewers cannot create side-channel review tasks"
                        )
                    prior_global_access = (
                        await session.execute(
                            select(TagGovernanceAuditEvent.id)
                            .where(
                                TagGovernanceAuditEvent.tenant_id == tenant_id,
                                TagGovernanceAuditEvent.actor_user_id == actor_user_id,
                                TagGovernanceAuditEvent.action == "blind_sensitive_read",
                                TagGovernanceAuditEvent.resource_type == "semantic_global",
                                TagGovernanceAuditEvent.resource_id == 0,
                            )
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if prior_global_access is None:
                        await self._audit(
                            session,
                            tenant_id=tenant_id,
                            resource_type="semantic_global",
                            resource_id=0,
                            action="blind_sensitive_read",
                            actor_user_id=actor_user_id,
                            payload={"access_kind": "review_batch_creation"},
                        )
            trusted_observation: TagDeploymentObservation | None = None
            if trusted_observation_id is not None:
                trusted_observation = (
                    await session.execute(
                        select(TagDeploymentObservation).where(
                            TagDeploymentObservation.id == trusted_observation_id,
                            TagDeploymentObservation.tenant_id == tenant_id,
                            TagDeploymentObservation.source == "monitor",
                            TagDeploymentObservation.is_trusted.is_(True),
                        )
                    )
                ).scalar_one_or_none()
                if trusted_observation is None:
                    raise GovernanceError(
                        "review sampling lineage requires a trusted monitor observation"
                    )
                trusted_sampling_lineage = True
            if not trusted_sampling_lineage and (
                sampling_probability is not None or selection_policy in _TRUSTED_SAMPLING_POLICIES
            ):
                raise GovernanceError(
                    "representative sampling policy and probability are service-owned"
                )
            if sampling_probability is not None and not 0 < sampling_probability <= 1:
                raise GovernanceError("review sampling_probability must be in (0, 1]")
            if batch_id is not None:
                existing = list(
                    (
                        await session.execute(
                            select(TagReviewTask)
                            .where(
                                TagReviewTask.tenant_id == tenant_id,
                                TagReviewTask.batch_id == resolved_batch_id,
                            )
                            .order_by(TagReviewTask.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                if existing:
                    return existing
            tasks: list[TagReviewTask] = []
            gold_source_runs: dict[tuple[str, int, int], TagExtractionRun] = {}
            for item in subjects:
                subject_type = str(item["subject_type"])
                subject_id = int(item["subject_id"])
                reception_id, scenario = await self._resolve_subject(
                    session,
                    tenant_id=tenant_id,
                    subject_type=subject_type,
                    subject_id=subject_id,
                )
                proposed_fact_id = None if reason == "gold" else item.get("proposed_fact_id")
                proposed_fact: TagAssignmentFact | None = None
                if proposed_fact_id is not None:
                    proposed_fact = (
                        await session.execute(
                            select(TagAssignmentFact).where(
                                TagAssignmentFact.id == int(proposed_fact_id),
                                TagAssignmentFact.tenant_id == tenant_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if (
                        proposed_fact is None
                        or proposed_fact.subject_type != subject_type
                        or proposed_fact.subject_id != subject_id
                        or proposed_fact.tag_key != str(item["tag_key"])
                    ):
                        raise AssignmentValidationError(
                            "proposed fact does not match the review subject"
                        )
                elif reason != "gold":
                    proposed_fact = (
                        await session.execute(
                            select(TagAssignmentFact)
                            .join(
                                TagAssignmentCurrent,
                                and_(
                                    TagAssignmentCurrent.fact_id == TagAssignmentFact.id,
                                    TagAssignmentCurrent.tenant_id == TagAssignmentFact.tenant_id,
                                ),
                            )
                            .where(
                                TagAssignmentCurrent.tenant_id == tenant_id,
                                TagAssignmentCurrent.subject_type == subject_type,
                                TagAssignmentCurrent.subject_id == subject_id,
                                TagAssignmentCurrent.tag_key == str(item["tag_key"]),
                                TagAssignmentFact.tenant_id == tenant_id,
                                TagAssignmentFact.tombstone.is_(False),
                            )
                        )
                    ).scalar_one_or_none()
                schema_version_id = (
                    item.get("schema_version_id")
                    if reason == "gold"
                    else (
                        proposed_fact.schema_version_id
                        if proposed_fact is not None
                        else item.get("schema_version_id")
                    )
                )
                if schema_version_id is None:
                    raise AssignmentValidationError(
                        "review subjects require a published schema version"
                    )
                schema_version = (
                    await session.execute(
                        select(TagSchemaVersion).where(
                            TagSchemaVersion.id == int(schema_version_id),
                            TagSchemaVersion.tenant_id == tenant_id,
                            TagSchemaVersion.status.in_(["published", "deprecated"]),
                        )
                    )
                ).scalar_one_or_none()
                if schema_version is None:
                    raise AssignmentValidationError(
                        "review schema does not exist as an immutable tenant version"
                    )
                definition = next(
                    (
                        definition
                        for definition in schema_version.definitions
                        if isinstance(definition, dict)
                        and definition.get("key") == str(item["tag_key"])
                    ),
                    None,
                )
                if (
                    definition is None
                    or subject_type not in (definition.get("subject_types") or [])
                    or (definition.get("scenarios") and scenario not in definition["scenarios"])
                ):
                    raise AssignmentValidationError("review tag does not apply to this subject")
                server_bound_source_run: TagExtractionRun | None = None
                if reason == "gold":
                    gold_source_key = (subject_type, subject_id, int(schema_version_id))
                    server_bound_source_run = gold_source_runs.get(gold_source_key)
                    if server_bound_source_run is None:
                        candidate_source_runs = list(
                            (
                                await session.execute(
                                    select(TagExtractionRun)
                                    .join(
                                        TaggerVersion,
                                        TaggerVersion.id == TagExtractionRun.tagger_version_id,
                                    )
                                    .where(
                                        TagExtractionRun.tenant_id == tenant_id,
                                        TagExtractionRun.subject_type == subject_type,
                                        TagExtractionRun.subject_id == subject_id,
                                        TagExtractionRun.status == "completed",
                                        TaggerVersion.tenant_id == tenant_id,
                                        TaggerVersion.schema_version_id == int(schema_version_id),
                                        TaggerVersion.status == "qualified",
                                    )
                                    .order_by(
                                        TagExtractionRun.served_current.desc(),
                                        TagExtractionRun.finished_at.desc(),
                                        TagExtractionRun.id.desc(),
                                    )
                                    .limit(20)
                                )
                            )
                            .scalars()
                            .all()
                        )
                        server_bound_source_run = next(
                            (
                                run
                                for run in candidate_source_runs
                                if run.input_hash
                                and isinstance(run.input_snapshot, Mapping)
                                and bool(run.input_snapshot)
                            ),
                            None,
                        )
                    if server_bound_source_run is None:
                        raise AssignmentValidationError(
                            "gold matrix cells require a server-owned completed "
                            "extraction snapshot for the published schema"
                        )
                    gold_source_runs[gold_source_key] = server_bound_source_run
                    proposed_fact = (
                        await session.execute(
                            select(TagAssignmentFact)
                            .where(
                                TagAssignmentFact.tenant_id == tenant_id,
                                TagAssignmentFact.extraction_run_id == server_bound_source_run.id,
                                TagAssignmentFact.subject_type == subject_type,
                                TagAssignmentFact.subject_id == subject_id,
                                TagAssignmentFact.tag_key == str(item["tag_key"]),
                                TagAssignmentFact.tombstone.is_(False),
                            )
                            .order_by(
                                TagAssignmentFact.revision.desc(),
                                TagAssignmentFact.id.desc(),
                            )
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                tagger_version_id = (
                    proposed_fact.tagger_version_id
                    if proposed_fact is not None
                    else (
                        server_bound_source_run.tagger_version_id
                        if server_bound_source_run is not None
                        else item.get("tagger_version_id")
                    )
                )
                source_deployment_id = (
                    proposed_fact.deployment_id
                    if proposed_fact is not None
                    else (
                        server_bound_source_run.deployment_id
                        if server_bound_source_run is not None
                        else (
                            item.get("source_deployment_id") if trusted_sampling_lineage else None
                        )
                    )
                )
                source_extraction_run_id = (
                    proposed_fact.extraction_run_id
                    if proposed_fact is not None
                    else (
                        server_bound_source_run.id
                        if server_bound_source_run is not None
                        else (
                            item.get("source_extraction_run_id")
                            if trusted_sampling_lineage
                            else None
                        )
                    )
                )
                source_harness_execution_id = (
                    item.get("source_harness_execution_id") if trusted_sampling_lineage else None
                )
                source_run: TagExtractionRun | None = server_bound_source_run
                if source_extraction_run_id is not None:
                    source_extraction_run_id = int(source_extraction_run_id)
                    if source_run is None:
                        source_run = (
                            await session.execute(
                                select(TagExtractionRun).where(
                                    TagExtractionRun.id == source_extraction_run_id,
                                    TagExtractionRun.tenant_id == tenant_id,
                                )
                            )
                        ).scalar_one_or_none()
                    if (
                        source_run is None
                        or source_run.subject_type != subject_type
                        or source_run.subject_id != subject_id
                    ):
                        raise AssignmentValidationError(
                            "review extraction lineage does not match the subject"
                        )
                    if tagger_version_id is None:
                        tagger_version_id = source_run.tagger_version_id
                    elif (
                        source_run.tagger_version_id is not None
                        and int(tagger_version_id) != source_run.tagger_version_id
                    ):
                        raise AssignmentValidationError(
                            "review extraction lineage does not match the tagger"
                        )
                    if source_deployment_id is None:
                        source_deployment_id = source_run.deployment_id
                    elif (
                        source_run.deployment_id is not None
                        and int(source_deployment_id) != source_run.deployment_id
                    ):
                        raise AssignmentValidationError(
                            "review extraction lineage does not match the deployment"
                        )
                source_execution: TagHarnessExecution | None = None
                if source_harness_execution_id is not None:
                    source_harness_execution_id = int(source_harness_execution_id)
                    source_execution = (
                        await session.execute(
                            select(TagHarnessExecution).where(
                                TagHarnessExecution.id == source_harness_execution_id,
                                TagHarnessExecution.tenant_id == tenant_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if (
                        source_execution is None
                        or source_execution.subject_type != subject_type
                        or source_execution.subject_id != subject_id
                    ):
                        raise AssignmentValidationError(
                            "review Harness lineage does not match the subject"
                        )
                    if source_extraction_run_id is None:
                        source_extraction_run_id = source_execution.extraction_run_id
                    elif (
                        source_execution.extraction_run_id is not None
                        and source_execution.extraction_run_id != source_extraction_run_id
                    ):
                        raise AssignmentValidationError(
                            "review Harness lineage does not match the extraction run"
                        )
                    if tagger_version_id is None:
                        tagger_version_id = source_execution.tagger_version_id
                    elif int(tagger_version_id) != source_execution.tagger_version_id:
                        raise AssignmentValidationError(
                            "review Harness lineage does not match the tagger"
                        )
                    if source_deployment_id is None:
                        source_deployment_id = source_execution.deployment_id
                    elif (
                        source_execution.deployment_id is not None
                        and int(source_deployment_id) != source_execution.deployment_id
                    ):
                        raise AssignmentValidationError(
                            "review Harness lineage does not match the deployment"
                        )
                elif source_run is not None:
                    source_execution = (
                        await session.execute(
                            select(TagHarnessExecution)
                            .where(
                                TagHarnessExecution.tenant_id == tenant_id,
                                TagHarnessExecution.extraction_run_id == source_run.id,
                                TagHarnessExecution.subject_type == subject_type,
                                TagHarnessExecution.subject_id == subject_id,
                            )
                            .order_by(TagHarnessExecution.id.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if source_execution is not None:
                        source_harness_execution_id = source_execution.id
                if source_deployment_id is not None:
                    source_deployment_id = int(source_deployment_id)
                    deployment_exists = (
                        await session.execute(
                            select(TagDeployment.id).where(
                                TagDeployment.id == source_deployment_id,
                                TagDeployment.tenant_id == tenant_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if deployment_exists is None:
                        raise AssignmentValidationError(
                            "review deployment lineage does not exist for the tenant"
                        )
                if (
                    trusted_observation is not None
                    and source_deployment_id != trusted_observation.deployment_id
                ):
                    raise AssignmentValidationError(
                        "trusted review sample does not belong to its monitor deployment"
                    )
                sampled_deployment_stage: str | None = None
                sampled_deployment_revision: int | None = None
                if source_run is not None and source_run.origin == "serving":
                    sampled_deployment_stage = source_run.deployment_stage
                    sampled_deployment_revision = source_run.deployment_revision
                if trusted_observation is not None:
                    if (
                        sampled_deployment_stage is not None
                        and sampled_deployment_stage != trusted_observation.stage
                    ):
                        raise AssignmentValidationError(
                            "trusted review sample stage does not match its extraction run"
                        )
                    if (
                        sampled_deployment_revision is not None
                        and sampled_deployment_revision != trusted_observation.deployment_revision
                    ):
                        raise AssignmentValidationError(
                            "trusted review sample revision does not match its extraction run"
                        )
                    sampled_deployment_stage = trusted_observation.stage
                    sampled_deployment_revision = trusted_observation.deployment_revision
                sampling_manifest_checksum: str | None = None
                if (
                    selection_policy in _TRUSTED_SAMPLING_POLICIES
                    or sampling_probability is not None
                ):
                    if (
                        source_deployment_id is None
                        or source_run is None
                        or source_run.origin != "serving"
                        or source_extraction_run_id is None
                        or sampled_deployment_stage is None
                        or sampled_deployment_revision is None
                        or sampling_probability is None
                    ):
                        raise AssignmentValidationError(
                            "trusted review sampling requires a serving extraction snapshot"
                        )
                    if (
                        selection_policy
                        in {
                            "representative_random",
                            "representative_audit",
                            "random_audit",
                        }
                        and sampled_deployment_stage != "shadow"
                        and not source_run.served_current
                    ):
                        raise AssignmentValidationError(
                            "Canary representative audits require an actually served route"
                        )
                    sampling_manifest_checksum = review_sampling_manifest_checksum(
                        deployment_id=source_deployment_id,
                        deployment_stage=sampled_deployment_stage,
                        deployment_revision=sampled_deployment_revision,
                        extraction_run_id=source_extraction_run_id,
                        subject_type=subject_type,
                        subject_id=subject_id,
                        tag_key=str(item["tag_key"]),
                        selection_policy=selection_policy,
                        selection_policy_version=selection_policy_version,
                        sampling_probability=sampling_probability,
                    )
                if tagger_version_id is not None:
                    tagger = (
                        await session.execute(
                            select(TaggerVersion).where(
                                TaggerVersion.id == int(tagger_version_id),
                                TaggerVersion.tenant_id == tenant_id,
                                TaggerVersion.schema_version_id == int(schema_version_id),
                            )
                        )
                    ).scalar_one_or_none()
                    if tagger is None:
                        raise AssignmentValidationError("review tagger does not match its schema")
                evidence_refs = (
                    list(proposed_fact.evidence_refs)
                    if proposed_fact is not None
                    else ([] if reason == "gold" else list(item.get("evidence_refs") or []))
                )
                await self._validate_evidence_ownership(
                    session,
                    tenant_id=tenant_id,
                    reception_id=reception_id,
                    evidence_refs=evidence_refs,
                )
                reviewer_slots = (
                    2
                    if _is_double_blind_review(
                        reason=reason,
                        selection_policy=selection_policy,
                    )
                    else 1
                )
                for _reviewer_slot in range(reviewer_slots):
                    tasks.append(
                        TagReviewTask(
                            tenant_id=tenant_id,
                            batch_id=resolved_batch_id,
                            subject_type=subject_type,
                            subject_id=subject_id,
                            reception_id=reception_id,
                            tag_key=str(item["tag_key"]),
                            proposed_value=(
                                proposed_fact.tag_value
                                if proposed_fact is not None
                                else (None if reason == "gold" else item.get("proposed_value"))
                            ),
                            confidence=(
                                proposed_fact.confidence
                                if proposed_fact is not None
                                else (None if reason == "gold" else item.get("confidence"))
                            ),
                            evidence_refs=evidence_refs,
                            proposed_fact_id=(
                                proposed_fact.id if proposed_fact is not None else None
                            ),
                            schema_version_id=int(schema_version_id),
                            tagger_version_id=(
                                int(tagger_version_id) if tagger_version_id is not None else None
                            ),
                            review_bundle_id=resolved_review_bundle_id,
                            selection_policy=selection_policy,
                            selection_policy_version=selection_policy_version,
                            sampling_probability=sampling_probability,
                            blind_mode=effective_blind_mode,
                            source_deployment_id=source_deployment_id,
                            source_extraction_run_id=source_extraction_run_id,
                            source_harness_execution_id=source_harness_execution_id,
                            sampled_deployment_stage=sampled_deployment_stage,
                            sampled_deployment_revision=sampled_deployment_revision,
                            sampling_manifest_checksum=sampling_manifest_checksum,
                            reason=reason,
                            priority=int(item.get("priority", 0)),
                            status="pending",
                            created_by=actor_user_id,
                        )
                    )
            session.add_all(tasks)
            await session.flush()
            for task in tasks:
                await self._audit(
                    session,
                    tenant_id=tenant_id,
                    resource_type="tag_review_task",
                    resource_id=task.id,
                    action="created",
                    actor_user_id=actor_user_id,
                    payload={"reason": reason},
                )
            return tasks

    async def list_reviews(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        limit: int = 200,
    ) -> list[TagReviewTask]:
        async with self._factory() as session:
            stmt = select(TagReviewTask).where(TagReviewTask.tenant_id == tenant_id)
            if status is not None:
                stmt = stmt.where(TagReviewTask.status == status)
            return list(
                (
                    await session.execute(
                        stmt.order_by(
                            TagReviewTask.priority.desc(),
                            TagReviewTask.created_at,
                            TagReviewTask.id,
                        ).limit(_bounded_list_limit(limit))
                    )
                )
                .scalars()
                .all()
            )

    async def list_reviews_for_viewer(
        self,
        *,
        tenant_id: str,
        reviewer_user_id: int,
        status: str | None = None,
        limit: int = 200,
    ) -> list[TagReviewTask]:
        """List review work without leaking blind siblings across filters/pages.

        ``active`` is the blind-safe work surface: pending tasks plus the
        reviewer's own claimed task.  It never records a semantic reservation;
        the API masks every pending model hint.  Explicit semantic/history
        modes serialize with ``claim_review`` and reserve access for blind tasks
        that already existed when the history was read.
        """

        async with self._factory() as session, session.begin():
            user_lock = (
                await session.execute(
                    select(User.id)
                    .where(
                        User.id == reviewer_user_id,
                        User.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if user_lock is None:
                raise GovernanceNotFoundError("reviewer not found")
            effective_status = status or "active"
            if effective_status not in {
                "active",
                "pending",
                "claimed",
                "resolved",
                "skipped",
                "all",
            }:
                raise GovernanceError("unsupported review queue status")
            active_blind_ids = set(
                (
                    await session.execute(
                        select(TagReviewTask.id).where(
                            TagReviewTask.tenant_id == tenant_id,
                            TagReviewTask.blind_mode.is_(True),
                            TagReviewTask.status == "claimed",
                            TagReviewTask.claimed_by == reviewer_user_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            pending_blind_keys = {
                (str(row.subject_type), int(row.subject_id), str(row.tag_key))
                for row in (
                    await session.execute(
                        select(
                            TagReviewTask.subject_type,
                            TagReviewTask.subject_id,
                            TagReviewTask.tag_key,
                        ).where(
                            TagReviewTask.tenant_id == tenant_id,
                            TagReviewTask.blind_mode.is_(True),
                            TagReviewTask.status == "pending",
                        )
                    )
                ).all()
            }
            stmt = select(TagReviewTask).where(TagReviewTask.tenant_id == tenant_id)
            if effective_status == "active":
                stmt = stmt.where(
                    or_(
                        TagReviewTask.status == "pending",
                        and_(
                            TagReviewTask.status == "claimed",
                            TagReviewTask.claimed_by == reviewer_user_id,
                        ),
                    )
                )
            elif effective_status != "all":
                stmt = stmt.where(TagReviewTask.status == effective_status)
            rows = list(
                (
                    await session.execute(
                        stmt.order_by(
                            TagReviewTask.priority.desc(),
                            TagReviewTask.created_at,
                            TagReviewTask.id,
                        ).limit(_bounded_list_limit(limit))
                    )
                )
                .scalars()
                .all()
            )
            if active_blind_ids:
                return [row for row in rows if row.id in active_blind_ids]
            rows = [
                row
                for row in rows
                if not (
                    row.blind_mode
                    and row.status == "resolved"
                    and row.claimed_by != reviewer_user_id
                )
                and (
                    (row.status == "claimed" and row.claimed_by == reviewer_user_id)
                    or (row.blind_mode and row.status == "pending")
                    or (str(row.subject_type), int(row.subject_id), str(row.tag_key))
                    not in pending_blind_keys
                )
            ]
            exposes_semantics = any(not row.blind_mode or row.status == "resolved" for row in rows)
            if effective_status != "active" and exposes_semantics:
                await self._audit(
                    session,
                    tenant_id=tenant_id,
                    resource_type="semantic_global",
                    resource_id=0,
                    action="blind_sensitive_read",
                    actor_user_id=reviewer_user_id,
                    payload={"access_kind": "review_history"},
                )
            return rows

    async def has_active_blind_review(
        self,
        *,
        tenant_id: str,
        reviewer_user_id: int,
        reception_id: int | None = None,
    ) -> bool:
        """Return whether a reviewer is inside an unrevealed blind-review session."""

        async with self._factory() as session:
            stmt = select(TagReviewTask.id).where(
                TagReviewTask.tenant_id == tenant_id,
                TagReviewTask.blind_mode.is_(True),
                TagReviewTask.status == "claimed",
                TagReviewTask.claimed_by == reviewer_user_id,
            )
            if reception_id is not None:
                stmt = stmt.where(TagReviewTask.reception_id == reception_id)
            return (await session.execute(stmt.limit(1))).scalar_one_or_none() is not None

    async def active_blind_review_task_ids(
        self,
        *,
        tenant_id: str,
        reviewer_user_id: int,
    ) -> set[int]:
        """Return only the opaque tasks a reviewer may see during blind review."""

        async with self._factory() as session:
            return set(
                (
                    await session.execute(
                        select(TagReviewTask.id).where(
                            TagReviewTask.tenant_id == tenant_id,
                            TagReviewTask.blind_mode.is_(True),
                            TagReviewTask.status == "claimed",
                            TagReviewTask.claimed_by == reviewer_user_id,
                        )
                    )
                )
                .scalars()
                .all()
            )

    async def record_blind_sensitive_access(
        self,
        *,
        tenant_id: str,
        actor_user_id: int,
        access_kind: str,
        reception_id: int | None = None,
    ) -> bool:
        """Atomically reserve semantic access against blind-task assignment.

        The authoritative user row is the shared serialization key with
        ``claim_review``.  ``False`` means a blind task won the race and the
        caller must redact or deny the semantic response.
        """

        resource_type = "reception_semantics" if reception_id is not None else "semantic_global"
        resource_id = int(reception_id or 0)
        async with self._factory() as session, session.begin():
            user_lock = (
                await session.execute(
                    select(User.id)
                    .where(
                        User.id == actor_user_id,
                        User.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if user_lock is None:
                raise GovernanceNotFoundError("semantic access actor not found")
            active_stmt = select(TagReviewTask.id).where(
                TagReviewTask.tenant_id == tenant_id,
                TagReviewTask.blind_mode.is_(True),
                TagReviewTask.status == "claimed",
                TagReviewTask.claimed_by == actor_user_id,
            )
            if reception_id is not None:
                active_stmt = active_stmt.where(TagReviewTask.reception_id == reception_id)
            if (await session.execute(active_stmt.limit(1))).scalar_one_or_none() is not None:
                return False
            existing = None
            if resource_type == "reception_semantics":
                existing = (
                    await session.execute(
                        select(TagGovernanceAuditEvent.id)
                        .where(
                            TagGovernanceAuditEvent.tenant_id == tenant_id,
                            TagGovernanceAuditEvent.actor_user_id == actor_user_id,
                            TagGovernanceAuditEvent.action == "blind_sensitive_read",
                            TagGovernanceAuditEvent.resource_type == resource_type,
                            TagGovernanceAuditEvent.resource_id == resource_id,
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
            if resource_type == "semantic_global" or existing is None:
                await self._audit(
                    session,
                    tenant_id=tenant_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    action="blind_sensitive_read",
                    actor_user_id=actor_user_id,
                    payload={"access_kind": access_kind},
                )
            return True

    async def claim_review(
        self, *, tenant_id: str, task_id: int, reviewer_user_id: int
    ) -> TagReviewTask:
        async with self._factory() as session, session.begin():
            task_preview = (
                await session.execute(
                    select(TagReviewTask).where(
                        TagReviewTask.id == task_id,
                        TagReviewTask.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if task_preview is None:
                raise GovernanceNotFoundError("review task not found")
            reviewer_lock = (
                await session.execute(
                    select(User.id)
                    .where(
                        User.id == reviewer_user_id,
                        User.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if reviewer_lock is None:
                raise GovernanceNotFoundError("reviewer not found")
            other_active_task = (
                await session.execute(
                    select(TagReviewTask.id)
                    .where(
                        TagReviewTask.tenant_id == tenant_id,
                        TagReviewTask.status == "claimed",
                        TagReviewTask.claimed_by == reviewer_user_id,
                        TagReviewTask.id != task_id,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if other_active_task is not None:
                raise GovernanceConflictError(
                    "reviewer must resolve the active task before claiming another"
                )
            await self._lock_review_serialization_scope(
                session,
                tenant_id=tenant_id,
                task=task_preview,
            )
            task = (
                await session.execute(
                    select(TagReviewTask)
                    .where(
                        TagReviewTask.id == task_id,
                        TagReviewTask.tenant_id == tenant_id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
            if task.status == "pending":
                if task.blind_mode and task.created_by == reviewer_user_id:
                    raise GovernanceConflictError(
                        "blind review tasks cannot be claimed by their creator"
                    )
                if task.blind_mode:
                    access_predicate = TagGovernanceAuditEvent.resource_type == "semantic_global"
                    if task.reception_id is not None:
                        access_predicate = or_(
                            access_predicate,
                            and_(
                                TagGovernanceAuditEvent.resource_type == "reception_semantics",
                                TagGovernanceAuditEvent.resource_id == task.reception_id,
                            ),
                        )
                    prior_semantic_access = (
                        await session.execute(
                            select(TagGovernanceAuditEvent.id)
                            .where(
                                TagGovernanceAuditEvent.tenant_id == tenant_id,
                                TagGovernanceAuditEvent.actor_user_id == reviewer_user_id,
                                TagGovernanceAuditEvent.action == "blind_sensitive_read",
                                access_predicate,
                                or_(
                                    TagGovernanceAuditEvent.resource_type != "semantic_global",
                                    TagGovernanceAuditEvent.created_at >= task.created_at,
                                ),
                            )
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if prior_semantic_access is not None:
                        raise GovernanceConflictError(
                            "reviewer previously accessed semantic output for this "
                            "blind-review scope"
                        )
                if task.review_bundle_id:
                    already_claimed = (
                        await session.execute(
                            select(TagReviewTask.id)
                            .where(
                                TagReviewTask.tenant_id == tenant_id,
                                TagReviewTask.review_bundle_id == task.review_bundle_id,
                                TagReviewTask.subject_type == task.subject_type,
                                TagReviewTask.subject_id == task.subject_id,
                                TagReviewTask.tag_key == task.tag_key,
                                TagReviewTask.id != task.id,
                                TagReviewTask.status == "claimed",
                                TagReviewTask.claimed_by == reviewer_user_id,
                            )
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    already_reviewed = (
                        await session.execute(
                            select(TagReviewDecision.id)
                            .join(
                                TagReviewTask,
                                TagReviewTask.id == TagReviewDecision.task_id,
                            )
                            .where(
                                TagReviewDecision.tenant_id == tenant_id,
                                TagReviewDecision.reviewer_user_id == reviewer_user_id,
                                TagReviewTask.tenant_id == tenant_id,
                                TagReviewTask.review_bundle_id == task.review_bundle_id,
                                TagReviewTask.subject_type == task.subject_type,
                                TagReviewTask.subject_id == task.subject_id,
                                TagReviewTask.tag_key == task.tag_key,
                            )
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if already_claimed is not None or already_reviewed is not None:
                        raise GovernanceConflictError(
                            "the same reviewer cannot label this bundled subject twice"
                        )
                if not task.blind_mode:
                    await self._audit(
                        session,
                        tenant_id=tenant_id,
                        resource_type="semantic_global",
                        resource_id=0,
                        action="blind_sensitive_read",
                        actor_user_id=reviewer_user_id,
                        payload={"access_kind": "nonblind_review_claim"},
                    )
                task.status = "claimed"
                task.claimed_by = reviewer_user_id
                task.claimed_at = _utcnow()
            elif task.status != "claimed" or task.claimed_by != reviewer_user_id:
                raise GovernanceConflictError("review task is already claimed or resolved")
            return task

    async def release_review(
        self,
        *,
        tenant_id: str,
        task_id: int,
        actor_user_id: int,
        force: bool = False,
    ) -> TagReviewTask:
        async with self._factory() as session, session.begin():
            task_preview = (
                await session.execute(
                    select(TagReviewTask).where(
                        TagReviewTask.id == task_id,
                        TagReviewTask.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if task_preview is None:
                raise GovernanceNotFoundError("review task not found")
            if task_preview.status != "claimed" or task_preview.claimed_by is None:
                raise GovernanceConflictError("only a claimed review task can be released")
            claimed_by = int(task_preview.claimed_by)
            if not force and claimed_by != actor_user_id:
                raise GovernanceConflictError(
                    "review task can only be released by its current claimant"
                )
            reviewer_lock = (
                await session.execute(
                    select(User.id)
                    .where(
                        User.id == claimed_by,
                        User.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if reviewer_lock is None:
                raise GovernanceNotFoundError("reviewer not found")
            await self._lock_review_serialization_scope(
                session,
                tenant_id=tenant_id,
                task=task_preview,
            )
            task = (
                await session.execute(
                    select(TagReviewTask)
                    .where(
                        TagReviewTask.id == task_id,
                        TagReviewTask.tenant_id == tenant_id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
            if task.status != "claimed" or task.claimed_by != claimed_by:
                raise GovernanceConflictError(
                    "review task changed while the claim was being released"
                )
            task.status = "pending"
            task.claimed_by = None
            task.claimed_at = None
            await self._audit(
                session,
                tenant_id=tenant_id,
                resource_type="tag_review_task",
                resource_id=task.id,
                action="claim_released",
                actor_user_id=actor_user_id,
                payload={"previous_claimant_id": claimed_by, "force": force},
            )
            return task

    async def decide_review(
        self,
        *,
        tenant_id: str,
        task_id: int,
        reviewer_user_id: int,
        action: str,
        corrected_value: Any,
        reason_code: str,
        note: str | None,
        evidence_refs: list[dict[str, Any]],
        adjudication: bool = False,
        truth_state: str | None = None,
        truth_tier: str = "t1",
        annotator_round: int = 1,
        primary_failure_stage: str | None = None,
        reason_codes: list[str] | None = None,
        reviewer_confidence: float | None = None,
        review_duration_ms: int | None = None,
    ) -> tuple[TagReviewTask, TagReviewDecision, TagAssignmentFact | None]:
        async with self._factory() as session, session.begin():
            task_preview = (
                await session.execute(
                    select(TagReviewTask).where(
                        TagReviewTask.id == task_id,
                        TagReviewTask.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if task_preview is None:
                raise GovernanceNotFoundError("review task not found")
            await self._lock_review_serialization_scope(
                session,
                tenant_id=tenant_id,
                task=task_preview,
            )
            task = (
                await session.execute(
                    select(TagReviewTask)
                    .where(
                        TagReviewTask.id == task_id,
                        TagReviewTask.tenant_id == tenant_id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
            if task.status != "claimed" or task.claimed_by is None:
                raise GovernanceConflictError(
                    "review task must be claimed before it can be resolved"
                )
            if task.claimed_by != reviewer_user_id:
                raise GovernanceConflictError(
                    "review task must be resolved by its current claimant"
                )
            if adjudication:
                if task.reason != "adjudication":
                    raise GovernanceError("T3 truth can only be created from an adjudication task")
                if truth_tier != "t3" or annotator_round != 3:
                    raise GovernanceError(
                        "adjudication decisions require truth_tier=t3 and annotator_round=3"
                    )
                if not task.review_bundle_id:
                    raise GovernanceError(
                        "adjudication tasks require a review bundle with independent labels"
                    )
                await self._certified_adjudication_predecessors(
                    session,
                    tenant_id=tenant_id,
                    task=task,
                    adjudicator_user_id=reviewer_user_id,
                )
                if (
                    action not in {"correct", "reject"}
                    or truth_state not in {"present", "absent", "not_applicable"}
                    or (truth_state == "present" and action != "correct")
                    or (truth_state in {"absent", "not_applicable"} and action != "reject")
                ):
                    raise GovernanceError(
                        "T3 adjudication requires a definitive present, absent, "
                        "or not_applicable decision"
                    )
            else:
                if truth_tier == "t3" or annotator_round == 3:
                    raise GovernanceError("T3 truth requires the explicit adjudication workflow")
                if task.blind_mode and task.reason in _BLIND_REVIEW_REASONS:
                    truth_tier = "t2"
                    annotator_round = 1
                if truth_tier == "t2" and (
                    not task.blind_mode or task.reason not in _BLIND_REVIEW_REASONS
                ):
                    raise GovernanceError(
                        "T2 truth requires a blind audit, drift, critical, or gold task"
                    )
            claimed_by = task.claimed_by
            if action not in {"accept", "correct", "reject", "uncertain", "escalate"}:
                raise GovernanceError("unsupported review decision action")
            if task.blind_mode and action == "accept":
                raise GovernanceError("blind reviews require an independent explicit label")
            if action in {"uncertain", "escalate"}:
                if truth_state not in {None, "uncertain"}:
                    raise GovernanceError(
                        "uncertain/escalate decisions require uncertain truth_state"
                    )
                now = _utcnow()
                decision = TagReviewDecision(
                    tenant_id=tenant_id,
                    task_id=task.id,
                    action=action,
                    corrected_value=None,
                    reason_code=reason_code,
                    note=note,
                    evidence_refs=list(evidence_refs),
                    resulting_fact_id=None,
                    reviewer_user_id=reviewer_user_id,
                    adjudication=adjudication,
                    truth_state="uncertain",
                    truth_tier=truth_tier,
                    annotator_round=annotator_round,
                    primary_failure_stage=primary_failure_stage,
                    reason_codes=list(reason_codes or []),
                    reviewer_confidence=reviewer_confidence,
                    review_duration_ms=review_duration_ms,
                    decided_at=now,
                )
                session.add(decision)
                transition = await session.execute(
                    update(TagReviewTask)
                    .where(
                        TagReviewTask.id == task.id,
                        TagReviewTask.tenant_id == tenant_id,
                        TagReviewTask.status == "claimed",
                        TagReviewTask.claimed_by == claimed_by,
                        TagReviewTask.resolved_at.is_(None),
                    )
                    .values(status="resolved", resolved_at=now)
                )
                if cast(CursorResult[Any], transition).rowcount != 1:
                    raise GovernanceConflictError(
                        "review task changed while the decision was being submitted"
                    )
                await session.flush()
                feedback_event = TagFeedbackEvent(
                    tenant_id=tenant_id,
                    harness_execution_id=task.source_harness_execution_id,
                    review_decision_id=decision.id,
                    deployment_id=task.source_deployment_id,
                    source="human",
                    truth_tier=truth_tier,
                    subject_type=task.subject_type,
                    subject_id=task.subject_id,
                    tag_key=task.tag_key,
                    truth_state="uncertain",
                    error_stage=primary_failure_stage,
                    correction={
                        "action": action,
                        "evidence_refs": list(evidence_refs),
                        "reason_code": reason_code,
                        "reason_codes": list(reason_codes or []),
                    },
                    payload={
                        "adjudication": adjudication,
                        "annotator_round": annotator_round,
                        "blind_mode": bool(task.blind_mode),
                        "review_duration_ms": review_duration_ms,
                        "reviewer_confidence": reviewer_confidence,
                    },
                    input_hash=None,
                    training_eligible=False,
                    selection_policy=task.selection_policy,
                    sampling_probability=task.sampling_probability,
                    occurred_at=now,
                )
                session.add(feedback_event)
                await session.flush()
                await self._materialize_feedback_learning(
                    session,
                    event=feedback_event,
                    task=task,
                    actor_user_id=reviewer_user_id,
                )
                followup_task: TagReviewTask | None = None
                if action == "escalate":
                    followup_task = TagReviewTask(
                        tenant_id=tenant_id,
                        batch_id=f"adjudication-{task.id}-{secrets.token_hex(6)}",
                        review_bundle_id=task.review_bundle_id,
                        selection_policy=task.selection_policy,
                        selection_policy_version=task.selection_policy_version,
                        sampling_probability=task.sampling_probability,
                        blind_mode=True,
                        subject_type=task.subject_type,
                        subject_id=task.subject_id,
                        reception_id=task.reception_id,
                        tag_key=task.tag_key,
                        proposed_value=task.proposed_value,
                        confidence=task.confidence,
                        evidence_refs=list(task.evidence_refs),
                        proposed_fact_id=task.proposed_fact_id,
                        schema_version_id=task.schema_version_id,
                        tagger_version_id=task.tagger_version_id,
                        source_deployment_id=task.source_deployment_id,
                        source_extraction_run_id=task.source_extraction_run_id,
                        source_harness_execution_id=task.source_harness_execution_id,
                        sampled_deployment_stage=task.sampled_deployment_stage,
                        sampled_deployment_revision=task.sampled_deployment_revision,
                        sampling_manifest_checksum=task.sampling_manifest_checksum,
                        reason="adjudication",
                        status="pending",
                        priority=max(100, task.priority),
                        created_by=reviewer_user_id,
                    )
                    session.add(followup_task)
                    await session.flush()
                await session.refresh(task)
                await self._audit(
                    session,
                    tenant_id=tenant_id,
                    resource_type="tag_review_task",
                    resource_id=task.id,
                    action=action,
                    actor_user_id=reviewer_user_id,
                    payload={
                        "decision_id": decision.id,
                        "resulting_fact_id": None,
                        "adjudication_task_id": (
                            followup_task.id if followup_task is not None else None
                        ),
                    },
                )
                return task, decision, None
            if action == "correct" and corrected_value is None:
                raise GovernanceError("corrected_value is required for correct")
            if action == "correct" and not evidence_refs:
                raise AssignmentValidationError("manual corrections require evidence")
            resolved_truth_state = truth_state or ("absent" if action == "reject" else "present")
            if action == "reject" and resolved_truth_state not in {
                "absent",
                "not_applicable",
            }:
                raise GovernanceError("reject requires truth_state=absent or not_applicable")
            if action in {"accept", "correct"} and resolved_truth_state != "present":
                raise GovernanceError("accept/correct decisions require truth_state=present")
            value = corrected_value if action == "correct" else task.proposed_value
            decision_evidence = (
                evidence_refs if action == "correct" else evidence_refs or list(task.evidence_refs)
            )
            double_blind_round = (
                not adjudication
                and task.blind_mode
                and _is_double_blind_review(
                    reason=str(task.reason),
                    selection_policy=str(task.selection_policy),
                )
            )
            truth_only = (
                adjudication
                or double_blind_round
                or (
                    action == "reject"
                    and (resolved_truth_state == "not_applicable" or task.proposed_fact_id is None)
                )
            )
            if truth_only:
                if task.reception_id is None:
                    raise GovernanceConflictError(
                        "review truth requires a reception-scoped subject"
                    )
                await self._validate_evidence_ownership(
                    session,
                    tenant_id=tenant_id,
                    reception_id=int(task.reception_id),
                    evidence_refs=decision_evidence,
                )
                if resolved_truth_state == "present":
                    schema_version = (
                        await session.execute(
                            select(TagSchemaVersion).where(
                                TagSchemaVersion.id == task.schema_version_id,
                                TagSchemaVersion.tenant_id == tenant_id,
                            )
                        )
                    ).scalar_one()
                    definition = next(
                        (
                            item
                            for item in schema_version.definitions
                            if isinstance(item, dict) and item.get("key") == task.tag_key
                        ),
                        None,
                    )
                    if definition is None:
                        raise AssignmentValidationError(
                            "review tag is absent from its immutable schema"
                        )
                    validate_assignment(
                        definition=definition,
                        label_value=value,
                        confidence=1,
                        evidence_refs=decision_evidence,
                    )
                if task.proposed_fact_id is None and not double_blind_round and not adjudication:
                    current_exists = (
                        await session.execute(
                            select(TagAssignmentCurrent.id).where(
                                TagAssignmentCurrent.tenant_id == tenant_id,
                                TagAssignmentCurrent.subject_type == task.subject_type,
                                TagAssignmentCurrent.subject_id == task.subject_id,
                                TagAssignmentCurrent.tag_key == task.tag_key,
                            )
                        )
                    ).scalar_one_or_none()
                    if current_exists is not None:
                        raise GovernanceConflictError(
                            "review is stale; the label now has a current fact"
                        )
                prior_rounds = list(
                    (
                        await session.execute(
                            select(TagReviewDecision)
                            .join(
                                TagReviewTask,
                                TagReviewTask.id == TagReviewDecision.task_id,
                            )
                            .where(
                                TagReviewDecision.tenant_id == tenant_id,
                                TagReviewTask.tenant_id == tenant_id,
                                TagReviewTask.review_bundle_id == task.review_bundle_id,
                                TagReviewTask.subject_type == task.subject_type,
                                TagReviewTask.subject_id == task.subject_id,
                                TagReviewTask.tag_key == task.tag_key,
                                TagReviewTask.blind_mode.is_(True),
                                or_(
                                    TagReviewTask.reason.in_(_DOUBLE_BLIND_REVIEW_REASONS),
                                    TagReviewTask.selection_policy.in_(_RELEASE_REVIEW_POLICIES),
                                ),
                                TagReviewDecision.adjudication.is_(False),
                                TagReviewDecision.truth_tier == "t2",
                            )
                            .order_by(TagReviewDecision.decided_at, TagReviewDecision.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                if double_blind_round:
                    if not task.review_bundle_id:
                        raise GovernanceConflictError(
                            "double-blind review requires a review bundle"
                        )
                    if any(item.reviewer_user_id == reviewer_user_id for item in prior_rounds):
                        raise GovernanceConflictError(
                            "the same reviewer cannot label this bundled subject twice"
                        )
                    if len(prior_rounds) >= 2:
                        raise GovernanceConflictError(
                            "both double-blind review rounds are already complete"
                        )
                effective_truth_tier = "t2" if double_blind_round else truth_tier
                effective_round = len(prior_rounds) + 1 if double_blind_round else annotator_round
                now = _utcnow()
                decision = TagReviewDecision(
                    tenant_id=tenant_id,
                    task_id=task.id,
                    action=action,
                    corrected_value=corrected_value,
                    reason_code=reason_code,
                    note=note,
                    evidence_refs=decision_evidence,
                    resulting_fact_id=None,
                    reviewer_user_id=reviewer_user_id,
                    adjudication=adjudication,
                    truth_state=resolved_truth_state,
                    truth_tier=effective_truth_tier,
                    annotator_round=effective_round,
                    primary_failure_stage=primary_failure_stage,
                    reason_codes=list(reason_codes or []),
                    reviewer_confidence=reviewer_confidence,
                    review_duration_ms=review_duration_ms,
                    decided_at=now,
                )
                session.add(decision)
                transition = await session.execute(
                    update(TagReviewTask)
                    .where(
                        TagReviewTask.id == task.id,
                        TagReviewTask.tenant_id == tenant_id,
                        TagReviewTask.status == "claimed",
                        TagReviewTask.claimed_by == claimed_by,
                        TagReviewTask.resolved_at.is_(None),
                    )
                    .values(status="resolved", resolved_at=now)
                )
                if cast(CursorResult[Any], transition).rowcount != 1:
                    raise GovernanceConflictError(
                        "review task changed while the decision was being submitted"
                    )
                await session.flush()
                training_eligible = (
                    not double_blind_round
                    and not adjudication
                    and effective_truth_tier in {"t2", "t3"}
                    and resolved_truth_state in {"present", "absent"}
                    and (primary_failure_stage or "tag_reasoning") not in _UPSTREAM_FAILURE_STAGES
                )
                feedback_event = TagFeedbackEvent(
                    tenant_id=tenant_id,
                    harness_execution_id=task.source_harness_execution_id,
                    review_decision_id=decision.id,
                    deployment_id=task.source_deployment_id,
                    source="human",
                    truth_tier=effective_truth_tier,
                    subject_type=task.subject_type,
                    subject_id=task.subject_id,
                    tag_key=task.tag_key,
                    truth_state=resolved_truth_state,
                    error_stage=primary_failure_stage,
                    correction={
                        "action": action,
                        "corrected_value": corrected_value,
                        "evidence_refs": decision_evidence,
                        "reason_code": reason_code,
                        "reason_codes": list(reason_codes or []),
                    },
                    payload={
                        "adjudication": adjudication,
                        "annotator_round": effective_round,
                        "blind_mode": bool(task.blind_mode),
                        "review_duration_ms": review_duration_ms,
                        "reviewer_confidence": reviewer_confidence,
                    },
                    input_hash=None,
                    training_eligible=training_eligible,
                    selection_policy=task.selection_policy,
                    sampling_probability=task.sampling_probability,
                    occurred_at=now,
                )
                session.add(feedback_event)
                await session.flush()
                await self._materialize_feedback_learning(
                    session,
                    event=feedback_event,
                    task=task,
                    actor_user_id=reviewer_user_id,
                )
                completed_rounds = [*prior_rounds, decision]
                if (
                    double_blind_round
                    and len(completed_rounds) == 2
                    and {item.annotator_round for item in completed_rounds} == {1, 2}
                    and len({item.reviewer_user_id for item in completed_rounds}) == 2
                ):
                    existing_adjudication = (
                        await session.execute(
                            select(TagReviewTask.id)
                            .where(
                                TagReviewTask.tenant_id == tenant_id,
                                TagReviewTask.review_bundle_id == task.review_bundle_id,
                                TagReviewTask.subject_type == task.subject_type,
                                TagReviewTask.subject_id == task.subject_id,
                                TagReviewTask.tag_key == task.tag_key,
                                TagReviewTask.reason == "adjudication",
                            )
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if existing_adjudication is None:
                        session.add(
                            TagReviewTask(
                                tenant_id=tenant_id,
                                batch_id=(f"adjudication-{task.id}-{secrets.token_hex(6)}"),
                                review_bundle_id=task.review_bundle_id,
                                selection_policy=task.selection_policy,
                                selection_policy_version=task.selection_policy_version,
                                sampling_probability=task.sampling_probability,
                                blind_mode=True,
                                subject_type=task.subject_type,
                                subject_id=task.subject_id,
                                reception_id=task.reception_id,
                                tag_key=task.tag_key,
                                # Keep the immutable candidate prediction for
                                # release metrics. Blind API resources redact
                                # these fields until adjudication is submitted.
                                proposed_value=task.proposed_value,
                                confidence=task.confidence,
                                evidence_refs=list(task.evidence_refs),
                                proposed_fact_id=task.proposed_fact_id,
                                schema_version_id=task.schema_version_id,
                                tagger_version_id=task.tagger_version_id,
                                source_deployment_id=task.source_deployment_id,
                                source_extraction_run_id=task.source_extraction_run_id,
                                source_harness_execution_id=task.source_harness_execution_id,
                                sampled_deployment_stage=task.sampled_deployment_stage,
                                sampled_deployment_revision=task.sampled_deployment_revision,
                                sampling_manifest_checksum=task.sampling_manifest_checksum,
                                reason="adjudication",
                                status="pending",
                                priority=max(100, task.priority),
                                created_by=reviewer_user_id,
                            )
                        )
                await session.refresh(task)
                await self._audit(
                    session,
                    tenant_id=tenant_id,
                    resource_type="tag_review_task",
                    resource_id=task.id,
                    action=action,
                    actor_user_id=reviewer_user_id,
                    payload={
                        "decision_id": decision.id,
                        "resulting_fact_id": None,
                        "truth_only": True,
                    },
                )
                return task, decision, None

            tombstone = action == "reject"
            if tombstone and not decision_evidence:
                raise AssignmentValidationError(
                    "rejected assignments require negative-label evidence"
                )
            reception = (
                await session.execute(
                    select(Reception)
                    .where(
                        Reception.id == task.reception_id,
                        Reception.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if reception is None:
                raise GovernanceNotFoundError("review reception not found")
            if task.tag_key == "stage" and not tombstone and not isinstance(value, str):
                raise AssignmentValidationError("stage corrections require a string value")
            stage_change = (
                await project_stage_change_in_session(
                    session,
                    tenant_id=tenant_id,
                    reception_id=reception.id,
                    dialogue_unit_id=task.subject_id,
                    stage=value,
                    evidence_refs=decision_evidence,
                )
                if task.subject_type == "dialogue_unit"
                and task.tag_key == "stage"
                and not tombstone
                else None
            )

            proposed_fact: TagAssignmentFact | None = None
            if task.proposed_fact_id is not None:
                proposed_fact = (
                    await session.execute(
                        select(TagAssignmentFact).where(
                            TagAssignmentFact.id == task.proposed_fact_id,
                            TagAssignmentFact.tenant_id == tenant_id,
                        )
                    )
                ).scalar_one_or_none()
                if proposed_fact is None:
                    raise GovernanceConflictError("proposed assignment fact no longer exists")
            current_fact = (
                await session.execute(
                    select(TagAssignmentFact)
                    .join(
                        TagAssignmentCurrent,
                        and_(
                            TagAssignmentCurrent.fact_id == TagAssignmentFact.id,
                            TagAssignmentCurrent.tenant_id == TagAssignmentFact.tenant_id,
                        ),
                    )
                    .where(
                        TagAssignmentCurrent.tenant_id == tenant_id,
                        TagAssignmentCurrent.subject_type == task.subject_type,
                        TagAssignmentCurrent.subject_id == task.subject_id,
                        TagAssignmentCurrent.tag_key == task.tag_key,
                        TagAssignmentFact.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                proposed_fact is not None
                and current_fact is not None
                and current_fact.id != proposed_fact.id
                and current_fact.revision > proposed_fact.revision
            ):
                raise GovernanceConflictError(
                    "proposed assignment is stale; reload before reviewing"
                )
            if proposed_fact is None and current_fact is not None:
                raise GovernanceConflictError("review is stale; the label now has a current fact")
            if tombstone and proposed_fact is None:
                raise AssignmentValidationError(
                    "reject requires a persisted proposed assignment fact"
                )
            if not tombstone and value is None:
                raise AssignmentValidationError(
                    "accept/correct requires a concrete reviewed tag value"
                )

            correction_reason = note.strip() if note and note.strip() else reason_code
            source_hash = canonical_checksum(
                {
                    "task_id": task.id,
                    "action": action,
                    "value": value,
                    "evidence_refs": decision_evidence,
                    "reviewer_user_id": reviewer_user_id,
                }
            )
            publish_decision = not tombstone or (
                current_fact is not None
                and proposed_fact is not None
                and current_fact.id == proposed_fact.id
            )
            superseded_fact = proposed_fact if tombstone else current_fact
            fact = await self._append_assignment_in_session(
                session,
                tenant_id=tenant_id,
                subject_type=task.subject_type,
                subject_id=task.subject_id,
                tag_key=task.tag_key,
                tag_value=None if tombstone else value,
                confidence=1.0,
                evidence_refs=decision_evidence,
                source="manual",
                schema_version_id=task.schema_version_id,
                tagger_version_id=None,
                extraction_run_id=None,
                deployment_id=None,
                input_hash=source_hash,
                actor_user_id=reviewer_user_id,
                tombstone=tombstone,
                publish_current=publish_decision,
                expected_current_fact_id=(
                    current_fact.id if publish_decision and current_fact is not None else None
                ),
                expected_current_absent=task.proposed_fact_id is None,
                superseded_fact_id_override=(
                    proposed_fact.id if tombstone and proposed_fact is not None else None
                ),
            )
            await self._audit(
                session,
                tenant_id=tenant_id,
                resource_type="tag_assignment_fact",
                resource_id=fact.id,
                action="manual_corrected",
                actor_user_id=reviewer_user_id,
                payload={
                    "reason": correction_reason,
                    "subject_type": task.subject_type,
                    "subject_id": task.subject_id,
                    "tag_key": task.tag_key,
                    "superseded_fact_id": (
                        superseded_fact.id if superseded_fact is not None else None
                    ),
                    "published_current": publish_decision,
                    "bootstrap_canonical": current_fact is None,
                },
            )
            await self.project_manual_correction_in_session(
                session,
                tenant_id=tenant_id,
                reception=reception,
                superseded_fact=superseded_fact,
                corrected_fact=fact,
                reason=correction_reason,
                actor_user_id=reviewer_user_id,
                stage_change=stage_change,
            )
            now = _utcnow()
            decision = TagReviewDecision(
                tenant_id=tenant_id,
                task_id=task.id,
                action=action,
                corrected_value=corrected_value,
                reason_code=reason_code,
                note=note,
                evidence_refs=decision_evidence,
                resulting_fact_id=fact.id,
                reviewer_user_id=reviewer_user_id,
                adjudication=adjudication,
                truth_state=truth_state,
                truth_tier=truth_tier,
                annotator_round=annotator_round,
                primary_failure_stage=primary_failure_stage,
                reason_codes=list(reason_codes or []),
                reviewer_confidence=reviewer_confidence,
                review_duration_ms=review_duration_ms,
                decided_at=now,
            )
            session.add(decision)
            transition = await session.execute(
                update(TagReviewTask)
                .where(
                    TagReviewTask.id == task.id,
                    TagReviewTask.tenant_id == tenant_id,
                    TagReviewTask.status == "claimed",
                    TagReviewTask.claimed_by == claimed_by,
                    TagReviewTask.resolved_at.is_(None),
                )
                .values(
                    status="resolved",
                    resolved_at=now,
                )
            )
            if cast(CursorResult[Any], transition).rowcount != 1:
                raise GovernanceConflictError(
                    "review task changed while the decision was being submitted"
                )
            await session.flush()
            resolved_truth_state = truth_state or ("absent" if action == "reject" else "present")
            training_eligible = (
                truth_tier in {"t2", "t3"}
                and resolved_truth_state in {"present", "absent"}
                and (primary_failure_stage or "tag_reasoning") not in _UPSTREAM_FAILURE_STAGES
            )
            feedback_event = TagFeedbackEvent(
                tenant_id=tenant_id,
                harness_execution_id=task.source_harness_execution_id,
                review_decision_id=decision.id,
                deployment_id=task.source_deployment_id,
                source="human",
                truth_tier=truth_tier,
                subject_type=task.subject_type,
                subject_id=task.subject_id,
                tag_key=task.tag_key,
                truth_state=resolved_truth_state,
                error_stage=primary_failure_stage,
                correction={
                    "action": action,
                    "corrected_value": corrected_value,
                    "evidence_refs": decision_evidence,
                    "reason_code": reason_code,
                    "reason_codes": list(reason_codes or []),
                },
                payload={
                    "adjudication": adjudication,
                    "annotator_round": annotator_round,
                    "blind_mode": bool(task.blind_mode),
                    "review_duration_ms": review_duration_ms,
                    "reviewer_confidence": reviewer_confidence,
                },
                input_hash=fact.input_hash,
                training_eligible=training_eligible,
                selection_policy=task.selection_policy,
                sampling_probability=task.sampling_probability,
                occurred_at=now,
            )
            session.add(feedback_event)
            await session.flush()
            await self._materialize_feedback_learning(
                session,
                event=feedback_event,
                task=task,
                actor_user_id=reviewer_user_id,
            )
            await session.refresh(task)
            await self._audit(
                session,
                tenant_id=tenant_id,
                resource_type="tag_review_task",
                resource_id=task.id,
                action=action,
                actor_user_id=reviewer_user_id,
                payload={"decision_id": decision.id, "resulting_fact_id": fact.id},
            )
            return task, decision, fact

    async def create_gold_set(
        self,
        *,
        tenant_id: str,
        key: str,
        name: str,
        description: str | None,
        schema_version_id: int,
        actor_user_id: int,
    ) -> TagGoldSet:
        async with self._factory() as session, session.begin():
            schema_version = (
                await session.execute(
                    select(TagSchemaVersion).where(
                        TagSchemaVersion.id == schema_version_id,
                        TagSchemaVersion.tenant_id == tenant_id,
                        TagSchemaVersion.status == "published",
                    )
                )
            ).scalar_one_or_none()
            if schema_version is None:
                raise GovernanceNotFoundError("published tag schema version not found")
            gold_set = TagGoldSet(
                tenant_id=tenant_id,
                key=key,
                name=name,
                description=description,
                schema_version_id=schema_version_id,
                created_by=actor_user_id,
            )
            session.add(gold_set)
            await session.flush()
            return gold_set

    async def list_gold_sets(
        self,
        *,
        tenant_id: str,
        limit: int = 200,
    ) -> list[TagGoldSet]:
        async with self._factory() as session:
            return list(
                (
                    await session.execute(
                        select(TagGoldSet)
                        .where(TagGoldSet.tenant_id == tenant_id)
                        .order_by(TagGoldSet.created_at.desc(), TagGoldSet.id.desc())
                        .limit(_bounded_list_limit(limit))
                    )
                )
                .scalars()
                .all()
            )

    async def freeze_gold_set(
        self,
        *,
        tenant_id: str,
        gold_set_id: int,
        version: str,
        decision_ids: list[int],
        actor_user_id: int,
        cohort: Mapping[str, Any] | None = None,
        require_complete: bool = False,
    ) -> TagGoldSetVersion:
        async with self._factory() as session, session.begin():
            gold_set = (
                await session.execute(
                    select(TagGoldSet).where(
                        TagGoldSet.id == gold_set_id,
                        TagGoldSet.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if gold_set is None:
                raise GovernanceNotFoundError("gold set not found")
            if bool(decision_ids) == bool(cohort):
                raise GovernanceError("exactly one of decision_ids or cohort is required")
            cohort_tasks: list[TagReviewTask] | None = None
            cohort_decided_task_ids: set[int] = set()
            if cohort is not None:
                review_bundle_ids = cohort.get("review_bundle_ids")
                truth_tiers = cohort.get("truth_tiers", ["t2", "t3"])
                subject_types = cohort.get("subject_types", ["dialogue_unit", "reception"])
                if not isinstance(review_bundle_ids, list) or not review_bundle_ids:
                    raise GovernanceError("gold cohort requires at least one review_bundle_id")
                cohort_tasks = list(
                    (
                        await session.execute(
                            select(TagReviewTask)
                            .where(
                                TagReviewTask.tenant_id == tenant_id,
                                TagReviewTask.review_bundle_id.in_(review_bundle_ids),
                                TagReviewTask.subject_type.in_(subject_types),
                            )
                            .order_by(TagReviewTask.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                cohort_task_ids = [task.id for task in cohort_tasks]
                if cohort_task_ids:
                    cohort_decided_task_ids = set(
                        (
                            await session.execute(
                                select(TagReviewDecision.task_id).where(
                                    TagReviewDecision.tenant_id == tenant_id,
                                    TagReviewDecision.task_id.in_(cohort_task_ids),
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                decisions = list(
                    (
                        await session.execute(
                            select(TagReviewDecision)
                            .join(
                                TagReviewTask,
                                TagReviewTask.id == TagReviewDecision.task_id,
                            )
                            .where(
                                TagReviewDecision.tenant_id == tenant_id,
                                TagReviewDecision.truth_tier.in_(truth_tiers),
                                TagReviewTask.tenant_id == tenant_id,
                                TagReviewTask.status == "resolved",
                                TagReviewTask.review_bundle_id.in_(review_bundle_ids),
                                TagReviewTask.subject_type.in_(subject_types),
                            )
                            .order_by(TagReviewDecision.id)
                        )
                    )
                    .scalars()
                    .all()
                )
            else:
                decisions = list(
                    (
                        await session.execute(
                            select(TagReviewDecision).where(
                                TagReviewDecision.tenant_id == tenant_id,
                                TagReviewDecision.id.in_(decision_ids),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if len(decisions) != len(set(decision_ids)):
                    raise GovernanceError("all gold labels must come from resolved decisions")
                cohort_decided_task_ids = {decision.task_id for decision in decisions}
            invalid_decisions: list[int] = []
            task_ids = {decision.task_id for decision in decisions}
            tasks_by_id = {
                task.id: task
                for task in (
                    (
                        await session.execute(
                            select(TagReviewTask).where(
                                TagReviewTask.id.in_(task_ids),
                                TagReviewTask.tenant_id == tenant_id,
                                TagReviewTask.status == "resolved",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            }
            candidates_by_identity: dict[
                tuple[str, int, str],
                list[tuple[TagReviewDecision, TagReviewTask]],
            ] = defaultdict(list)
            for decision in decisions:
                task = tasks_by_id.get(decision.task_id)
                if task is None:
                    invalid_decisions.append(decision.id)
                    continue
                candidates_by_identity[(task.subject_type, task.subject_id, task.tag_key)].append(
                    (decision, task)
                )

            selected_decisions: list[TagReviewDecision] = []
            predecessor_ids_by_decision: dict[int, list[int]] = {}
            for identity, candidates in candidates_by_identity.items():
                certified_t3_candidates = [
                    (decision, task)
                    for decision, task in candidates
                    if decision.truth_tier == "t3"
                    and decision.adjudication
                    and int(decision.annotator_round) == 3
                    and decision.truth_state in {"present", "absent", "not_applicable"}
                ]
                if len(certified_t3_candidates) > 1:
                    raise GovernanceConflictError(
                        f"gold set contains multiple decisions for the same subject/tag: {identity}"
                    )
                if certified_t3_candidates:
                    decision, task = certified_t3_candidates[0]
                    predecessors = await self._certified_adjudication_predecessors(
                        session,
                        tenant_id=tenant_id,
                        task=task,
                        adjudicator_user_id=decision.reviewer_user_id,
                    )
                    predecessor_ids = {predecessor.id for predecessor, _prior_task in predecessors}
                    unexpected = [
                        candidate.id
                        for candidate, _candidate_task in candidates
                        if candidate.id != decision.id and candidate.id not in predecessor_ids
                    ]
                    if unexpected:
                        raise GovernanceConflictError(
                            "gold set contains multiple decisions for the same "
                            f"subject/tag: {identity}"
                        )
                    selected_decisions.append(decision)
                    predecessor_ids_by_decision[decision.id] = sorted(predecessor_ids)
                    continue
                if len(candidates) > 1:
                    raise GovernanceConflictError(
                        f"gold set contains multiple decisions for the same subject/tag: {identity}"
                    )
                decision, task = candidates[0]
                if (
                    decision.truth_tier == "t2"
                    and task.blind_mode
                    and task.reason in {"critical", "gold"}
                ):
                    raise GovernanceConflictError(
                        "double-blind T2 rounds require a certified T3 adjudication"
                    )
                selected_decisions.append(decision)
            decisions = sorted(selected_decisions, key=lambda item: item.id)
            rows: list[dict[str, Any]] = []
            seen_labels: set[tuple[str, int, str]] = set()
            schema_version = (
                await session.execute(
                    select(TagSchemaVersion).where(
                        TagSchemaVersion.id == gold_set.schema_version_id,
                        TagSchemaVersion.tenant_id == tenant_id,
                    )
                )
            ).scalar_one()
            for decision in decisions:
                task = tasks_by_id.get(decision.task_id)
                resulting_fact = (
                    await session.execute(
                        select(TagAssignmentFact).where(
                            TagAssignmentFact.id == decision.resulting_fact_id,
                            TagAssignmentFact.tenant_id == tenant_id,
                        )
                    )
                ).scalar_one_or_none()
                resolved_truth_state = decision.truth_state or (
                    "absent" if decision.action == "reject" else "present"
                )
                valid_truth_state = resolved_truth_state in {
                    "present",
                    "absent",
                    "not_applicable",
                }
                truth_only_t3 = (
                    decision.truth_tier == "t3"
                    and decision.adjudication
                    and int(decision.annotator_round) == 3
                )
                requires_fact = resolved_truth_state == "present" and not truth_only_t3
                fact_lineage_valid = (
                    resulting_fact is not None
                    and task is not None
                    and resulting_fact.subject_type == task.subject_type
                    and resulting_fact.subject_id == task.subject_id
                    and resulting_fact.tag_key == task.tag_key
                    and resulting_fact.schema_version_id == task.schema_version_id
                    and resulting_fact.tombstone == (resolved_truth_state == "absent")
                )
                optional_absent_fact_valid = resolved_truth_state == "absent" and (
                    resulting_fact is None
                    or (
                        fact_lineage_valid
                        and resulting_fact is not None
                        and resulting_fact.tombstone
                    )
                )
                not_applicable_fact_valid = (
                    resolved_truth_state != "not_applicable" or resulting_fact is None
                )
                certified_t3 = decision.truth_tier != "t3" or (
                    decision.adjudication and int(decision.annotator_round) == 3
                )
                if (
                    task is None
                    or task.schema_version_id != gold_set.schema_version_id
                    or task.reception_id is None
                    or not valid_truth_state
                    or not certified_t3
                    or (requires_fact and not fact_lineage_valid)
                    or (resolved_truth_state == "absent" and not optional_absent_fact_valid)
                    or not not_applicable_fact_valid
                ):
                    invalid_decisions.append(decision.id)
                    continue
                identity = (task.subject_type, task.subject_id, task.tag_key)
                if identity in seen_labels:
                    raise GovernanceConflictError(
                        f"gold set contains multiple decisions for the same subject/tag: {identity}"
                    )
                seen_labels.add(identity)
                proposed_fact = (
                    (
                        await session.execute(
                            select(TagAssignmentFact).where(
                                TagAssignmentFact.id == task.proposed_fact_id,
                                TagAssignmentFact.tenant_id == tenant_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if task.proposed_fact_id is not None
                    else None
                )
                source_execution = (
                    (
                        await session.execute(
                            select(TagHarnessExecution).where(
                                TagHarnessExecution.id == task.source_harness_execution_id,
                                TagHarnessExecution.tenant_id == tenant_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if task.source_harness_execution_id is not None
                    else None
                )
                if task.source_harness_execution_id is not None and (
                    source_execution is None
                    or source_execution.subject_type != task.subject_type
                    or source_execution.subject_id != task.subject_id
                ):
                    invalid_decisions.append(decision.id)
                    continue
                source_extraction_run_id = (
                    task.source_extraction_run_id
                    or (proposed_fact.extraction_run_id if proposed_fact is not None else None)
                    or (
                        source_execution.extraction_run_id if source_execution is not None else None
                    )
                    or (resulting_fact.extraction_run_id if resulting_fact is not None else None)
                )
                source_run = (
                    (
                        await session.execute(
                            select(TagExtractionRun).where(
                                TagExtractionRun.id == source_extraction_run_id,
                                TagExtractionRun.tenant_id == tenant_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if source_extraction_run_id is not None
                    else None
                )
                if source_extraction_run_id is not None and (
                    source_run is None
                    or source_run.subject_type != task.subject_type
                    or source_run.subject_id != task.subject_id
                ):
                    invalid_decisions.append(decision.id)
                    continue
                if source_execution is None and source_run is not None:
                    source_execution = (
                        await session.execute(
                            select(TagHarnessExecution)
                            .where(
                                TagHarnessExecution.tenant_id == tenant_id,
                                TagHarnessExecution.extraction_run_id == source_run.id,
                                TagHarnessExecution.subject_type == task.subject_type,
                                TagHarnessExecution.subject_id == task.subject_id,
                            )
                            .order_by(TagHarnessExecution.id.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                if (
                    source_execution is not None
                    and source_run is not None
                    and source_execution.extraction_run_id != source_run.id
                ):
                    invalid_decisions.append(decision.id)
                    continue
                source_deployment_id = (
                    task.source_deployment_id
                    or (proposed_fact.deployment_id if proposed_fact is not None else None)
                    or (source_execution.deployment_id if source_execution is not None else None)
                    or (resulting_fact.deployment_id if resulting_fact is not None else None)
                )
                if source_deployment_id is not None:
                    deployment_exists = (
                        await session.execute(
                            select(TagDeployment.id).where(
                                TagDeployment.id == source_deployment_id,
                                TagDeployment.tenant_id == tenant_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if deployment_exists is None:
                        invalid_decisions.append(decision.id)
                        continue
                rows.append(
                    {
                        "decision": decision,
                        "task": task,
                        "value": (
                            resulting_fact.tag_value
                            if resolved_truth_state == "present" and resulting_fact is not None
                            else (
                                (
                                    decision.corrected_value
                                    if decision.action == "correct"
                                    else task.proposed_value
                                )
                                if resolved_truth_state == "present" and truth_only_t3
                                else None
                            )
                        ),
                        "evidence_refs": (
                            list(resulting_fact.evidence_refs)
                            if resulting_fact is not None
                            else list(decision.evidence_refs)
                        ),
                        "truth_state": resolved_truth_state,
                        "truth_tier": decision.truth_tier,
                        "input_hash": (
                            source_run.input_hash
                            if source_run is not None
                            else (
                                source_execution.input_hash
                                if source_execution is not None
                                else (
                                    resulting_fact.input_hash
                                    if resulting_fact is not None
                                    else None
                                )
                            )
                        ),
                        "input_snapshot": (
                            deepcopy(source_run.input_snapshot) if source_run is not None else {}
                        ),
                        "annotation_quality": {
                            "adjudication": bool(decision.adjudication),
                            "annotator_round": int(decision.annotator_round),
                            "reviewer_confidence": decision.reviewer_confidence,
                            "review_duration_ms": decision.review_duration_ms,
                            "predecessor_decision_ids": predecessor_ids_by_decision.get(
                                decision.id,
                                [],
                            ),
                            "source_deployment_id": source_deployment_id,
                            "source_extraction_run_id": (
                                source_run.id if source_run is not None else None
                            ),
                            "source_harness_execution_id": (
                                source_execution.id if source_execution is not None else None
                            ),
                        },
                        "cohort": task.review_bundle_id,
                    }
                )
            if invalid_decisions:
                raise GovernanceConflictError(
                    "gold set contains unresolved, lineage-invalid, schema-mismatched, "
                    f"or evidence-less decisions: {sorted(invalid_decisions)}"
                )
            if not rows:
                raise GovernanceError("gold set version cannot be empty")
            subject_tasks = (
                cohort_tasks if cohort_tasks is not None else [row["task"] for row in rows]
            )
            subjects = {(task.subject_type, task.subject_id) for task in subject_tasks}
            reception_ids = {
                int(task.reception_id) for task in subject_tasks if task.reception_id is not None
            }
            scenario_by_reception_id: dict[int, str] = {
                int(reception_id): str(scenario)
                for reception_id, scenario in (
                    await session.execute(
                        select(Reception.id, Reception.scenario).where(
                            Reception.tenant_id == tenant_id,
                            Reception.id.in_(reception_ids),
                        )
                    )
                ).all()
            }
            subject_scenarios = {
                (task.subject_type, task.subject_id): scenario_by_reception_id.get(
                    int(task.reception_id)
                )
                for task in subject_tasks
                if task.reception_id is not None
            }
            expected_identities = {
                (subject_type, subject_id, str(definition["key"]))
                for subject_type, subject_id in subjects
                for definition in schema_version.definitions
                if isinstance(definition, dict)
                and definition.get("key")
                and subject_type in definition.get("subject_types", [])
                and (
                    not definition.get("scenarios")
                    or subject_scenarios.get((subject_type, subject_id))
                    in definition.get("scenarios", [])
                )
            }
            missing_identities = sorted(expected_identities - seen_labels)
            incomplete_input_ids = sorted(
                row["decision"].id
                for row in rows
                if not row["input_hash"] or not row["input_snapshot"]
            )
            weak_truth_ids = sorted(
                row["decision"].id for row in rows if row["truth_tier"] not in {"t2", "t3"}
            )
            unfinished_task_ids = sorted(
                task.id
                for task in (cohort_tasks or [])
                if task.status != "resolved" or task.id not in cohort_decided_task_ids
            )
            input_bindings_by_subject: dict[
                tuple[str, int],
                set[tuple[str | None, str, int | None]],
            ] = defaultdict(set)
            for row in rows:
                task = row["task"]
                input_bindings_by_subject[(task.subject_type, task.subject_id)].add(
                    (
                        row["input_hash"],
                        canonical_checksum(row["input_snapshot"]),
                        row["annotation_quality"]["source_extraction_run_id"],
                    )
                )
            inconsistent_input_subjects = sorted(
                subject
                for subject, bindings in input_bindings_by_subject.items()
                if len(bindings) != 1
            )
            if require_complete and (
                missing_identities
                or incomplete_input_ids
                or weak_truth_ids
                or unfinished_task_ids
                or inconsistent_input_subjects
            ):
                raise GovernanceConflictError(
                    "gold cohort is incomplete: "
                    f"missing_labels={len(missing_identities)}, "
                    f"missing_input_snapshots={len(incomplete_input_ids)}, "
                    f"inconsistent_input_subjects={len(inconsistent_input_subjects)}, "
                    f"weak_truths={len(weak_truth_ids)}, "
                    f"unfinished_reviews={len(unfinished_task_ids)}"
                )
            completeness_manifest = {
                "complete": not (
                    missing_identities
                    or incomplete_input_ids
                    or weak_truth_ids
                    or unfinished_task_ids
                    or inconsistent_input_subjects
                ),
                "legacy_sparse": not require_complete,
                "subject_count": len(subjects),
                "expected_label_count": len(expected_identities),
                "observed_label_count": len(seen_labels),
                "missing_label_count": len(missing_identities),
                "missing_input_snapshot_count": len(incomplete_input_ids),
                "inconsistent_input_subject_count": len(inconsistent_input_subjects),
                "weak_truth_count": len(weak_truth_ids),
                "unfinished_review_count": len(unfinished_task_ids),
                "cohort": deepcopy(dict(cohort or {})),
            }
            rows.sort(
                key=lambda row: (
                    row["task"].subject_type,
                    row["task"].subject_id,
                    row["task"].tag_key,
                    row["decision"].id,
                )
            )
            truth_tiers_by_reception: dict[int, set[str]] = defaultdict(set)
            for row in rows:
                reception_key = int(row["task"].reception_id or row["task"].subject_id)
                truth_tiers_by_reception[reception_key].add(str(row["truth_tier"]))
            snapshot_data = [
                {
                    "review_decision_id": row["decision"].id,
                    "reception_id": row["task"].reception_id,
                    "subject_type": row["task"].subject_type,
                    "subject_id": row["task"].subject_id,
                    "tag_key": row["task"].tag_key,
                    "tag_value": row["value"],
                    "truth_state": row["truth_state"],
                    "truth_tier": row["truth_tier"],
                    "evidence_refs": row["evidence_refs"],
                    "input_hash": row["input_hash"],
                    "input_snapshot": row["input_snapshot"],
                    "annotation_quality": row["annotation_quality"],
                    "cohort": row["cohort"],
                    "completeness_manifest": completeness_manifest,
                    "split": deterministic_gold_split(
                        tenant_id,
                        row["task"].reception_id or row["task"].subject_id,
                        truth_tier=(
                            "t3"
                            if truth_tiers_by_reception[
                                int(row["task"].reception_id or row["task"].subject_id)
                            ]
                            == {"t3"}
                            else "t2"
                        ),
                    ),
                }
                for row in rows
            ]
            dataset_snapshot_hash = compute_gold_dataset_snapshot_hash(snapshot_data)
            snapshot = TagGoldSetVersion(
                tenant_id=tenant_id,
                gold_set_id=gold_set_id,
                version=version,
                status="draft",
                checksum=dataset_snapshot_hash,
                dataset_snapshot_hash=dataset_snapshot_hash,
                completeness_manifest=completeness_manifest,
                item_count=len(rows),
                frozen_by=actor_user_id,
                frozen_at=_utcnow(),
            )
            session.add(snapshot)
            await session.flush()
            frozen_labels: list[TagGoldLabel] = []
            for row, frozen_item in zip(rows, snapshot_data, strict=True):
                task = row["task"]
                decision = row["decision"]
                gold_label = TagGoldLabel(
                    tenant_id=tenant_id,
                    gold_set_version_id=snapshot.id,
                    review_decision_id=decision.id,
                    reception_id=task.reception_id,
                    subject_type=task.subject_type,
                    subject_id=task.subject_id,
                    tag_key=task.tag_key,
                    tag_value=row["value"],
                    evidence_refs=row["evidence_refs"],
                    truth_state=row["truth_state"],
                    truth_tier=row["truth_tier"],
                    input_hash=row["input_hash"],
                    input_snapshot=row["input_snapshot"],
                    annotation_quality=row["annotation_quality"],
                    cohort=row["cohort"],
                    completeness_manifest=completeness_manifest,
                    split=frozen_item["split"],
                )
                session.add(gold_label)
                frozen_labels.append(gold_label)
            await session.flush()
            for row, frozen_item, gold_label in zip(
                rows,
                snapshot_data,
                frozen_labels,
                strict=True,
            ):
                await self._assign_feedback_lane(
                    session,
                    decision=row["decision"],
                    task=row["task"],
                    gold_label=gold_label,
                    gold_set_version_id=snapshot.id,
                    dataset_split=str(frozen_item["split"]),
                    actor_user_id=actor_user_id,
                )
            snapshot.status = "frozen"
            await session.flush()
            return snapshot

    async def create_server_bound_optimization_candidate(
        self,
        *,
        tenant_id: str,
        gold_set_version_id: int,
        actor_user_id: int,
    ) -> tuple[TaggerVersion, dict[str, Any]]:
        """Create a candidate against the tenant's current production baseline.

        This is the public/manual compatibility path.  The caller supplies only
        the frozen dataset; the production Harness identity is resolved from
        server-owned deployment state and is never accepted from the request.
        """

        async with self._factory() as session:
            gold_row = (
                await session.execute(
                    select(TagGoldSetVersion, TagGoldSet)
                    .join(TagGoldSet, TagGoldSet.id == TagGoldSetVersion.gold_set_id)
                    .where(
                        TagGoldSetVersion.id == gold_set_version_id,
                        TagGoldSetVersion.tenant_id == tenant_id,
                        TagGoldSetVersion.status == "frozen",
                        TagGoldSet.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if gold_row is None:
                raise GovernanceNotFoundError("frozen gold set version not found")
            _snapshot, gold_set = gold_row
            baseline_id = (
                await session.execute(
                    select(TaggerVersion.id)
                    .join(
                        TagDeployment,
                        TagDeployment.tagger_version_id == TaggerVersion.id,
                    )
                    .where(
                        TagDeployment.tenant_id == tenant_id,
                        TagDeployment.status == "production",
                        TaggerVersion.tenant_id == tenant_id,
                        TaggerVersion.schema_version_id == gold_set.schema_version_id,
                        TaggerVersion.status == "qualified",
                    )
                    .order_by(
                        TagDeployment.approved_at.desc(),
                        TagDeployment.id.desc(),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
        if baseline_id is None:
            raise GovernanceConflictError("no qualified production Harness matches the gold schema")
        return await self.create_optimization_candidate(
            tenant_id=tenant_id,
            gold_set_version_id=gold_set_version_id,
            production_tagger_version_id=int(baseline_id),
            actor_user_id=actor_user_id,
        )

    async def create_optimization_candidate(
        self,
        *,
        tenant_id: str,
        gold_set_version_id: int,
        production_tagger_version_id: int,
        actor_user_id: int,
        optimization_run_id: int | None = None,
        worker_id: str | None = None,
        error_samples: Sequence[Mapping[str, Any]] | None = None,
        extra_candidates: Sequence[InjectedCandidate] = (),
    ) -> tuple[TaggerVersion, dict[str, Any]]:
        """Create a deterministic draft from train errors and validation scores.

        Holdout examples are rejected even when a caller knows their IDs.  The
        optimizer never updates the production version.
        """

        reject_client_error_samples(error_samples)
        async with self._factory() as session:
            optimization_run: TagOptimizationRun | None = None
            optimization_job_id: int | None = None
            optimization_lease_owner: str | None = None
            optimization_lease_token: str | None = None
            optimization_trial_ids: tuple[int, ...] | None = None
            search_manifest_checksum: str | None = None
            max_search_candidates = 32
            if optimization_run_id is not None:
                optimization_run = (
                    await session.execute(
                        select(TagOptimizationRun)
                        .where(
                            TagOptimizationRun.id == optimization_run_id,
                            TagOptimizationRun.tenant_id == tenant_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if optimization_run is None:
                    raise GovernanceNotFoundError("optimization run not found")
                if (
                    optimization_run.gold_set_version_id != gold_set_version_id
                    or optimization_run.baseline_tagger_version_id != production_tagger_version_id
                ):
                    raise GovernanceConflictError(
                        "optimization run is bound to a different baseline or gold set"
                    )
                if optimization_run.status != "running" or optimization_run.phase != "search":
                    raise GovernanceConflictError("optimization run is not active")
                if optimization_run.job_id is None:
                    raise GovernanceConflictError("optimization job is not active")
                optimization_job = (
                    await session.execute(
                        select(TagExtractionJob)
                        .where(
                            TagExtractionJob.id == optimization_run.job_id,
                            TagExtractionJob.tenant_id == tenant_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if (
                    optimization_job is None
                    or optimization_job.job_type != "optimize"
                    or optimization_job.status != "running"
                    or optimization_job.scope.get("optimization_run_id") != optimization_run.id
                ):
                    raise GovernanceConflictError("optimization job is not active")
                if worker_id is not None and optimization_job.lease_owner != worker_id:
                    raise GovernanceConflictError(
                        "optimization job is leased by a different worker"
                    )
                optimization_job_id = int(optimization_job.id)
                optimization_lease_owner = optimization_job.lease_owner
                optimization_lease_token = optimization_job.lease_token
                if (
                    optimization_lease_owner is None
                    or optimization_lease_token is None
                    or optimization_job.lease_expires_at is None
                    or _aware_utc(optimization_job.lease_expires_at) < _utcnow()
                ):
                    raise GovernanceConflictError(
                        "optimization job requires an active lease before Provider trials"
                    )
                configured_max_trials = optimization_run.search_budget.get("max_trials", 32)
                if (
                    isinstance(configured_max_trials, bool)
                    or not isinstance(configured_max_trials, int)
                    or not 1 <= configured_max_trials <= 32
                ):
                    raise GovernanceConflictError(
                        "optimization run contains an invalid max_trials budget"
                    )
                max_search_candidates = configured_max_trials
                existing_candidate = (
                    await session.execute(
                        select(TaggerVersion).where(
                            TaggerVersion.tenant_id == tenant_id,
                            TaggerVersion.optimization_run_id == optimization_run_id,
                        )
                    )
                ).scalar_one_or_none()
                if existing_candidate is not None:
                    audit = (
                        await session.execute(
                            select(TagGovernanceAuditEvent)
                            .where(
                                TagGovernanceAuditEvent.tenant_id == tenant_id,
                                TagGovernanceAuditEvent.resource_type == "tagger_version",
                                TagGovernanceAuditEvent.resource_id == existing_candidate.id,
                                TagGovernanceAuditEvent.action == "optimization_candidate_created",
                            )
                            .order_by(
                                TagGovernanceAuditEvent.occurred_at.desc(),
                                TagGovernanceAuditEvent.id.desc(),
                            )
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    return (
                        existing_candidate,
                        deepcopy(audit.payload) if audit is not None else {},
                    )
            snapshot = (
                await session.execute(
                    select(TagGoldSetVersion).where(
                        TagGoldSetVersion.id == gold_set_version_id,
                        TagGoldSetVersion.tenant_id == tenant_id,
                        TagGoldSetVersion.status == "frozen",
                    )
                )
            ).scalar_one_or_none()
            production = (
                await session.execute(
                    select(TaggerVersion).where(
                        TaggerVersion.id == production_tagger_version_id,
                        TaggerVersion.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if snapshot is None:
                raise GovernanceNotFoundError("frozen gold set version not found")
            if production is None:
                raise GovernanceNotFoundError("production tagger version not found")
            if optimization_run is not None and str(
                snapshot.dataset_snapshot_hash or snapshot.checksum or ""
            ) != str(optimization_run.dataset_snapshot_hash):
                raise GovernanceConflictError(
                    "optimization run dataset snapshot no longer matches its frozen gold set"
                )
            if production.status != "qualified":
                raise GovernanceConflictError("optimization baseline must be a qualified tagger")
            gold_set = (
                await session.execute(
                    select(TagGoldSet).where(
                        TagGoldSet.id == snapshot.gold_set_id,
                        TagGoldSet.tenant_id == tenant_id,
                    )
                )
            ).scalar_one()
            if gold_set.schema_version_id != production.schema_version_id:
                raise GovernanceConflictError("gold set and production tagger schemas differ")
            schema_version = (
                await session.execute(
                    select(TagSchemaVersion).where(
                        TagSchemaVersion.id == production.schema_version_id,
                        TagSchemaVersion.tenant_id == tenant_id,
                    )
                )
            ).scalar_one()
            critical_keys = {
                str(item["key"])
                for item in schema_version.definitions
                if isinstance(item, dict)
                and item.get("key")
                and (bool(item.get("critical")) or bool(item.get("critical_values")))
            }
            evidence_required_keys = {
                str(item["key"])
                for item in schema_version.definitions
                if isinstance(item, dict)
                and item.get("key")
                and bool(item.get("evidence_required"))
            }
            candidate_gold_predicates: list[Any] = [
                TagGoldLabel.tenant_id == tenant_id,
                TagGoldLabel.gold_set_version_id == gold_set_version_id,
                TagGoldLabel.split.in_(["train", "validation"]),
                TagGoldLabel.truth_tier.in_(["t2", "t3"]),
                TagGoldLabel.truth_state.in_(["present", "absent"]),
            ]
            if optimization_run is not None:
                reception_scope, resolved_reception_ids = self._validated_frozen_reception_scope(
                    optimization_run.cohort
                )
                if reception_scope == "explicit":
                    candidate_gold_predicates.append(
                        TagGoldLabel.reception_id.in_(resolved_reception_ids)
                    )
                run_filters = optimization_run.cohort.get("filters")
                if isinstance(run_filters, Mapping):
                    raw_label_keys = run_filters.get("label_keys")
                    if isinstance(raw_label_keys, list) and raw_label_keys:
                        candidate_gold_predicates.append(
                            TagGoldLabel.tag_key.in_([str(value) for value in raw_label_keys])
                        )

            proposed_fact_alias = aliased(TagAssignmentFact)
            resulting_fact_alias = aliased(TagAssignmentFact)
            feedback_rows = (
                await session.execute(
                    select(
                        TagGoldLabel,
                        TagReviewTask,
                        TagReviewDecision,
                        proposed_fact_alias,
                        resulting_fact_alias,
                        TagHarnessExecution,
                    )
                    .join(
                        TagReviewDecision,
                        (TagReviewDecision.id == TagGoldLabel.review_decision_id)
                        & (TagReviewDecision.tenant_id == tenant_id),
                    )
                    .join(
                        TagReviewTask,
                        (TagReviewTask.id == TagReviewDecision.task_id)
                        & (TagReviewTask.tenant_id == tenant_id),
                    )
                    .outerjoin(
                        proposed_fact_alias,
                        (proposed_fact_alias.id == TagReviewTask.proposed_fact_id)
                        & (proposed_fact_alias.tenant_id == tenant_id),
                    )
                    .outerjoin(
                        resulting_fact_alias,
                        (resulting_fact_alias.id == TagReviewDecision.resulting_fact_id)
                        & (resulting_fact_alias.tenant_id == tenant_id),
                    )
                    .outerjoin(
                        TagHarnessExecution,
                        (TagHarnessExecution.id == TagReviewTask.source_harness_execution_id)
                        & (TagHarnessExecution.tenant_id == tenant_id),
                    )
                    .where(
                        *candidate_gold_predicates,
                        TagReviewTask.status == "resolved",
                        TagReviewTask.tagger_version_id == production_tagger_version_id,
                        or_(
                            and_(
                                TagReviewTask.proposed_fact_id.is_(None),
                                TagReviewTask.tagger_version_id == production_tagger_version_id,
                            ),
                            proposed_fact_alias.tagger_version_id == production_tagger_version_id,
                        ),
                    )
                    .order_by(TagGoldLabel.id)
                )
            ).all()
            if not feedback_rows:
                raise GovernanceError(
                    "no persisted train/validation feedback exists for this production tagger"
                )

            def execution_usage_value(
                execution: TagHarnessExecution | None,
                key: str,
            ) -> int | None:
                if execution is None or not isinstance(execution.output_snapshot, Mapping):
                    return None
                usage = execution.output_snapshot.get("usage")
                if not isinstance(usage, Mapping):
                    return None
                value = usage.get(key)
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                ):
                    return None
                return value

            def execution_output_value(
                execution: TagHarnessExecution | None,
                key: str,
            ) -> int | None:
                if execution is None or not isinstance(execution.output_snapshot, Mapping):
                    return None
                value = execution.output_snapshot.get(key)
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                ):
                    return None
                return value

            def execution_reviewed_tag(
                execution: TagHarnessExecution | None,
                tag_key: str,
            ) -> bool | None:
                if execution is None or not isinstance(execution.output_snapshot, Mapping):
                    return None
                review_items = execution.output_snapshot.get("review_items")
                review_item_count = execution_output_value(
                    execution,
                    "review_item_count",
                )
                if (
                    not isinstance(review_items, list)
                    or review_item_count is None
                    or len(review_items) != review_item_count
                ):
                    return None
                persisted_keys: list[str] = []
                for item in review_items:
                    if not isinstance(item, Mapping):
                        return None
                    persisted_key = item.get("tag_key")
                    if not isinstance(persisted_key, str) or not persisted_key:
                        return None
                    persisted_keys.append(persisted_key)
                return tag_key in persisted_keys

            feedback_samples = [
                {
                    "gold_label_id": label.id,
                    "predicted_value": (
                        proposed_fact.tag_value if proposed_fact is not None else None
                    ),
                    "baseline_assignment": (
                        {
                            "tag_key": str(proposed_fact.tag_key),
                            "tag_value": deepcopy(proposed_fact.tag_value),
                            "confidence": float(proposed_fact.confidence or 0),
                            "evidence_refs": deepcopy(
                                proposed_fact.evidence_refs or []
                            ),
                        }
                        if proposed_fact is not None
                        else None
                    ),
                    "score": (
                        float(proposed_fact.confidence or 0) if proposed_fact is not None else 0.0
                    ),
                    "evidence_terms": [
                        str(evidence.get("text_excerpt") or evidence.get("text") or "").strip()[
                            :128
                        ]
                        for evidence in (
                            resulting_fact.evidence_refs
                            if resulting_fact is not None
                            else decision.evidence_refs
                        )
                        if isinstance(evidence, dict)
                        and (evidence.get("text_excerpt") or evidence.get("text"))
                    ][:16],
                    "label": label,
                    "review_reason": task.reason,
                    "decision_action": decision.action,
                    "primary_failure_stage": (
                        getattr(decision, "primary_failure_stage", None) or "tag_reasoning"
                    ),
                    "harness_execution_id": (
                        int(harness_execution.id) if harness_execution is not None else None
                    ),
                    "provider_tokens": (
                        int(harness_execution.token_count)
                        if harness_execution is not None
                        else None
                    ),
                    "provider_cost_units": (
                        float(harness_execution.cost_units)
                        if harness_execution is not None
                        else None
                    ),
                    "provider_cost_microunits": (
                        execution_usage_value(
                            harness_execution,
                            "cost_microunits",
                        )
                    ),
                    "provider_cold_cost_microunits": (
                        execution_usage_value(
                            harness_execution,
                            "cold_cache_cost_microunits",
                        )
                    ),
                    "provider_calls": (
                        execution_usage_value(
                            harness_execution,
                            "provider_calls",
                        )
                    ),
                    "provider_input_tokens": execution_usage_value(
                        harness_execution,
                        "provider_input_tokens",
                    ),
                    "provider_output_tokens": execution_usage_value(
                        harness_execution,
                        "provider_output_tokens",
                    ),
                    "reused_input_tokens": execution_usage_value(
                        harness_execution,
                        "reused_input_tokens",
                    ),
                    "reused_output_tokens": execution_usage_value(
                        harness_execution,
                        "reused_output_tokens",
                    ),
                    "cache_hits": execution_usage_value(
                        harness_execution,
                        "cache_hits",
                    ),
                    "unknown_billed_tokens": execution_usage_value(
                        harness_execution,
                        "unknown_billed_tokens",
                    ),
                    "review_item_count": execution_output_value(
                        harness_execution,
                        "review_item_count",
                    ),
                    "baseline_reviewed": execution_reviewed_tag(
                        harness_execution,
                        str(label.tag_key),
                    ),
                    "provider_latency_ms": (
                        int(harness_execution.latency_ms) if harness_execution is not None else None
                    ),
                }
                for (
                    label,
                    task,
                    decision,
                    proposed_fact,
                    resulting_fact,
                    harness_execution,
                ) in feedback_rows
            ]
            eligible_feedback_samples = [
                sample
                for sample in feedback_samples
                if str(sample["primary_failure_stage"]) not in _UPSTREAM_FAILURE_STAGES
            ]
            if not eligible_feedback_samples:
                raise GovernanceError(
                    "persisted feedback contains only upstream audio/ASR failures"
                )

            train_errors: dict[str, dict[str, int]] = {}
            validation_samples: dict[str, list[tuple[float, bool]]] = {}
            for sample in eligible_feedback_samples:
                label = cast(TagGoldLabel, sample["label"])
                subject_tag_key = f"{label.subject_type}:{label.tag_key}"
                predicted = sample.get("predicted_value")
                is_correct = predicted == label.tag_value
                sample["is_correct"] = is_correct
                sample["subject_tag_key"] = subject_tag_key
                score = float(sample.get("score", 0))
                if label.split == "train" and not is_correct:
                    key = f"{predicted!r}->{label.tag_value!r}"
                    train_errors.setdefault(subject_tag_key, {})[key] = (
                        train_errors.setdefault(subject_tag_key, {}).get(key, 0) + 1
                    )
                elif label.split == "validation":
                    validation_samples.setdefault(subject_tag_key, []).append((score, is_correct))

            thresholds = dict(production.thresholds)
            threshold_search: dict[str, dict[str, Any]] = {}
            for subject_tag_key, samples in validation_samples.items():
                _subject_type, tag_key = subject_tag_key.split(":", 1)
                best_threshold = float(
                    thresholds.get(
                        subject_tag_key,
                        thresholds.get(tag_key, thresholds.get("default", 0.5)),
                    )
                )
                best_score = -1.0
                best_recall = 0.0
                for step in range(101):
                    threshold = step / 100
                    tp = sum(score >= threshold and correct for score, correct in samples)
                    fp = sum(score >= threshold and not correct for score, correct in samples)
                    fn = sum(score < threshold and correct for score, correct in samples)
                    recall = tp / (tp + fn) if tp + fn else 0.0
                    if tag_key in critical_keys:
                        denominator = (5 * tp) + (4 * fn) + fp
                        score_value = (5 * tp / denominator) if denominator else 0.0
                        if recall < 0.95:
                            continue
                    else:
                        denominator = (2 * tp) + fp + fn
                        score_value = (2 * tp / denominator) if denominator else 0.0
                    if best_score < score_value or (
                        score_value == best_score and abs(threshold - best_threshold) < 1e-12
                    ):
                        best_score = score_value
                        best_threshold = threshold
                        best_recall = recall
                if best_score < 0:
                    raise GovernanceConflictError(
                        f"critical label {subject_tag_key} has no threshold meeting recall 0.95"
                    )
                thresholds[subject_tag_key] = round(best_threshold, 2)
                threshold_search[subject_tag_key] = {
                    "threshold": round(best_threshold, 2),
                    "objective": "f2" if tag_key in critical_keys else "f1",
                    "validation_score": round(best_score, 6),
                    "validation_recall": round(best_recall, 6),
                    "sample_count": len(samples),
                }

            persisted_search_samples = [
                {
                    "gold_label_id": int(cast(TagGoldLabel, sample["label"]).id),
                    "subject_type": cast(TagGoldLabel, sample["label"]).subject_type,
                    "subject_id": cast(TagGoldLabel, sample["label"]).subject_id,
                    "tag_key": cast(TagGoldLabel, sample["label"]).tag_key,
                    "subject_tag_key": str(sample["subject_tag_key"]),
                    "split": cast(TagGoldLabel, sample["label"]).split,
                    "tenant_id": tenant_id,
                    "baseline_tagger_version_id": production_tagger_version_id,
                    "input_snapshot": deepcopy(
                        cast(TagGoldLabel, sample["label"]).input_snapshot or {}
                    ),
                    "gold_value": deepcopy(
                        cast(TagGoldLabel, sample["label"]).tag_value
                    ),
                    "truth_state": cast(TagGoldLabel, sample["label"]).truth_state,
                    "gold_evidence_refs": deepcopy(
                        cast(TagGoldLabel, sample["label"]).evidence_refs or []
                    ),
                    "schema_definitions": deepcopy(schema_version.definitions),
                    "baseline_predicted_value": deepcopy(
                        sample.get("predicted_value")
                    ),
                    "baseline_assignment": deepcopy(
                        sample.get("baseline_assignment")
                    ),
                    "baseline_is_correct": bool(sample.get("is_correct")),
                    "score": float(sample.get("score", 0)),
                    "is_correct": bool(sample.get("is_correct")),
                    "is_critical": (cast(TagGoldLabel, sample["label"]).tag_key in critical_keys),
                    "evidence_required": (
                        cast(TagGoldLabel, sample["label"]).tag_key
                        in evidence_required_keys
                    ),
                    "primary_failure_stage": str(sample["primary_failure_stage"]),
                    "harness_execution_id": sample.get("harness_execution_id"),
                    "provider_tokens": sample.get("provider_tokens"),
                    "provider_cost_units": sample.get("provider_cost_units"),
                    "provider_cost_microunits": sample.get(
                        "provider_cost_microunits"
                    ),
                    "provider_cold_cost_microunits": sample.get(
                        "provider_cold_cost_microunits"
                    ),
                    "provider_calls": sample.get("provider_calls"),
                    "provider_input_tokens": sample.get("provider_input_tokens"),
                    "provider_output_tokens": sample.get("provider_output_tokens"),
                    "reused_input_tokens": sample.get("reused_input_tokens"),
                    "reused_output_tokens": sample.get("reused_output_tokens"),
                    "cache_hits": sample.get("cache_hits"),
                    "unknown_billed_tokens": sample.get("unknown_billed_tokens"),
                    "review_item_count": sample.get("review_item_count"),
                    "baseline_reviewed": sample.get("baseline_reviewed"),
                    "provider_latency_ms": sample.get("provider_latency_ms"),
                }
                for sample in eligible_feedback_samples
            ]
            existing_harness_spec = getattr(production, "harness_spec", None)
            if not isinstance(existing_harness_spec, dict) or not existing_harness_spec:
                rule_harness_spec = production.rule_bundle.get("harness_spec")
                existing_harness_spec = (
                    deepcopy(rule_harness_spec)
                    if isinstance(rule_harness_spec, dict)
                    else {
                        "context": {"neighbor_units": 0},
                        "memory": {"example_count": 0, "strategy": "similar"},
                        "orchestration": {
                            "route": {
                                "rule": "rule_only",
                                "llm": "weak_llm",
                                "hybrid": "rule_llm_fusion",
                            }.get(production.engine, "weak_llm"),
                            "fusion": "score_priority",
                        },
                        "output": {"threshold_offset": 0.0},
                    }
                )
            existing_harness_spec = resolve_harness_spec(
                SimpleNamespace(
                    engine=production.engine,
                    prompt_content=production.prompt_content,
                    rule_bundle=production.rule_bundle,
                    thresholds=thresholds,
                    harness_spec=deepcopy(existing_harness_spec),
                    harness_spec_version=production.harness_spec_version,
                )
            )
            existing_harness_spec["output"]["thresholds"] = deepcopy(thresholds)
            trial_executor = self._optimization_trial_executor or PersistedPredictionTrialExecutor(
                baseline_thresholds=production.thresholds,
            )
            objective_policy = (
                str(optimization_run.objective.get("policy", "balanced"))
                if optimization_run is not None
                else "balanced"
            )
            candidate_envelope = _bounded_candidate_configs(
                existing_harness_spec,
                materialized_dimensions=trial_executor.materialized_dimensions,
                extra_candidates=extra_candidates,
            )[:max_search_candidates]
            if optimization_run is not None:
                phase_a_trials = list(
                    (
                        await session.execute(
                            select(TagOptimizationTrial)
                            .where(
                                TagOptimizationTrial.tenant_id == tenant_id,
                                TagOptimizationTrial.optimization_run_id
                                == optimization_run.id,
                            )
                            .order_by(TagOptimizationTrial.ordinal)
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .all()
                )
                if not phase_a_trials:
                    for ordinal, (mutation, candidate_config) in enumerate(
                        candidate_envelope,
                        start=1,
                    ):
                        dimension = mutation.split("=", 1)[0].split(".", 1)[0]
                        phase_a_trial = TagOptimizationTrial(
                            tenant_id=tenant_id,
                            optimization_run_id=optimization_run.id,
                            ordinal=ordinal,
                            mutation={
                                "description": mutation,
                                "dimension": dimension,
                            },
                            harness_spec=deepcopy(candidate_config),
                            status="running",
                            phase="train",
                            reward_vector={},
                            metrics={},
                            gate_results={},
                            summary={"compatibility_materialized": True},
                            next_actions=[],
                            artifacts=[],
                            started_at=_utcnow(),
                        )
                        session.add(phase_a_trial)
                        phase_a_trials.append(phase_a_trial)
                    await session.flush()
                if phase_a_trials and len(phase_a_trials) < len(candidate_envelope):
                    raise GovernanceConflictError(
                        "candidate envelope exceeds the persisted bounded trials"
                    )
                search_manifest_checksum = canonical_checksum(
                    _search_manifest_payload(
                        dataset_snapshot_hash=str(
                            snapshot.dataset_snapshot_hash or snapshot.checksum or ""
                        ),
                        baseline_tagger_version_id=int(production.id),
                        baseline_config_checksum=str(production.config_checksum),
                        schema_checksum=str(schema_version.checksum),
                        candidate_checksums=[
                            canonical_checksum(candidate)
                            for _mutation, candidate in candidate_envelope
                        ],
                        gold_inputs=[
                            {
                                "gold_label_id": int(sample["gold_label_id"]),
                                "input_hash": str(
                                    cast(TagGoldLabel, sample["label"]).input_hash
                                ),
                                "harness_execution_id": sample.get(
                                    "harness_execution_id"
                                ),
                                "baseline_reviewed": sample.get(
                                    "baseline_reviewed"
                                ),
                            }
                            for sample in eligible_feedback_samples
                        ],
                        extra_candidates=extra_candidates,
                    )
                )
                optimization_run.summary = {
                    **dict(optimization_run.summary),
                    "search_manifest_checksum": search_manifest_checksum,
                }
                if phase_a_trials:
                    optimization_trial_ids = tuple(
                        int(trial.id)
                        for trial in phase_a_trials[: len(candidate_envelope)]
                    )
                    for index, (mutation, candidate_config) in enumerate(
                        candidate_envelope
                    ):
                        phase_a_trial = phase_a_trials[index]
                        dimension = mutation.split("=", 1)[0].split(".", 1)[0]
                        phase_a_trial.mutation = {
                            "description": mutation,
                            "dimension": dimension,
                            "candidate_checksum": canonical_checksum(candidate_config),
                        }
                        phase_a_trial.harness_spec = deepcopy(candidate_config)
                        phase_a_trial.status = "running"
                        phase_a_trial.started_at = phase_a_trial.started_at or _utcnow()
                await session.flush()

            # Provider trials must never run while holding the optimization
            # run/job/trial row locks.  This commit freezes a plain manifest and
            # deliberately releases every database lock before external I/O.
            persisted_search_budget = (
                deepcopy(dict(optimization_run.search_budget))
                if optimization_run is not None
                else None
            )
            await session.commit()

            reserve_budget_callback: TrialBudgetReserve | None = None
            settle_budget_callback: TrialBudgetSettle | None = None
            if optimization_run_id is not None:
                assert optimization_job_id is not None
                assert optimization_lease_owner is not None
                assert optimization_lease_token is not None
                assert search_manifest_checksum is not None

                async def durable_reserve_budget(
                    trial_index: int,
                    mutation: str,
                    candidate_checksum: str,
                    estimate: Mapping[str, int | None],
                ) -> Mapping[str, Any]:
                    return await self._reserve_optimization_trial_budget(
                        tenant_id=tenant_id,
                        optimization_run_id=optimization_run_id,
                        optimization_job_id=optimization_job_id,
                        gold_set_version_id=gold_set_version_id,
                        production_tagger_version_id=production_tagger_version_id,
                        search_manifest_checksum=search_manifest_checksum,
                        lease_owner=optimization_lease_owner,
                        lease_token=optimization_lease_token,
                        worker_id=worker_id,
                        trial_index=trial_index,
                        mutation=mutation,
                        candidate_checksum=candidate_checksum,
                        estimate=estimate,
                    )

                async def durable_settle_budget(
                    reservation: Mapping[str, Any],
                    actual: Mapping[str, int],
                ) -> Mapping[str, Any]:
                    return await self._settle_optimization_trial_budget(
                        tenant_id=tenant_id,
                        optimization_run_id=optimization_run_id,
                        optimization_job_id=optimization_job_id,
                        gold_set_version_id=gold_set_version_id,
                        production_tagger_version_id=production_tagger_version_id,
                        search_manifest_checksum=search_manifest_checksum,
                        lease_owner=optimization_lease_owner,
                        lease_token=optimization_lease_token,
                        worker_id=worker_id,
                        reservation=reservation,
                        actual=actual,
                    )

                reserve_budget_callback = durable_reserve_budget
                settle_budget_callback = durable_settle_budget

            harness_search = await execute_harness_trials(
                baseline_config=existing_harness_spec,
                feedback_samples=persisted_search_samples,
                trial_executor=trial_executor,
                max_candidates=max_search_candidates,
                objective_policy=objective_policy,
                budget=persisted_search_budget,
                reserve_budget=reserve_budget_callback,
                settle_budget=settle_budget_callback,
                optimization_run_id=optimization_run_id,
                optimization_trial_ids=optimization_trial_ids,
            )
            if (
                self._optimization_trial_executor is not None
                and not harness_search.winner.reward.feasible
            ):
                raise GovernanceError(
                    "optimizer has no quality-safe candidate with complete provider usage"
                )
            if optimization_run is not None:
                # Re-acquire locks in the same deterministic order used by
                # phase A.  A cancelled/reclaimed/expired lease invalidates all
                # out-of-transaction results and prevents a stale worker write.
                optimization_run = (
                    await session.execute(
                        select(TagOptimizationRun)
                        .where(
                            TagOptimizationRun.id == optimization_run_id,
                            TagOptimizationRun.tenant_id == tenant_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if (
                    optimization_run is None
                    or optimization_run.status != "running"
                    or optimization_run.phase != "search"
                    or optimization_run.gold_set_version_id != gold_set_version_id
                    or optimization_run.baseline_tagger_version_id
                    != production_tagger_version_id
                    or optimization_run.summary.get("search_manifest_checksum")
                    != search_manifest_checksum
                ):
                    raise GovernanceConflictError(
                        "optimization run changed while trials were executing"
                    )
                optimization_job = (
                    await session.execute(
                        select(TagExtractionJob)
                        .where(
                            TagExtractionJob.id == optimization_job_id,
                            TagExtractionJob.tenant_id == tenant_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if (
                    optimization_job is None
                    or optimization_job.status != "running"
                    or optimization_job.job_type != "optimize"
                    or optimization_job.scope.get("optimization_run_id")
                    != optimization_run.id
                    or optimization_job.lease_owner != optimization_lease_owner
                    or optimization_job.lease_token != optimization_lease_token
                    or (worker_id is not None and optimization_job.lease_owner != worker_id)
                    or (
                        worker_id is not None
                        and (
                            optimization_job.lease_expires_at is None
                            or _aware_utc(optimization_job.lease_expires_at) < _utcnow()
                        )
                    )
                ):
                    raise GovernanceConflictError(
                        "optimization job lease changed while trials were executing"
                    )
                persisted_trials = list(
                    (
                        await session.execute(
                            select(TagOptimizationTrial)
                            .where(
                                TagOptimizationTrial.tenant_id == tenant_id,
                                TagOptimizationTrial.optimization_run_id == optimization_run.id,
                            )
                            .order_by(TagOptimizationTrial.ordinal)
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .all()
                )
                if persisted_trials and len(persisted_trials) < len(harness_search.trials):
                    raise GovernanceConflictError(
                        "executed trials exceed the persisted bounded envelope"
                    )
                if persisted_trials:
                    for index, executed_trial in enumerate(harness_search.trials):
                        persisted_trial = persisted_trials[index]
                        dimension = executed_trial.mutation.split("=", 1)[0].split(".", 1)[0]
                        persisted_trial.mutation = {
                            "description": executed_trial.mutation,
                            "dimension": dimension,
                        }
                        persisted_trial.harness_spec = deepcopy(executed_trial.config)

            summary = {
                tag_key: [
                    {"error": pattern, "count": count}
                    for pattern, count in sorted(
                        patterns.items(),
                        key=lambda item: (-item[1], item[0]),
                    )
                ]
                for tag_key, patterns in sorted(train_errors.items())
            }
            # Training diagnostics stay in optimizer artifacts. Serving prompt
            # and rules must be the exact materialized values evaluated by the
            # selected trial; no post-selection JSON suffix or generated rule.
            winner_harness_spec = materialize_trial_candidate(harness_search.winner.config)
            prompt_content = str(winner_harness_spec["generation"]["prompt_template"])
            rule_bundle = deepcopy(dict(winner_harness_spec["orchestration"]["rule_bundle"]))
            generated_rules: list[dict[str, Any]] = []
            validate_rule_bundle(
                rule_bundle,
                engine=production.engine,
                definitions=cast(list[dict[str, Any]], schema_version.definitions),
            )
            version = (
                f"{production.version}-opt-r{optimization_run_id}"
                if optimization_run_id is not None
                else f"{production.version}-opt-{int(_utcnow().timestamp())}"
            )
            config = {
                "schema_version_id": production.schema_version_id,
                "engine": production.engine,
                "prompt_content": prompt_content,
                "rule_bundle": rule_bundle,
                "model_version": production.model_version,
                "thresholds": thresholds,
                "harness_spec_version": "2.0",
                "harness_spec": winner_harness_spec,
                "parent_version_id": production.id,
                "origin": "optimizer",
                "gold_set_version_id": gold_set_version_id,
                "optimization_run_id": optimization_run_id,
            }
            candidate = TaggerVersion(
                tenant_id=tenant_id,
                schema_version_id=production.schema_version_id,
                version=version,
                engine=production.engine,
                prompt_content=prompt_content,
                rule_bundle=rule_bundle,
                model_version=production.model_version,
                thresholds=thresholds,
                harness_spec_version="2.0",
                harness_spec=winner_harness_spec,
                parent_version_id=production.id,
                origin="optimizer",
                optimization_run_id=optimization_run_id,
                change_summary=("bounded-search winner: " + harness_search.winner.mutation),
                config_checksum=canonical_checksum(config),
                status="draft",
                created_by=actor_user_id,
            )
            session.add(candidate)
            await session.flush()
            metadata = {
                "source_tagger_version_id": production.id,
                "gold_set_version_id": snapshot.id,
                "train_error_summary": summary,
                "threshold_search": threshold_search,
                "generated_rule_count": len(generated_rules),
                "holdout_read": False,
                "bounded_search": {
                    "eligible_sample_count": harness_search.eligible_sample_count,
                    "excluded_upstream_count": (
                        len(feedback_samples) - len(eligible_feedback_samples)
                    ),
                    "max_candidates": max_search_candidates,
                    "objective_policy": objective_policy,
                    "trial_count": len(harness_search.trials),
                    "winner": {
                        "index": harness_search.winner.index,
                        "mutation": harness_search.winner.mutation,
                        "reward": {
                            "feasible": harness_search.winner.reward.feasible,
                            "quality_delta": harness_search.winner.reward.quality_delta,
                            "review_rate_delta": (harness_search.winner.reward.review_rate_delta),
                            "p95_latency_delta": (harness_search.winner.reward.p95_latency_delta),
                            "cost_delta": harness_search.winner.reward.cost_delta,
                        },
                    },
                    "trials": [
                        {
                            "index": trial.index,
                            "mutation": trial.mutation,
                            "reward": {
                                "feasible": trial.reward.feasible,
                                "quality_delta": trial.reward.quality_delta,
                                "review_rate_delta": trial.reward.review_rate_delta,
                                "p95_latency_delta": trial.reward.p95_latency_delta,
                                "cost_delta": trial.reward.cost_delta,
                            },
                            "metrics": dict(trial.metrics),
                        }
                        for trial in harness_search.trials
                    ],
                },
            }
            await self._audit(
                session,
                tenant_id=tenant_id,
                resource_type="tagger_version",
                resource_id=candidate.id,
                action="optimization_candidate_created",
                actor_user_id=actor_user_id,
                payload=metadata,
            )
            await session.commit()
            return candidate, metadata

    @staticmethod
    def _validated_frozen_reception_scope(
        cohort: Mapping[str, Any],
    ) -> tuple[str, tuple[int, ...]]:
        raw_ids = cohort.get("resolved_reception_ids")
        if raw_ids is None:
            return "unresolved", ()
        if not isinstance(raw_ids, list) or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in raw_ids
        ):
            raise GovernanceConflictError(
                "optimization cohort contains invalid frozen reception IDs"
            )
        reception_ids = tuple(sorted(set(raw_ids)))
        if list(reception_ids) != raw_ids:
            raise GovernanceConflictError(
                "optimization cohort frozen reception IDs must be sorted and unique"
            )
        expected_checksum = canonical_checksum({"resolved_reception_ids": list(reception_ids)})
        if cohort.get("resolved_reception_checksum") != expected_checksum:
            raise GovernanceConflictError(
                "optimization cohort frozen reception checksum does not match"
            )
        scope = str(
            cohort.get(
                "resolved_reception_scope",
                "explicit" if reception_ids else "all",
            )
        )
        if scope not in {"all", "explicit"}:
            raise GovernanceConflictError("optimization cohort frozen reception scope is invalid")
        if scope == "all" and reception_ids:
            raise GovernanceConflictError(
                "an all-reception optimization cohort cannot contain explicit IDs"
            )
        return scope, reception_ids

    @staticmethod
    def _parse_optimization_cohort_datetime(
        value: object,
        *,
        field_name: str,
    ) -> datetime | None:
        if value in {None, ""}:
            return None
        if not isinstance(value, str):
            raise GovernanceError(f"optimization cohort {field_name} must be an ISO timestamp")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise GovernanceError(
                f"optimization cohort {field_name} must be an ISO timestamp"
            ) from exc
        if parsed.tzinfo is None:
            raise GovernanceError(f"optimization cohort {field_name} must include a timezone")
        return parsed.astimezone(UTC)

    @staticmethod
    async def _resolve_optimization_cohort(
        session: AsyncSession,
        *,
        tenant_id: str,
        cohort: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Resolve mutable business filters once and freeze their Reception set."""

        filters_value = cohort.get("filters")
        filters = dict(filters_value) if isinstance(filters_value, Mapping) else {}

        def string_values(name: str) -> list[str]:
            raw = filters.get(name, [])
            if not isinstance(raw, list) or any(
                not isinstance(value, str) or not value for value in raw
            ):
                raise GovernanceError(f"optimization cohort filters.{name} must be a string list")
            return sorted(set(raw))

        store_ids = string_values("store_ids")
        agent_names = string_values("agent_names")
        scenarios = string_values("scenarios")
        group_keys = string_values("group_keys")
        label_keys = string_values("label_keys")
        raw_reception_ids = filters.get("reception_ids", [])
        if not isinstance(raw_reception_ids, list) or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in raw_reception_ids
        ):
            raise GovernanceError(
                "optimization cohort filters.reception_ids must contain positive integers"
            )
        requested_reception_ids = sorted(set(raw_reception_ids))
        group_ids_value = cohort.get("group_ids", [])
        if not isinstance(group_ids_value, list) or any(
            not isinstance(value, str) or not value for value in group_ids_value
        ):
            raise GovernanceError("optimization cohort group_ids must be a string list")
        group_ids = sorted(set(group_ids_value))
        started_from = TagGovernanceService._parse_optimization_cohort_datetime(
            filters.get("started_from"),
            field_name="filters.started_from",
        )
        started_to = TagGovernanceService._parse_optimization_cohort_datetime(
            filters.get("started_to"),
            field_name="filters.started_to",
        )
        if started_from is not None and started_to is not None and started_to < started_from:
            raise GovernanceError(
                "optimization cohort filters.started_to must not precede started_from"
            )

        reception_constrained = bool(
            store_ids
            or agent_names
            or scenarios
            or group_keys
            or group_ids
            or requested_reception_ids
            or started_from is not None
            or started_to is not None
            or bool(cohort.get("conflict_only"))
        )
        if not reception_constrained:
            resolved_ids: list[int] = []
            scope = "all"
        else:
            predicates: list[Any] = [Reception.tenant_id == tenant_id]
            if store_ids:
                predicates.append(Reception.store_id.in_(store_ids))
            if agent_names:
                predicates.append(Reception.agent_name.in_(agent_names))
            if scenarios:
                predicates.append(Reception.scenario.in_(scenarios))
            if requested_reception_ids:
                predicates.append(Reception.id.in_(requested_reception_ids))
            if started_from is not None:
                predicates.append(Reception.started_at >= started_from)
            if started_to is not None:
                predicates.append(Reception.started_at <= started_to)
            resolved_ids = sorted(
                int(value)
                for value in (
                    await session.execute(
                        select(Reception.id).where(*predicates).order_by(Reception.id)
                    )
                ).scalars()
            )
            scope = "explicit"

            assignment_predicates: list[Any] = [
                DialogueTagAssignment.tenant_id == tenant_id,
                DialogueTagAssignment.is_current.is_(True),
            ]
            if resolved_ids:
                assignment_predicates.append(DialogueTagAssignment.reception_id.in_(resolved_ids))
            else:
                assignment_predicates.append(DialogueTagAssignment.id.in_([]))
            group_conditions: list[Any] = []
            if group_keys:
                group_conditions.append(DialogueTagAssignment.group_key.in_(group_keys))
            for group_id in group_ids:
                group_key, separator, group_version = group_id.partition("@")
                if not separator or not group_key or not group_version:
                    raise GovernanceError(
                        "optimization cohort group_ids must use group_key@version"
                    )
                group_conditions.append(
                    and_(
                        DialogueTagAssignment.group_key == group_key,
                        DialogueTagAssignment.group_version == group_version,
                    )
                )
            if group_conditions:
                assignment_predicates.append(or_(*group_conditions))
            if label_keys:
                assignment_predicates.append(DialogueTagAssignment.label_key.in_(label_keys))
            if group_conditions or bool(cohort.get("conflict_only")):
                if bool(cohort.get("conflict_only")):
                    matching_ids = (
                        await session.execute(
                            select(DialogueTagAssignment.reception_id)
                            .where(*assignment_predicates)
                            .group_by(
                                DialogueTagAssignment.reception_id,
                                DialogueTagAssignment.label_key,
                            )
                            .having(
                                func.count(func.distinct(DialogueTagAssignment.label_value)) > 1
                            )
                        )
                    ).scalars()
                else:
                    matching_ids = (
                        await session.execute(
                            select(DialogueTagAssignment.reception_id)
                            .where(*assignment_predicates)
                            .distinct()
                        )
                    ).scalars()
                resolved_ids = sorted(set(resolved_ids).intersection(int(v) for v in matching_ids))

        resolved = deepcopy(dict(cohort))
        resolved["filters"] = deepcopy(filters)
        resolved["resolved_reception_scope"] = scope
        resolved["resolved_reception_ids"] = resolved_ids
        resolved["resolved_reception_checksum"] = canonical_checksum(
            {"resolved_reception_ids": resolved_ids}
        )
        return resolved

    @staticmethod
    def _optimization_feedback_cohort_key(cohort: Mapping[str, Any]) -> str:
        """Identify one semantic feedback cohort without frozen/runtime fields."""

        raw_filters = cohort.get("filters")
        normalized_filters: dict[str, Any] = {}
        if isinstance(raw_filters, Mapping):
            for key, value in sorted(raw_filters.items(), key=lambda item: str(item[0])):
                normalized_filters[str(key)] = (
                    sorted(value, key=lambda item: str(item)) if isinstance(value, list) else value
                )
        raw_group_ids = cohort.get("group_ids")
        group_ids = (
            sorted(str(value) for value in raw_group_ids) if isinstance(raw_group_ids, list) else []
        )
        return canonical_checksum(
            {
                "filters": normalized_filters,
                "group_ids": group_ids,
                "conflict_only": bool(cohort.get("conflict_only", False)),
            }
        )

    @staticmethod
    async def _optimization_feedback_coverage(
        session: AsyncSession,
        *,
        tenant_id: str,
        cohort: Mapping[str, Any],
        schema_definitions: (
            Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] | None
        ) = None,
    ) -> OptimizationFeedbackCoverage:
        """Measure only new, certified semantic feedback since the last real run."""

        resolved_cohort = (
            dict(cohort)
            if cohort.get("resolved_reception_ids") is not None
            else await TagGovernanceService._resolve_optimization_cohort(
                session,
                tenant_id=tenant_id,
                cohort=cohort,
            )
        )
        cohort_key = TagGovernanceService._optimization_feedback_cohort_key(resolved_cohort)
        historical_runs = list(
            (
                await session.execute(
                    select(TagOptimizationRun)
                    .where(
                        TagOptimizationRun.tenant_id == tenant_id,
                        TagOptimizationRun.job_id.is_not(None),
                        TagOptimizationRun.status != "cancelled",
                    )
                    .order_by(
                        TagOptimizationRun.created_at.desc(),
                        TagOptimizationRun.id.desc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        last_run = next(
            (
                run
                for run in historical_runs
                if (
                    (
                        run.summary.get("feedback_cohort_key")
                        if isinstance(run.summary, dict)
                        else None
                    )
                    or TagGovernanceService._optimization_feedback_cohort_key(
                        run.cohort if isinstance(run.cohort, dict) else {}
                    )
                )
                == cohort_key
            ),
            None,
        )
        last_run_at = last_run.created_at if last_run is not None else None
        after_event_id = 0
        if last_run is not None and isinstance(last_run.summary, dict):
            raw_watermark = last_run.summary.get("feedback_watermark_event_id")
            if isinstance(raw_watermark, int) and not isinstance(raw_watermark, bool):
                after_event_id = max(0, raw_watermark)
        reception_scope, reception_ids = TagGovernanceService._validated_frozen_reception_scope(
            resolved_cohort
        )
        visible_t3_lane = (
            select(TagFeedbackLaneAssignment.id)
            .where(
                TagFeedbackLaneAssignment.tenant_id == tenant_id,
                TagFeedbackLaneAssignment.feedback_event_id == TagFeedbackEvent.id,
                TagFeedbackLaneAssignment.split.in_(_LEARNING_DATASET_SPLITS),
            )
            .exists()
        )
        predicates: list[Any] = [
            TagFeedbackEvent.tenant_id == tenant_id,
            or_(
                and_(
                    TagFeedbackEvent.truth_tier != "t3",
                    TagFeedbackEvent.training_eligible.is_(True),
                ),
                and_(
                    TagFeedbackEvent.truth_tier == "t3",
                    visible_t3_lane,
                ),
            ),
            TagFeedbackEvent.truth_tier.in_(["t2", "t3"]),
            TagFeedbackEvent.truth_state.in_(["present", "absent"]),
            or_(
                TagFeedbackEvent.error_stage.is_(None),
                TagFeedbackEvent.error_stage.not_in(_UPSTREAM_FAILURE_STAGES),
            ),
        ]
        if after_event_id:
            predicates.append(TagFeedbackEvent.id > after_event_id)
        elif last_run_at is not None:
            predicates.append(TagFeedbackEvent.occurred_at > last_run_at)
        if reception_scope == "explicit":
            dialogue_subject_ids = select(DialogueUnit.id).where(
                DialogueUnit.tenant_id == tenant_id,
                DialogueUnit.reception_id.in_(reception_ids),
            )
            predicates.append(
                or_(
                    and_(
                        TagFeedbackEvent.subject_type == "reception",
                        TagFeedbackEvent.subject_id.in_(reception_ids),
                    ),
                    and_(
                        TagFeedbackEvent.subject_type == "dialogue_unit",
                        TagFeedbackEvent.subject_id.in_(dialogue_subject_ids),
                    ),
                )
            )
        filters = resolved_cohort.get("filters")
        requested_labels: set[str] = set()
        if isinstance(filters, Mapping):
            raw_labels = filters.get("label_keys")
            if isinstance(raw_labels, list):
                requested_labels = {
                    str(value) for value in raw_labels if isinstance(value, str) and value
                }
        required_pairs = (
            schema_subject_tag_pairs(
                schema_definitions,
                label_keys=requested_labels or None,
            )
            if schema_definitions is not None
            else ()
        )
        if schema_definitions is not None:
            required_tag_keys = sorted({tag_key for _subject_type, tag_key in required_pairs})
            if required_tag_keys:
                predicates.append(TagFeedbackEvent.tag_key.in_(required_tag_keys))
            else:
                predicates.append(TagFeedbackEvent.id.in_([]))
        elif requested_labels:
            predicates.append(TagFeedbackEvent.tag_key.in_(sorted(requested_labels)))
        rows = (
            await session.execute(
                select(
                    TagFeedbackEvent.subject_type,
                    TagFeedbackEvent.tag_key,
                    func.count(func.distinct(TagFeedbackEvent.subject_id)),
                    func.max(TagFeedbackEvent.id),
                )
                .where(*predicates)
                .group_by(
                    TagFeedbackEvent.subject_type,
                    TagFeedbackEvent.tag_key,
                )
                .order_by(
                    TagFeedbackEvent.subject_type,
                    TagFeedbackEvent.tag_key,
                )
            )
        ).all()
        by_tag_counter: Counter[str] = Counter()
        if schema_definitions is not None:
            required_pair_set = set(required_pairs)
            by_subject_tag = {
                f"{subject_type}:{tag_key}": 0 for subject_type, tag_key in required_pairs
            }
            accepted_rows = [
                row
                for row in rows
                if (str(row.subject_type), str(row.tag_key)) in required_pair_set
            ]
            for row in accepted_rows:
                subject_type = str(row.subject_type)
                tag_key = str(row.tag_key)
                support = int(row[2])
                by_subject_tag[f"{subject_type}:{tag_key}"] = support
                by_tag_counter[tag_key] += support
        else:
            accepted_rows = list(rows)
            by_subject_tag = {
                f"{row.subject_type}:{row.tag_key}": int(row[2]) for row in accepted_rows
            }
            for row in accepted_rows:
                by_tag_counter[str(row.tag_key)] += int(row[2])
        by_tag = dict(sorted(by_tag_counter.items()))
        max_event_id = max(
            [
                after_event_id,
                *(int(row[3]) for row in accepted_rows if row[3] is not None),
            ]
        )
        if schema_definitions is not None:
            for _subject_type, tag_key in required_pairs:
                by_tag.setdefault(tag_key, 0)
        else:
            for tag_key in requested_labels:
                by_tag.setdefault(tag_key, 0)
        total = sum(by_tag.values())
        blockers: list[str] = []
        if total < 200:
            blockers.append("new_t2_t3_feedback_below_200")
        if schema_definitions is not None:
            blockers.extend(
                f"tag_support_below_30:{subject_tag}"
                for subject_tag, support in sorted(by_subject_tag.items())
                if support < 30
            )
            if not required_pairs:
                blockers.append("no_applicable_schema_subject_tags")
        else:
            domains_by_tag: dict[str, set[str]] = defaultdict(set)
            for row in accepted_rows:
                domains_by_tag[str(row.tag_key)].add(str(row.subject_type))
            for subject_tag, support in sorted(by_subject_tag.items()):
                if support >= 30:
                    continue
                _subject_type, tag_key = subject_tag.split(":", 1)
                blocker_key = (
                    subject_tag if len(domains_by_tag.get(tag_key, set())) > 1 else tag_key
                )
                blockers.append(f"tag_support_below_30:{blocker_key}")
            for tag_key in sorted(requested_labels):
                if by_tag[tag_key] == 0:
                    blockers.append(f"tag_support_below_30:{tag_key}")
        if not by_tag:
            blockers.append("no_affected_labels")
        return OptimizationFeedbackCoverage(
            total=total,
            by_tag=by_tag,
            by_subject_tag=by_subject_tag,
            cohort_key=cohort_key,
            since=last_run_at,
            after_event_id=after_event_id,
            max_event_id=max_event_id,
            passed=not blockers,
            blockers=tuple(blockers),
        )

    async def create_server_bound_optimization_run(
        self,
        *,
        tenant_id: str,
        cohort: Mapping[str, Any],
        target_policy: Mapping[str, Any],
        search_budget: Mapping[str, Any],
        actor_user_id: int,
        trigger: str | None = None,
    ) -> TagOptimizationRun:
        """Resolve the current production Schema, baseline and complete gold server-side."""

        async with self._factory() as session:
            production = (
                await session.execute(
                    select(TaggerVersion)
                    .join(
                        TagDeployment,
                        TagDeployment.tagger_version_id == TaggerVersion.id,
                    )
                    .where(
                        TagDeployment.tenant_id == tenant_id,
                        TagDeployment.status == "production",
                        TaggerVersion.tenant_id == tenant_id,
                        TaggerVersion.status == "qualified",
                    )
                    .order_by(
                        TagDeployment.approved_at.desc(),
                        TagDeployment.id.desc(),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if production is None:
                raise GovernanceConflictError(
                    "no qualified production Harness is available for optimization"
                )
            gold_rows = (
                await session.execute(
                    select(TagGoldSetVersion)
                    .join(
                        TagGoldSet,
                        TagGoldSet.id == TagGoldSetVersion.gold_set_id,
                    )
                    .where(
                        TagGoldSetVersion.tenant_id == tenant_id,
                        TagGoldSetVersion.status == "frozen",
                        TagGoldSet.tenant_id == tenant_id,
                        TagGoldSet.schema_version_id == production.schema_version_id,
                    )
                    .order_by(
                        TagGoldSetVersion.frozen_at.desc(),
                        TagGoldSetVersion.id.desc(),
                    )
                )
            ).scalars()
            gold = next(
                (row for row in gold_rows if _gold_manifest_is_complete(row.completeness_manifest)),
                None,
            )
            if gold is None:
                raise GovernanceConflictError(
                    "no complete frozen gold set matches the production Harness schema"
                )
        source = str(cohort.get("source") or "")
        derived_trigger = trigger or ("insight" if "insight" in source else "manual")
        return await self.create_optimization_run(
            tenant_id=tenant_id,
            gold_set_version_id=gold.id,
            cohort=cohort,
            objective=target_policy,
            search_budget=search_budget,
            trigger=derived_trigger,
            actor_user_id=actor_user_id,
        )

    async def create_optimization_run(
        self,
        *,
        tenant_id: str,
        gold_set_version_id: int,
        cohort: Mapping[str, Any],
        objective: Mapping[str, Any],
        search_budget: Mapping[str, Any],
        trigger: str,
        actor_user_id: int,
    ) -> TagOptimizationRun:
        """Create a server-bound optimization run and deterministic trial envelope."""

        max_trials = search_budget.get("max_trials", 32)
        sealed_queries = search_budget.get("sealed_holdout_queries", 1)
        if (
            isinstance(max_trials, bool)
            or not isinstance(max_trials, int)
            or not 1 <= max_trials <= 32
        ):
            raise GovernanceError("optimization max_trials must be between 1 and 32")
        if sealed_queries != 1:
            raise GovernanceError("sealed_holdout_queries must equal 1")
        if trigger not in {"manual", "insight", "scheduled", "feedback_threshold"}:
            raise GovernanceError("unsupported optimization trigger")
        policy = objective.get("policy")
        if policy not in {"balanced", "quality_first", "efficiency_guarded"}:
            raise GovernanceError("unsupported optimization objective policy")
        if not isinstance(cohort.get("source"), str) or not cohort.get("source"):
            raise GovernanceError("optimization cohort requires a source")

        async with self._factory() as session, session.begin():
            snapshot_row = (
                await session.execute(
                    select(TagGoldSetVersion, TagGoldSet)
                    .join(TagGoldSet, TagGoldSet.id == TagGoldSetVersion.gold_set_id)
                    .where(
                        TagGoldSetVersion.id == gold_set_version_id,
                        TagGoldSetVersion.tenant_id == tenant_id,
                        TagGoldSetVersion.status == "frozen",
                        TagGoldSet.tenant_id == tenant_id,
                    )
                )
            ).one_or_none()
            if snapshot_row is None:
                raise GovernanceNotFoundError("frozen gold set version not found")
            snapshot, gold_set = snapshot_row
            if not _gold_manifest_is_complete(snapshot.completeness_manifest):
                raise GovernanceConflictError(
                    "optimization requires a complete, non-legacy gold matrix"
                )
            dataset_snapshot_hash = snapshot.dataset_snapshot_hash or snapshot.checksum
            if not dataset_snapshot_hash:
                raise GovernanceConflictError(
                    "optimization gold set has no frozen dataset snapshot hash"
                )
            schema_version = (
                await session.execute(
                    select(TagSchemaVersion).where(
                        TagSchemaVersion.id == gold_set.schema_version_id,
                        TagSchemaVersion.tenant_id == tenant_id,
                        TagSchemaVersion.status.in_(["published", "deprecated"]),
                    )
                )
            ).scalar_one_or_none()
            if schema_version is None:
                raise GovernanceConflictError(
                    "optimization schema is not an immutable published version"
                )
            schema_definitions = tuple(
                item for item in schema_version.definitions if isinstance(item, Mapping)
            )
            required_schema_pairs = schema_subject_tag_pairs(schema_definitions)
            baseline_row = (
                await session.execute(
                    select(TaggerVersion)
                    .join(
                        TagDeployment,
                        TagDeployment.tagger_version_id == TaggerVersion.id,
                    )
                    .where(
                        TagDeployment.tenant_id == tenant_id,
                        TagDeployment.status == "production",
                        TaggerVersion.tenant_id == tenant_id,
                        TaggerVersion.schema_version_id == gold_set.schema_version_id,
                        TaggerVersion.status == "qualified",
                    )
                    .order_by(
                        TagDeployment.approved_at.desc(),
                        TagDeployment.id.desc(),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if baseline_row is None:
                raise GovernanceConflictError(
                    "no qualified production Harness matches the gold schema"
                )
            sealed_release_key = canonical_checksum(
                {
                    "dataset_snapshot_hash": str(dataset_snapshot_hash),
                }
            )
            existing_sealed_release = (
                await session.execute(
                    select(TagOptimizationRun.id)
                    .where(
                        TagOptimizationRun.tenant_id == tenant_id,
                        TagOptimizationRun.sealed_release_key == sealed_release_key,
                    )
                    .limit(1)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if existing_sealed_release is not None:
                raise GovernanceConflictError("gold_not_release_ready")
            baseline_spec = baseline_row.harness_spec
            if not isinstance(baseline_spec, dict) or not baseline_spec:
                embedded = baseline_row.rule_bundle.get("harness_spec")
                baseline_spec = (
                    deepcopy(embedded)
                    if isinstance(embedded, dict)
                    else {
                        "context": {"neighbor_units": 0},
                        "memory": {"example_count": 0, "strategy": "similar"},
                        "orchestration": {
                            "route": {
                                "rule": "rule_only",
                                "llm": "weak_llm",
                                "hybrid": "rule_llm_fusion",
                            }.get(baseline_row.engine, "weak_llm"),
                            "fusion": "score_priority",
                        },
                        "output": {"threshold_offset": 0.0},
                    }
                )
            required_schema_pair_set = set(required_schema_pairs)
            required_scenario_pairs: set[tuple[str, str, str]] = set()
            required_critical_values: set[tuple[str, str, str]] = set()
            required_scenario_critical_values: set[tuple[str, str, str, str]] = set()
            present_only_schema_pairs: set[tuple[str, str]] = set()
            present_only_scenario_pairs: set[tuple[str, str, str]] = set()
            for definition in schema_definitions:
                tag_key = definition.get("key")
                if not isinstance(tag_key, str) or not tag_key:
                    continue
                definition_pairs = schema_subject_tag_pairs([definition])
                if definition.get("required"):
                    present_only_schema_pairs.update(definition_pairs)
                critical_values = critical_enum_values(definition)
                required_critical_values.update(
                    (subject_type, pair_tag_key, value)
                    for subject_type, pair_tag_key in definition_pairs
                    for value in critical_values
                )
                declared_scenarios = definition.get("scenarios")
                scenarios = (
                    tuple(
                        str(value)
                        for value in declared_scenarios
                        if isinstance(value, str) and value
                    )
                    if isinstance(declared_scenarios, Sequence)
                    and not isinstance(declared_scenarios, (str, bytes))
                    else ()
                )
                required_scenario_pairs.update(
                    (subject_type, scenario, pair_tag_key)
                    for subject_type, pair_tag_key in definition_pairs
                    for scenario in scenarios
                )
                if definition.get("required"):
                    present_only_scenario_pairs.update(
                        (subject_type, scenario, pair_tag_key)
                        for subject_type, pair_tag_key in definition_pairs
                        for scenario in scenarios
                    )
                required_scenario_critical_values.update(
                    (subject_type, scenario, pair_tag_key, value)
                    for subject_type, pair_tag_key in definition_pairs
                    for scenario in scenarios
                    for value in critical_values
                )

            holdout_predicates = (
                TagGoldLabel.tenant_id == tenant_id,
                TagGoldLabel.gold_set_version_id == snapshot.id,
                TagGoldLabel.split == "holdout",
                TagGoldLabel.truth_tier == "t3",
                TagGoldLabel.truth_state.in_(["present", "absent"]),
            )
            holdout_support_rows = (
                await session.execute(
                    select(
                        TagGoldLabel.subject_type,
                        TagGoldLabel.tag_key,
                        func.count(func.distinct(TagGoldLabel.subject_id)),
                    )
                    .where(*holdout_predicates)
                    .group_by(
                        TagGoldLabel.subject_type,
                        TagGoldLabel.tag_key,
                    )
                )
            ).all()
            holdout_state_rows = (
                await session.execute(
                    select(
                        TagGoldLabel.subject_type,
                        TagGoldLabel.tag_key,
                        TagGoldLabel.truth_state,
                        func.count(func.distinct(TagGoldLabel.subject_id)),
                    )
                    .where(*holdout_predicates)
                    .group_by(
                        TagGoldLabel.subject_type,
                        TagGoldLabel.tag_key,
                        TagGoldLabel.truth_state,
                    )
                )
            ).all()
            positive_value_rows = (
                await session.execute(
                    select(
                        TagGoldLabel.subject_type,
                        TagGoldLabel.tag_key,
                        TagGoldLabel.tag_value,
                        func.count(func.distinct(TagGoldLabel.subject_id)),
                    )
                    .where(
                        *holdout_predicates,
                        TagGoldLabel.truth_state == "present",
                    )
                    .group_by(
                        TagGoldLabel.subject_type,
                        TagGoldLabel.tag_key,
                        TagGoldLabel.tag_value,
                    )
                )
            ).all()
            scenario_support_rows: Sequence[Any] = ()
            scenario_state_rows: Sequence[Any] = ()
            scenario_positive_value_rows: Sequence[Any] = ()
            if required_scenario_pairs:
                scenario_support_rows = (
                    await session.execute(
                        select(
                            TagGoldLabel.subject_type,
                            Reception.scenario,
                            TagGoldLabel.tag_key,
                            func.count(func.distinct(TagGoldLabel.subject_id)),
                        )
                        .join(
                            Reception,
                            and_(
                                Reception.id == TagGoldLabel.reception_id,
                                Reception.tenant_id == TagGoldLabel.tenant_id,
                            ),
                        )
                        .where(*holdout_predicates)
                        .group_by(
                            TagGoldLabel.subject_type,
                            Reception.scenario,
                            TagGoldLabel.tag_key,
                        )
                    )
                ).all()
                scenario_state_rows = (
                    await session.execute(
                        select(
                            TagGoldLabel.subject_type,
                            Reception.scenario,
                            TagGoldLabel.tag_key,
                            TagGoldLabel.truth_state,
                            func.count(func.distinct(TagGoldLabel.subject_id)),
                        )
                        .join(
                            Reception,
                            and_(
                                Reception.id == TagGoldLabel.reception_id,
                                Reception.tenant_id == TagGoldLabel.tenant_id,
                            ),
                        )
                        .where(*holdout_predicates)
                        .group_by(
                            TagGoldLabel.subject_type,
                            Reception.scenario,
                            TagGoldLabel.tag_key,
                            TagGoldLabel.truth_state,
                        )
                    )
                ).all()
                scenario_positive_value_rows = (
                    await session.execute(
                        select(
                            TagGoldLabel.subject_type,
                            Reception.scenario,
                            TagGoldLabel.tag_key,
                            TagGoldLabel.tag_value,
                            func.count(func.distinct(TagGoldLabel.subject_id)),
                        )
                        .join(
                            Reception,
                            and_(
                                Reception.id == TagGoldLabel.reception_id,
                                Reception.tenant_id == TagGoldLabel.tenant_id,
                            ),
                        )
                        .where(
                            *holdout_predicates,
                            TagGoldLabel.truth_state == "present",
                        )
                        .group_by(
                            TagGoldLabel.subject_type,
                            Reception.scenario,
                            TagGoldLabel.tag_key,
                            TagGoldLabel.tag_value,
                        )
                    )
                ).all()

            holdout_support = {
                (str(row[0]), str(row[1])): int(row[2])
                for row in holdout_support_rows
            }
            holdout_state_support = {
                (str(row[0]), str(row[1]), str(row[2])): int(row[3])
                for row in holdout_state_rows
            }
            positive_value_support = {
                (str(row[0]), str(row[1]), str(row[2])): int(row[3])
                for row in positive_value_rows
                if row[2] is not None
            }
            scenario_support = {
                (str(row[0]), str(row[1]), str(row[2])): int(row[3])
                for row in scenario_support_rows
                if row[1] is not None
            }
            scenario_state_support = {
                (str(row[0]), str(row[1]), str(row[2]), str(row[3])): int(row[4])
                for row in scenario_state_rows
                if row[1] is not None
            }
            scenario_positive_value_support = {
                (
                    str(row[0]),
                    str(row[1]),
                    str(row[2]),
                    str(row[3]),
                ): int(row[4])
                for row in scenario_positive_value_rows
                if row[1] is not None and row[3] is not None
            }
            checked_schema_pairs = required_schema_pair_set | set(holdout_support)
            critical_support_floor = minimum_perfect_wilson_support()
            release_ready = bool(required_schema_pair_set)
            release_ready = release_ready and all(
                holdout_support.get(pair, 0) >= 30
                and holdout_state_support.get((*pair, "present"), 0) > 0
                and (
                    pair in present_only_schema_pairs
                    or holdout_state_support.get((*pair, "absent"), 0) > 0
                )
                for pair in checked_schema_pairs
            )
            release_ready = release_ready and all(
                positive_value_support.get(required_value, 0) >= critical_support_floor
                for required_value in required_critical_values
            )
            release_ready = release_ready and all(
                scenario_support.get(required_scenario, 0) >= 30
                and scenario_state_support.get((*required_scenario, "present"), 0) > 0
                and (
                    required_scenario in present_only_scenario_pairs
                    or scenario_state_support.get((*required_scenario, "absent"), 0) > 0
                )
                for required_scenario in required_scenario_pairs
            )
            release_ready = release_ready and all(
                scenario_positive_value_support.get(required_value, 0)
                >= critical_support_floor
                for required_value in required_scenario_critical_values
            )
            if not release_ready:
                raise GovernanceConflictError("gold_not_release_ready")

            resolved_cohort = await self._resolve_optimization_cohort(
                session,
                tenant_id=tenant_id,
                cohort=cohort,
            )
            resolved_cohort["feedback_cohort_key"] = self._optimization_feedback_cohort_key(
                resolved_cohort
            )
            reception_scope, resolved_reception_ids = self._validated_frozen_reception_scope(
                resolved_cohort
            )
            optimization_gold_predicates: list[Any] = [
                TagGoldLabel.tenant_id == tenant_id,
                TagGoldLabel.gold_set_version_id == snapshot.id,
                TagGoldLabel.truth_tier.in_(["t2", "t3"]),
                TagGoldLabel.truth_state.in_(["present", "absent"]),
            ]
            if reception_scope == "explicit":
                optimization_gold_predicates.append(
                    TagGoldLabel.reception_id.in_(resolved_reception_ids)
                )
            resolved_filters = resolved_cohort.get("filters")
            if isinstance(resolved_filters, Mapping):
                resolved_label_keys = resolved_filters.get("label_keys")
                if isinstance(resolved_label_keys, list) and resolved_label_keys:
                    optimization_gold_predicates.append(
                        TagGoldLabel.tag_key.in_(str(value) for value in resolved_label_keys)
                    )
            optimization_lane_rows = (
                await session.execute(
                    select(
                        TagGoldLabel.split,
                        func.count(TagGoldLabel.id),
                    )
                    .where(
                        *optimization_gold_predicates,
                        TagGoldLabel.split.in_(["train", "validation"]),
                    )
                    .group_by(TagGoldLabel.split)
                )
            ).all()
            optimization_lane_counts = {
                str(split): int(count)
                for split, count in optimization_lane_rows
            }
            if any(
                optimization_lane_counts.get(split, 0) <= 0
                for split in ("train", "validation")
            ):
                raise GovernanceConflictError("gold_not_optimization_ready")
            feedback_coverage = await self._optimization_feedback_coverage(
                session,
                tenant_id=tenant_id,
                cohort=resolved_cohort,
                schema_definitions=schema_definitions,
            )
            resolved_baseline_spec = resolve_harness_spec(
                SimpleNamespace(
                    engine=baseline_row.engine,
                    prompt_content=baseline_row.prompt_content,
                    rule_bundle=baseline_row.rule_bundle,
                    thresholds=baseline_row.thresholds,
                    harness_spec=baseline_spec,
                    harness_spec_version=baseline_row.harness_spec_version,
                )
            )
            trial_configs = (
                [
                    (mutation, config)
                    for mutation, config in _bounded_candidate_configs(
                        resolved_baseline_spec,
                        materialized_dimensions=(
                            self._optimization_materialized_dimensions()
                        ),
                    )[:max_trials]
                ]
                if feedback_coverage.passed
                else []
            )
            next_actions = (
                ["execute_bounded_search"]
                if feedback_coverage.passed
                else ["collect_more_t2_t3_feedback", "coverage_diagnostic_only"]
            )
            effective_trigger = (
                "feedback_threshold"
                if trigger == "scheduled" and feedback_coverage.passed
                else trigger
            )
            now = _utcnow()
            run = TagOptimizationRun(
                tenant_id=tenant_id,
                baseline_tagger_version_id=baseline_row.id,
                gold_set_version_id=snapshot.id,
                dataset_snapshot_hash=str(dataset_snapshot_hash),
                sealed_release_key=(
                    sealed_release_key if feedback_coverage.passed else None
                ),
                trigger=effective_trigger,
                status=("queued" if feedback_coverage.passed else "completed"),
                phase=("prepare" if feedback_coverage.passed else "completed"),
                cohort=deepcopy(resolved_cohort),
                objective=deepcopy(dict(objective)),
                search_budget={
                    **deepcopy(dict(search_budget)),
                    "max_trials": max_trials,
                    "sealed_holdout_queries": 1,
                },
                summary={
                    "eligible_feedback_count": feedback_coverage.total,
                    "new_feedback_count": feedback_coverage.total,
                    "feedback_by_tag": feedback_coverage.by_tag,
                    "feedback_by_subject_tag": feedback_coverage.by_subject_tag,
                    "feedback_since": (
                        feedback_coverage.since.isoformat()
                        if feedback_coverage.since is not None
                        else None
                    ),
                    "feedback_after_event_id": feedback_coverage.after_event_id,
                    "feedback_watermark_event_id": feedback_coverage.max_event_id,
                    "feedback_cohort_key": feedback_coverage.cohort_key,
                    "coverage_gate_passed": feedback_coverage.passed,
                    "coverage_blockers": list(feedback_coverage.blockers),
                    "gold_preflight_passed": True,
                    "holdout_read": False,
                    "server_bound_baseline": True,
                    "trial_count": len(trial_configs),
                    "diagnostic_only": not feedback_coverage.passed,
                },
                next_actions=next_actions,
                artifacts=[],
                finished_at=(None if feedback_coverage.passed else now),
                created_by=actor_user_id,
            )
            session.add(run)
            try:
                await session.flush()
            except IntegrityError as exc:
                if run.sealed_release_key is not None:
                    raise GovernanceConflictError("gold_not_release_ready") from exc
                raise
            for ordinal, (mutation, config) in enumerate(trial_configs, start=1):
                dimension = mutation.split("=", 1)[0].split(".", 1)[0]
                session.add(
                    TagOptimizationTrial(
                        tenant_id=tenant_id,
                        optimization_run_id=run.id,
                        ordinal=ordinal,
                        mutation={
                            "description": mutation,
                            "dimension": dimension,
                        },
                        harness_spec=config,
                        status="pending",
                        phase="train",
                        reward_vector={},
                        metrics={},
                        gate_results={},
                        summary={},
                        next_actions=[],
                        artifacts=[],
                    )
                )
            job: TagExtractionJob | None = None
            if feedback_coverage.passed:
                job = TagExtractionJob(
                    tenant_id=tenant_id,
                    job_type="optimize",
                    origin="system",
                    status="queued",
                    scope={"optimization_run_id": run.id},
                    tagger_version_id=baseline_row.id,
                    idempotency_key=f"optimization-run:{run.id}",
                    total_items=1,
                    completed_items=0,
                    failed_items=0,
                    failed_subset=[],
                    attempt_count=0,
                    max_attempts=3,
                    revision=1,
                    created_by=actor_user_id,
                )
                session.add(job)
                await session.flush()
                run.job_id = job.id
            await self._audit(
                session,
                tenant_id=tenant_id,
                resource_type="tag_optimization_run",
                resource_id=run.id,
                action=(
                    "optimization_run_created"
                    if feedback_coverage.passed
                    else "optimization_coverage_diagnostic_created"
                ),
                actor_user_id=actor_user_id,
                payload={
                    "baseline_tagger_version_id": baseline_row.id,
                    "dataset_snapshot_hash": str(dataset_snapshot_hash),
                    "gold_set_version_id": snapshot.id,
                    "job_id": job.id if job is not None else None,
                    "max_trials": max_trials,
                    "sealed_holdout_queries": 1,
                    "coverage_gate_passed": feedback_coverage.passed,
                    "coverage_blockers": list(feedback_coverage.blockers),
                },
            )
            await session.flush()
            return run

    async def run_weekly_optimization_checks(
        self,
        *,
        at: datetime,
        actor_user_id: int = 0,
    ) -> list[TagOptimizationRun]:
        """Run one idempotent coverage check per production tenant and ISO week."""

        if at.tzinfo is None:
            raise GovernanceError("weekly optimization check requires a timezone-aware timestamp")
        iso_year, iso_week, _weekday = at.astimezone(UTC).isocalendar()
        schedule_key = f"{iso_year}-W{iso_week:02d}"
        async with self._factory() as session:
            tenant_ids = list(
                (
                    await session.execute(
                        select(TagDeployment.tenant_id)
                        .where(TagDeployment.status == "production")
                        .distinct()
                        .order_by(TagDeployment.tenant_id)
                    )
                )
                .scalars()
                .all()
            )
        created: list[TagOptimizationRun] = []
        for raw_tenant_id in tenant_ids:
            tenant_id = str(raw_tenant_id)
            async with self._factory() as session:
                previous = list(
                    (
                        await session.execute(
                            select(TagOptimizationRun)
                            .where(
                                TagOptimizationRun.tenant_id == tenant_id,
                                TagOptimizationRun.trigger.in_(["scheduled", "feedback_threshold"]),
                            )
                            .order_by(
                                TagOptimizationRun.created_at.desc(),
                                TagOptimizationRun.id.desc(),
                            )
                            .limit(60)
                        )
                    )
                    .scalars()
                    .all()
                )
                already_checked = any(
                    isinstance(item.cohort, dict)
                    and item.cohort.get("schedule_key") == schedule_key
                    for item in previous
                )
                active_run = (
                    await session.execute(
                        select(TagOptimizationRun.id)
                        .where(
                            TagOptimizationRun.tenant_id == tenant_id,
                            TagOptimizationRun.job_id.is_not(None),
                            TagOptimizationRun.status.in_(["queued", "running"]),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
            if already_checked or active_run is not None:
                continue
            try:
                run = await self.create_server_bound_optimization_run(
                    tenant_id=tenant_id,
                    cohort={
                        "source": "weekly_feedback_check",
                        "filters": {},
                        "group_ids": [],
                        "schedule_key": schedule_key,
                    },
                    target_policy={"policy": "balanced"},
                    search_budget={
                        "max_trials": 32,
                        "sealed_holdout_queries": 1,
                    },
                    actor_user_id=actor_user_id,
                    trigger="scheduled",
                )
            except (GovernanceConflictError, GovernanceNotFoundError):
                continue
            created.append(run)
        return created

    async def list_optimization_runs(
        self,
        *,
        tenant_id: str,
        limit: int = 200,
    ) -> list[TagOptimizationRun]:
        async with self._factory() as session:
            return list(
                (
                    await session.execute(
                        select(TagOptimizationRun)
                        .where(TagOptimizationRun.tenant_id == tenant_id)
                        .order_by(
                            TagOptimizationRun.created_at.desc(),
                            TagOptimizationRun.id.desc(),
                        )
                        .limit(_bounded_list_limit(limit))
                    )
                )
                .scalars()
                .all()
            )

    async def compare_optimization_trials(
        self,
        *,
        tenant_id: str,
        optimization_run_id: int,
        left_trial_id: int,
        right_trial_id: int,
    ) -> dict[str, Any]:
        if left_trial_id == right_trial_id:
            raise GovernanceError("candidate comparison requires two different trials")
        async with self._factory() as session:
            run_exists = (
                await session.execute(
                    select(TagOptimizationRun.id).where(
                        TagOptimizationRun.id == optimization_run_id,
                        TagOptimizationRun.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if run_exists is None:
                raise GovernanceNotFoundError("optimization run not found")
            trials = list(
                (
                    await session.execute(
                        select(TagOptimizationTrial).where(
                            TagOptimizationTrial.tenant_id == tenant_id,
                            TagOptimizationTrial.optimization_run_id == optimization_run_id,
                            TagOptimizationTrial.id.in_([left_trial_id, right_trial_id]),
                        )
                    )
                )
                .scalars()
                .all()
            )
            by_id = {int(trial.id): trial for trial in trials}
            if set(by_id) != {left_trial_id, right_trial_id}:
                raise GovernanceNotFoundError(
                    "one or both optimization trials were not found in this run"
                )
            candidate_ids = {
                int(trial.candidate_tagger_version_id)
                for trial in trials
                if trial.candidate_tagger_version_id is not None
            }
            badcase_counts: dict[int, int] = {}
            if candidate_ids:
                badcase_counts = {
                    int(candidate_id): int(count)
                    for candidate_id, count in (
                        await session.execute(
                            select(
                                TagBadcase.fix_candidate_tagger_version_id,
                                func.count(TagBadcase.id),
                            )
                            .where(
                                TagBadcase.tenant_id == tenant_id,
                                TagBadcase.fix_candidate_tagger_version_id.in_(candidate_ids),
                                TagBadcase.status.in_(["open", "candidate_fix", "reopened"]),
                                TagBadcase.dataset_split.not_in(_HIDDEN_DATASET_SPLITS),
                            )
                            .group_by(TagBadcase.fix_candidate_tagger_version_id)
                        )
                    ).all()
                    if candidate_id is not None
                }
            left = by_id[left_trial_id]
            right = by_id[right_trial_id]
            return build_candidate_comparison(
                left_trial_id=left_trial_id,
                right_trial_id=right_trial_id,
                left_spec=left.harness_spec,
                right_spec=right.harness_spec,
                left_metrics=left.metrics,
                right_metrics=right.metrics,
                left_reward=left.reward_vector,
                right_reward=right.reward_vector,
                left_badcase_count=badcase_counts.get(
                    int(left.candidate_tagger_version_id),
                    0,
                )
                if left.candidate_tagger_version_id is not None
                else 0,
                right_badcase_count=badcase_counts.get(
                    int(right.candidate_tagger_version_id),
                    0,
                )
                if right.candidate_tagger_version_id is not None
                else 0,
            )

    async def cancel_optimization_run(
        self,
        *,
        tenant_id: str,
        optimization_run_id: int,
        actor_user_id: int,
    ) -> TagOptimizationRun:
        cancelled_at = _utcnow()
        async with self._factory() as session, session.begin():
            run = (
                await session.execute(
                    select(TagOptimizationRun)
                    .where(
                        TagOptimizationRun.id == optimization_run_id,
                        TagOptimizationRun.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if run is None:
                raise GovernanceNotFoundError("optimization run not found")
            if run.status == "completed":
                raise GovernanceConflictError("completed optimization runs cannot be cancelled")
            if run.status == "cancelled":
                return run
            run.status = "cancelled"
            run.finished_at = cancelled_at
            run.summary = {
                **dict(run.summary),
                "cancelled_by": actor_user_id,
                "cancelled_at": cancelled_at.isoformat(),
            }
            run.next_actions = []

            if run.job_id is not None:
                job = (
                    await session.execute(
                        select(TagExtractionJob)
                        .where(
                            TagExtractionJob.id == run.job_id,
                            TagExtractionJob.tenant_id == tenant_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if job is not None and job.status not in {
                    "completed",
                    "failed",
                    "cancelled",
                }:
                    job.status = "cancelled"
                    job.lease_owner = None
                    job.lease_token = None
                    job.lease_expires_at = None
                    job.next_attempt_at = None
                    job.last_error_code = "OPTIMIZATION_CANCELLED"
                    job.last_error_message = f"optimization run {optimization_run_id} was cancelled"
                    job.revision += 1
                    job.finished_at = cancelled_at

            evaluation_jobs = list(
                (
                    await session.execute(
                        select(TagExtractionJob)
                        .where(
                            TagExtractionJob.tenant_id == tenant_id,
                            TagExtractionJob.job_type == "evaluate",
                            TagExtractionJob.status.in_(["queued", "running", "retry_wait"]),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            for job in evaluation_jobs:
                if job.scope.get("optimization_run_id") != run.id:
                    continue
                job.status = "cancelled"
                job.lease_owner = None
                job.lease_token = None
                job.lease_expires_at = None
                job.next_attempt_at = None
                job.last_error_code = "OPTIMIZATION_CANCELLED"
                job.last_error_message = f"optimization run {optimization_run_id} was cancelled"
                job.revision += 1
                job.finished_at = cancelled_at
                await self._sync_evaluation_job_state(
                    session,
                    job=job,
                    state="cancelled",
                    now=cancelled_at,
                    error_message=job.last_error_message,
                )

            unattached_drafts = list(
                (
                    await session.execute(
                        select(TaggerVersion)
                        .where(
                            TaggerVersion.tenant_id == tenant_id,
                            TaggerVersion.optimization_run_id == run.id,
                            TaggerVersion.status == "draft",
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            for candidate in unattached_drafts:
                candidate.status = "rejected"
                candidate.qualified_at = None

            active_trials = list(
                (
                    await session.execute(
                        select(TagOptimizationTrial).where(
                            TagOptimizationTrial.tenant_id == tenant_id,
                            TagOptimizationTrial.optimization_run_id == run.id,
                            TagOptimizationTrial.status.in_(["pending", "running"]),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for trial in active_trials:
                trial.status = "cancelled"
                trial.summary = {
                    **dict(trial.summary),
                    "cancelled": True,
                }
                trial.next_actions = []
                trial.finished_at = cancelled_at
            await self._audit(
                session,
                tenant_id=tenant_id,
                resource_type="tag_optimization_run",
                resource_id=run.id,
                action="optimization_run_cancelled",
                actor_user_id=actor_user_id,
                payload={"job_id": run.job_id},
            )
            return run

    async def execute_optimization_run(
        self,
        *,
        tenant_id: str,
        optimization_run_id: int,
        actor_user_id: int = 0,
        worker_id: str | None = None,
    ) -> TaggerVersion:
        """Execute a durable, server-bound bounded search exactly once per run."""

        started_at = _utcnow()
        async with self._factory() as session, session.begin():
            run = (
                await session.execute(
                    select(TagOptimizationRun)
                    .where(
                        TagOptimizationRun.id == optimization_run_id,
                        TagOptimizationRun.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if run is None:
                raise GovernanceNotFoundError("optimization run not found")
            if run.status == "cancelled":
                raise GovernanceConflictError("optimization run is cancelled")
            if run.status == "completed" and run.winner_tagger_version_id is not None:
                winner = await session.get(TaggerVersion, run.winner_tagger_version_id)
                if winner is None or winner.tenant_id != tenant_id:
                    raise GovernanceConflictError(
                        "completed optimization run has no tenant-owned winner"
                    )
                return winner
            run.status = "running"
            run.phase = "search"
            run.started_at = run.started_at or started_at
            run.finished_at = None
            run.next_actions = ["evaluate_bounded_trials"]
            baseline_tagger_version_id = int(run.baseline_tagger_version_id)
            gold_set_version_id = int(run.gold_set_version_id)
            trials = list(
                (
                    await session.execute(
                        select(TagOptimizationTrial)
                        .where(
                            TagOptimizationTrial.tenant_id == tenant_id,
                            TagOptimizationTrial.optimization_run_id == run.id,
                        )
                        .order_by(TagOptimizationTrial.ordinal)
                    )
                )
                .scalars()
                .all()
            )
            if not trials:
                raise GovernanceConflictError("optimization run has no bounded trial envelope")
            for trial in trials:
                if trial.status in {"pending", "failed"}:
                    trial.status = "running"
                    trial.started_at = trial.started_at or started_at
                    trial.finished_at = None

        try:
            candidate, metadata = await self.create_optimization_candidate(
                tenant_id=tenant_id,
                gold_set_version_id=gold_set_version_id,
                production_tagger_version_id=baseline_tagger_version_id,
                actor_user_id=actor_user_id,
                optimization_run_id=optimization_run_id,
                worker_id=worker_id,
            )
        except Exception as exc:
            failed_at = _utcnow()
            async with self._factory() as session, session.begin():
                run = (
                    await session.execute(
                        select(TagOptimizationRun)
                        .where(
                            TagOptimizationRun.id == optimization_run_id,
                            TagOptimizationRun.tenant_id == tenant_id,
                            TagOptimizationRun.status != "completed",
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if run is not None and run.status != "cancelled":
                    run.status = "failed"
                    run.finished_at = failed_at
                    run.summary = {
                        **dict(run.summary),
                        "error_code": exc.__class__.__name__,
                        "error_message": str(exc),
                    }
                    run.next_actions = ["retry_persistent_job", "inspect_gold_inputs"]
                    running_trials = list(
                        (
                            await session.execute(
                                select(TagOptimizationTrial).where(
                                    TagOptimizationTrial.tenant_id == tenant_id,
                                    TagOptimizationTrial.optimization_run_id == run.id,
                                    TagOptimizationTrial.status == "running",
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    for trial in running_trials:
                        trial.status = "failed"
                        trial.finished_at = failed_at
                        trial.summary = {
                            "error_code": exc.__class__.__name__,
                            "error_message": str(exc),
                        }
            raise

        completed_at = _utcnow()
        async with self._factory() as session, session.begin():
            run = (
                await session.execute(
                    select(TagOptimizationRun)
                    .where(
                        TagOptimizationRun.id == optimization_run_id,
                        TagOptimizationRun.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if run is None:
                raise GovernanceNotFoundError("optimization run not found")
            if run.status == "cancelled":
                raise GovernanceConflictError("optimization run was cancelled during execution")
            if run.status == "completed" and run.winner_tagger_version_id is not None:
                winner = await session.get(TaggerVersion, run.winner_tagger_version_id)
                if winner is None or winner.tenant_id != tenant_id:
                    raise GovernanceConflictError(
                        "completed optimization run has no tenant-owned winner"
                    )
                return winner

            trials = list(
                (
                    await session.execute(
                        select(TagOptimizationTrial)
                        .where(
                            TagOptimizationTrial.tenant_id == tenant_id,
                            TagOptimizationTrial.optimization_run_id == run.id,
                        )
                        .order_by(TagOptimizationTrial.ordinal)
                    )
                )
                .scalars()
                .all()
            )
            bounded = metadata.get("bounded_search")
            bounded_payload = bounded if isinstance(bounded, Mapping) else {}
            raw_trial_payloads = bounded_payload.get("trials")
            trial_payloads = (
                list(raw_trial_payloads) if isinstance(raw_trial_payloads, list) else []
            )
            raw_winner = bounded_payload.get("winner")
            winner_payload = raw_winner if isinstance(raw_winner, Mapping) else {}
            winner_index = winner_payload.get("index")
            if not isinstance(winner_index, int):
                candidate_checksum = canonical_checksum(candidate.harness_spec or {})
                winner_index = next(
                    (
                        trial.ordinal - 1
                        for trial in trials
                        if canonical_checksum(trial.harness_spec) == candidate_checksum
                    ),
                    0,
                )
            if not 0 <= winner_index < len(trials):
                raise GovernanceConflictError(
                    "bounded search winner is outside the persisted trial envelope"
                )
            for trial in trials:
                index = trial.ordinal - 1
                payload = (
                    trial_payloads[index]
                    if index < len(trial_payloads) and isinstance(trial_payloads[index], Mapping)
                    else {}
                )
                reward = payload.get("reward")
                metrics = payload.get("metrics")
                trial.status = "completed" if payload or index == winner_index else "pruned"
                trial.phase = "validation"
                trial.reward_vector = dict(reward) if isinstance(reward, Mapping) else {}
                trial.metrics = dict(metrics) if isinstance(metrics, Mapping) else {}
                trial.gate_results = {"feasible": bool(trial.reward_vector.get("feasible", False))}
                trial.summary = {
                    "mutation": trial.mutation.get("description"),
                    "selected": index == winner_index,
                }
                trial.next_actions = ["create_candidate_version"] if index == winner_index else []
                trial.artifacts = (
                    [f"tagger_version:{candidate.id}"] if index == winner_index else []
                )
                trial.candidate_tagger_version_id = candidate.id if index == winner_index else None
                trial.finished_at = completed_at

            baseline_trial = trials[0]
            selected_trial = trials[winner_index]
            if canonical_checksum(selected_trial.harness_spec) != canonical_checksum(
                candidate.harness_spec or {}
            ):
                raise GovernanceConflictError(
                    "candidate Harness does not match the selected persisted trial"
                )
            candidate_comparison = (
                {
                    "dimensions": [
                        {
                            "dimension": dimension,
                            "before": deepcopy(baseline_trial.harness_spec.get(dimension, {})),
                            "after": deepcopy(selected_trial.harness_spec.get(dimension, {})),
                        }
                        for dimension in _HARNESS_DIMENSIONS
                    ],
                    "metric_deltas": {},
                    "reward_deltas": {},
                    "improved_badcase_count": 0,
                    "regressed_badcase_count": 0,
                    "recommendation": {
                        "trial_id": int(selected_trial.id),
                        "basis": "baseline_retained_by_lexicographic_reward",
                    },
                    "status": "warning",
                    "summary": "bounded search retained the baseline Harness",
                    "next_actions": ["inspect_prompt_and_rule_candidate"],
                    "artifacts": [
                        f"tag_optimization_trial:{selected_trial.id}",
                    ],
                }
                if selected_trial.id == baseline_trial.id
                else build_candidate_comparison(
                    left_trial_id=int(baseline_trial.id),
                    right_trial_id=int(selected_trial.id),
                    left_spec=baseline_trial.harness_spec,
                    right_spec=selected_trial.harness_spec,
                    left_metrics=baseline_trial.metrics,
                    right_metrics=selected_trial.metrics,
                    left_reward=baseline_trial.reward_vector,
                    right_reward=selected_trial.reward_vector,
                )
            )
            run.candidate_tagger_version_id = candidate.id
            # A bounded-search winner is only a candidate.  The sealed holdout
            # evaluator is the sole authority allowed to set the final winner.
            run.winner_tagger_version_id = None
            run.status = "running"
            run.phase = "validation"
            run.summary = {
                **dict(run.summary),
                **deepcopy(metadata),
                "candidate_tagger_version_id": candidate.id,
                "candidate_comparison": candidate_comparison,
                "worker_completed": True,
                "search_completed": True,
                "worker_id": worker_id,
            }
            run.next_actions = ["enqueue_sealed_holdout_evaluation"]
            run.artifacts = [
                f"tagger_version:{candidate.id}",
                f"gold_set_version:{run.gold_set_version_id}",
            ]
            run.finished_at = None
            await self._audit(
                session,
                tenant_id=tenant_id,
                resource_type="tag_optimization_run",
                resource_id=run.id,
                action="optimization_search_completed",
                actor_user_id=actor_user_id,
                payload={
                    "candidate_tagger_version_id": candidate.id,
                    "winner_trial_ordinal": winner_index + 1,
                    "job_id": run.job_id,
                },
            )

        try:
            # Local import avoids a module cycle: the evaluator consumes the
            # sealed-holdout policy helpers defined in this module.
            from audio_graphy.services.tag_evaluator import TagEvaluationService

            evaluation_run, evaluation_job = await TagEvaluationService(self._factory).enqueue(
                tenant_id=tenant_id,
                tagger_version_id=int(candidate.id),
                gold_set_version_id=gold_set_version_id,
                baseline_tagger_version_id=baseline_tagger_version_id,
                idempotency_key=(f"optimization-run:{optimization_run_id}:sealed-holdout"),
                actor_user_id=actor_user_id,
                evaluation_lane="holdout",
                release_service=True,
                trusted_optimization_binding=True,
            )
        except Exception as exc:
            failed_at = _utcnow()
            async with self._factory() as session, session.begin():
                failed_run = (
                    await session.execute(
                        select(TagOptimizationRun)
                        .where(
                            TagOptimizationRun.id == optimization_run_id,
                            TagOptimizationRun.tenant_id == tenant_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if failed_run is not None and failed_run.status not in {"cancelled", "completed"}:
                    failed_run.status = "failed"
                    failed_run.phase = "validation"
                    failed_run.finished_at = failed_at
                    failed_run.summary = {
                        **dict(failed_run.summary),
                        "evaluation_enqueue_error_code": exc.__class__.__name__,
                        "evaluation_enqueue_error_message": str(exc),
                    }
                    failed_run.next_actions = [
                        "retry_sealed_holdout_enqueue",
                        "inspect_holdout_contract",
                    ]
            raise

        async with self._factory() as session, session.begin():
            run = (
                await session.execute(
                    select(TagOptimizationRun)
                    .where(
                        TagOptimizationRun.id == optimization_run_id,
                        TagOptimizationRun.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if run is None:
                raise GovernanceNotFoundError("optimization run not found")
            if run.status == "cancelled":
                raise GovernanceConflictError(
                    "optimization run was cancelled before holdout evaluation"
                )
            run.status = "running"
            run.phase = "holdout"
            run.finished_at = None
            run.summary = {
                **dict(run.summary),
                "evaluation_run_id": int(evaluation_run.id),
                "evaluation_job_id": int(evaluation_job.id),
                "sealed_holdout_queries_used": 1,
            }
            run.next_actions = ["await_sealed_holdout_evaluation"]
            evaluation_artifact = f"tag_evaluation_run:{evaluation_run.id}"
            job_artifact = f"tag_job:{evaluation_job.id}"
            run.artifacts = [
                *[
                    item
                    for item in run.artifacts
                    if item != evaluation_artifact and item != job_artifact
                ],
                evaluation_artifact,
                job_artifact,
            ]
            await self._audit(
                session,
                tenant_id=tenant_id,
                resource_type="tag_optimization_run",
                resource_id=run.id,
                action="sealed_holdout_evaluation_queued",
                actor_user_id=actor_user_id,
                payload={
                    "candidate_tagger_version_id": candidate.id,
                    "evaluation_run_id": evaluation_run.id,
                    "evaluation_job_id": evaluation_job.id,
                },
            )
        return candidate

    async def get_optimization_run(
        self,
        *,
        tenant_id: str,
        optimization_run_id: int,
    ) -> tuple[TagOptimizationRun, list[TagOptimizationTrial]]:
        async with self._factory() as session:
            run = (
                await session.execute(
                    select(TagOptimizationRun).where(
                        TagOptimizationRun.id == optimization_run_id,
                        TagOptimizationRun.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if run is None:
                raise GovernanceNotFoundError("optimization run not found")
            trials = list(
                (
                    await session.execute(
                        select(TagOptimizationTrial)
                        .where(
                            TagOptimizationTrial.tenant_id == tenant_id,
                            TagOptimizationTrial.optimization_run_id == run.id,
                        )
                        .order_by(TagOptimizationTrial.ordinal)
                    )
                )
                .scalars()
                .all()
            )
            return run, trials

    async def get_harness_execution(
        self,
        *,
        tenant_id: str,
        harness_execution_id: int,
    ) -> tuple[TagHarnessExecution, list[TagHarnessStageTrace]]:
        async with self._factory() as session:
            execution = (
                await session.execute(
                    select(TagHarnessExecution).where(
                        TagHarnessExecution.id == harness_execution_id,
                        TagHarnessExecution.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if execution is None:
                raise GovernanceNotFoundError("Harness execution not found")
            traces = list(
                (
                    await session.execute(
                        select(TagHarnessStageTrace)
                        .where(
                            TagHarnessStageTrace.tenant_id == tenant_id,
                            TagHarnessStageTrace.harness_execution_id == execution.id,
                        )
                        .order_by(TagHarnessStageTrace.sequence_no)
                    )
                )
                .scalars()
                .all()
            )
            return execution, traces

    async def list_badcases(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        failure_stage: str | None = None,
        tag_key: str | None = None,
        limit: int = 200,
    ) -> list[TagBadcase]:
        predicates = [
            TagBadcase.tenant_id == tenant_id,
            TagBadcase.dataset_split.not_in(_HIDDEN_DATASET_SPLITS),
        ]
        if status:
            predicates.append(TagBadcase.status == status)
        if failure_stage:
            predicates.append(TagBadcase.failure_stage == failure_stage)
        if tag_key:
            predicates.append(TagBadcase.tag_key == tag_key)
        async with self._factory() as session:
            return list(
                (
                    await session.execute(
                        select(TagBadcase)
                        .where(*predicates)
                        .order_by(
                            TagBadcase.occurrence_count.desc(),
                            TagBadcase.last_seen_at.desc(),
                            TagBadcase.id.desc(),
                        )
                        .limit(_bounded_list_limit(limit))
                    )
                )
                .scalars()
                .all()
            )

    async def list_experience_cases(
        self,
        *,
        tenant_id: str,
        limit: int = 200,
    ) -> list[TagExperienceCase]:
        """Retrieve only experiences that are eligible outside the sealed lane."""

        async with self._factory() as session:
            return list(
                (
                    await session.execute(
                        select(TagExperienceCase)
                        .where(
                            TagExperienceCase.tenant_id == tenant_id,
                            TagExperienceCase.eligible.is_(True),
                            TagExperienceCase.dataset_split.not_in(_HIDDEN_DATASET_SPLITS),
                        )
                        .order_by(
                            TagExperienceCase.materialized_at.desc(),
                            TagExperienceCase.id.desc(),
                        )
                        .limit(_bounded_list_limit(limit))
                    )
                )
                .scalars()
                .all()
            )

    async def get_evolution_overview(self, *, tenant_id: str) -> dict[str, Any]:
        """Return server-derived evolution readiness; callers never supply raw IDs."""

        async with self._factory() as session:
            production_row = (
                await session.execute(
                    select(TagDeployment, TaggerVersion)
                    .join(TaggerVersion, TaggerVersion.id == TagDeployment.tagger_version_id)
                    .where(
                        TagDeployment.tenant_id == tenant_id,
                        TagDeployment.status == "production",
                        TaggerVersion.tenant_id == tenant_id,
                    )
                    .order_by(
                        TagDeployment.approved_at.desc(),
                        TagDeployment.id.desc(),
                    )
                    .limit(1)
                )
            ).one_or_none()
            active_deployment = (
                await session.execute(
                    select(TagDeployment)
                    .where(
                        TagDeployment.tenant_id == tenant_id,
                        TagDeployment.status.in_(
                            [
                                "shadow",
                                "canary_5",
                                "canary_25",
                                "awaiting_admin",
                                "production",
                            ]
                        ),
                    )
                    .order_by(
                        TagDeployment.created_at.desc(),
                        TagDeployment.id.desc(),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            gold_rows = (
                await session.execute(
                    select(TagGoldSetVersion, TagGoldSet)
                    .join(TagGoldSet, TagGoldSet.id == TagGoldSetVersion.gold_set_id)
                    .where(
                        TagGoldSetVersion.tenant_id == tenant_id,
                        TagGoldSetVersion.status == "frozen",
                        TagGoldSet.tenant_id == tenant_id,
                    )
                    .order_by(
                        TagGoldSetVersion.frozen_at.desc(),
                        TagGoldSetVersion.id.desc(),
                    )
                )
            ).all()
            recommended_gold: tuple[TagGoldSetVersion, TagGoldSet] | None = next(
                (
                    (version, gold_set)
                    for version, gold_set in gold_rows
                    if _gold_manifest_is_complete(version.completeness_manifest)
                ),
                None,
            )
            production_harness: TaggerVersion | None = (
                production_row[1] if production_row is not None else None
            )
            production_schema_definitions: Sequence[Mapping[str, Any]] | None = None
            latest_evaluation = None
            if production_harness is not None:
                production_schema = (
                    await session.execute(
                        select(TagSchemaVersion).where(
                            TagSchemaVersion.id == production_harness.schema_version_id,
                            TagSchemaVersion.tenant_id == tenant_id,
                            TagSchemaVersion.status.in_(["published", "deprecated"]),
                        )
                    )
                ).scalar_one_or_none()
                production_schema_definitions = (
                    tuple(
                        item for item in production_schema.definitions if isinstance(item, Mapping)
                    )
                    if production_schema is not None
                    else ()
                )
                latest_evaluation = (
                    await session.execute(
                        select(TagEvaluationRun)
                        .where(
                            TagEvaluationRun.tenant_id == tenant_id,
                            TagEvaluationRun.tagger_version_id == production_harness.id,
                            TagEvaluationRun.status == "completed",
                        )
                        .order_by(
                            TagEvaluationRun.finished_at.desc(),
                            TagEvaluationRun.id.desc(),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()

            visible_t3_lane = (
                select(TagFeedbackLaneAssignment.id)
                .where(
                    TagFeedbackLaneAssignment.tenant_id == tenant_id,
                    TagFeedbackLaneAssignment.feedback_event_id == TagFeedbackEvent.id,
                    TagFeedbackLaneAssignment.split.in_(_LEARNING_DATASET_SPLITS),
                )
                .exists()
            )
            eligible_predicates = [
                TagFeedbackEvent.tenant_id == tenant_id,
                or_(
                    and_(
                        TagFeedbackEvent.truth_tier != "t3",
                        TagFeedbackEvent.training_eligible.is_(True),
                    ),
                    and_(
                        TagFeedbackEvent.truth_tier == "t3",
                        visible_t3_lane,
                    ),
                ),
                TagFeedbackEvent.truth_tier.in_(["t2", "t3"]),
                TagFeedbackEvent.truth_state.in_(["present", "absent"]),
                or_(
                    TagFeedbackEvent.error_stage.is_(None),
                    TagFeedbackEvent.error_stage.not_in(_UPSTREAM_FAILURE_STAGES),
                ),
            ]
            eligible_count = int(
                (
                    await session.execute(
                        select(func.count(TagFeedbackEvent.id)).where(*eligible_predicates)
                    )
                ).scalar_one()
            )
            feedback_coverage = await self._optimization_feedback_coverage(
                session,
                tenant_id=tenant_id,
                cohort={"source": "evolution_overview"},
                schema_definitions=production_schema_definitions,
            )
            new_since_last_run = feedback_coverage.total
            representative_audit_count = int(
                (
                    await session.execute(
                        select(func.count(TagFeedbackEvent.id)).where(
                            TagFeedbackEvent.tenant_id == tenant_id,
                            TagFeedbackEvent.selection_policy.in_(
                                [
                                    "representative_random",
                                    "representative_audit",
                                    "random_audit",
                                ]
                            ),
                        )
                    )
                ).scalar_one()
            )
            adjudicated_count = int(
                (
                    await session.execute(
                        select(func.count(TagFeedbackEvent.id)).where(
                            TagFeedbackEvent.tenant_id == tenant_id,
                            TagFeedbackEvent.truth_tier == "t3",
                        )
                    )
                ).scalar_one()
            )

            latest_observation = None
            release: dict[str, Any] | None = None
            if active_deployment is not None:
                latest_observation = (
                    await session.execute(
                        select(TagDeploymentObservation)
                        .where(
                            TagDeploymentObservation.tenant_id == tenant_id,
                            TagDeploymentObservation.deployment_id == active_deployment.id,
                        )
                        .order_by(
                            TagDeploymentObservation.window_end.desc(),
                            TagDeploymentObservation.id.desc(),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                aggregate = (
                    await session.execute(
                        select(
                            func.coalesce(func.sum(TagDeploymentObservation.served_count), 0),
                            func.coalesce(func.sum(TagDeploymentObservation.paired_count), 0),
                            func.coalesce(func.sum(TagDeploymentObservation.audited_count), 0),
                            func.coalesce(func.sum(TagDeploymentObservation.adjudicated_count), 0),
                        ).where(
                            TagDeploymentObservation.tenant_id == tenant_id,
                            TagDeploymentObservation.deployment_id == active_deployment.id,
                            TagDeploymentObservation.stage == active_deployment.status,
                            TagDeploymentObservation.source == "monitor",
                            TagDeploymentObservation.is_trusted.is_(True),
                        )
                    )
                ).one()
                waiting_reasons: list[str] = []
                readiness: PromotionReadiness | None = None
                if active_deployment.status in _PROMOTION_REQUIREMENTS:
                    readiness = await self._trusted_promotion_readiness(
                        session,
                        deployment=active_deployment,
                        at=_utcnow(),
                    )
                    waiting_reasons = list(readiness.unmet)
                if active_deployment.promotion_paused and active_deployment.pause_reason:
                    waiting_reasons.append(str(active_deployment.pause_reason))
                release = {
                    "stage": active_deployment.status,
                    "served_count": (
                        int(readiness.observed["served_count"])
                        if readiness is not None
                        else int(aggregate[0])
                    ),
                    "paired_count": (
                        int(readiness.observed["paired_count"])
                        if readiness is not None
                        else int(aggregate[1])
                    ),
                    "audited_count": (
                        int(readiness.observed["audited_count"])
                        if readiness is not None
                        else int(aggregate[2])
                    ),
                    "adjudicated_count": int(aggregate[3]),
                    "counts_by_subject_type": (
                        {
                            subject_type: {
                                metric: int(
                                    readiness.observed.get(
                                        f"{metric}_count:{subject_type}",
                                        0,
                                    )
                                )
                                for metric in ("served", "paired", "audited")
                            }
                            for subject_type in ("dialogue_unit", "reception")
                            if any(
                                f"{metric}_count:{subject_type}" in readiness.observed
                                for metric in ("served", "paired", "audited")
                            )
                        }
                        if readiness is not None
                        else {}
                    ),
                    "waiting_reasons": waiting_reasons,
                    "promotion_paused": bool(active_deployment.promotion_paused),
                }

            observation_metrics = (
                latest_observation.metrics if latest_observation is not None else {}
            )
            input_psi = observation_metrics.get("input_psi")
            output_jsd = observation_metrics.get("output_jsd")
            drift_status = (
                "paused"
                if active_deployment is not None and active_deployment.promotion_paused
                else (
                    "watch"
                    if (
                        (input_psi is not None and float(input_psi) > 0.2)
                        or (output_jsd is not None and float(output_jsd) > 0.1)
                    )
                    else "stable"
                )
            )
            evaluation_metrics = latest_evaluation.metrics if latest_evaluation is not None else {}
            feedback_blockers = list(feedback_coverage.blockers)
            if recommended_gold is None:
                feedback_blockers.append("complete_gold_set_missing")
            next_actions: list[str] = []
            if recommended_gold is None:
                next_actions.append("create_complete_gold_set")
            if not feedback_coverage.passed:
                next_actions.append("collect_t2_t3_feedback")
            if not feedback_blockers:
                next_actions.append("start_optimization")
            return {
                "production_harness": (
                    {
                        "id": production_harness.id,
                        "version": production_harness.version,
                        "status": production_harness.status,
                        "updated_at": production_harness.updated_at,
                    }
                    if production_harness is not None
                    else None
                ),
                "recommended_gold_set_version_id": (
                    recommended_gold[0].id if recommended_gold is not None else None
                ),
                "recommended_gold_set_label": (
                    f"{recommended_gold[1].name} · {recommended_gold[0].version}"
                    if recommended_gold is not None
                    else None
                ),
                "quality": {
                    "unbiased_macro_f1": evaluation_metrics.get("macro_f1"),
                    "critical_recall_lcb": evaluation_metrics.get("critical_recall_lcb"),
                    "evidence_iou": evaluation_metrics.get("evidence_iou"),
                    "worst_slice_f1": evaluation_metrics.get("worst_slice_f1"),
                    "delta_vs_baseline": (
                        evaluation_metrics.get("paired_accuracy", {}).get("delta")
                        if isinstance(evaluation_metrics.get("paired_accuracy"), dict)
                        else None
                    ),
                },
                "feedback": {
                    "eligible_count": eligible_count,
                    "new_since_last_run": new_since_last_run,
                    "new_by_tag": feedback_coverage.by_tag,
                    "representative_audit_count": representative_audit_count,
                    "adjudicated_count": adjudicated_count,
                    "coverage_rate": (
                        recommended_gold[0].completeness_manifest.get("coverage_rate")
                        if recommended_gold is not None
                        and isinstance(recommended_gold[0].completeness_manifest, dict)
                        else None
                    ),
                    "next_run_eligible": not feedback_blockers,
                    "blockers": feedback_blockers,
                },
                "drift": {
                    "status": drift_status,
                    "input_psi": input_psi,
                    "output_jsd": output_jsd,
                    "affected_slices": observation_metrics.get("drift_affected_slices", []),
                },
                "release": release,
                "next_actions": next_actions,
            }

    async def list_evaluations(
        self,
        *,
        tenant_id: str,
        limit: int = 200,
    ) -> list[tuple[TagEvaluationRun, list[TagGateResult]]]:
        async with self._factory() as session:
            runs = list(
                (
                    await session.execute(
                        select(TagEvaluationRun)
                        .where(TagEvaluationRun.tenant_id == tenant_id)
                        .order_by(
                            TagEvaluationRun.created_at.desc(),
                            TagEvaluationRun.id.desc(),
                        )
                        .limit(_bounded_list_limit(limit))
                    )
                )
                .scalars()
                .all()
            )
            if not runs:
                return []
            gate_rows = list(
                (
                    await session.execute(
                        select(TagGateResult)
                        .where(
                            TagGateResult.tenant_id == tenant_id,
                            TagGateResult.evaluation_run_id.in_([run.id for run in runs]),
                        )
                        .order_by(
                            TagGateResult.evaluation_run_id,
                            TagGateResult.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            gates_by_run: dict[int, list[TagGateResult]] = {run.id: [] for run in runs}
            for gate in gate_rows:
                gates_by_run[gate.evaluation_run_id].append(gate)
            return [(run, gates_by_run[run.id]) for run in runs]

    async def create_deployment(
        self,
        *,
        tenant_id: str,
        tagger_version_id: int,
        evaluation_run_id: int,
        baseline_tagger_version_id: int,
        actor_user_id: int,
        override_reason: str | None = None,
    ) -> TagDeployment:
        async with self._factory() as session, session.begin():
            evaluation = (
                await session.execute(
                    select(TagEvaluationRun).where(
                        TagEvaluationRun.id == evaluation_run_id,
                        TagEvaluationRun.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if evaluation is None:
                raise GovernanceNotFoundError("tag evaluation not found")
            if evaluation.tagger_version_id != tagger_version_id:
                raise GovernanceConflictError("evaluation and deployment tagger differ")
            if evaluation.baseline_tagger_version_id != baseline_tagger_version_id:
                raise GovernanceConflictError(
                    "deployment baseline differs from the evaluated baseline"
                )
            if evaluation.status != "completed":
                raise GovernanceConflictError("evaluation is not complete")
            if not (
                isinstance(evaluation.metrics, dict)
                and evaluation.metrics.get("evaluation_lane") == "holdout"
                and evaluation.metrics.get("sealed_release") is True
            ):
                raise GovernanceConflictError(
                    "deployment requires a release-service sealed holdout evaluation"
                )
            tagger = (
                await session.execute(
                    select(TaggerVersion)
                    .where(
                        TaggerVersion.id == tagger_version_id,
                        TaggerVersion.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if tagger is None:
                raise GovernanceNotFoundError("tagger version not found")
            if not evaluation.passed:
                raise GovernanceConflictError(
                    "failed evaluation cannot be deployed; hard gates are not overridable"
                )
            elif tagger.status != "qualified":
                raise GovernanceConflictError("only a qualified tagger can be deployed")
            if baseline_tagger_version_id == tagger_version_id:
                raise GovernanceConflictError("baseline and candidate taggers must differ")
            baseline = (
                await session.execute(
                    select(TaggerVersion).where(
                        TaggerVersion.id == baseline_tagger_version_id,
                        TaggerVersion.tenant_id == tenant_id,
                        TaggerVersion.status == "qualified",
                    )
                )
            ).scalar_one_or_none()
            if baseline is None:
                raise GovernanceConflictError(
                    "baseline tagger must be qualified in the current tenant"
                )
            if baseline.schema_version_id != tagger.schema_version_id:
                raise GovernanceConflictError("baseline and candidate schemas differ")
            await session.execute(
                select(TagSchemaVersion.id)
                .where(
                    TagSchemaVersion.id == tagger.schema_version_id,
                    TagSchemaVersion.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            reused_evaluation = (
                await session.execute(
                    select(TagDeployment.id)
                    .where(
                        TagDeployment.tenant_id == tenant_id,
                        TagDeployment.evaluation_run_id == evaluation_run_id,
                    )
                    .with_for_update()
                    .limit(1)
                )
            ).scalar_one_or_none()
            if reused_evaluation is not None:
                raise GovernanceConflictError(
                    "a sealed holdout evaluation can start only one deployment"
                )
            current_production = (
                await session.execute(
                    select(TagDeployment, TaggerVersion)
                    .join(
                        TaggerVersion,
                        TaggerVersion.id == TagDeployment.tagger_version_id,
                    )
                    .where(
                        TagDeployment.tenant_id == tenant_id,
                        TagDeployment.status == "production",
                        TaggerVersion.tenant_id == tenant_id,
                        TaggerVersion.schema_version_id == tagger.schema_version_id,
                    )
                    .with_for_update()
                )
            ).all()
            if len(current_production) != 1:
                raise GovernanceConflictError(
                    "deployment requires exactly one current production baseline"
                )
            production_deployment, production_tagger = current_production[0]
            if (
                production_tagger.id != baseline_tagger_version_id
                or production_deployment.tagger_version_id != baseline_tagger_version_id
            ):
                raise GovernanceConflictError(
                    "evaluated baseline is stale; rerun against current production"
                )
            active_release = (
                await session.execute(
                    select(TagDeployment.id)
                    .join(
                        TaggerVersion,
                        TaggerVersion.id == TagDeployment.tagger_version_id,
                    )
                    .where(
                        TagDeployment.tenant_id == tenant_id,
                        TagDeployment.status.in_(
                            [
                                "shadow",
                                "canary_5",
                                "canary_25",
                                "awaiting_admin",
                            ]
                        ),
                        TaggerVersion.tenant_id == tenant_id,
                        TaggerVersion.schema_version_id == tagger.schema_version_id,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if active_release is not None:
                raise GovernanceConflictError(
                    "another candidate deployment is active for this schema"
                )
            deployment = TagDeployment(
                tenant_id=tenant_id,
                tagger_version_id=tagger_version_id,
                evaluation_run_id=evaluation_run_id,
                baseline_tagger_version_id=baseline_tagger_version_id,
                status="shadow",
                traffic_percent=0,
                revision=1,
                promotion_paused=False,
                created_by=actor_user_id,
            )
            session.add(deployment)
            await session.flush()
            await self._audit(
                session,
                tenant_id=tenant_id,
                resource_type="tag_deployment",
                resource_id=deployment.id,
                action="shadow_started",
                actor_user_id=actor_user_id,
                payload={
                    "minimum_support_override": False,
                    "override_reason": None,
                },
            )
            return deployment

    async def list_deployments(
        self,
        *,
        tenant_id: str,
        limit: int = 200,
    ) -> list[TagDeployment]:
        async with self._factory() as session:
            return list(
                (
                    await session.execute(
                        select(TagDeployment)
                        .where(TagDeployment.tenant_id == tenant_id)
                        .order_by(
                            TagDeployment.created_at.desc(),
                            TagDeployment.id.desc(),
                        )
                        .limit(_bounded_list_limit(limit))
                    )
                )
                .scalars()
                .all()
            )

    async def list_deployment_observations(
        self,
        *,
        tenant_id: str,
        deployment_id: int,
        limit: int,
    ) -> list[TagDeploymentObservation]:
        """Return one tenant's release-health timeline for a known deployment."""

        async with self._factory() as session:
            deployment_exists = (
                await session.execute(
                    select(TagDeployment.id).where(
                        TagDeployment.id == deployment_id,
                        TagDeployment.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if deployment_exists is None:
                raise GovernanceNotFoundError("tag deployment not found")
            return list(
                (
                    await session.execute(
                        select(TagDeploymentObservation)
                        .where(
                            TagDeploymentObservation.tenant_id == tenant_id,
                            TagDeploymentObservation.deployment_id == deployment_id,
                        )
                        .order_by(
                            TagDeploymentObservation.window_end.desc(),
                            TagDeploymentObservation.id.desc(),
                        )
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )

    @staticmethod
    async def _deployment_baseline_freshness(
        session: AsyncSession,
        *,
        deployment: TagDeployment,
    ) -> tuple[bool, int | None, str | None]:
        """Validate that a release is still evaluated against the live baseline.

        The caller must hold the candidate schema-version row lock.  This gate is
        shared by automatic promotion and final approval so neither path can
        advance a candidate after its evaluated production baseline changed.
        """

        candidate = (
            await session.execute(
                select(TaggerVersion).where(
                    TaggerVersion.tenant_id == deployment.tenant_id,
                    TaggerVersion.id == deployment.tagger_version_id,
                )
            )
        ).scalar_one_or_none()
        baseline = (
            await session.execute(
                select(TaggerVersion).where(
                    TaggerVersion.tenant_id == deployment.tenant_id,
                    TaggerVersion.id == deployment.baseline_tagger_version_id,
                )
            )
        ).scalar_one_or_none()
        evaluation = (
            await session.execute(
                select(TagEvaluationRun).where(
                    TagEvaluationRun.tenant_id == deployment.tenant_id,
                    TagEvaluationRun.id == deployment.evaluation_run_id,
                )
            )
        ).scalar_one_or_none()
        if candidate is None or baseline is None or evaluation is None:
            return False, None, "baseline_lineage_missing"
        if candidate.status != "qualified" or baseline.status != "qualified":
            return False, None, "candidate_or_baseline_not_qualified"
        if candidate.schema_version_id != baseline.schema_version_id:
            return False, None, "baseline_schema_changed"
        if (
            evaluation.status != "completed"
            or not evaluation.passed
            or evaluation.tagger_version_id != candidate.id
            or evaluation.baseline_tagger_version_id != baseline.id
            or not isinstance(evaluation.metrics, dict)
            or evaluation.metrics.get("evaluation_lane") != "holdout"
            or evaluation.metrics.get("sealed_release") is not True
        ):
            return False, None, "sealed_evaluation_binding_invalid"
        gold_version = (
            await session.execute(
                select(TagGoldSetVersion).where(
                    TagGoldSetVersion.tenant_id == deployment.tenant_id,
                    TagGoldSetVersion.id == evaluation.gold_set_version_id,
                    TagGoldSetVersion.status == "frozen",
                )
            )
        ).scalar_one_or_none()
        if gold_version is None or evaluation.dataset_snapshot_hash != str(
            gold_version.dataset_snapshot_hash or gold_version.checksum or ""
        ):
            return False, None, "sealed_evaluation_snapshot_changed"
        production_rows = list(
            (
                await session.execute(
                    select(TagDeployment)
                    .join(
                        TaggerVersion,
                        TaggerVersion.id == TagDeployment.tagger_version_id,
                    )
                    .where(
                        TagDeployment.tenant_id == deployment.tenant_id,
                        TagDeployment.status == "production",
                        TagDeployment.id != deployment.id,
                        TaggerVersion.tenant_id == deployment.tenant_id,
                        TaggerVersion.schema_version_id == candidate.schema_version_id,
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        current_tagger_id = (
            int(production_rows[0].tagger_version_id) if len(production_rows) == 1 else None
        )
        if len(production_rows) != 1:
            return False, current_tagger_id, "production_baseline_cardinality_invalid"
        if current_tagger_id != int(deployment.baseline_tagger_version_id or 0):
            return False, current_tagger_id, "evaluated_baseline_is_stale"
        return True, current_tagger_id, None

    async def _trusted_promotion_readiness(
        self,
        session: AsyncSession,
        *,
        deployment: TagDeployment,
        at: datetime,
        stage: str | None = None,
        revision: int | None = None,
    ) -> PromotionReadiness:
        effective_stage = stage or str(deployment.status)
        effective_revision = int(deployment.revision) if revision is None else revision
        observations = list(
            (
                await session.execute(
                    select(TagDeploymentObservation)
                    .where(
                        TagDeploymentObservation.tenant_id == deployment.tenant_id,
                        TagDeploymentObservation.deployment_id == deployment.id,
                        TagDeploymentObservation.stage == effective_stage,
                        TagDeploymentObservation.deployment_revision == effective_revision,
                        TagDeploymentObservation.source == "monitor",
                        TagDeploymentObservation.is_trusted.is_(True),
                    )
                    .order_by(
                        TagDeploymentObservation.window_start,
                        TagDeploymentObservation.window_end,
                        TagDeploymentObservation.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        tagger = (
            await session.execute(
                select(TaggerVersion).where(
                    TaggerVersion.tenant_id == deployment.tenant_id,
                    TaggerVersion.id == deployment.tagger_version_id,
                )
            )
        ).scalar_one()
        schema = (
            await session.execute(
                select(TagSchemaVersion).where(
                    TagSchemaVersion.tenant_id == deployment.tenant_id,
                    TagSchemaVersion.id == tagger.schema_version_id,
                )
            )
        ).scalar_one()
        supported_subject_types = sorted(
            {
                str(subject_type)
                for definition in schema.definitions
                if isinstance(definition, dict)
                for subject_type in (definition.get("subject_types") or ())
                if subject_type in {"dialogue_unit", "reception"}
            }
        )
        counted_by_kind_and_type: dict[str, dict[str, int]] = {
            "served": {},
            "paired": {},
            "audited": {},
        }
        for count_kind, subject_type, count in (
            await session.execute(
                select(
                    TagDeploymentAuditSubject.count_kind,
                    TagDeploymentAuditSubject.subject_type,
                    func.count(TagDeploymentAuditSubject.id),
                )
                .where(
                    TagDeploymentAuditSubject.tenant_id == deployment.tenant_id,
                    TagDeploymentAuditSubject.deployment_id == deployment.id,
                    TagDeploymentAuditSubject.stage == effective_stage,
                    TagDeploymentAuditSubject.deployment_revision == effective_revision,
                )
                .group_by(
                    TagDeploymentAuditSubject.count_kind,
                    TagDeploymentAuditSubject.subject_type,
                )
            )
        ).all():
            counted_by_kind_and_type[str(count_kind)][str(subject_type)] = int(count)

        def domain_floor(count_kind: str) -> int:
            return (
                min(
                    counted_by_kind_and_type[count_kind].get(subject_type, 0)
                    for subject_type in supported_subject_types
                )
                if supported_subject_types
                else 0
            )

        served_count = domain_floor("served")
        paired_count = domain_floor("paired")
        audited_count = domain_floor("audited")
        first_window_start = observations[0].window_start if observations else None
        if effective_stage == "shadow":
            stage_started_at = _aware_utc(deployment.created_at)
        elif first_window_start is not None:
            stage_started_at = _aware_utc(first_window_start)
        else:
            stage_started_at = at
        readiness = evaluate_promotion_readiness(
            stage=effective_stage,
            elapsed=_aware_utc(at) - stage_started_at,
            served_count=int(served_count),
            paired_count=int(paired_count),
            audited_count=int(audited_count),
        )
        required_duration = timedelta(hours=readiness.requirements["duration_hours"])
        observed_until = _aware_utc(at).replace(
            minute=(_aware_utc(at).minute // 5) * 5,
            second=0,
            microsecond=0,
        )
        required_from = observed_until - required_duration
        cursor = required_from
        contiguous = True
        for observation in observations:
            start = _aware_utc(observation.window_start)
            end = _aware_utc(observation.window_end)
            if end <= required_from:
                continue
            if start != cursor or end - start != timedelta(minutes=5):
                contiguous = False
                break
            cursor = end
            if cursor == observed_until:
                break
        fresh = bool(observations) and _aware_utc(observations[-1].window_end) == observed_until
        contiguous = contiguous and cursor == observed_until
        unmet = list(readiness.unmet)
        requirements = dict(readiness.requirements)
        observed = dict(readiness.observed)
        for metric, count_kind in (
            ("served_count", "served"),
            ("paired_count", "paired"),
            ("audited_count", "audited"),
        ):
            for subject_type in supported_subject_types:
                metric_key = f"{metric}:{subject_type}"
                requirements[metric_key] = readiness.requirements[metric]
                observed[metric_key] = counted_by_kind_and_type[count_kind].get(
                    subject_type,
                    0,
                )
                if observed[metric_key] < requirements[metric_key]:
                    unmet.append(metric_key)
        if not fresh:
            unmet.append("monitor_freshness")
        if not contiguous:
            unmet.append("monitor_continuity")
        return PromotionReadiness(
            passed=not unmet,
            stage=readiness.stage,
            elapsed_hours=readiness.elapsed_hours,
            requirements=requirements,
            observed=observed,
            unmet=tuple(dict.fromkeys(unmet)),
        )

    async def transition_deployment(
        self,
        *,
        tenant_id: str,
        deployment_id: int,
        action: str,
        actor_user_id: int,
        expected_revision: int,
        reason: str | None = None,
    ) -> TagDeployment:
        async with self._factory() as session, session.begin():
            deployment_ref = (
                await session.execute(
                    select(TagDeployment.tagger_version_id).where(
                        TagDeployment.id == deployment_id,
                        TagDeployment.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if deployment_ref is None:
                raise GovernanceNotFoundError("tag deployment not found")
            schema_version_id = (
                await session.execute(
                    select(TaggerVersion.schema_version_id).where(
                        TaggerVersion.id == deployment_ref,
                        TaggerVersion.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if schema_version_id is None:
                raise GovernanceNotFoundError("deployment tagger version not found")
            # All rollout state changes for one schema serialize here before
            # taking an individual deployment lock.
            await session.execute(
                select(TagSchemaVersion.id)
                .where(
                    TagSchemaVersion.id == schema_version_id,
                    TagSchemaVersion.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            deployment = (
                await session.execute(
                    select(TagDeployment)
                    .where(
                        TagDeployment.id == deployment_id,
                        TagDeployment.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if deployment is None:
                raise GovernanceNotFoundError("tag deployment not found")
            if deployment.revision != expected_revision:
                raise GovernanceConflictError("deployment revision changed; reload before retrying")
            now = _utcnow()
            if action in {"promote", "approve"} and deployment.promotion_paused:
                raise GovernanceConflictError("deployment promotion is paused by monitoring")
            if action == "promote":
                raise GovernanceConflictError(
                    "shadow and canary promotion is controlled by the trusted monitor"
                )
            elif action == "resume":
                if not deployment.promotion_paused:
                    raise GovernanceConflictError("deployment promotion is not paused")
                if deployment.pause_reason != "distribution drift requires review":
                    raise GovernanceConflictError(
                        "only a completed distribution-drift review can resume promotion"
                    )
                if deployment.status not in {
                    "shadow",
                    "canary_5",
                    "canary_25",
                    "awaiting_admin",
                }:
                    raise GovernanceConflictError("deployment is not in a resumable rollout stage")
                if reason is None or not reason.strip():
                    raise GovernanceError("deployment resume requires an admin justification")
                pause_observation = (
                    await session.execute(
                        select(TagDeploymentObservation)
                        .where(
                            TagDeploymentObservation.tenant_id == tenant_id,
                            TagDeploymentObservation.deployment_id == deployment.id,
                            TagDeploymentObservation.stage == deployment.status,
                            TagDeploymentObservation.source == "monitor",
                            TagDeploymentObservation.is_trusted.is_(True),
                            TagDeploymentObservation.action == "pause",
                        )
                        .order_by(TagDeploymentObservation.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if pause_observation is None:
                    raise GovernanceConflictError(
                        "deployment pause has no trusted drift observation"
                    )
                review_jobs = list(
                    (
                        await session.execute(
                            select(TagExtractionJob)
                            .where(
                                TagExtractionJob.tenant_id == tenant_id,
                                TagExtractionJob.job_type == "review_batch",
                                TagExtractionJob.origin == "monitor",
                                TagExtractionJob.tagger_version_id == deployment.tagger_version_id,
                            )
                            .order_by(TagExtractionJob.id.desc())
                            .limit(500)
                        )
                    )
                    .scalars()
                    .all()
                )
                drift_review_job = next(
                    (
                        job
                        for job in review_jobs
                        if job.scope.get("deployment_id") == deployment.id
                        and job.scope.get("trusted_observation_id") == pause_observation.id
                        and job.scope.get("selection_policy") == "drift_audit"
                    ),
                    None,
                )
                if drift_review_job is None or drift_review_job.status != "completed":
                    raise GovernanceConflictError(
                        "distribution-drift review batch has not been materialized"
                    )
                review_bundle_id = drift_review_job.scope.get("review_bundle_id")
                if not isinstance(review_bundle_id, str) or not review_bundle_id:
                    raise GovernanceConflictError(
                        "distribution-drift review batch has no immutable bundle"
                    )
                drift_tasks = list(
                    (
                        await session.execute(
                            select(TagReviewTask)
                            .where(
                                TagReviewTask.tenant_id == tenant_id,
                                TagReviewTask.review_bundle_id == review_bundle_id,
                                TagReviewTask.selection_policy == "drift_audit",
                            )
                            .order_by(TagReviewTask.id)
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .all()
                )
                if (
                    not drift_tasks
                    or len(drift_tasks) != drift_review_job.total_items
                    or any(task.status != "resolved" for task in drift_tasks)
                ):
                    raise GovernanceConflictError(
                        "distribution-drift review tasks are not complete"
                    )
                drift_task_ids = {task.id for task in drift_tasks}
                definitive_decisions = list(
                    (
                        await session.execute(
                            select(TagReviewDecision)
                            .where(
                                TagReviewDecision.tenant_id == tenant_id,
                                TagReviewDecision.task_id.in_(drift_task_ids),
                                TagReviewDecision.truth_tier.in_(["t2", "t3"]),
                                TagReviewDecision.truth_state.in_(["present", "absent"]),
                            )
                            .order_by(
                                TagReviewDecision.task_id,
                                TagReviewDecision.annotator_round.desc(),
                                TagReviewDecision.id.desc(),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                decision_by_task: dict[int, TagReviewDecision] = {}
                for decision in definitive_decisions:
                    current = decision_by_task.get(decision.task_id)
                    if current is None or (
                        decision.truth_tier == "t3" and current.truth_tier != "t3"
                    ):
                        decision_by_task[decision.task_id] = decision
                if set(decision_by_task) != drift_task_ids:
                    raise GovernanceConflictError(
                        "distribution-drift review requires definitive T2/T3 decisions"
                    )
                disagreement_task_ids: list[int] = []
                for task in drift_tasks:
                    decision = decision_by_task[task.id]
                    if task.proposed_value is None:
                        agrees_with_candidate = decision.truth_state == "absent"
                    else:
                        reviewed_value = (
                            decision.corrected_value
                            if decision.corrected_value is not None
                            else task.proposed_value
                        )
                        agrees_with_candidate = (
                            decision.truth_state == "present"
                            and canonical_checksum(reviewed_value)
                            == canonical_checksum(task.proposed_value)
                        )
                    if not agrees_with_candidate:
                        disagreement_task_ids.append(task.id)
                if disagreement_task_ids:
                    raise GovernanceConflictError(
                        "distribution-drift review found candidate label errors; "
                        "rollback or create a new candidate before resuming"
                    )
                deployment.promotion_paused = False
                deployment.pause_reason = None
            elif action == "approve":
                if deployment.status != "awaiting_admin":
                    raise GovernanceConflictError("deployment is not awaiting admin approval")
                (
                    baseline_fresh,
                    _current_baseline_id,
                    baseline_reason,
                ) = await self._deployment_baseline_freshness(
                    session,
                    deployment=deployment,
                )
                if not baseline_fresh:
                    raise GovernanceConflictError(
                        "deployment baseline is stale; release must be evaluated again "
                        f"({baseline_reason})"
                    )
                candidate = (
                    await session.execute(
                        select(TaggerVersion).where(
                            TaggerVersion.id == deployment.tagger_version_id,
                            TaggerVersion.tenant_id == tenant_id,
                        )
                    )
                ).scalar_one()
                # The schema-version row is the serialization point for production
                # routing. This prevents concurrent approvals from leaving two
                # production deployments for the same tenant/schema.
                await session.execute(
                    select(TagSchemaVersion.id)
                    .where(
                        TagSchemaVersion.id == candidate.schema_version_id,
                        TagSchemaVersion.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
                prior_production = list(
                    (
                        await session.execute(
                            select(TagDeployment)
                            .join(
                                TaggerVersion,
                                TaggerVersion.id == TagDeployment.tagger_version_id,
                            )
                            .where(
                                TagDeployment.tenant_id == tenant_id,
                                TagDeployment.status == "production",
                                TagDeployment.id != deployment.id,
                                TaggerVersion.tenant_id == tenant_id,
                                TaggerVersion.schema_version_id == candidate.schema_version_id,
                            )
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .all()
                )
                for previous in prior_production:
                    previous.status = "retired"
                    previous.traffic_percent = 0
                    previous.revision += 1
                    await self._audit(
                        session,
                        tenant_id=tenant_id,
                        resource_type="tag_deployment",
                        resource_id=previous.id,
                        action="retire_on_replacement",
                        actor_user_id=actor_user_id,
                        payload={"replacement_deployment_id": deployment.id},
                    )
                deployment.status = "production"
                deployment.traffic_percent = 100
                deployment.approved_by = actor_user_id
                deployment.approved_at = now
            elif action == "rollback":
                if deployment.status in {"rolled_back", "retired"}:
                    return deployment
                await self._rollback_visibility(
                    session,
                    deployment=deployment,
                    actor_user_id=actor_user_id,
                    reason=reason or "manual",
                    now=now,
                )
            else:
                raise GovernanceError("unsupported deployment action")
            deployment.revision += 1
            await self._audit(
                session,
                tenant_id=tenant_id,
                resource_type="tag_deployment",
                resource_id=deployment.id,
                action=action,
                actor_user_id=actor_user_id,
                payload={"status": deployment.status, "reason": reason},
            )
            return deployment

    async def _rollback_visibility(
        self,
        session: AsyncSession,
        *,
        deployment: TagDeployment,
        actor_user_id: int,
        reason: str,
        now: datetime,
        restore_tagger_version_id: int | None = None,
        restore_bound_baseline: bool = True,
        reactivate_baseline: bool = True,
        cascade_children: bool = True,
    ) -> None:
        effective_restore_tagger_id = (
            deployment.baseline_tagger_version_id
            if restore_bound_baseline
            else restore_tagger_version_id
        )
        affected = list(
            (
                await session.execute(
                    select(TagAssignmentCurrent, TagAssignmentFact)
                    .join(
                        TagAssignmentFact,
                        TagAssignmentFact.id == TagAssignmentCurrent.fact_id,
                    )
                    .where(
                        TagAssignmentCurrent.tenant_id == deployment.tenant_id,
                        TagAssignmentFact.deployment_id == deployment.id,
                    )
                    .with_for_update()
                )
            ).all()
        )
        missing_dialogue_unit_ids: set[int] = set()
        for current, candidate_fact in affected:
            baseline: TagAssignmentFact | None = None
            if effective_restore_tagger_id is not None:
                baseline = (
                    await session.execute(
                        select(TagAssignmentFact)
                        .where(
                            TagAssignmentFact.tenant_id == deployment.tenant_id,
                            TagAssignmentFact.subject_type == candidate_fact.subject_type,
                            TagAssignmentFact.subject_id == candidate_fact.subject_id,
                            TagAssignmentFact.tag_key == candidate_fact.tag_key,
                            TagAssignmentFact.tagger_version_id == effective_restore_tagger_id,
                            or_(
                                TagAssignmentFact.deployment_id.is_(None),
                                TagAssignmentFact.deployment_id != deployment.id,
                            ),
                        )
                        .order_by(
                            TagAssignmentFact.revision.desc(),
                            TagAssignmentFact.id.desc(),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
            if baseline is None:
                await session.delete(current)
                if candidate_fact.dialogue_unit_id is not None:
                    missing_dialogue_unit_ids.add(candidate_fact.dialogue_unit_id)
                continue
            current.fact_id = baseline.id
            current.revision = baseline.revision
        if missing_dialogue_unit_ids and effective_restore_tagger_id is not None:
            idempotency_key = f"rollback-remediate:{deployment.id}:{deployment.revision}"
            existing = (
                await session.execute(
                    select(TagExtractionJob).where(
                        TagExtractionJob.tenant_id == deployment.tenant_id,
                        TagExtractionJob.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    TagExtractionJob(
                        tenant_id=deployment.tenant_id,
                        job_type="remediate",
                        origin="system",
                        status="queued",
                        scope={
                            "dialogue_unit_ids": sorted(missing_dialogue_unit_ids),
                            "reason": reason,
                        },
                        tagger_version_id=effective_restore_tagger_id,
                        idempotency_key=idempotency_key,
                        total_items=len(missing_dialogue_unit_ids),
                        completed_items=0,
                        failed_items=0,
                        attempt_count=0,
                        max_attempts=3,
                        revision=1,
                        created_by=actor_user_id,
                    )
                )
        candidate_tagger = (
            await session.execute(
                select(TaggerVersion)
                .where(
                    TaggerVersion.id == deployment.tagger_version_id,
                    TaggerVersion.tenant_id == deployment.tenant_id,
                )
                .with_for_update()
            )
        ).scalar_one()
        candidate_tagger.status = "rejected"
        candidate_tagger.qualified_at = None
        baseline_deployment = None
        if reactivate_baseline and effective_restore_tagger_id is not None:
            baseline_deployment = (
                await session.execute(
                    select(TagDeployment)
                    .where(
                        TagDeployment.tenant_id == deployment.tenant_id,
                        TagDeployment.tagger_version_id == effective_restore_tagger_id,
                        TagDeployment.id != deployment.id,
                        TagDeployment.status.in_(["production", "retired"]),
                    )
                    .order_by(
                        TagDeployment.approved_at.desc(),
                        TagDeployment.id.desc(),
                    )
                    .limit(1)
                    .with_for_update()
                )
            ).scalar_one_or_none()
        if (
            reactivate_baseline
            and baseline_deployment is not None
            and baseline_deployment.status != "production"
        ):
            baseline_deployment.status = "production"
            baseline_deployment.traffic_percent = 100
            baseline_deployment.promotion_paused = False
            baseline_deployment.pause_reason = None
            baseline_deployment.revision += 1
            await self._audit(
                session,
                tenant_id=cast(str, deployment.tenant_id),
                resource_type="tag_deployment",
                resource_id=baseline_deployment.id,
                action="reactivate_after_rollback",
                actor_user_id=actor_user_id,
                payload={"rolled_back_deployment_id": deployment.id},
            )
        elif reactivate_baseline and baseline_deployment is None:
            await self._audit(
                session,
                tenant_id=cast(str, deployment.tenant_id),
                resource_type="tag_deployment",
                resource_id=deployment.id,
                action="baseline_route_fallback",
                actor_user_id=actor_user_id,
                payload={
                    "baseline_tagger_version_id": effective_restore_tagger_id,
                    "route_source": "rolled_back_deployment",
                    "reason": (
                        "baseline has no historical deployment; workers route through "
                        "this rollback record until a production route is approved"
                    ),
                },
            )
        deployment.status = "rolled_back"
        deployment.traffic_percent = 0
        deployment.promotion_paused = True
        deployment.pause_reason = reason
        deployment.rolled_back_by = actor_user_id
        deployment.rolled_back_at = now
        deployment.rollback_reason = reason
        if cascade_children:
            child_deployments = list(
                (
                    await session.execute(
                        select(TagDeployment)
                        .where(
                            TagDeployment.tenant_id == deployment.tenant_id,
                            TagDeployment.id != deployment.id,
                            TagDeployment.baseline_tagger_version_id
                            == deployment.tagger_version_id,
                            TagDeployment.status.in_(
                                [
                                    "shadow",
                                    "canary_5",
                                    "canary_25",
                                    "awaiting_admin",
                                    "production",
                                ]
                            ),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            for child in child_deployments:
                await self._rollback_visibility(
                    session,
                    deployment=child,
                    actor_user_id=actor_user_id,
                    reason=f"stale_baseline:{deployment.id}",
                    now=now,
                    restore_tagger_version_id=effective_restore_tagger_id,
                    restore_bound_baseline=False,
                    reactivate_baseline=False,
                    cascade_children=False,
                )
                child.revision += 1
                await self._audit(
                    session,
                    tenant_id=cast(str, deployment.tenant_id),
                    resource_type="tag_deployment",
                    resource_id=child.id,
                    action="rollback_stale_baseline",
                    actor_user_id=actor_user_id,
                    payload={
                        "invalidated_baseline_deployment_id": deployment.id,
                        "restored_tagger_version_id": effective_restore_tagger_id,
                    },
                )

    async def record_deployment_observation(
        self,
        *,
        tenant_id: str,
        deployment_id: int,
        sample_reception_ids: list[int],
        metrics: dict[str, Any],
        breach_codes: list[str],
        window_start: datetime,
        window_end: datetime,
        actor_user_id: int,
        review_fact_ids: list[int] | None = None,
        expected_stage: str | None = None,
        expected_revision: int | None = None,
        source: str = "manual",
        provenance: Mapping[str, Any] | None = None,
        is_trusted: bool = False,
        served_count: int = 0,
        paired_count: int = 0,
        audited_count: int = 0,
        adjudicated_count: int = 0,
        served_subject_keys: Sequence[tuple[str, int]] | None = None,
        paired_subject_keys: Sequence[tuple[str, int]] | None = None,
        audited_subject_keys: Sequence[tuple[str, int]] | None = None,
    ) -> tuple[TagDeploymentObservation, TagDeployment]:
        """Persist one health window and enforce pause/automatic rollback policy."""

        hard_breaches = {
            "schema_inconsistent",
            "evidence_inconsistent",
            "duplicate_current",
            "critical_recall",
            "budget_exhausted",
        }
        if source not in {"monitor", "manual", "imported"}:
            raise GovernanceError("deployment observation source is invalid")
        if is_trusted and source != "monitor":
            raise GovernanceError("only monitor observations can be trusted")
        observation_counts = {
            "served_count": served_count,
            "paired_count": paired_count,
            "audited_count": audited_count,
            "adjudicated_count": adjudicated_count,
        }
        if any(isinstance(value, bool) or int(value) < 0 for value in observation_counts.values()):
            raise GovernanceError("deployment observation counts must be non-negative integers")
        async with self._factory() as session, session.begin():
            deployment_ref = (
                await session.execute(
                    select(TagDeployment.tagger_version_id).where(
                        TagDeployment.id == deployment_id,
                        TagDeployment.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if deployment_ref is None:
                raise GovernanceNotFoundError("tag deployment not found")
            schema_version_id = (
                await session.execute(
                    select(TaggerVersion.schema_version_id).where(
                        TaggerVersion.id == deployment_ref,
                        TaggerVersion.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if schema_version_id is None:
                raise GovernanceNotFoundError("deployment tagger version not found")
            await session.execute(
                select(TagSchemaVersion.id)
                .where(
                    TagSchemaVersion.id == schema_version_id,
                    TagSchemaVersion.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            deployment = (
                await session.execute(
                    select(TagDeployment)
                    .where(
                        TagDeployment.id == deployment_id,
                        TagDeployment.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if deployment is None:
                raise GovernanceNotFoundError("tag deployment not found")
            existing_observation = (
                await session.execute(
                    select(TagDeploymentObservation).where(
                        TagDeploymentObservation.tenant_id == tenant_id,
                        TagDeploymentObservation.deployment_id == deployment_id,
                        TagDeploymentObservation.window_start == window_start,
                        TagDeploymentObservation.window_end == window_end,
                    )
                )
            ).scalar_one_or_none()
            if existing_observation is not None:
                return existing_observation, deployment
            observable_states = {
                "shadow",
                "canary_5",
                "canary_25",
                "awaiting_admin",
                "production",
            }
            if deployment.status not in observable_states:
                raise GovernanceStaleObservationError(
                    "inactive deployment cannot accept a health observation"
                )
            if (expected_stage is not None and str(deployment.status) != expected_stage) or (
                expected_revision is not None and int(deployment.revision) != expected_revision
            ):
                raise GovernanceStaleObservationError(
                    "deployment changed while its health window was collected"
                )
            observed_stage = deployment.status
            observed_revision = int(deployment.revision)
            if any(
                isinstance(reception_id, bool) or reception_id <= 0
                for reception_id in sample_reception_ids
            ):
                raise GovernanceError(
                    "deployment observation reception IDs must be positive integers"
                )
            if review_fact_ids is not None and any(
                isinstance(fact_id, bool) or fact_id <= 0 for fact_id in review_fact_ids
            ):
                raise GovernanceError(
                    "deployment observation review fact IDs must be positive integers"
                )
            reception_ids = set(sample_reception_ids)
            owned_reception_ids = (
                set(
                    (
                        await session.execute(
                            select(Reception.id).where(
                                Reception.tenant_id == tenant_id,
                                Reception.id.in_(reception_ids),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if reception_ids
                else set()
            )
            if owned_reception_ids != reception_ids:
                raise GovernanceError(
                    "deployment observation contains missing or cross-tenant receptions"
                )
            already_counted = (
                set(
                    (
                        await session.execute(
                            select(TagDeploymentObservationSample.reception_id).where(
                                TagDeploymentObservationSample.tenant_id == tenant_id,
                                TagDeploymentObservationSample.deployment_id == deployment.id,
                                TagDeploymentObservationSample.stage == observed_stage,
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
            new_reception_ids = sorted(reception_ids - already_counted)
            persisted_metrics = dict(metrics)
            persisted_metrics["window_reception_count"] = float(len(reception_ids))
            trusted_control = source == "monitor" and is_trusted
            if not trusted_control:
                persisted_metrics.pop("provider_budget", None)
                breach_codes = [
                    code for code in breach_codes if code not in _SERVER_BUDGET_BREACHES
                ]
            elif _SERVER_BUDGET_BREACHES.intersection(breach_codes):
                provider_budget_metrics = persisted_metrics.get("provider_budget")
                if (
                    not isinstance(provider_budget_metrics, Mapping)
                    or provider_budget_metrics.get("source") != "server_linked_jobs"
                ):
                    raise GovernanceError(
                        "trusted provider budget breaches require server-linked job metrics"
                    )
            requested_subjects_by_kind = {
                kind: {
                    (str(subject_type), int(subject_id))
                    for subject_type, subject_id in subject_keys
                }
                for kind, subject_keys in (
                    ("served", served_subject_keys),
                    ("paired", paired_subject_keys),
                    ("audited", audited_subject_keys),
                )
                if subject_keys is not None
            }
            new_subjects_by_kind: dict[str, list[tuple[str, int]]] = {
                kind: [] for kind in requested_subjects_by_kind
            }
            if trusted_control and requested_subjects_by_kind:
                requested_subject_keys = set().union(*requested_subjects_by_kind.values())
                if any(
                    subject_type not in {"dialogue_unit", "reception"} or subject_id <= 0
                    for subject_type, subject_id in requested_subject_keys
                ):
                    raise GovernanceError(
                        "deployment stage subjects must be positive dialogue/reception identities"
                    )
                dialogue_subject_ids = {
                    subject_id
                    for subject_type, subject_id in requested_subject_keys
                    if subject_type == "dialogue_unit"
                }
                reception_subject_ids = {
                    subject_id
                    for subject_type, subject_id in requested_subject_keys
                    if subject_type == "reception"
                }
                owned_subjects: set[tuple[str, int]] = set()
                if dialogue_subject_ids:
                    owned_subjects.update(
                        ("dialogue_unit", int(subject_id))
                        for subject_id in (
                            await session.execute(
                                select(DialogueUnit.id).where(
                                    DialogueUnit.tenant_id == tenant_id,
                                    DialogueUnit.id.in_(dialogue_subject_ids),
                                )
                            )
                        ).scalars()
                    )
                if reception_subject_ids:
                    owned_subjects.update(
                        ("reception", int(subject_id))
                        for subject_id in (
                            await session.execute(
                                select(Reception.id).where(
                                    Reception.tenant_id == tenant_id,
                                    Reception.id.in_(reception_subject_ids),
                                )
                            )
                        ).scalars()
                    )
                if owned_subjects != requested_subject_keys:
                    raise GovernanceError(
                        "deployment stage subjects contain missing or cross-tenant identities"
                    )
                previously_counted_subjects: dict[str, set[tuple[str, int]]] = {
                    kind: set() for kind in requested_subjects_by_kind
                }
                for row in (
                    await session.execute(
                        select(
                            TagDeploymentAuditSubject.count_kind,
                            TagDeploymentAuditSubject.subject_type,
                            TagDeploymentAuditSubject.subject_id,
                        ).where(
                            TagDeploymentAuditSubject.tenant_id == tenant_id,
                            TagDeploymentAuditSubject.deployment_id == deployment.id,
                            TagDeploymentAuditSubject.stage == observed_stage,
                            TagDeploymentAuditSubject.deployment_revision == observed_revision,
                            TagDeploymentAuditSubject.count_kind.in_(requested_subjects_by_kind),
                        )
                    )
                ).all():
                    previously_counted_subjects[str(row.count_kind)].add(
                        (str(row.subject_type), int(row.subject_id))
                    )
                new_subjects_by_kind = {
                    kind: sorted(requested - previously_counted_subjects.get(kind, set()))
                    for kind, requested in requested_subjects_by_kind.items()
                }
                if served_subject_keys is not None:
                    served_count = len(new_subjects_by_kind.get("served", ()))
                if paired_subject_keys is not None:
                    paired_count = len(new_subjects_by_kind.get("paired", ()))
                if audited_subject_keys is not None:
                    audited_count = len(new_subjects_by_kind.get("audited", ()))
                    adjudicated_count = audited_count
                persisted_metrics["stage_new_subject_count_by_kind"] = {
                    kind: len(subjects) for kind, subjects in sorted(new_subjects_by_kind.items())
                }
                persisted_metrics["stage_new_audited_subject_count"] = len(
                    new_subjects_by_kind.get("audited", ())
                )
            historical_observations: list[TagDeploymentObservation] = []
            if trusted_control:
                historical_observations = list(
                    (
                        await session.execute(
                            select(TagDeploymentObservation)
                            .where(
                                TagDeploymentObservation.tenant_id == tenant_id,
                                TagDeploymentObservation.deployment_id == deployment_id,
                                TagDeploymentObservation.stage == observed_stage,
                                TagDeploymentObservation.deployment_revision == observed_revision,
                                TagDeploymentObservation.source == "monitor",
                                TagDeploymentObservation.is_trusted.is_(True),
                                TagDeploymentObservation.window_start
                                >= window_end
                                - (_DRIFT_POLICY_WINDOW * _DRIFT_POLICY_REQUIRED_WINDOWS),
                                TagDeploymentObservation.window_end <= window_start,
                            )
                            .order_by(
                                TagDeploymentObservation.window_start,
                                TagDeploymentObservation.window_end,
                                TagDeploymentObservation.id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            policy_samples = [
                _PolicyObservation(
                    window_start=_aware_utc(item.window_start),
                    window_end=_aware_utc(item.window_end),
                    metrics=dict(item.metrics),
                )
                for item in historical_observations
            ]
            if trusted_control:
                policy_samples.append(
                    _PolicyObservation(
                        window_start=_aware_utc(window_start),
                        window_end=_aware_utc(window_end),
                        metrics=persisted_metrics,
                    )
                )
                error_policy = _evaluate_error_policy(
                    policy_samples,
                    window_end=window_end,
                )
                efficiency_policy = _evaluate_efficiency_policy(
                    policy_samples,
                    window_end=window_end,
                    required=bool(persisted_metrics.get("efficiency_required")),
                )
                drift_policy = _evaluate_drift_policy(
                    policy_samples,
                    window_end=window_end,
                )
            else:
                error_policy = {
                    "complete": False,
                    "consecutive_breach": False,
                    "reason": "untrusted_observation",
                    "threshold": _ERROR_RATE_THRESHOLD,
                    "comparison": ">=",
                    "window_minutes": int(_ERROR_POLICY_WINDOW.total_seconds() // 60),
                    "required_consecutive_windows": _ERROR_POLICY_REQUIRED_WINDOWS,
                    "windows": [],
                }
                efficiency_policy = {
                    "required": bool(persisted_metrics.get("efficiency_required")),
                    "complete": False,
                    "consecutive_breach": False,
                    "hard_breach": False,
                    "reason": "untrusted_observation",
                    "soft_threshold": _EFFICIENCY_SOFT_REGRESSION_THRESHOLD,
                    "hard_threshold": _EFFICIENCY_HARD_REGRESSION_THRESHOLD,
                    "comparison": ">",
                    "window_minutes": int(
                        _EFFICIENCY_POLICY_WINDOW.total_seconds() // 60
                    ),
                    "required_consecutive_windows": (
                        _EFFICIENCY_POLICY_REQUIRED_WINDOWS
                    ),
                    "windows": [],
                }
                drift_policy = {
                    "complete": False,
                    "consecutive_breach": False,
                    "reason": "untrusted_observation",
                    "jsd_threshold": _DRIFT_JSD_THRESHOLD,
                    "psi_threshold": _DRIFT_PSI_THRESHOLD,
                    "comparison": ">",
                    "minimum_paired_samples": _DRIFT_MIN_PAIRED_SAMPLES,
                    "window_hours": int(_DRIFT_POLICY_WINDOW.total_seconds() // 3600),
                    "required_consecutive_windows": _DRIFT_POLICY_REQUIRED_WINDOWS,
                    "affected_tags": [],
                    "affected_inputs": [],
                    "affected_domains": [],
                    "windows": [],
                }
            persisted_metrics["error_policy"] = error_policy
            persisted_metrics["efficiency_policy"] = efficiency_policy
            persisted_metrics["drift_policy"] = drift_policy
            action = "observe"
            drift_review_job: TagExtractionJob | None = None
            budget_review_job: TagExtractionJob | None = None
            should_rollback = trusted_control and bool(hard_breaches.intersection(breach_codes))
            error_breach = trusted_control and bool(error_policy["consecutive_breach"])
            efficiency_hard_breach = trusted_control and bool(
                efficiency_policy["hard_breach"]
            )
            efficiency_soft_breach = (
                trusted_control
                and bool(efficiency_policy["consecutive_breach"])
                and not efficiency_hard_breach
            )
            should_rollback = (
                should_rollback or error_breach or efficiency_hard_breach
            )
            drift_only = (
                trusted_control and bool(drift_policy["consecutive_breach"]) and not should_rollback
            )
            budget_near_exhaustion = (
                trusted_control
                and "budget_near_exhaustion" in breach_codes
                and not should_rollback
            )
            if should_rollback:
                action = "rollback"
                control_breaches = set(breach_codes)
                if error_breach:
                    control_breaches.add("error_rate")
                if efficiency_hard_breach:
                    control_breaches.add("efficiency_regression")
                    if "efficiency_regression" not in breach_codes:
                        breach_codes.append("efficiency_regression")
                await self._rollback_visibility(
                    session,
                    deployment=deployment,
                    actor_user_id=actor_user_id,
                    reason="monitoring:" + ",".join(sorted(control_breaches)),
                    now=window_end,
                )
                deployment.revision += 1
            elif budget_near_exhaustion:
                action = "pause"
                pause_reason = "provider budget nearing exhaustion requires review"
                if not (deployment.promotion_paused and deployment.pause_reason == pause_reason):
                    deployment.promotion_paused = True
                    deployment.pause_reason = pause_reason
                    deployment.revision += 1
                active_review_jobs = list(
                    (
                        await session.execute(
                            select(TagExtractionJob)
                            .where(
                                TagExtractionJob.tenant_id == tenant_id,
                                TagExtractionJob.job_type == "review_batch",
                                TagExtractionJob.origin == "monitor",
                                TagExtractionJob.tagger_version_id == deployment.tagger_version_id,
                                TagExtractionJob.status.in_(
                                    {
                                        "queued",
                                        "running",
                                        "retry_wait",
                                        "completed",
                                    }
                                ),
                            )
                            .order_by(TagExtractionJob.id.desc())
                            .limit(500)
                        )
                    )
                    .scalars()
                    .all()
                )
                budget_review_job = next(
                    (
                        job
                        for job in active_review_jobs
                        if job.scope.get("deployment_id") == deployment.id
                        and job.scope.get("selection_policy") == "budget_guard"
                    ),
                    None,
                )
                if budget_review_job is None:
                    budget_fact_predicates = [
                        TagAssignmentFact.tenant_id == tenant_id,
                        TagAssignmentFact.deployment_id == deployment.id,
                        TagAssignmentFact.tagger_version_id == deployment.tagger_version_id,
                        TagAssignmentFact.tombstone.is_(False),
                    ]
                    selected_fact_ids = set(review_fact_ids or [])
                    if selected_fact_ids:
                        budget_fact_predicates.append(TagAssignmentFact.id.in_(selected_fact_ids))
                    else:
                        budget_fact_predicates.extend(
                            [
                                TagAssignmentFact.assigned_at >= window_start,
                                TagAssignmentFact.assigned_at < window_end,
                            ]
                        )
                    budget_facts = list(
                        (
                            await session.execute(
                                select(TagAssignmentFact)
                                .where(*budget_fact_predicates)
                                .order_by(
                                    TagAssignmentFact.assigned_at.desc(),
                                    TagAssignmentFact.id.desc(),
                                )
                                .limit(2_000)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    if not budget_facts and not selected_fact_ids:
                        budget_facts = list(
                            (
                                await session.execute(
                                    select(TagAssignmentFact)
                                    .where(
                                        TagAssignmentFact.tenant_id == tenant_id,
                                        TagAssignmentFact.deployment_id == deployment.id,
                                        TagAssignmentFact.tagger_version_id
                                        == deployment.tagger_version_id,
                                        TagAssignmentFact.tombstone.is_(False),
                                    )
                                    .order_by(
                                        TagAssignmentFact.assigned_at.desc(),
                                        TagAssignmentFact.id.desc(),
                                    )
                                    .limit(2_000)
                                )
                            )
                            .scalars()
                            .all()
                        )
                    deduplicated_budget_facts: list[TagAssignmentFact] = []
                    seen_budget_subject_tags: set[tuple[str, int, str]] = set()
                    for fact in budget_facts:
                        identity = (
                            str(fact.subject_type),
                            int(fact.subject_id),
                            str(fact.tag_key),
                        )
                        if identity in seen_budget_subject_tags:
                            continue
                        seen_budget_subject_tags.add(identity)
                        deduplicated_budget_facts.append(fact)
                    deduplicated_budget_facts.sort(
                        key=lambda fact: canonical_checksum(
                            {
                                "deployment_id": deployment.id,
                                "window_end": _aware_utc(window_end).isoformat(),
                                "fact_id": fact.id,
                            }
                        )
                    )
                    selected_budget_facts = deduplicated_budget_facts[:100]
                    budget_subjects = [
                        {
                            "subject_type": fact.subject_type,
                            "subject_id": fact.subject_id,
                            "reception_id": fact.reception_id,
                            "tag_key": fact.tag_key,
                            "proposed_fact_id": fact.id,
                            "schema_version_id": fact.schema_version_id,
                            "tagger_version_id": fact.tagger_version_id,
                            "confidence": fact.confidence,
                            "evidence_refs": fact.evidence_refs,
                        }
                        for fact in selected_budget_facts
                    ]
                    if not budget_subjects:
                        terminal_at = func.coalesce(
                            TagExtractionRun.finished_at,
                            TagExtractionRun.updated_at,
                        )
                        budget_runs = list(
                            (
                                await session.execute(
                                    select(TagExtractionRun)
                                    .where(
                                        TagExtractionRun.tenant_id == tenant_id,
                                        TagExtractionRun.origin == "serving",
                                        TagExtractionRun.deployment_id == deployment.id,
                                        TagExtractionRun.deployment_stage == observed_stage,
                                        TagExtractionRun.deployment_revision
                                        == observed_revision,
                                        TagExtractionRun.subject_type.in_(
                                            {"dialogue_unit", "reception"}
                                        ),
                                        TagExtractionRun.status.in_(
                                            {"completed", "cached", "failed"}
                                        ),
                                        terminal_at >= window_start,
                                        terminal_at < window_end,
                                    )
                                    .order_by(TagExtractionRun.id)
                                    .limit(100)
                                )
                            )
                            .scalars()
                            .all()
                        )
                        schema_definitions = (
                            await session.execute(
                                select(TagSchemaVersion.definitions).where(
                                    TagSchemaVersion.id == schema_version_id,
                                    TagSchemaVersion.tenant_id == tenant_id,
                                )
                            )
                        ).scalar_one()
                        for run in budget_runs:
                            _, scenario = await self._resolve_subject(
                                session,
                                tenant_id=tenant_id,
                                subject_type=str(run.subject_type),
                                subject_id=int(run.subject_id),
                            )
                            for definition in schema_definitions:
                                if (
                                    not isinstance(definition, Mapping)
                                    or not str(definition.get("key", "")).strip()
                                    or run.subject_type
                                    not in (definition.get("subject_types") or [])
                                    or (
                                        definition.get("scenarios")
                                        and scenario not in definition["scenarios"]
                                    )
                                ):
                                    continue
                                budget_subjects.append(
                                    {
                                        "subject_type": str(run.subject_type),
                                        "subject_id": int(run.subject_id),
                                        "tag_key": str(definition["key"]),
                                        "schema_version_id": int(schema_version_id),
                                        "tagger_version_id": int(
                                            deployment.tagger_version_id
                                        ),
                                        "source_deployment_id": int(deployment.id),
                                        "source_extraction_run_id": int(run.id),
                                        "confidence": None,
                                        "evidence_refs": [],
                                    }
                                )
                                if len(budget_subjects) >= 100:
                                    break
                            if len(budget_subjects) >= 100:
                                break
                    if budget_subjects:
                        review_bundle_id = (
                            f"budget-guard-{deployment.id}-"
                            f"{_aware_utc(window_end).strftime('%Y%m%dT%H%M%SZ')}"
                        )[:64]
                        budget_review_job = TagExtractionJob(
                            tenant_id=tenant_id,
                            job_type="review_batch",
                            origin="monitor",
                            status="queued",
                            scope={
                                "deployment_id": deployment.id,
                                "reason": "audit",
                                "budget_state": "near_exhaustion",
                                "review_bundle_id": review_bundle_id,
                                "selection_policy": "budget_guard",
                                "selection_policy_version": "1",
                                "blind_mode": True,
                                "population_size": max(
                                    len(deduplicated_budget_facts),
                                    len(budget_subjects),
                                ),
                                "subjects": budget_subjects,
                            },
                            tagger_version_id=deployment.tagger_version_id,
                            idempotency_key=(
                                f"deployment-budget-review:{deployment.id}:{window_end.isoformat()}"
                            )[:128],
                            total_items=len(budget_subjects),
                            completed_items=0,
                            failed_items=0,
                            attempt_count=0,
                            max_attempts=3,
                            revision=1,
                            created_by=actor_user_id,
                        )
                        session.add(budget_review_job)
                        await session.flush()
                provider_budget_metrics = persisted_metrics.get("provider_budget")
                if isinstance(provider_budget_metrics, dict):
                    provider_budget_metrics = dict(provider_budget_metrics)
                    provider_budget_metrics["review_status"] = (
                        "queued_or_in_progress"
                        if budget_review_job is not None
                        else "no_reviewable_facts"
                    )
                    provider_budget_metrics["review_job_id"] = (
                        int(budget_review_job.id) if budget_review_job is not None else None
                    )
                    persisted_metrics["provider_budget"] = provider_budget_metrics
            elif efficiency_soft_breach:
                action = "pause"
                if "efficiency_regression" not in breach_codes:
                    breach_codes.append("efficiency_regression")
                deployment.promotion_paused = True
                deployment.pause_reason = (
                    "provider token/cost regression exceeded 10% in two "
                    "complete 15-minute windows"
                )
                deployment.revision += 1
            elif drift_only:
                action = "pause"
                deployment.promotion_paused = True
                deployment.pause_reason = "distribution drift requires review"
                deployment.revision += 1
                affected_domains_raw = drift_policy.get(
                    "affected_domains",
                    drift_policy.get("affected_tags", []),
                )
                affected_domains = (
                    {
                        str(domain_key)
                        for domain_key in affected_domains_raw
                        if isinstance(domain_key, str) and domain_key
                    }
                    if isinstance(affected_domains_raw, list)
                    else set()
                )
                output_domains: set[tuple[str, str]] = set()
                input_subject_types: set[str] = set()
                legacy_tag_keys: set[str] = set()
                global_domain = False
                for domain_key in affected_domains:
                    subject_type, tag_key, signal = _parse_drift_domain_key(domain_key)
                    if signal == "global":
                        global_domain = True
                    elif signal == "input" and subject_type is not None:
                        input_subject_types.add(subject_type)
                    elif subject_type is not None and tag_key is not None:
                        output_domains.add((subject_type, tag_key))
                    elif tag_key is not None:
                        legacy_tag_keys.add(tag_key)
                drift_fact_predicates = [
                    TagAssignmentFact.tenant_id == tenant_id,
                    TagAssignmentFact.deployment_id == deployment.id,
                    TagAssignmentFact.tagger_version_id == deployment.tagger_version_id,
                    TagAssignmentFact.tombstone.is_(False),
                ]
                selected_fact_ids = set(review_fact_ids or [])
                if selected_fact_ids:
                    drift_fact_predicates.append(TagAssignmentFact.id.in_(selected_fact_ids))
                else:
                    drift_fact_predicates.extend(
                        [
                            TagAssignmentFact.assigned_at
                            >= window_end - (_DRIFT_POLICY_WINDOW * _DRIFT_POLICY_REQUIRED_WINDOWS),
                            TagAssignmentFact.assigned_at < window_end,
                        ]
                    )
                domain_fact_predicates = [
                    and_(
                        TagAssignmentFact.subject_type == subject_type,
                        TagAssignmentFact.tag_key == tag_key,
                    )
                    for subject_type, tag_key in sorted(output_domains)
                ]
                if input_subject_types:
                    domain_fact_predicates.append(
                        TagAssignmentFact.subject_type.in_(input_subject_types)
                    )
                if legacy_tag_keys:
                    domain_fact_predicates.append(TagAssignmentFact.tag_key.in_(legacy_tag_keys))
                if not global_domain and domain_fact_predicates:
                    drift_fact_predicates.append(or_(*domain_fact_predicates))
                drift_facts = list(
                    (
                        await session.execute(
                            select(TagAssignmentFact)
                            .where(*drift_fact_predicates)
                            .order_by(
                                TagAssignmentFact.assigned_at.desc(),
                                TagAssignmentFact.id.desc(),
                            )
                            .limit(2_000)
                        )
                    )
                    .scalars()
                    .all()
                )
                deduplicated_facts: list[TagAssignmentFact] = []
                seen_subject_tags: set[tuple[str, int, str]] = set()
                for fact in drift_facts:
                    identity = (
                        str(fact.subject_type),
                        int(fact.subject_id),
                        str(fact.tag_key),
                    )
                    if identity in seen_subject_tags:
                        continue
                    seen_subject_tags.add(identity)
                    deduplicated_facts.append(fact)
                population_size = len(deduplicated_facts)
                sample_size = min(population_size, 100)
                deduplicated_facts.sort(
                    key=lambda fact: canonical_checksum(
                        {
                            "deployment_id": deployment.id,
                            "window_end": _aware_utc(window_end).isoformat(),
                            "fact_id": fact.id,
                        }
                    )
                )
                selected_facts = deduplicated_facts[:sample_size]
                sampling_probability = sample_size / population_size if population_size else None
                subjects = [
                    {
                        "subject_type": fact.subject_type,
                        "subject_id": fact.subject_id,
                        "reception_id": fact.reception_id,
                        "tag_key": fact.tag_key,
                        "proposed_fact_id": fact.id,
                        "schema_version_id": fact.schema_version_id,
                        "tagger_version_id": fact.tagger_version_id,
                        "confidence": fact.confidence,
                        "evidence_refs": fact.evidence_refs,
                    }
                    for fact in selected_facts
                ]
                if subjects:
                    key = f"deployment-drift-review:{deployment.id}:{window_end.isoformat()}"
                    review_bundle_id = (
                        f"drift-audit-{deployment.id}-"
                        f"{_aware_utc(window_end).strftime('%Y%m%dT%H%M%SZ')}"
                    )[:64]
                    drift_review_job = TagExtractionJob(
                        tenant_id=tenant_id,
                        job_type="review_batch",
                        origin="monitor",
                        status="queued",
                        scope={
                            "deployment_id": deployment.id,
                            "reason": "drift",
                            "affected_domains": sorted(affected_domains),
                            "review_bundle_id": review_bundle_id,
                            "selection_policy": "drift_audit",
                            "selection_policy_version": "1",
                            "sampling_probability": sampling_probability,
                            "blind_mode": True,
                            "population_size": population_size,
                            "subjects": subjects,
                        },
                        tagger_version_id=deployment.tagger_version_id,
                        idempotency_key=key[:128],
                        total_items=len(subjects),
                        completed_items=0,
                        failed_items=0,
                        attempt_count=0,
                        max_attempts=3,
                        revision=1,
                        created_by=actor_user_id,
                    )
            observation = TagDeploymentObservation(
                tenant_id=tenant_id,
                deployment_id=deployment.id,
                deployment_revision=observed_revision,
                stage=observed_stage,
                window_start=window_start,
                window_end=window_end,
                sample_count=len(new_reception_ids),
                source=source,
                provenance=dict(provenance or {}),
                is_trusted=is_trusted,
                served_count=int(served_count),
                paired_count=int(paired_count),
                audited_count=int(audited_count),
                adjudicated_count=int(adjudicated_count),
                metrics=persisted_metrics,
                breach_codes=breach_codes,
                action=action,
            )
            session.add(observation)
            await session.flush()
            if budget_review_job is not None:
                linked_observation_ids = list(
                    budget_review_job.scope.get("linked_observation_ids") or []
                )
                if observation.id not in linked_observation_ids:
                    linked_observation_ids.append(observation.id)
                budget_review_job.scope = {
                    **dict(budget_review_job.scope),
                    "trusted_observation_id": budget_review_job.scope.get(
                        "trusted_observation_id",
                        observation.id,
                    ),
                    "linked_observation_ids": linked_observation_ids,
                }
                session.add(budget_review_job)
            if drift_review_job is not None:
                drift_review_job.scope = {
                    **dict(drift_review_job.scope),
                    "trusted_observation_id": observation.id,
                }
                session.add(drift_review_job)
            session.add_all(
                [
                    TagDeploymentObservationSample(
                        tenant_id=tenant_id,
                        deployment_id=deployment.id,
                        observation_id=observation.id,
                        stage=observed_stage,
                        reception_id=reception_id,
                    )
                    for reception_id in new_reception_ids
                ]
            )
            session.add_all(
                [
                    TagDeploymentAuditSubject(
                        tenant_id=tenant_id,
                        deployment_id=deployment.id,
                        first_observation_id=observation.id,
                        stage=observed_stage,
                        deployment_revision=observed_revision,
                        count_kind=count_kind,
                        subject_type=subject_type,
                        subject_id=subject_id,
                    )
                    for count_kind, subjects in new_subjects_by_kind.items()
                    for subject_type, subject_id in subjects
                ]
            )
            auto_transition: str | None = None
            readiness: PromotionReadiness | None = None
            if (
                observation.source == "monitor"
                and observation.is_trusted
                and observed_stage == "shadow"
            ):
                readiness = await self._trusted_promotion_readiness(
                    session,
                    deployment=deployment,
                    at=window_end,
                    stage=str(observed_stage),
                    revision=observed_revision,
                )
                sampling_complete = _shadow_sampling_requirements_met(readiness)
                sampling_metrics = {
                    metric
                    for metric in readiness.requirements
                    if metric in {"paired_count", "audited_count"}
                    or metric.startswith(("paired_count:", "audited_count:"))
                }
                persisted_metrics["shadow_sampling"] = {
                    "complete": sampling_complete,
                    "observed": {
                        metric: readiness.observed[metric]
                        for metric in sorted(sampling_metrics)
                    },
                    "requirements": {
                        metric: readiness.requirements[metric]
                        for metric in sorted(sampling_metrics)
                    },
                }
                if sampling_complete and deployment.sampling_complete_at is None:
                    deployment.sampling_complete_at = _aware_utc(window_end)
            if (
                action == "observe"
                and not deployment.promotion_paused
                and not error_breach
                and not breach_codes
                and bool(efficiency_policy["complete"])
                and observation.source == "monitor"
                and observation.is_trusted
                and observed_stage in {"shadow", "canary_5", "canary_25"}
            ):
                if readiness is None:
                    readiness = await self._trusted_promotion_readiness(
                        session,
                        deployment=deployment,
                        at=window_end,
                    )
                persisted_metrics["promotion_readiness"] = {
                    "elapsed_hours": readiness.elapsed_hours,
                    "observed": readiness.observed,
                    "requirements": readiness.requirements,
                    "unmet": list(readiness.unmet),
                }
                if readiness.passed:
                    (
                        baseline_fresh,
                        current_baseline_id,
                        baseline_reason,
                    ) = await self._deployment_baseline_freshness(
                        session,
                        deployment=deployment,
                    )
                    persisted_metrics["baseline_freshness"] = {
                        "passed": baseline_fresh,
                        "reason": baseline_reason,
                        "bound_baseline_tagger_version_id": (deployment.baseline_tagger_version_id),
                        "current_production_tagger_version_id": current_baseline_id,
                    }
                    if not baseline_fresh:
                        action = "rollback"
                        if "stale_baseline" not in breach_codes:
                            breach_codes.append("stale_baseline")
                        await self._rollback_visibility(
                            session,
                            deployment=deployment,
                            actor_user_id=actor_user_id,
                            reason=f"monitoring:stale_baseline:{baseline_reason}",
                            now=window_end,
                            restore_tagger_version_id=current_baseline_id,
                            restore_bound_baseline=False,
                            reactivate_baseline=False,
                        )
                        deployment.revision += 1
                        observation.action = action
                        observation.breach_codes = list(breach_codes)
                    else:
                        next_stage, traffic = {
                            "shadow": ("canary_5", 5),
                            "canary_5": ("canary_25", 25),
                            "canary_25": ("awaiting_admin", 25),
                        }[observed_stage]
                        deployment.status = next_stage
                        deployment.traffic_percent = traffic
                        deployment.revision += 1
                        auto_transition = next_stage
            observation.metrics = dict(persisted_metrics)
            await self._audit(
                session,
                tenant_id=tenant_id,
                resource_type="tag_deployment",
                resource_id=deployment.id,
                action=f"observation_{action}",
                actor_user_id=actor_user_id,
                payload={
                    "observation_id": observation.id,
                    "metrics": persisted_metrics,
                    "stage_new_reception_count": len(new_reception_ids),
                    "breach_codes": breach_codes,
                    "auto_transition": auto_transition,
                },
            )
            return observation, deployment

    async def list_audit_events(
        self, *, tenant_id: str, limit: int = 100
    ) -> list[TagGovernanceAuditEvent]:
        async with self._factory() as session:
            return list(
                (
                    await session.execute(
                        select(TagGovernanceAuditEvent)
                        .where(TagGovernanceAuditEvent.tenant_id == tenant_id)
                        .order_by(
                            TagGovernanceAuditEvent.occurred_at.desc(),
                            TagGovernanceAuditEvent.id.desc(),
                        )
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )


__all__ = [
    "AssignmentValidationError",
    "Gate",
    "GateEvaluation",
    "GovernanceConflictError",
    "GovernanceError",
    "GovernanceNotFoundError",
    "GovernanceStaleObservationError",
    "HarnessOptimizationTrial",
    "HarnessSearchResult",
    "HarnessTrialExecutor",
    "OptimizationReward",
    "PersistedPredictionTrialExecutor",
    "PromotionReadiness",
    "TagGovernanceService",
    "TagJobBudgetExhaustedError",
    "TagJobBudgetReservation",
    "bounded_harness_search",
    "build_candidate_comparison",
    "canonical_checksum",
    "compute_gold_dataset_snapshot_hash",
    "compute_input_hash",
    "deterministic_gold_split",
    "enforce_sealed_holdout_access",
    "evaluate_promotion_readiness",
    "evaluate_quality_gates",
    "execute_harness_trials",
    "reject_client_error_samples",
    "resolve_serving_tagger_route",
    "schema_subject_tag_pairs",
    "stable_canary_bucket",
    "stable_job_idempotency_key",
    "validate_assignment",
    "validate_rule_bundle",
]
