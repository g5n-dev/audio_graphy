"""Versioned dialogue-tag extractor over persisted, scrubbed segment evidence."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from random import Random
from time import perf_counter
from typing import Any, ClassVar, Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.adapters.protocols import LLMAdapter, LLMResponse
from audio_graphy.core.pii import scrubbed_segment_text
from audio_graphy.models.reception import DialogueUnit, Reception, ReceptionRecording
from audio_graphy.models.segment import Segment
from audio_graphy.models.tag_governance import (
    TagAssignmentCurrent,
    TagAssignmentFact,
    TagDeployment,
    TagExtractionRun,
    TaggerVersion,
    TagHarnessExecution,
    TagHarnessStageTrace,
    TagSchemaVersion,
)
from audio_graphy.services.llm_gateway import (
    CachePolicy,
    LLMProvenance,
    LLMRequest,
    LLMUsageContext,
    execute_llm,
)
from audio_graphy.services.tag_governance import (
    AssignmentValidationError,
    GovernanceNotFoundError,
    TagGovernanceService,
    TagJobBudgetExhaustedError,
    canonical_checksum,
    compute_input_hash,
    validate_assignment,
)
from audio_graphy.services.tag_harness_runtime import (
    build_scene_profile,
    build_stage_observation,
    estimate_prompt_tokens,
    fuse_assignments,
    output_token_budget,
    resolve_harness_spec,
)

_TAG_ASSIGNMENT_PARSER_VERSION = "strict-tag-assignments-json-v2"
_TAG_ASSIGNMENT_POSTPROCESSOR_VERSION = "schema-threshold-evidence-v2"
_TAG_TRANSPORT_PROMPT_VERSION = "tag-transport-prompt-v2"
_TAG_ASSIGNMENT_TTL_SECONDS = 90 * 24 * 60 * 60
_MIN_LLM_OUTPUT_TOKENS = 256
_MAX_LLM_OUTPUT_TOKENS = 2_048
_MAX_EVIDENCE_SEGMENTS_PER_ASSIGNMENT = 16
_RECEPTION_FACT_EVIDENCE_SEGMENTS_PER_UNIT = 2
_RECEPTION_FACT_EVIDENCE_TEXT_CHARS = 320
_RECEPTION_FACT_TRANSPORT_VERSION = "dialogue-unit-facts-v1"
_MIN_COLD_TOKEN_REDUCTION = 0.20
_MIN_PAIRED_TOKEN_REDUCTION_LCB = 0.10
_MIN_COLD_COST_REDUCTION = 0.15
_MAX_P95_LATENCY_REGRESSION = 0.05
_MAX_REVIEW_RATE_INCREASE = 0.01
_EFFICIENCY_BOOTSTRAP_ITERATIONS = 2_000
_MAX_REPORTED_FLIPS = 200
# Share of max_input_tokens the transport may occupy. The preflight report and the
# real batcher must read this from the same place or the preflight proves nothing.
_INPUT_BUDGET_UTILIZATION = 0.90
_DEFAULT_MAX_INPUT_TOKENS = 12_000


@dataclass(frozen=True, slots=True)
class EfficiencyEnvelope:
    """Bounds the efficiency half of trial feasibility.

    The quality gate is deliberately not part of this envelope: relaxing cost never
    relaxes correctness. Only the cost-shaped thresholds move, so a candidate that
    trades tokens for accuracy can still be measured instead of being rejected before
    its quality is ever compared against the baseline.
    """

    key: Literal["token_reduction_v1", "quality_uplift_v1"]
    min_cold_token_reduction: float
    min_paired_token_reduction_lcb: float
    min_cold_cost_reduction: float
    max_p95_latency_regression: float
    max_review_rate_increase: float
    require_provider_calls_nonincrease: bool


TOKEN_REDUCTION_V1 = EfficiencyEnvelope(
    key="token_reduction_v1",
    min_cold_token_reduction=_MIN_COLD_TOKEN_REDUCTION,
    min_paired_token_reduction_lcb=_MIN_PAIRED_TOKEN_REDUCTION_LCB,
    min_cold_cost_reduction=_MIN_COLD_COST_REDUCTION,
    max_p95_latency_regression=_MAX_P95_LATENCY_REGRESSION,
    max_review_rate_increase=_MAX_REVIEW_RATE_INCREASE,
    require_provider_calls_nonincrease=True,
)
"""The historical envelope: a candidate must pay for itself in tokens and cost."""

QUALITY_UPLIFT_V1 = EfficiencyEnvelope(
    key="quality_uplift_v1",
    min_cold_token_reduction=-0.15,
    min_paired_token_reduction_lcb=-0.25,
    min_cold_cost_reduction=-0.15,
    max_p95_latency_regression=_MAX_P95_LATENCY_REGRESSION,
    max_review_rate_increase=_MAX_REVIEW_RATE_INCREASE,
    require_provider_calls_nonincrease=True,
)
"""Allows a prompt to grow by up to 15%, still refusing extra provider round-trips."""

EFFICIENCY_ENVELOPES: dict[str, EfficiencyEnvelope] = {
    TOKEN_REDUCTION_V1.key: TOKEN_REDUCTION_V1,
    QUALITY_UPLIFT_V1.key: QUALITY_UPLIFT_V1,
}


def _usable_input_tokens(max_input_tokens: int) -> int:
    """Tokens the transport may spend on one call, prompt and schema included."""

    return max(1, math.floor(max_input_tokens * _INPUT_BUDGET_UTILIZATION))


@dataclass(frozen=True, slots=True)
class PromptInputBudgetReport:
    """What a candidate prompt costs before a single transcript segment is added.

    A compiled prompt can fail in two ways that a quality metric never sees: it can
    overflow the per-call input budget outright, or it can merely shrink the headroom
    enough that long subjects start splitting into extra provider calls -- which the
    efficiency envelope refuses regardless of how good the prompt is.
    """

    prompt_tokens: int
    schema_tokens: int
    fixed_tokens: int
    usable_tokens: int
    headroom_tokens: int
    fits: bool
    baseline_fixed_tokens: int
    baseline_headroom_tokens: int
    headroom_delta: int

    @property
    def headroom_shrink_ratio(self) -> float:
        """Fraction of the baseline's segment headroom this candidate gives up."""

        if self.baseline_headroom_tokens <= 0:
            return 0.0
        return -self.headroom_delta / self.baseline_headroom_tokens


def prompt_input_budget_report(
    candidate: Mapping[str, Any],
    *,
    baseline: Mapping[str, Any],
    definitions: Mapping[str, Mapping[str, Any]],
) -> PromptInputBudgetReport:
    """Measure a candidate prompt against the same budget the batcher enforces."""

    def fixed_cost(config: Mapping[str, Any]) -> tuple[int, int, int]:
        generation = config.get("generation")
        section: Mapping[str, Any] = generation if isinstance(generation, Mapping) else {}
        prompt_content = section.get("prompt_template", "")
        if not isinstance(prompt_content, str):
            raise AssignmentValidationError("generation.prompt_template must be a string")
        raw_budget = section.get("max_input_tokens", _DEFAULT_MAX_INPUT_TOKENS)
        if isinstance(raw_budget, bool) or not isinstance(raw_budget, int) or raw_budget <= 0:
            raise AssignmentValidationError("generation.max_input_tokens must be a positive int")
        total = TagExtractor._estimated_transport_input_tokens(
            prompt_content=prompt_content,
            definitions=definitions,
            segment_texts=(),
        )
        prompt_tokens = estimate_prompt_tokens(TagExtractor._system_prompt(prompt_content))
        return total, prompt_tokens, _usable_input_tokens(raw_budget)

    candidate_fixed, candidate_prompt_tokens, usable_tokens = fixed_cost(candidate)
    baseline_fixed, _baseline_prompt_tokens, baseline_usable = fixed_cost(baseline)
    headroom = usable_tokens - candidate_fixed
    baseline_headroom = baseline_usable - baseline_fixed
    return PromptInputBudgetReport(
        prompt_tokens=candidate_prompt_tokens,
        schema_tokens=candidate_fixed - candidate_prompt_tokens,
        fixed_tokens=candidate_fixed,
        usable_tokens=usable_tokens,
        headroom_tokens=headroom,
        # Mirrors the guard in _segment_batches_for_input_budget: anything above the
        # usable budget raises before a single segment is packed.
        fits=candidate_fixed <= usable_tokens,
        baseline_fixed_tokens=baseline_fixed,
        baseline_headroom_tokens=baseline_headroom,
        headroom_delta=headroom - baseline_headroom,
    )


def rescaled_input_budget_report(
    stored: Mapping[str, Any],
    *,
    prompt_content: str,
) -> dict[str, Any]:
    """Re-derive a stored report for a prompt rewritten against the same schema.

    A re-materialized artifact keeps its parent's tag definitions and per-call
    budget and changes only the policy text, so ``schema_tokens``,
    ``usable_tokens`` and both baseline figures carry over unchanged and
    everything else follows from the new prompt. Copying the parent's report
    wholesale instead — as this used to — prices the accepted subset using the
    rejected superset's headroom, right beside a token count recomputed from the
    child.

    Returns the input unchanged if it is missing any field this needs; a partial
    report is not worth guessing at, and the caller has nothing better to store.
    """

    required = ("schema_tokens", "usable_tokens", "baseline_fixed_tokens")
    if any(not isinstance(stored.get(key), int) for key in required):
        return dict(stored)

    schema_tokens = int(stored["schema_tokens"])
    usable_tokens = int(stored["usable_tokens"])
    baseline_fixed = int(stored["baseline_fixed_tokens"])
    baseline_headroom = int(stored.get("baseline_headroom_tokens", usable_tokens - baseline_fixed))

    prompt_tokens = estimate_prompt_tokens(TagExtractor._system_prompt(prompt_content))
    fixed_tokens = prompt_tokens + schema_tokens
    headroom = usable_tokens - fixed_tokens
    headroom_delta = headroom - baseline_headroom
    return {
        "prompt_tokens": prompt_tokens,
        "schema_tokens": schema_tokens,
        "fixed_tokens": fixed_tokens,
        "usable_tokens": usable_tokens,
        "headroom_tokens": headroom,
        "baseline_fixed_tokens": baseline_fixed,
        "baseline_headroom_tokens": baseline_headroom,
        "headroom_delta": headroom_delta,
        "headroom_shrink_ratio": (
            -headroom_delta / baseline_headroom if baseline_headroom > 0 else 0.0
        ),
        "fits": fixed_tokens <= usable_tokens,
    }


class _TagOutputFormatError(AssignmentValidationError):
    """The model output cannot be decoded as the required JSON shape."""


class _TagEvidenceOutputError(AssignmentValidationError):
    """The model output references malformed or unknown evidence."""


class _TagBudgetExceededError(TagJobBudgetExhaustedError):
    """A bounded Harness cannot safely reserve another provider request."""


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    run_id: int
    input_hash: str
    input_snapshot: dict[str, Any]
    assignments: tuple[dict[str, Any], ...]
    cached: bool
    provider_tokens: int = 0
    provider_calls: int = 0
    cost_microunits: int = 0


@dataclass(frozen=True, slots=True)
class PredictionBatch:
    input_hash: str
    input_snapshot: dict[str, Any]
    candidates: tuple[dict[str, Any], ...]
    assignments: tuple[dict[str, Any], ...]
    review_items: tuple[dict[str, Any], ...]
    conflict_tag_keys: tuple[str, ...]
    harness_spec: dict[str, Any]
    scene_profile: dict[str, Any]
    route: str
    stage_traces: tuple[dict[str, Any], ...]
    latency_ms: int
    token_count: int
    cost_units: float
    provider_input_tokens: int = 0
    provider_output_tokens: int = 0
    reused_input_tokens: int = 0
    reused_output_tokens: int = 0
    provider_calls: int = 0
    cache_hits: int = 0
    strong_escalations: int = 0
    cost_microunits: int = 0
    counterfactual_saved_cost_microunits: int = 0
    unknown_billed_tokens: int = 0


@dataclass(frozen=True, slots=True)
class LLMAssignmentBatch:
    assignments: dict[str, dict[str, Any]]
    model_tier: str
    model: str
    token_count: int
    cached: bool
    provider_input_tokens: int
    provider_output_tokens: int
    reused_input_tokens: int
    reused_output_tokens: int
    provider_calls: int
    cache_hits: int
    cost_microunits: int
    counterfactual_saved_cost_microunits: int
    unknown_billed_tokens: int


@dataclass(frozen=True, slots=True)
class _PredictionSubject:
    """Minimal subject identity required by the pure Harness runtime."""

    tenant_id: str
    id: int
    reception_id: int
    source_recording_id: int | None
    version: int


@dataclass(frozen=True, slots=True)
class _SnapshotSegment:
    """Immutable segment reconstructed only from a frozen evaluation snapshot."""

    id: int
    recording_id: int
    start_sec: float
    end_sec: float
    speaker: str | None
    vad_conf: float | None


@dataclass(frozen=True, slots=True)
class PreparedDialogueInput:
    """Content-addressed extraction input prepared without invoking an LLM."""

    unit: DialogueUnit | _PredictionSubject
    subject_type: str
    tagger: TaggerVersion
    schema: TagSchemaVersion
    scenario: str
    segment_texts: tuple[tuple[Segment | _SnapshotSegment, str], ...]
    refs_by_segment: dict[int, dict[str, Any]]
    transcript: str
    input_hash: str
    input_snapshot: dict[str, Any]
    definitions: dict[str, dict[str, Any]]
    llm_segment_texts: tuple[tuple[Segment | _SnapshotSegment, str], ...] | None = None


class TagExtractor:
    """Apply a declarative rule bundle and optional structured LLM fallback."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        llm: LLMAdapter | None = None,
        weak_llm: LLMAdapter | None = None,
        strong_llm: LLMAdapter | None = None,
        enable_hybrid_rule_short_circuit: bool = False,
    ) -> None:
        if llm is not None and weak_llm is not None and llm is not weak_llm:
            raise ValueError("pass either llm or weak_llm, not two different adapters")
        self._factory = session_factory
        self._weak_llm = weak_llm or llm
        self._strong_llm = strong_llm
        self._llm = self._weak_llm
        self._enable_hybrid_rule_short_circuit = enable_hybrid_rule_short_circuit
        self._governance = TagGovernanceService(session_factory)

    async def _load_input(
        self,
        *,
        tenant_id: str,
        dialogue_unit_id: int,
        tagger_version_id: int,
    ) -> tuple[
        DialogueUnit,
        TaggerVersion,
        TagSchemaVersion,
        str,
        str,
        list[Segment],
        dict[int, dict[str, Any]],
    ]:
        async with self._factory() as session:
            unit = (
                await session.execute(
                    select(DialogueUnit).where(
                        DialogueUnit.id == dialogue_unit_id,
                        DialogueUnit.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            tagger = (
                await session.execute(
                    select(TaggerVersion).where(
                        TaggerVersion.id == tagger_version_id,
                        TaggerVersion.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if unit is None:
                raise GovernanceNotFoundError("dialogue unit not found")
            if tagger is None:
                raise GovernanceNotFoundError("tagger version not found")
            reception_identity = (
                await session.execute(
                    select(Reception.scenario, Reception.store_id).where(
                        Reception.id == unit.reception_id,
                        Reception.tenant_id == tenant_id,
                    )
                )
            ).one_or_none()
            if reception_identity is None:
                raise GovernanceNotFoundError("dialogue reception not found")
            scenario = str(reception_identity.scenario)
            store_id = str(reception_identity.store_id)
            schema = (
                await session.execute(
                    select(TagSchemaVersion).where(
                        TagSchemaVersion.id == tagger.schema_version_id,
                        TagSchemaVersion.tenant_id == tenant_id,
                        TagSchemaVersion.status.in_(["published", "deprecated"]),
                    )
                )
            ).scalar_one_or_none()
            if schema is None:
                raise AssignmentValidationError(
                    "tagger must reference an immutable published schema version"
                )
            mappings = list(
                (
                    await session.execute(
                        select(ReceptionRecording).where(
                            ReceptionRecording.tenant_id == tenant_id,
                            ReceptionRecording.reception_id == unit.reception_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            allowed_recording_ids = {mapping.recording_id for mapping in mappings}
            mappings_by_recording: dict[int, list[ReceptionRecording]] = {}
            for mapping in mappings:
                mappings_by_recording.setdefault(mapping.recording_id, []).append(mapping)
            refs_by_segment = {
                int(ref["segment_id"]): dict(ref)
                for ref in unit.segment_refs
                if isinstance(ref, dict) and ref.get("segment_id") is not None
            }
            segment_ids = sorted(refs_by_segment)
            if segment_ids:
                segments = list(
                    (
                        await session.execute(
                            select(Segment)
                            .where(
                                Segment.tenant_id == tenant_id,
                                Segment.id.in_(segment_ids),
                            )
                            .order_by(Segment.start_sec, Segment.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                if {segment.id for segment in segments} != set(segment_ids):
                    raise AssignmentValidationError(
                        "dialogue unit contains missing or cross-tenant segment references"
                    )
                if any(segment.recording_id not in allowed_recording_ids for segment in segments):
                    raise AssignmentValidationError(
                        "segment evidence does not belong to the dialogue reception"
                    )
            elif unit.source_recording_id is not None:
                if unit.source_recording_id not in allowed_recording_ids:
                    raise AssignmentValidationError(
                        "dialogue source recording does not belong to the reception"
                    )
                segments = list(
                    (
                        await session.execute(
                            select(Segment)
                            .where(
                                Segment.tenant_id == tenant_id,
                                Segment.recording_id == unit.source_recording_id,
                                Segment.start_sec < unit.end_sec,
                                Segment.end_sec > unit.start_sec,
                            )
                            .order_by(Segment.start_sec, Segment.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                refs_by_segment = {
                    segment.id: {
                        "segment_id": segment.id,
                        "recording_id": segment.recording_id,
                        "source_start_sec": max(
                            float(segment.start_sec),
                            unit.start_sec,
                            min(
                                float(mapping.source_start_sec)
                                for mapping in mappings_by_recording[segment.recording_id]
                            ),
                        ),
                        "source_end_sec": min(
                            float(segment.end_sec),
                            unit.end_sec,
                            max(
                                float(cast(float, mapping.source_end_sec))
                                for mapping in mappings_by_recording[segment.recording_id]
                            ),
                        ),
                    }
                    for segment in segments
                }
            else:
                segments = []
            for segment in segments:
                ref = refs_by_segment[segment.id]
                ref_recording_id = int(ref.get("recording_id", segment.recording_id))
                if ref_recording_id != segment.recording_id:
                    raise AssignmentValidationError(
                        "segment reference recording_id does not match persisted segment"
                    )
                source_start = float(ref.get("source_start_sec", segment.start_sec))
                source_end = float(ref.get("source_end_sec", segment.end_sec))
                if source_end <= source_start:
                    raise AssignmentValidationError("segment reference has an empty time window")
                if source_start >= segment.end_sec or source_end <= segment.start_sec:
                    raise AssignmentValidationError(
                        "segment reference window does not overlap persisted evidence"
                    )
                if not any(
                    source_start >= float(mapping.source_start_sec) - 1e-6
                    and source_end <= float(cast(float, mapping.source_end_sec)) + 1e-6
                    for mapping in mappings_by_recording.get(segment.recording_id, [])
                ):
                    raise AssignmentValidationError(
                        "segment reference window is outside the reception recording span"
                    )
            return unit, tagger, schema, scenario, store_id, segments, refs_by_segment

    @staticmethod
    def _segment_evidence(
        segment: Segment | _SnapshotSegment,
        text: str,
        ref: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ref = ref or {}
        source_start = max(
            float(segment.start_sec),
            float(ref.get("source_start_sec", segment.start_sec)),
        )
        source_end = min(
            float(segment.end_sec),
            float(ref.get("source_end_sec", segment.end_sec)),
        )
        return {
            "segment_id": segment.id,
            "ref_id": f"segment:{segment.id}",
            "kind": "audio_segment",
            "recording_id": segment.recording_id,
            "start_sec": source_start,
            "end_sec": source_end,
            "source_start_ms": round(source_start * 1_000),
            "source_end_ms": round(source_end * 1_000),
            "timeline_start_sec": ref.get("timeline_start_sec"),
            "timeline_end_sec": ref.get("timeline_end_sec"),
            "timeline_start_ms": (
                round(float(ref["timeline_start_sec"]) * 1_000)
                if ref.get("timeline_start_sec") is not None
                else None
            ),
            "timeline_end_ms": (
                round(float(ref["timeline_end_sec"]) * 1_000)
                if ref.get("timeline_end_sec") is not None
                else None
            ),
            "speaker": segment.speaker,
            "text": text[:240],
            "text_excerpt": text[:240],
        }

    @staticmethod
    def _rule_matches(text: str, rule: dict[str, Any]) -> bool:
        contains_any = [str(item) for item in rule.get("contains_any", [])]
        contains_all = [str(item) for item in rule.get("contains_all", [])]
        excludes = [str(item) for item in rule.get("not_contains", [])]
        if contains_any and not any(token in text for token in contains_any):
            return False
        if contains_all and not all(token in text for token in contains_all):
            return False
        return not any(token in text for token in excludes)

    def _rule_assignments(
        self,
        *,
        tagger: TaggerVersion,
        subject_type: str,
        definitions: dict[str, dict[str, Any]],
        segment_texts: list[tuple[Segment | _SnapshotSegment, str]],
        refs_by_segment: dict[int, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        transcript = "\n".join(text for _segment, text in segment_texts)
        selected: dict[str, dict[str, Any]] = {}
        rules = tagger.rule_bundle.get("rules", [])
        if not isinstance(rules, list):
            raise AssignmentValidationError("rule_bundle.rules must be a list")
        for rule in rules:
            if not isinstance(rule, dict):
                raise AssignmentValidationError("each rule must be an object")
            tag_key = str(rule.get("tag_key", ""))
            subject_types = rule.get("subject_types")
            if isinstance(subject_types, list) and subject_type not in subject_types:
                continue
            if tag_key not in definitions or not self._rule_matches(transcript, rule):
                continue
            tokens = [
                str(item) for key in ("contains_any", "contains_all") for item in rule.get(key, [])
            ]
            evidence = [
                self._segment_evidence(
                    segment,
                    text,
                    refs_by_segment.get(segment.id),
                )
                for segment, text in segment_texts
                if not tokens or any(token in text for token in tokens)
            ]
            confidence = float(rule.get("confidence", 1.0))
            assignment = {
                "tag_key": tag_key,
                "tag_value": rule.get("value"),
                "confidence": confidence,
                "evidence_refs": evidence,
                "source": "rule",
            }
            validate_assignment(
                definition=definitions[tag_key],
                label_value=assignment["tag_value"],
                confidence=confidence,
                evidence_refs=evidence,
            )
            previous = selected.get(tag_key)
            if previous is None or confidence > float(previous["confidence"]):
                selected[tag_key] = assignment
        return selected

    @staticmethod
    def _transport_segment_ids(
        segment_texts: Sequence[tuple[Segment | _SnapshotSegment, str]],
    ) -> tuple[dict[str, tuple[Segment | _SnapshotSegment, str]], dict[int, str]]:
        """Return dense transport IDs without exposing large database identifiers."""

        by_transport: dict[str, tuple[Segment | _SnapshotSegment, str]] = {}
        by_segment: dict[int, str] = {}
        for index, (segment, text) in enumerate(segment_texts):
            transport_id = f"s{index}"
            by_transport[transport_id] = (segment, text)
            by_segment[int(segment.id)] = transport_id
        return by_transport, by_segment

    @staticmethod
    def _dynamic_output_tokens(*, label_count: int, configured_cap: int) -> int:
        """Bound JSON output by the number of labels that can be emitted."""

        if label_count <= 0:
            return 0
        return output_token_budget(
            label_count,
            configured_cap=min(
                _MAX_LLM_OUTPUT_TOKENS,
                max(_MIN_LLM_OUTPUT_TOKENS, int(configured_cap)),
            ),
        )

    @staticmethod
    def _transport_definition(definition: Mapping[str, Any]) -> dict[str, Any]:
        """Project out local policy fields before sending a definition to the model."""

        projected: dict[str, Any] = {}
        for key in (
            "key",
            "name",
            "category",
            "value_type",
            "allowed_values",
        ):
            value = definition.get(key)
            if value is None or value is False or value == []:
                continue
            projected[key] = deepcopy(value)
        return projected

    @classmethod
    def _segment_batches_for_input_budget(
        cls,
        *,
        segment_texts: Sequence[tuple[Segment | _SnapshotSegment, str]],
        definitions: Mapping[str, Mapping[str, Any]],
        prompt_content: str,
        max_input_tokens: int,
        weak_candidates: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> list[list[tuple[Segment | _SnapshotSegment, str]]]:
        """Split against the final messages plus strict response Schema."""

        usable_tokens = _usable_input_tokens(max_input_tokens)
        if (
            cls._estimated_transport_input_tokens(
                prompt_content=prompt_content,
                definitions=definitions,
                segment_texts=(),
                weak_candidates=weak_candidates,
            )
            > usable_tokens
        ):
            raise AssignmentValidationError(
                "tag schema and prompt exceed the subject input token budget"
            )

        batches: list[list[tuple[Segment | _SnapshotSegment, str]]] = []
        current: list[tuple[Segment | _SnapshotSegment, str]] = []
        for segment, text in segment_texts:
            pending = text
            if not pending:
                trial = [*current, (segment, "")]
                if (
                    current
                    and cls._estimated_transport_input_tokens(
                        prompt_content=prompt_content,
                        definitions=definitions,
                        segment_texts=trial,
                        weak_candidates=weak_candidates,
                    )
                    > usable_tokens
                ):
                    batches.append(current)
                    current = [(segment, "")]
                else:
                    current = trial
                continue
            while pending:
                if current and int(current[-1][0].id) == int(segment.id):
                    batches.append(current)
                    current = []
                trial = [*current, (segment, pending)]
                if (
                    cls._estimated_transport_input_tokens(
                        prompt_content=prompt_content,
                        definitions=definitions,
                        segment_texts=trial,
                        weak_candidates=weak_candidates,
                    )
                    <= usable_tokens
                ):
                    current = trial
                    break
                if current:
                    batches.append(current)
                    current = []
                    continue
                low = 1
                high = len(pending)
                while low < high:
                    middle = (low + high + 1) // 2
                    if (
                        cls._estimated_transport_input_tokens(
                            prompt_content=prompt_content,
                            definitions=definitions,
                            segment_texts=((segment, pending[:middle]),),
                            weak_candidates=weak_candidates,
                        )
                        <= usable_tokens
                    ):
                        low = middle
                    else:
                        high = middle - 1
                if (
                    cls._estimated_transport_input_tokens(
                        prompt_content=prompt_content,
                        definitions=definitions,
                        segment_texts=((segment, pending[:low]),),
                        weak_candidates=weak_candidates,
                    )
                    > usable_tokens
                ):
                    raise AssignmentValidationError(
                        "one segment cannot fit the subject input token budget"
                    )
                batches.append([(segment, pending[:low])])
                pending = pending[low:]
        if current:
            batches.append(current)
        return batches or [[]]

    @staticmethod
    def _usage_tokens(response: LLMResponse) -> tuple[int, int]:
        def _value(*keys: str) -> int:
            for key in keys:
                value = response.usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    return value
            return 0

        return (
            _value("prompt_tokens", "input_tokens"),
            _value("completion_tokens", "output_tokens"),
        )

    @staticmethod
    def _provider_attempt_bound(adapter: LLMAdapter) -> int:
        attempts = getattr(adapter, "max_provider_attempts", 1)
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
            raise AssignmentValidationError(
                "LLM gateway returned an invalid provider-attempt bound"
            )
        return attempts

    @classmethod
    def _estimate_provider_cost(
        cls,
        adapter: LLMAdapter,
        *,
        input_tokens: int,
        output_tokens: int,
    ) -> int | None:
        estimator = getattr(adapter, "estimate_cost_microunits", None)
        if not callable(estimator):
            return None
        value = estimator(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise AssignmentValidationError("LLM price estimator returned an invalid cost")
        return value * cls._provider_attempt_bound(adapter)

    @classmethod
    def _estimate_provider_cost_for_token_budget(
        cls,
        adapter: LLMAdapter,
        *,
        total_tokens: int,
    ) -> int | None:
        """Price an unsplit repair budget at the more expensive token rate."""

        input_cost = cls._estimate_provider_cost(
            adapter,
            input_tokens=total_tokens,
            output_tokens=0,
        )
        output_cost = cls._estimate_provider_cost(
            adapter,
            input_tokens=0,
            output_tokens=total_tokens,
        )
        if input_cost is None or output_cost is None:
            return None
        return max(input_cost, output_cost)

    @staticmethod
    def _price_usage_cost(
        adapter: LLMAdapter,
        usage: Mapping[str, int],
    ) -> int | None:
        pricer = getattr(adapter, "price_usage_cost_microunits", None)
        if not callable(pricer):
            return None
        try:
            value = pricer(usage)
        except RuntimeError:
            return None
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise AssignmentValidationError("LLM usage pricer returned an invalid cost")
        return value

    @staticmethod
    def _tag_value_response_schema(
        definitions: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        value_schemas: list[dict[str, Any]] = []
        seen: set[str] = set()
        for definition in definitions.values():
            value_type = str(definition.get("value_type", "string"))
            schema: dict[str, Any]
            if value_type == "enum":
                schema = {"enum": list(definition.get("allowed_values") or [])}
            elif value_type == "number":
                schema = {"type": "number"}
            elif value_type == "boolean":
                schema = {"type": "boolean"}
            else:
                schema = {"type": "string"}
            identity = json.dumps(schema, sort_keys=True, ensure_ascii=False)
            if identity not in seen:
                seen.add(identity)
                value_schemas.append(schema)
        if len(value_schemas) == 1:
            return value_schemas[0]
        return {"anyOf": value_schemas}

    @classmethod
    def _assignment_item_response_schema(
        cls,
        *,
        tag_key: str,
        definition: Mapping[str, Any],
        allowed_transport_ids: Sequence[str],
        evidence_schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved_evidence_schema = (
            deepcopy(dict(evidence_schema))
            if evidence_schema is not None
            else {
                "type": "array",
                "uniqueItems": True,
                "maxItems": min(
                    len(allowed_transport_ids),
                    _MAX_EVIDENCE_SEGMENTS_PER_ASSIGNMENT,
                ),
                "items": {
                    "type": "string",
                    "enum": list(allowed_transport_ids),
                },
                **({"minItems": 1} if bool(definition.get("evidence_required")) else {}),
            }
        )
        return {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "tag_key",
                "tag_value",
                "confidence",
                "evidence_segment_ids",
            ],
            "properties": {
                "tag_key": {
                    "type": "string",
                    "const": tag_key,
                },
                "tag_value": cls._tag_value_response_schema({tag_key: definition}),
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                "evidence_segment_ids": resolved_evidence_schema,
            },
        }

    @classmethod
    def _transport_contract(
        cls,
        *,
        definitions: Mapping[str, Mapping[str, Any]],
        segment_texts: Sequence[tuple[Segment | _SnapshotSegment, str]],
        weak_candidates: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        by_transport, transport_by_segment = cls._transport_segment_ids(segment_texts)
        user_payload: dict[str, Any] = {
            "schema": [
                cls._transport_definition(definition) for definition in definitions.values()
            ],
            "segments": [
                {
                    "id": transport_id,
                    "speaker": segment.speaker,
                    "start_ms": round(float(segment.start_sec) * 1_000),
                    "end_ms": round(float(segment.end_sec) * 1_000),
                    "text": text,
                }
                for transport_id, (segment, text) in by_transport.items()
            ],
        }
        if weak_candidates is not None:
            user_payload["weak_candidates"] = [
                {
                    "tag_key": key,
                    "tag_value": candidate.get("tag_value"),
                    "confidence": candidate.get("confidence"),
                    "evidence_segment_ids": [
                        transport_by_segment[int(reference["segment_id"])]
                        for reference in candidate.get("evidence_refs", [])
                        if (
                            isinstance(reference, Mapping)
                            and reference.get("segment_id") is not None
                            and int(reference["segment_id"]) in transport_by_segment
                        )
                    ],
                }
                for key, candidate in sorted(weak_candidates.items())
                if key in definitions
            ]
        allowed_transport_ids = list(by_transport)
        shared_evidence_schemas: dict[str, dict[str, Any]] = {}
        if len(definitions) > 1:
            for required in sorted(
                {bool(definition.get("evidence_required")) for definition in definitions.values()}
            ):
                key = "evidence_required" if required else "evidence_optional"
                shared_evidence_schemas[key] = {
                    "type": "array",
                    "uniqueItems": True,
                    "maxItems": min(
                        len(allowed_transport_ids),
                        _MAX_EVIDENCE_SEGMENTS_PER_ASSIGNMENT,
                    ),
                    "items": {
                        "type": "string",
                        "enum": list(allowed_transport_ids),
                    },
                    **({"minItems": 1} if required else {}),
                }
        assignment_branches = []
        for tag_key, definition in sorted(definitions.items()):
            required = bool(definition.get("evidence_required"))
            assignment_branches.append(
                cls._assignment_item_response_schema(
                    tag_key=tag_key,
                    definition=definition,
                    allowed_transport_ids=allowed_transport_ids,
                    evidence_schema=(
                        {
                            "$ref": (
                                "#/$defs/evidence_required"
                                if required
                                else "#/$defs/evidence_optional"
                            )
                        }
                        if shared_evidence_schemas
                        else None
                    ),
                )
            )
        assignment_schema: dict[str, Any] = {
            "type": "object",
            "additionalProperties": False,
            "required": ["assignments"],
            "properties": {
                "assignments": {
                    "type": "array",
                    "maxItems": len(definitions),
                    "items": (
                        assignment_branches[0]
                        if len(assignment_branches) == 1
                        else {"anyOf": assignment_branches}
                    ),
                }
            },
        }
        if shared_evidence_schemas:
            assignment_schema["$defs"] = shared_evidence_schemas
        return user_payload, assignment_schema

    @classmethod
    def _estimated_transport_input_tokens(
        cls,
        *,
        prompt_content: str,
        definitions: Mapping[str, Mapping[str, Any]],
        segment_texts: Sequence[tuple[Segment | _SnapshotSegment, str]],
        weak_candidates: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> int:
        payload, response_schema = cls._transport_contract(
            definitions=definitions,
            segment_texts=segment_texts,
            weak_candidates=weak_candidates,
        )
        return (
            estimate_prompt_tokens(TagExtractor._system_prompt(prompt_content))
            + estimate_prompt_tokens(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            + estimate_prompt_tokens(
                json.dumps(
                    response_schema,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            + 32
        )

    @staticmethod
    def _system_prompt(prompt_content: str) -> str:
        stable_contract = (
            "任务：根据 schema 与 segments 判定有文本依据的标签。"
            "证据只能引用给定的短 segment id；不成立或不确定的标签省略。"
            "严格按 response schema 输出，不复述输入。"
        )
        normalized_policy = prompt_content.strip()
        if not normalized_policy:
            return stable_contract
        return f"{stable_contract}\n标签语义与判定规则：\n{normalized_policy}"

    def _parse_llm_assignments(
        self,
        text: str,
        *,
        definitions: dict[str, dict[str, Any]],
        segment_texts: list[tuple[Segment | _SnapshotSegment, str]],
        refs_by_segment: dict[int, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Strictly parse and validate one structured assignment response."""

        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise _TagOutputFormatError("LLM returned invalid JSON") from exc
        if not isinstance(payload, dict) or set(payload) != {"assignments"}:
            raise _TagOutputFormatError("LLM JSON must contain only assignments[]")
        raw_assignments = payload["assignments"]
        if not isinstance(raw_assignments, list):
            raise _TagOutputFormatError("LLM JSON must contain assignments[]")

        by_transport, _transport_by_segment = self._transport_segment_ids(segment_texts)
        output: dict[str, dict[str, Any]] = {}
        expected_keys = {
            "tag_key",
            "tag_value",
            "confidence",
            "evidence_segment_ids",
        }
        for raw in raw_assignments:
            if not isinstance(raw, dict) or set(raw) != expected_keys:
                raise _TagOutputFormatError(
                    "each LLM assignment must match the strict output schema"
                )
            tag_key = raw["tag_key"]
            if not isinstance(tag_key, str) or tag_key not in definitions:
                raise AssignmentValidationError("LLM assignment references an unknown tag")
            if tag_key in output:
                raise AssignmentValidationError("LLM returned duplicate tag assignments")

            raw_evidence_ids = raw["evidence_segment_ids"]
            if not isinstance(raw_evidence_ids, list) or any(
                not isinstance(transport_id, str) for transport_id in raw_evidence_ids
            ):
                raise _TagEvidenceOutputError("evidence_segment_ids must be compact string[]")
            if len(raw_evidence_ids) != len(set(raw_evidence_ids)):
                raise _TagEvidenceOutputError("evidence_segment_ids must not contain duplicates")
            if len(raw_evidence_ids) > _MAX_EVIDENCE_SEGMENTS_PER_ASSIGNMENT:
                raise _TagEvidenceOutputError(
                    "evidence_segment_ids exceeds the bounded evidence limit"
                )
            if any(transport_id not in by_transport for transport_id in raw_evidence_ids):
                raise _TagEvidenceOutputError("LLM assignment references unknown segment evidence")

            raw_confidence = raw["confidence"]
            if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
                raise AssignmentValidationError("LLM assignment confidence must be numeric")
            confidence = float(raw_confidence)
            if not math.isfinite(confidence):
                raise AssignmentValidationError("LLM assignment confidence must be finite")
            evidence = [
                self._segment_evidence(
                    by_transport[transport_id][0],
                    by_transport[transport_id][1],
                    refs_by_segment.get(int(by_transport[transport_id][0].id)),
                )
                for transport_id in raw_evidence_ids
            ]
            validate_assignment(
                definition=definitions[tag_key],
                label_value=raw["tag_value"],
                confidence=confidence,
                evidence_refs=evidence,
            )
            output[tag_key] = {
                "tag_key": tag_key,
                "tag_value": raw["tag_value"],
                "confidence": confidence,
                "evidence_refs": evidence,
                "source": "llm",
            }
        return output

    async def _llm_assignments(
        self,
        *,
        adapter: LLMAdapter,
        model_tier: str,
        tenant_id: str,
        unit: DialogueUnit | _PredictionSubject,
        subject_type: str,
        schema: TagSchemaVersion,
        tagger: TaggerVersion,
        definitions: dict[str, dict[str, Any]],
        transcript: str,
        segment_texts: list[tuple[Segment | _SnapshotSegment, str]],
        refs_by_segment: dict[int, dict[str, Any]],
        input_hash: str,
        input_snapshot: dict[str, Any],
        prompt_content: str,
        max_tokens: int,
        usage_context: LLMUsageContext | None = None,
        weak_candidates: Mapping[str, Mapping[str, Any]] | None = None,
        allow_format_repair: bool = True,
        repair_budget_reserver: Callable[[int], None] | None = None,
    ) -> LLMAssignmentBatch:
        if not definitions:
            raise AssignmentValidationError("LLM assignments require at least one tag definition")
        user_payload, assignment_schema = self._transport_contract(
            definitions=definitions,
            segment_texts=segment_texts,
            weak_candidates=weak_candidates,
        )
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self._system_prompt(prompt_content)},
            {
                "role": "user",
                "content": json.dumps(
                    user_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ]
        allowed_segment_ids = [segment.id for segment, _text in segment_texts]
        recording_ids = sorted(
            {
                *(segment.recording_id for segment, _text in segment_texts),
                *([unit.source_recording_id] if unit.source_recording_id is not None else []),
            }
        )
        provenance: list[LLMProvenance] = [
            LLMProvenance(subject_type, str(unit.id)),
        ]
        if subject_type != "reception":
            provenance.append(LLMProvenance("reception", str(unit.reception_id)))
        provenance.extend(
            LLMProvenance("recording", str(recording_id)) for recording_id in recording_ids
        )
        provenance.extend(
            LLMProvenance("segment", str(segment_id)) for segment_id in allowed_segment_ids
        )
        aggregation_lineage = input_snapshot.get("transport_aggregation")
        if isinstance(aggregation_lineage, Mapping):
            raw_units = aggregation_lineage.get("dialogue_units")
            if isinstance(raw_units, list):
                for raw_unit in raw_units:
                    if not isinstance(raw_unit, Mapping):
                        continue
                    dialogue_unit_id = raw_unit.get("dialogue_unit_id")
                    if isinstance(dialogue_unit_id, int) and not isinstance(dialogue_unit_id, bool):
                        provenance.append(LLMProvenance("dialogue_unit", str(dialogue_unit_id)))
                    raw_facts = raw_unit.get("facts")
                    if not isinstance(raw_facts, list):
                        continue
                    provenance.extend(
                        LLMProvenance(
                            "tag_assignment_fact",
                            str(raw_fact["fact_id"]),
                        )
                        for raw_fact in raw_facts
                        if isinstance(raw_fact, Mapping)
                        and isinstance(raw_fact.get("fact_id"), int)
                        and not isinstance(raw_fact.get("fact_id"), bool)
                    )
        provenance = list(dict.fromkeys(provenance))
        business_snapshot = {
            key: value for key, value in input_snapshot.items() if key != "transcript"
        }

        def _valid_assignments(response: LLMResponse) -> bool:
            try:
                self._parse_llm_assignments(
                    response.text,
                    definitions=definitions,
                    segment_texts=segment_texts,
                    refs_by_segment=refs_by_segment,
                )
            except (AssignmentValidationError, TypeError, ValueError):
                return False
            return True

        request = LLMRequest(
            tenant_id=tenant_id,
            purpose="dialogue_tag_assignments",
            model_tier=cast(Any, model_tier),
            provider=str(getattr(adapter, "provider", "openai-compatible")),
            model_epoch=str(
                getattr(
                    adapter,
                    "model_epoch",
                    getattr(adapter, "model", tagger.model_version),
                )
            ),
            messages=messages,
            prompt_version=(
                f"{_TAG_TRANSPORT_PROMPT_VERSION}:"
                f"tagger:{tagger.id}:{tagger.version}:{tagger.config_checksum}"
            ),
            schema_version=f"tag-schema:{schema.id}:{schema.checksum}",
            parser_version=_TAG_ASSIGNMENT_PARSER_VERSION,
            postprocessor_version=_TAG_ASSIGNMENT_POSTPROCESSOR_VERSION,
            temperature=0,
            top_p=1.0,
            max_tokens=max_tokens,
            response_format={
                "type": "json_schema",
                "name": "tag_assignments_v2",
                "description": (
                    "Return only assignments for supplied tag keys and cite only supplied "
                    "compact segment IDs."
                ),
            },
            response_schema=assignment_schema,
            business_snapshot={
                "input_hash": input_hash,
                **business_snapshot,
            },
            permission_scope={
                "tenant_id": tenant_id,
                "access_class": "canonical_tagging_worker",
            },
            provenance=tuple(provenance),
            cache_policy=CachePolicy.EXACT,
            ttl_seconds=_TAG_ASSIGNMENT_TTL_SECONDS,
            usage_context=replace(
                usage_context or LLMUsageContext(),
                tagger_version_id=tagger.id,
                optimization_run_id=(
                    (usage_context.optimization_run_id if usage_context else None)
                    or tagger.optimization_run_id
                ),
            ),
            response_validator=_valid_assignments,
        )
        responses = [await execute_llm(adapter, request)]
        try:
            parsed_assignments = self._parse_llm_assignments(
                responses[0].text,
                definitions=definitions,
                segment_texts=segment_texts,
                refs_by_segment=refs_by_segment,
            )
        except _TagOutputFormatError:
            if not allow_format_repair:
                raise
            repair_messages = (
                {
                    "role": "system",
                    "content": (
                        "Repair JSON syntax and shape only. Preserve proposed values; "
                        "return only the schema-valid JSON object."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "invalid_output": responses[0].text,
                            "response_schema": assignment_schema,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            )
            repair_max_tokens = min(512, max_tokens)
            if repair_budget_reserver is not None:
                repair_budget_reserver(
                    sum(
                        estimate_prompt_tokens(str(message["content"]))
                        for message in repair_messages
                    )
                    + estimate_prompt_tokens(
                        json.dumps(
                            assignment_schema,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
                    + repair_max_tokens
                    + 32
                )
            repair_request = replace(
                request,
                purpose="dialogue_tag_assignments_repair",
                messages=repair_messages,
                prompt_version=f"{request.prompt_version}:format-repair-v1",
                max_tokens=repair_max_tokens,
                business_snapshot={
                    "invalid_output_sha256": canonical_checksum(responses[0].text),
                },
                cache_policy=CachePolicy.BYPASS,
            )
            responses.append(await execute_llm(adapter, repair_request))
            parsed_assignments = self._parse_llm_assignments(
                responses[-1].text,
                definitions=definitions,
                segment_texts=segment_texts,
                refs_by_segment=refs_by_segment,
            )

        provider_input_tokens = 0
        provider_output_tokens = 0
        reused_input_tokens = 0
        reused_output_tokens = 0
        provider_calls = 0
        cache_hits = 0
        for response in responses:
            input_tokens, output_tokens = self._usage_tokens(response)
            provider_called = bool(response.provider_called and not response.cached)
            if provider_called:
                provider_input_tokens += input_tokens
                provider_output_tokens += output_tokens
                provider_calls += max(1, int(response.provider_attempts))
            else:
                reused_input_tokens += input_tokens
                reused_output_tokens += output_tokens
                cache_hits += 1
        cost_microunits = sum(
            response.cost_microunits
            for response in responses
            if response.provider_called and not response.cached
        )
        counterfactual_saved_cost_microunits = sum(
            self._price_usage_cost(adapter, response.usage) or 0
            for response in responses
            if not response.provider_called or response.cached
        )
        unknown_billed_tokens = sum(
            max(0, int(response.unknown_billed_tokens))
            for response in responses
            if response.provider_called and not response.cached
        )
        final_response = responses[-1]
        return LLMAssignmentBatch(
            assignments=parsed_assignments,
            model_tier=model_tier,
            model=final_response.model,
            token_count=provider_input_tokens + provider_output_tokens,
            cached=all(response.cached for response in responses),
            provider_input_tokens=provider_input_tokens,
            provider_output_tokens=provider_output_tokens,
            reused_input_tokens=reused_input_tokens,
            reused_output_tokens=reused_output_tokens,
            provider_calls=provider_calls,
            cache_hits=cache_hits,
            cost_microunits=cost_microunits,
            counterfactual_saved_cost_microunits=(counterfactual_saved_cost_microunits),
            unknown_billed_tokens=unknown_billed_tokens,
        )

    @staticmethod
    def _threshold_for(
        tagger: TaggerVersion,
        definitions: dict[str, dict[str, Any]],
        key: str,
        harness_spec: dict[str, Any] | None = None,
        subject_type: str | None = None,
    ) -> float:
        configured_thresholds = (
            harness_spec.get("output", {}).get("thresholds", {})
            if harness_spec is not None
            else tagger.thresholds
        )
        configured = (
            configured_thresholds.get(f"{subject_type}:{key}") if subject_type is not None else None
        )
        if configured is None:
            configured = configured_thresholds.get(key)
        if configured is None:
            configured = configured_thresholds.get("default")
        if configured is None:
            configured = definitions[key].get("threshold", 0.7)
        return float(configured)

    def _can_short_circuit_hybrid(
        self,
        *,
        tagger: TaggerVersion,
        subject_type: str,
        definitions: dict[str, dict[str, Any]],
        rule_results: dict[str, dict[str, Any]],
    ) -> bool:
        """Return whether qualified rules fully determine a hybrid result.

        This optimisation is intentionally opt-in. Critical definitions always
        retain the LLM comparison path, because removing it would also remove
        conflict-review evidence.
        """

        if not self._enable_hybrid_rule_short_circuit or tagger.engine != "hybrid":
            return False
        if not definitions or any(
            bool(item.get("critical")) or bool(item.get("critical_values"))
            for item in definitions.values()
        ):
            return False
        if set(rule_results) != set(definitions):
            return False
        return all(
            float(rule_results[key]["confidence"])
            >= self._threshold_for(
                tagger,
                definitions,
                key,
                subject_type=subject_type,
            )
            for key in definitions
        )

    @staticmethod
    def _dependency_order(
        assignments: tuple[dict[str, Any], ...],
        definitions: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Preflight one prediction batch and order dependencies before any write."""

        by_key = {str(item["tag_key"]): item for item in assignments}
        for key in by_key:
            exclusives = {
                str(value) for value in definitions[key].get("mutually_exclusive_with", [])
            }
            conflict = exclusives.intersection(by_key)
            if conflict:
                raise AssignmentValidationError(
                    f"prediction contains mutually exclusive tags: {key}, "
                    + ", ".join(sorted(conflict))
                )
        visiting: set[str] = set()
        visited: set[str] = set()
        ordered: list[dict[str, Any]] = []

        def visit(key: str) -> None:
            if key in visiting:
                raise AssignmentValidationError("prediction tag dependencies contain a cycle")
            if key in visited:
                return
            visiting.add(key)
            for dependency in definitions[key].get("depends_on", []):
                dependency_key = str(dependency)
                if dependency_key in by_key:
                    visit(dependency_key)
            visiting.remove(key)
            visited.add(key)
            ordered.append(by_key[key])

        for key in by_key:
            visit(key)
        return ordered

    async def _prepare_dialogue_unit(
        self,
        *,
        tenant_id: str,
        dialogue_unit_id: int,
        tagger_version_id: int,
        target_tag_keys: Sequence[str] | None = None,
    ) -> PreparedDialogueInput:
        """Build the complete content-addressed input without calling an LLM."""
        (
            unit,
            tagger,
            schema,
            scenario,
            store_id,
            segments,
            refs_by_segment,
        ) = await self._load_input(
            tenant_id=tenant_id,
            dialogue_unit_id=dialogue_unit_id,
            tagger_version_id=tagger_version_id,
        )
        segment_texts = tuple(
            (segment, scrubbed_segment_text(segment.text_scrubbed, segment.transcript))
            for segment in segments
        )
        transcript = "\n".join(text for _segment, text in segment_texts)
        applicable_definitions = self._definitions_for_subject(
            schema=schema,
            subject_type="dialogue_unit",
            scenario=scenario,
        )
        definitions, effective_target_keys = self._targeted_definitions(
            schema=schema,
            applicable=applicable_definitions,
            target_tag_keys=target_tag_keys,
        )
        segment_snapshot = [
            {
                "segment_id": segment.id,
                "recording_id": segment.recording_id,
                "version": segment.updated_at.isoformat(),
                "start_sec": float(segment.start_sec),
                "end_sec": float(segment.end_sec),
                "speaker": segment.speaker,
                "vad_confidence": segment.vad_conf,
                "reference": refs_by_segment.get(segment.id, {}),
                "text": text,
                "text_hash": compute_input_hash(
                    transcript=text,
                    segment_snapshot=[],
                    dialogue_unit_version=unit.version,
                    schema_checksum=schema.checksum,
                    tagger_checksum=tagger.config_checksum,
                    model_version=tagger.model_version,
                ),
            }
            for segment, text in segment_texts
        ]
        input_hash = compute_input_hash(
            transcript=transcript,
            segment_snapshot=[
                {key: value for key, value in item.items() if key != "text"}
                for item in segment_snapshot
            ],
            dialogue_unit_version=unit.version,
            schema_checksum=schema.checksum,
            tagger_checksum=tagger.config_checksum,
            model_version=tagger.model_version,
            context_snapshot={
                "subject_type": "dialogue_unit",
                "subject_id": unit.id,
                "reception_id": unit.reception_id,
                "scenario": scenario,
                "store_id": store_id,
                "target_tag_keys": list(effective_target_keys),
            },
        )
        input_snapshot = {
            "subject_type": "dialogue_unit",
            "subject_id": unit.id,
            "dialogue_unit_id": unit.id,
            "dialogue_unit_version": unit.version,
            "reception_id": unit.reception_id,
            "scenario": scenario,
            "store_id": store_id,
            "segments": segment_snapshot,
            "transcript": transcript,
            "schema_version_id": schema.id,
            "schema_checksum": schema.checksum,
            "tagger_version_id": tagger.id,
            "tagger_checksum": tagger.config_checksum,
            "model_version": tagger.model_version,
            "target_tag_keys": list(effective_target_keys),
        }
        return PreparedDialogueInput(
            unit=unit,
            subject_type="dialogue_unit",
            tagger=tagger,
            schema=schema,
            scenario=scenario,
            segment_texts=segment_texts,
            refs_by_segment=refs_by_segment,
            transcript=transcript,
            input_hash=input_hash,
            input_snapshot=input_snapshot,
            definitions=definitions,
        )

    async def _load_tagger_schema(
        self,
        *,
        tenant_id: str,
        tagger_version_id: int,
    ) -> tuple[TaggerVersion, TagSchemaVersion]:
        """Load only immutable Harness configuration, never business input."""

        async with self._factory() as session:
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
            schema = (
                await session.execute(
                    select(TagSchemaVersion).where(
                        TagSchemaVersion.id == tagger.schema_version_id,
                        TagSchemaVersion.tenant_id == tenant_id,
                        TagSchemaVersion.status.in_(["published", "deprecated"]),
                    )
                )
            ).scalar_one_or_none()
            if schema is None:
                raise AssignmentValidationError(
                    "tagger must reference an immutable published schema version"
                )
            return tagger, schema

    @staticmethod
    def _definitions_for_subject(
        *,
        schema: TagSchemaVersion,
        subject_type: str,
        scenario: str,
    ) -> dict[str, dict[str, Any]]:
        return {
            str(item["key"]): item
            for item in schema.definitions
            if isinstance(item, dict)
            and item.get("key")
            and subject_type in (item.get("subject_types") or [])
            and (not item.get("scenarios") or scenario in item.get("scenarios", []))
        }

    @staticmethod
    def _targeted_definitions(
        *,
        schema: TagSchemaVersion,
        applicable: dict[str, dict[str, Any]],
        target_tag_keys: Sequence[str] | None,
    ) -> tuple[dict[str, dict[str, Any]], tuple[str, ...]]:
        """Validate a requested schema subset and return its applicable domain."""

        if target_tag_keys is None:
            selected = dict(applicable)
            return selected, tuple(sorted(selected))
        if isinstance(target_tag_keys, str | bytes):
            raise AssignmentValidationError("target_tag_keys must be a sequence of tag keys")
        normalized: set[str] = set()
        for raw_key in target_tag_keys:
            key = str(raw_key).strip()
            if not key:
                raise AssignmentValidationError("target_tag_keys must not contain empty keys")
            normalized.add(key)
        schema_keys = {
            str(item["key"])
            for item in schema.definitions
            if isinstance(item, dict) and item.get("key")
        }
        unknown = sorted(normalized - schema_keys)
        if unknown:
            raise AssignmentValidationError(
                f"target_tag_keys contains unknown schema keys: {', '.join(unknown)}"
            )
        selected = {key: definition for key, definition in applicable.items() if key in normalized}
        return selected, tuple(sorted(selected))

    @staticmethod
    def _hashable_segment_snapshot(
        segments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Exclude replay-only cleartext while retaining its content hash."""

        return [
            {key: value for key, value in item.items() if key not in {"text", "text_scrubbed"}}
            for item in segments
        ]

    @staticmethod
    def _reception_unit_segment_ids(
        *,
        unit: DialogueUnit,
        segment_texts: Sequence[tuple[Segment, str]],
        refs_by_segment: Mapping[int, Mapping[str, Any]],
    ) -> tuple[int, ...]:
        """Resolve one unit to reception-owned, real Segment identifiers."""

        ordered_ids = [int(segment.id) for segment, _text in segment_texts]
        allowed_ids = set(ordered_ids)
        referenced_ids = {
            int(reference["segment_id"])
            for reference in unit.segment_refs
            if isinstance(reference, Mapping)
            and isinstance(reference.get("segment_id"), int)
            and not isinstance(reference.get("segment_id"), bool)
            and int(reference["segment_id"]) in allowed_ids
        }
        if referenced_ids:
            return tuple(segment_id for segment_id in ordered_ids if segment_id in referenced_ids)

        resolved: list[int] = []
        for segment, _text in segment_texts:
            reference = refs_by_segment.get(int(segment.id), {})
            timeline_start = reference.get("timeline_start_sec")
            timeline_end = reference.get("timeline_end_sec")
            if isinstance(timeline_start, int | float) and isinstance(timeline_end, int | float):
                overlaps = float(timeline_start) < float(unit.end_sec) and float(
                    timeline_end
                ) > float(unit.start_sec)
            else:
                overlaps = (
                    unit.source_recording_id == segment.recording_id
                    and float(segment.start_sec) < float(unit.end_sec)
                    and float(segment.end_sec) > float(unit.start_sec)
                )
            if overlaps:
                resolved.append(int(segment.id))
        return tuple(resolved)

    @classmethod
    def _cached_dialogue_run_matches_reception_input(
        cls,
        *,
        run: TagExtractionRun,
        unit: DialogueUnit,
        tagger: TaggerVersion,
        schema: TagSchemaVersion,
        segment_texts: Sequence[tuple[Segment, str]],
        refs_by_segment: Mapping[int, Mapping[str, Any]],
    ) -> bool:
        """Reject cached unit facts when their immutable source snapshot is stale."""

        snapshot = run.input_snapshot
        if (
            snapshot.get("dialogue_unit_id", snapshot.get("subject_id")) != unit.id
            or snapshot.get("dialogue_unit_version") != unit.version
            or snapshot.get("reception_id") != unit.reception_id
            or snapshot.get("schema_version_id") != schema.id
            or snapshot.get("schema_checksum") != schema.checksum
            or snapshot.get("tagger_version_id") != tagger.id
            or snapshot.get("tagger_checksum") != tagger.config_checksum
        ):
            return False
        raw_segments = snapshot.get("segments")
        if not isinstance(raw_segments, list) or any(
            not isinstance(item, Mapping) for item in raw_segments
        ):
            return False
        expected_ids = cls._reception_unit_segment_ids(
            unit=unit,
            segment_texts=segment_texts,
            refs_by_segment=refs_by_segment,
        )
        if len(raw_segments) != len(expected_ids):
            return False
        current_by_id = {
            int(segment.id): (
                segment,
                text,
            )
            for segment, text in segment_texts
        }
        for raw, expected_id in zip(raw_segments, expected_ids, strict=True):
            segment_id = raw.get("segment_id", raw.get("id"))
            if segment_id != expected_id or expected_id not in current_by_id:
                return False
            segment, text = current_by_id[expected_id]
            if (
                raw.get("version") != segment.updated_at.isoformat()
                or raw.get("text", raw.get("text_scrubbed")) != text
            ):
                return False
        return snapshot.get("transcript") == "\n".join(
            current_by_id[segment_id][1] for segment_id in expected_ids
        )

    async def _aggregate_reception_fact_transport(
        self,
        *,
        tenant_id: str,
        reception_id: int,
        tagger: TaggerVersion,
        schema: TagSchemaVersion,
        segment_texts: tuple[tuple[Segment, str], ...],
        refs_by_segment: Mapping[int, Mapping[str, Any]],
        source_reception_input_hash: str,
    ) -> tuple[tuple[tuple[Segment, str], ...], dict[str, Any]] | None:
        """Build a compact fact-first transport without changing source lineage."""

        async with self._factory() as session:
            raw_units = list(
                (
                    await session.execute(
                        select(DialogueUnit)
                        .where(
                            DialogueUnit.tenant_id == tenant_id,
                            DialogueUnit.reception_id == reception_id,
                        )
                        .order_by(
                            DialogueUnit.unit_index,
                            DialogueUnit.version.desc(),
                            DialogueUnit.id.desc(),
                        )
                    )
                )
                .scalars()
                .all()
            )
            units: list[DialogueUnit] = []
            seen_indexes: set[int] = set()
            for unit in raw_units:
                if unit.unit_index in seen_indexes:
                    continue
                seen_indexes.add(unit.unit_index)
                units.append(unit)
            if not units:
                return None

            unit_ids = [int(unit.id) for unit in units]
            current_facts = list(
                (
                    await session.execute(
                        select(TagAssignmentFact)
                        .join(
                            TagAssignmentCurrent,
                            TagAssignmentCurrent.fact_id == TagAssignmentFact.id,
                        )
                        .where(
                            TagAssignmentCurrent.tenant_id == tenant_id,
                            TagAssignmentCurrent.subject_type == "dialogue_unit",
                            TagAssignmentCurrent.subject_id.in_(unit_ids),
                            TagAssignmentFact.tenant_id == tenant_id,
                            TagAssignmentFact.subject_type == "dialogue_unit",
                            TagAssignmentFact.subject_id == TagAssignmentCurrent.subject_id,
                            TagAssignmentFact.schema_version_id == schema.id,
                            TagAssignmentFact.tombstone.is_(False),
                        )
                    )
                )
                .scalars()
                .all()
            )
            selected_runs: dict[int, TagExtractionRun] = {}
            cached_runs = list(
                (
                    await session.execute(
                        select(TagExtractionRun)
                        .where(
                            TagExtractionRun.tenant_id == tenant_id,
                            TagExtractionRun.subject_type == "dialogue_unit",
                            TagExtractionRun.subject_id.in_(unit_ids),
                            TagExtractionRun.tagger_version_id == tagger.id,
                            TagExtractionRun.status.in_(("completed", "cached")),
                        )
                        .order_by(
                            TagExtractionRun.subject_id,
                            TagExtractionRun.finished_at.desc(),
                            TagExtractionRun.id.desc(),
                        )
                    )
                )
                .scalars()
                .all()
            )
            unit_by_id = {int(unit.id): unit for unit in units}
            for run in cached_runs:
                unit_id = int(run.subject_id)
                if unit_id in selected_runs:
                    continue
                unit = unit_by_id[unit_id]
                if self._cached_dialogue_run_matches_reception_input(
                    run=run,
                    unit=unit,
                    tagger=tagger,
                    schema=schema,
                    segment_texts=segment_texts,
                    refs_by_segment=refs_by_segment,
                ):
                    selected_runs[unit_id] = run
            cached_facts = (
                list(
                    (
                        await session.execute(
                            select(TagAssignmentFact).where(
                                TagAssignmentFact.tenant_id == tenant_id,
                                TagAssignmentFact.subject_type == "dialogue_unit",
                                TagAssignmentFact.subject_id.in_(unit_ids),
                                TagAssignmentFact.schema_version_id == schema.id,
                                TagAssignmentFact.tagger_version_id == tagger.id,
                                TagAssignmentFact.extraction_run_id.in_(
                                    [run.id for run in selected_runs.values()]
                                ),
                                TagAssignmentFact.tombstone.is_(False),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if selected_runs
                else []
            )

            fact_context: dict[tuple[int, str], tuple[dict[str, Any], str]] = {}
            for fact in current_facts:
                fact_context[(int(fact.subject_id), str(fact.tag_key))] = (
                    {
                        "fact_id": int(fact.id),
                        "tag_key": str(fact.tag_key),
                        "tag_value": deepcopy(fact.tag_value),
                        "confidence": fact.confidence,
                        "input_hash": str(fact.input_hash),
                        "recipe_hash": str(fact.recipe_hash),
                        "extraction_run_id": fact.extraction_run_id,
                        "tagger_version_id": fact.tagger_version_id,
                        "schema_version_id": fact.schema_version_id,
                        "evidence_refs": deepcopy(fact.evidence_refs),
                    },
                    "current",
                )
            for fact in cached_facts:
                selected_run = selected_runs.get(int(fact.subject_id))
                if (
                    selected_run is None
                    or fact.extraction_run_id != selected_run.id
                    or fact.input_hash != selected_run.input_hash
                ):
                    continue
                fact_context.setdefault(
                    (int(fact.subject_id), str(fact.tag_key)),
                    (
                        {
                            "fact_id": int(fact.id),
                            "tag_key": str(fact.tag_key),
                            "tag_value": deepcopy(fact.tag_value),
                            "confidence": fact.confidence,
                            "input_hash": str(fact.input_hash),
                            "recipe_hash": str(fact.recipe_hash),
                            "extraction_run_id": fact.extraction_run_id,
                            "tagger_version_id": fact.tagger_version_id,
                            "schema_version_id": fact.schema_version_id,
                            "evidence_refs": deepcopy(fact.evidence_refs),
                        },
                        "cached",
                    ),
                )

        contexts_by_segment: dict[int, list[str]] = {}
        lineage: list[dict[str, Any]] = []
        for unit in units:
            unit_segment_ids = self._reception_unit_segment_ids(
                unit=unit,
                segment_texts=segment_texts,
                refs_by_segment=refs_by_segment,
            )
            facts = [
                (fact, origin)
                for (unit_id, _tag_key), (fact, origin) in sorted(
                    fact_context.items(),
                    key=lambda item: (item[0][0], item[0][1]),
                )
                if unit_id == unit.id
            ]
            preferred_evidence_ids = [
                int(reference["segment_id"])
                for fact, _origin in facts
                for reference in fact["evidence_refs"]
                if isinstance(reference, Mapping)
                and isinstance(reference.get("segment_id"), int)
                and not isinstance(reference.get("segment_id"), bool)
                and int(reference["segment_id"]) in unit_segment_ids
            ]
            fallback_ids = [
                *unit_segment_ids[:1],
                *unit_segment_ids[-1:],
            ]
            selected_evidence_ids: list[int] = []
            for segment_id in [*preferred_evidence_ids, *fallback_ids]:
                if segment_id not in selected_evidence_ids:
                    selected_evidence_ids.append(segment_id)
                if len(selected_evidence_ids) >= _RECEPTION_FACT_EVIDENCE_SEGMENTS_PER_UNIT:
                    break
            if not selected_evidence_ids:
                continue
            semantic_facts = [
                {
                    "tag_key": fact["tag_key"],
                    "tag_value": fact["tag_value"],
                    "confidence": fact["confidence"],
                }
                for fact, _origin in facts
            ]
            anchor_id = selected_evidence_ids[0]
            contexts_by_segment.setdefault(anchor_id, []).append(
                "dialogue_unit_facts="
                + json.dumps(
                    {
                        "dialogue_unit_id": int(unit.id),
                        "unit_index": int(unit.unit_index),
                        "facts": semantic_facts,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            for segment_id in selected_evidence_ids[1:]:
                contexts_by_segment.setdefault(segment_id, []).append(f"dialogue_unit_id={unit.id}")
            lineage.append(
                {
                    "dialogue_unit_id": int(unit.id),
                    "dialogue_unit_version": int(unit.version),
                    "transport_evidence_segment_ids": selected_evidence_ids,
                    "facts": [
                        {
                            **{key: value for key, value in fact.items() if key != "evidence_refs"},
                            "source": origin,
                        }
                        for fact, origin in facts
                    ],
                }
            )

        compact_segments: list[tuple[Segment, str]] = []
        transport_snapshot: list[dict[str, Any]] = []
        for segment, text in segment_texts:
            segment_id = int(segment.id)
            contexts = contexts_by_segment.get(segment_id)
            if not contexts:
                continue
            compact_text = (
                "\n".join(contexts) + "\nraw_evidence=" + text[:_RECEPTION_FACT_EVIDENCE_TEXT_CHARS]
            )
            compact_segments.append((segment, compact_text))
            transport_snapshot.append(
                {
                    "segment_id": segment_id,
                    "text": compact_text,
                }
            )
        if not compact_segments:
            return None
        return tuple(compact_segments), {
            "version": _RECEPTION_FACT_TRANSPORT_VERSION,
            "source_reception_input_hash": source_reception_input_hash,
            "full_segment_count": len(segment_texts),
            "transport_segment_count": len(compact_segments),
            "segments": transport_snapshot,
            "dialogue_units": lineage,
        }

    async def _prepare_reception(
        self,
        *,
        tenant_id: str,
        reception_id: int,
        tagger_version_id: int,
        target_tag_keys: Sequence[str] | None = None,
    ) -> PreparedDialogueInput:
        """Build a reception-level input without borrowing dialogue-unit labels."""

        async with self._factory() as session:
            reception = (
                await session.execute(
                    select(Reception).where(
                        Reception.id == reception_id,
                        Reception.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            tagger = (
                await session.execute(
                    select(TaggerVersion).where(
                        TaggerVersion.id == tagger_version_id,
                        TaggerVersion.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if reception is None:
                raise GovernanceNotFoundError("reception not found")
            if tagger is None:
                raise GovernanceNotFoundError("tagger version not found")
            schema = (
                await session.execute(
                    select(TagSchemaVersion).where(
                        TagSchemaVersion.id == tagger.schema_version_id,
                        TagSchemaVersion.tenant_id == tenant_id,
                        TagSchemaVersion.status.in_(["published", "deprecated"]),
                    )
                )
            ).scalar_one_or_none()
            if schema is None:
                raise AssignmentValidationError(
                    "tagger must reference an immutable published schema version"
                )
            mappings = list(
                (
                    await session.execute(
                        select(ReceptionRecording)
                        .where(
                            ReceptionRecording.tenant_id == tenant_id,
                            ReceptionRecording.reception_id == reception_id,
                        )
                        .order_by(
                            ReceptionRecording.sequence_no,
                            ReceptionRecording.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            mapping_by_recording = {mapping.recording_id: mapping for mapping in mappings}
            recording_ids = sorted(mapping_by_recording)
            segments = (
                list(
                    (
                        await session.execute(
                            select(Segment)
                            .where(
                                Segment.tenant_id == tenant_id,
                                Segment.recording_id.in_(recording_ids),
                            )
                            .order_by(Segment.recording_id, Segment.start_sec, Segment.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                if recording_ids
                else []
            )

        ordered_segments: list[tuple[Segment, dict[str, Any]]] = []
        for segment in segments:
            mapping = mapping_by_recording[segment.recording_id]
            source_start = max(float(segment.start_sec), float(mapping.source_start_sec))
            mapping_source_end = (
                float(mapping.source_end_sec)
                if mapping.source_end_sec is not None
                else float(segment.end_sec)
            )
            source_end = min(float(segment.end_sec), mapping_source_end)
            if source_end <= source_start:
                continue
            timeline_start = float(mapping.timeline_start_sec) + (
                source_start - float(mapping.source_start_sec)
            )
            timeline_end = float(mapping.timeline_start_sec) + (
                source_end - float(mapping.source_start_sec)
            )
            ordered_segments.append(
                (
                    segment,
                    {
                        "segment_id": segment.id,
                        "recording_id": segment.recording_id,
                        "source_start_sec": source_start,
                        "source_end_sec": source_end,
                        "timeline_start_sec": timeline_start,
                        "timeline_end_sec": timeline_end,
                        "sequence_no": mapping.sequence_no,
                    },
                )
            )
        ordered_segments.sort(
            key=lambda item: (
                int(item[1]["sequence_no"]),
                float(item[1]["timeline_start_sec"]),
                item[0].id,
            )
        )
        refs_by_segment = {
            segment.id: {key: value for key, value in ref.items() if key != "sequence_no"}
            for segment, ref in ordered_segments
        }
        segment_texts: tuple[tuple[Segment, str], ...] = tuple(
            (segment, scrubbed_segment_text(segment.text_scrubbed, segment.transcript))
            for segment, _ref in ordered_segments
        )
        transcript = "\n".join(text for _segment, text in segment_texts)
        applicable_definitions = self._definitions_for_subject(
            schema=schema,
            subject_type="reception",
            scenario=str(reception.scenario),
        )
        definitions, effective_target_keys = self._targeted_definitions(
            schema=schema,
            applicable=applicable_definitions,
            target_tag_keys=target_tag_keys,
        )
        segment_snapshot = [
            {
                "segment_id": segment.id,
                "recording_id": segment.recording_id,
                "version": segment.updated_at.isoformat(),
                "start_sec": float(segment.start_sec),
                "end_sec": float(segment.end_sec),
                "speaker": segment.speaker,
                "vad_confidence": segment.vad_conf,
                "reference": refs_by_segment[segment.id],
                "text": text,
                "text_hash": compute_input_hash(
                    transcript=text,
                    segment_snapshot=[],
                    dialogue_unit_version=reception.version,
                    schema_checksum=schema.checksum,
                    tagger_checksum=tagger.config_checksum,
                    model_version=tagger.model_version,
                ),
            }
            for segment, text in segment_texts
        ]
        input_hash = compute_input_hash(
            transcript=transcript,
            segment_snapshot=self._hashable_segment_snapshot(segment_snapshot),
            dialogue_unit_version=reception.version,
            schema_checksum=schema.checksum,
            tagger_checksum=tagger.config_checksum,
            model_version=tagger.model_version,
            context_snapshot={
                "subject_type": "reception",
                "subject_id": reception.id,
                "reception_id": reception.id,
                "scenario": reception.scenario,
                "store_id": reception.store_id,
                "target_tag_keys": list(effective_target_keys),
            },
        )
        input_snapshot = {
            "subject_type": "reception",
            "subject_id": reception.id,
            "subject_version": reception.version,
            "reception_id": reception.id,
            "reception_version": reception.version,
            "scenario": reception.scenario,
            "store_id": reception.store_id,
            "segments": segment_snapshot,
            "transcript": transcript,
            "schema_version_id": schema.id,
            "schema_checksum": schema.checksum,
            "tagger_version_id": tagger.id,
            "tagger_checksum": tagger.config_checksum,
            "model_version": tagger.model_version,
            "target_tag_keys": list(effective_target_keys),
        }
        llm_segment_texts: tuple[tuple[Segment, str], ...] | None = None
        harness_spec = resolve_harness_spec(tagger)
        route = str(harness_spec["orchestration"]["route"])
        prompt_content = str(harness_spec["generation"]["prompt_template"])
        max_input_tokens = int(harness_spec["generation"]["max_input_tokens"])
        usable_input_tokens = max(1, math.floor(max_input_tokens * 0.90))
        if (
            definitions
            and route
            in {
                "weak_llm",
                "weak_then_strong_critic",
                "rule_llm_fusion",
            }
            and self._estimated_transport_input_tokens(
                prompt_content=prompt_content,
                definitions=definitions,
                segment_texts=segment_texts,
            )
            > usable_input_tokens
        ):
            aggregation = await self._aggregate_reception_fact_transport(
                tenant_id=tenant_id,
                reception_id=reception_id,
                tagger=tagger,
                schema=schema,
                segment_texts=segment_texts,
                refs_by_segment=refs_by_segment,
                source_reception_input_hash=input_hash,
            )
            if aggregation is not None:
                llm_segment_texts, aggregation_snapshot = aggregation
                aggregation_checksum = canonical_checksum(aggregation_snapshot)
                aggregation_snapshot["checksum"] = aggregation_checksum
                source_input_hash = input_hash
                input_snapshot["transport_aggregation"] = aggregation_snapshot
                input_snapshot["source_input_hash"] = source_input_hash
                input_hash = compute_input_hash(
                    transcript=transcript,
                    segment_snapshot=self._hashable_segment_snapshot(segment_snapshot),
                    dialogue_unit_version=reception.version,
                    schema_checksum=schema.checksum,
                    tagger_checksum=tagger.config_checksum,
                    model_version=tagger.model_version,
                    context_snapshot={
                        "subject_type": "reception",
                        "subject_id": reception.id,
                        "reception_id": reception.id,
                        "scenario": reception.scenario,
                        "store_id": reception.store_id,
                        "target_tag_keys": list(effective_target_keys),
                        "transport_aggregation_checksum": aggregation_checksum,
                    },
                )
        return PreparedDialogueInput(
            unit=_PredictionSubject(
                tenant_id=tenant_id,
                id=reception.id,
                reception_id=reception.id,
                source_recording_id=(recording_ids[0] if len(recording_ids) == 1 else None),
                version=reception.version,
            ),
            subject_type="reception",
            tagger=tagger,
            schema=schema,
            scenario=str(reception.scenario),
            segment_texts=segment_texts,
            refs_by_segment=refs_by_segment,
            transcript=transcript,
            input_hash=input_hash,
            input_snapshot=input_snapshot,
            definitions=definitions,
            llm_segment_texts=llm_segment_texts,
        )

    async def _prepare_frozen_input(
        self,
        *,
        tenant_id: str,
        subject_type: str,
        subject_id: int,
        input_snapshot: dict[str, Any],
        tagger_version_id: int,
        target_tag_keys: Sequence[str] | None = None,
        materialized_harness_spec: Mapping[str, Any] | None = None,
    ) -> PreparedDialogueInput:
        """Rehydrate evaluation input exclusively from its immutable snapshot."""

        if subject_type not in {"dialogue_unit", "reception"}:
            raise AssignmentValidationError("unsupported frozen subject type")
        snapshot = deepcopy(input_snapshot)
        declared_subject_type = snapshot.get("subject_type")
        if declared_subject_type is None:
            declared_subject_type = (
                "dialogue_unit" if snapshot.get("dialogue_unit_id") is not None else "reception"
            )
        if declared_subject_type != subject_type:
            raise AssignmentValidationError("frozen snapshot subject type does not match")
        snapshot_subject_id = (
            snapshot.get("dialogue_unit_id")
            if subject_type == "dialogue_unit"
            else snapshot.get("reception_id")
        )
        if snapshot_subject_id is None:
            snapshot_subject_id = snapshot.get("subject_id")
        if (
            isinstance(snapshot_subject_id, bool)
            or not isinstance(snapshot_subject_id, int)
            or snapshot_subject_id != subject_id
        ):
            raise AssignmentValidationError("frozen snapshot subject identity does not match")
        reception_id = snapshot.get("reception_id")
        if (
            isinstance(reception_id, bool)
            or not isinstance(reception_id, int)
            or (subject_type == "reception" and reception_id != subject_id)
        ):
            raise AssignmentValidationError("frozen snapshot reception identity is invalid")

        tagger, schema = await self._load_tagger_schema(
            tenant_id=tenant_id,
            tagger_version_id=tagger_version_id,
        )
        if materialized_harness_spec is not None:
            tagger = self._materialized_trial_tagger(
                baseline=tagger,
                harness_spec=materialized_harness_spec,
            )
        frozen_schema_id = snapshot.get("schema_version_id")
        frozen_schema_checksum = snapshot.get("schema_checksum")
        if frozen_schema_id != schema.id or frozen_schema_checksum != schema.checksum:
            raise AssignmentValidationError(
                "frozen snapshot schema does not match the candidate Harness"
            )
        scenario = snapshot.get("scenario")
        transcript = snapshot.get("transcript")
        raw_segments = snapshot.get("segments")
        if not isinstance(scenario, str) or not scenario:
            raise AssignmentValidationError("frozen snapshot scenario is missing")
        if not isinstance(transcript, str):
            raise AssignmentValidationError("frozen snapshot transcript is missing")
        if not isinstance(raw_segments, list) or any(
            not isinstance(item, dict) for item in raw_segments
        ):
            raise AssignmentValidationError("frozen snapshot segments must be an object list")
        subject_version = snapshot.get(
            "dialogue_unit_version" if subject_type == "dialogue_unit" else "reception_version",
            snapshot.get("subject_version"),
        )
        if (
            isinstance(subject_version, bool)
            or not isinstance(subject_version, int)
            or subject_version <= 0
        ):
            raise AssignmentValidationError("frozen snapshot subject version is invalid")

        snapshot_targets = snapshot.get("target_tag_keys")
        if target_tag_keys is None and isinstance(snapshot_targets, list):
            target_tag_keys = tuple(str(value) for value in snapshot_targets)
        applicable_definitions = self._definitions_for_subject(
            schema=schema,
            subject_type=subject_type,
            scenario=scenario,
        )
        definitions, effective_target_keys = self._targeted_definitions(
            schema=schema,
            applicable=applicable_definitions,
            target_tag_keys=target_tag_keys,
        )

        transcript_lines = transcript.splitlines()
        segment_texts: list[tuple[Segment | _SnapshotSegment, str]] = []
        refs_by_segment: dict[int, dict[str, Any]] = {}
        normalized_segments: list[dict[str, Any]] = []
        seen_segment_ids: set[int] = set()
        for index, raw in enumerate(raw_segments):
            segment_id = raw.get("segment_id", raw.get("id"))
            recording_id = raw.get("recording_id")
            start_sec = raw.get("start_sec")
            end_sec = raw.get("end_sec")
            if (
                isinstance(segment_id, bool)
                or not isinstance(segment_id, int)
                or segment_id in seen_segment_ids
                or isinstance(recording_id, bool)
                or not isinstance(recording_id, int)
                or isinstance(start_sec, bool)
                or not isinstance(start_sec, (int, float))
                or isinstance(end_sec, bool)
                or not isinstance(end_sec, (int, float))
                or not math.isfinite(float(start_sec))
                or not math.isfinite(float(end_sec))
                or float(end_sec) <= float(start_sec)
            ):
                raise AssignmentValidationError("frozen snapshot contains an invalid segment")
            seen_segment_ids.add(segment_id)
            text = raw.get("text", raw.get("text_scrubbed"))
            if not isinstance(text, str):
                if len(raw_segments) == 1:
                    text = transcript
                elif len(transcript_lines) == len(raw_segments):
                    text = transcript_lines[index]
                else:
                    raise AssignmentValidationError(
                        "frozen snapshot lacks per-segment scrubbed text"
                    )
            speaker = raw.get("speaker")
            if speaker is not None and not isinstance(speaker, str):
                raise AssignmentValidationError("frozen snapshot segment speaker must be a string")
            raw_vad_confidence = raw.get("vad_confidence", raw.get("vad_conf"))
            if raw_vad_confidence is not None and (
                isinstance(raw_vad_confidence, bool)
                or not isinstance(raw_vad_confidence, (int, float))
                or not math.isfinite(float(raw_vad_confidence))
            ):
                raise AssignmentValidationError("frozen snapshot segment VAD confidence is invalid")
            reference = raw.get("reference") or {}
            if not isinstance(reference, dict):
                raise AssignmentValidationError(
                    "frozen snapshot segment reference must be an object"
                )
            segment = _SnapshotSegment(
                id=segment_id,
                recording_id=recording_id,
                start_sec=float(start_sec),
                end_sec=float(end_sec),
                speaker=speaker,
                vad_conf=(float(raw_vad_confidence) if raw_vad_confidence is not None else None),
            )
            refs_by_segment[segment_id] = deepcopy(reference)
            segment_texts.append((segment, text))
            normalized_segments.append(
                {
                    "segment_id": segment_id,
                    "recording_id": recording_id,
                    "version": raw.get("version"),
                    "start_sec": float(start_sec),
                    "end_sec": float(end_sec),
                    "speaker": speaker,
                    "vad_confidence": segment.vad_conf,
                    "reference": refs_by_segment[segment_id],
                    "text": text,
                    "text_hash": compute_input_hash(
                        transcript=text,
                        segment_snapshot=[],
                        dialogue_unit_version=subject_version,
                        schema_checksum=schema.checksum,
                        tagger_checksum=tagger.config_checksum,
                        model_version=tagger.model_version,
                    ),
                }
            )

        input_hash = compute_input_hash(
            transcript=transcript,
            segment_snapshot=self._hashable_segment_snapshot(normalized_segments),
            dialogue_unit_version=subject_version,
            schema_checksum=schema.checksum,
            tagger_checksum=tagger.config_checksum,
            model_version=tagger.model_version,
            context_snapshot={
                "subject_type": subject_type,
                "subject_id": subject_id,
                "reception_id": reception_id,
                "scenario": scenario,
                "store_id": snapshot.get("store_id"),
                "target_tag_keys": list(effective_target_keys),
            },
        )
        replay_snapshot = {
            **snapshot,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "segments": normalized_segments,
            "schema_version_id": schema.id,
            "schema_checksum": schema.checksum,
            "tagger_version_id": tagger.id,
            "tagger_checksum": tagger.config_checksum,
            "model_version": tagger.model_version,
            "target_tag_keys": list(effective_target_keys),
        }
        llm_segment_texts: tuple[tuple[Segment | _SnapshotSegment, str], ...] | None = None
        raw_aggregation = snapshot.get("transport_aggregation")
        if subject_type == "reception" and raw_aggregation is not None:
            raw_aggregation_without_checksum = (
                {key: value for key, value in raw_aggregation.items() if key != "checksum"}
                if isinstance(raw_aggregation, dict)
                else {}
            )
            if (
                not isinstance(raw_aggregation, dict)
                or raw_aggregation.get("version") != _RECEPTION_FACT_TRANSPORT_VERSION
                or raw_aggregation.get("source_reception_input_hash") != input_hash
                or raw_aggregation.get("checksum")
                != canonical_checksum(raw_aggregation_without_checksum)
                or not isinstance(raw_aggregation.get("segments"), list)
            ):
                raise AssignmentValidationError(
                    "frozen reception transport aggregation lineage is invalid"
                )
            segment_by_id = {int(segment.id): segment for segment, _text in segment_texts}
            frozen_transport: list[tuple[Segment | _SnapshotSegment, str]] = []
            seen_transport_ids: set[int] = set()
            for raw_transport in raw_aggregation["segments"]:
                if not isinstance(raw_transport, dict):
                    raise AssignmentValidationError("frozen reception transport segment is invalid")
                segment_id = raw_transport.get("segment_id")
                text = raw_transport.get("text")
                if (
                    isinstance(segment_id, bool)
                    or not isinstance(segment_id, int)
                    or segment_id in seen_transport_ids
                    or segment_id not in segment_by_id
                    or not isinstance(text, str)
                ):
                    raise AssignmentValidationError("frozen reception transport segment is invalid")
                seen_transport_ids.add(segment_id)
                frozen_transport.append((segment_by_id[segment_id], text))
            if not frozen_transport:
                raise AssignmentValidationError("frozen reception transport aggregation is empty")
            llm_segment_texts = tuple(frozen_transport)
            input_hash = compute_input_hash(
                transcript=transcript,
                segment_snapshot=self._hashable_segment_snapshot(normalized_segments),
                dialogue_unit_version=subject_version,
                schema_checksum=schema.checksum,
                tagger_checksum=tagger.config_checksum,
                model_version=tagger.model_version,
                context_snapshot={
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "reception_id": reception_id,
                    "scenario": scenario,
                    "store_id": snapshot.get("store_id"),
                    "target_tag_keys": list(effective_target_keys),
                    "transport_aggregation_checksum": raw_aggregation["checksum"],
                },
            )
        recording_ids = sorted({segment.recording_id for segment, _text in segment_texts})
        return PreparedDialogueInput(
            unit=_PredictionSubject(
                tenant_id=tenant_id,
                id=subject_id,
                reception_id=reception_id,
                source_recording_id=(recording_ids[0] if len(recording_ids) == 1 else None),
                version=subject_version,
            ),
            subject_type=subject_type,
            tagger=tagger,
            schema=schema,
            scenario=scenario,
            segment_texts=tuple(segment_texts),
            refs_by_segment=refs_by_segment,
            transcript=transcript,
            input_hash=input_hash,
            input_snapshot=replay_snapshot,
            definitions=definitions,
            llm_segment_texts=llm_segment_texts,
        )

    @staticmethod
    def _materialized_trial_tagger(
        *,
        baseline: TaggerVersion,
        harness_spec: Mapping[str, Any],
    ) -> TaggerVersion:
        """Create a detached immutable candidate whose bytes match one optimizer trial."""

        raw_spec = deepcopy(dict(harness_spec))
        provisional = TaggerVersion(
            id=baseline.id,
            tenant_id=baseline.tenant_id,
            schema_version_id=baseline.schema_version_id,
            version=baseline.version,
            engine=baseline.engine,
            prompt_content=baseline.prompt_content,
            rule_bundle=deepcopy(baseline.rule_bundle),
            model_version=baseline.model_version,
            thresholds=deepcopy(baseline.thresholds),
            harness_spec_version="2.0",
            harness_spec=raw_spec,
            parent_version_id=baseline.id,
            origin="optimizer",
            config_checksum=baseline.config_checksum,
            status="draft",
            created_by=baseline.created_by,
        )
        resolved = resolve_harness_spec(provisional)
        prompt_content = str(resolved["generation"]["prompt_template"])
        rule_bundle = deepcopy(dict(resolved["orchestration"]["rule_bundle"]))
        thresholds = deepcopy(dict(resolved["output"]["thresholds"]))
        checksum = canonical_checksum(
            {
                "schema_version_id": baseline.schema_version_id,
                "engine": baseline.engine,
                "prompt_content": prompt_content,
                "rule_bundle": rule_bundle,
                "model_version": baseline.model_version,
                "thresholds": thresholds,
                "harness_spec_version": "2.0",
                "harness_spec": resolved,
            }
        )
        return TaggerVersion(
            id=baseline.id,
            tenant_id=baseline.tenant_id,
            schema_version_id=baseline.schema_version_id,
            version=f"trial-{checksum[:12]}",
            engine=baseline.engine,
            prompt_content=prompt_content,
            rule_bundle=rule_bundle,
            model_version=baseline.model_version,
            thresholds=thresholds,
            harness_spec_version="2.0",
            harness_spec=resolved,
            parent_version_id=baseline.id,
            origin="optimizer",
            config_checksum=checksum,
            status="draft",
            created_by=baseline.created_by,
        )

    async def _predict_prepared(
        self,
        prepared: PreparedDialogueInput,
        *,
        budget_policy_override: Mapping[str, int] | None = None,
        usage_context: LLMUsageContext | None = None,
    ) -> PredictionBatch:
        """Execute the resolved, bounded Harness over an already hashed input."""

        started = perf_counter()
        unit = prepared.unit
        tagger = prepared.tagger
        schema = prepared.schema
        segment_texts = list(prepared.segment_texts)
        llm_segment_texts = list(
            prepared.llm_segment_texts
            if prepared.llm_segment_texts is not None
            else prepared.segment_texts
        )
        definitions = prepared.definitions
        refs_by_segment = prepared.refs_by_segment
        harness_spec = resolve_harness_spec(tagger)
        budget_policy = dict(harness_spec["generation"].get("budget_policy") or {})
        if budget_policy_override is not None:
            supported_limits = {
                "max_provider_calls",
                "max_provider_tokens",
                "max_cost_microunits",
                "max_wall_seconds",
            }
            unknown_limits = sorted(set(budget_policy_override) - supported_limits)
            if unknown_limits:
                raise AssignmentValidationError(
                    "unsupported extraction budget limits: " + ", ".join(unknown_limits)
                )
            for key, value in budget_policy_override.items():
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    raise AssignmentValidationError(
                        f"extraction budget {key} must be a positive integer"
                    )
                configured = budget_policy.get(key)
                budget_policy[key] = value if configured is None else min(int(configured), value)
        max_provider_calls = budget_policy.get("max_provider_calls")
        max_provider_tokens = budget_policy.get("max_provider_tokens")
        max_cost_microunits = budget_policy.get("max_cost_microunits")
        max_wall_seconds = budget_policy.get("max_wall_seconds")
        reserved_provider_calls = 0
        reserved_provider_tokens = 0
        reserved_cost_microunits = 0

        def reserve_repair_attempt(
            estimated_tokens: int,
            *,
            adapter: LLMAdapter,
        ) -> None:
            nonlocal reserved_provider_calls, reserved_provider_tokens
            nonlocal reserved_cost_microunits
            attempt_bound = self._provider_attempt_bound(adapter)
            reserved_tokens = estimated_tokens * attempt_bound
            if max_wall_seconds is not None and perf_counter() - started >= float(max_wall_seconds):
                raise _TagBudgetExceededError("wall-clock budget exhausted before format repair")
            if max_provider_calls is not None and reserved_provider_calls + attempt_bound > int(
                max_provider_calls
            ):
                raise _TagBudgetExceededError("provider call budget exhausted before format repair")
            if max_provider_tokens is not None and reserved_provider_tokens + reserved_tokens > int(
                max_provider_tokens
            ):
                raise _TagBudgetExceededError(
                    "provider token budget exhausted before format repair"
                )
            estimated_cost = self._estimate_provider_cost_for_token_budget(
                adapter,
                total_tokens=estimated_tokens,
            )
            if max_cost_microunits is not None:
                if estimated_cost is None:
                    raise _TagBudgetExceededError(
                        "cost budget requires a configured model price snapshot"
                    )
                if reserved_cost_microunits + estimated_cost > int(max_cost_microunits):
                    raise _TagBudgetExceededError(
                        "provider cost budget exhausted before format repair"
                    )
            reserved_provider_calls += attempt_bound
            reserved_provider_tokens += reserved_tokens
            reserved_cost_microunits += estimated_cost or 0

        def repair_reserver_for(adapter: LLMAdapter) -> Callable[[int], None]:
            def reserve(estimated_tokens: int) -> None:
                reserve_repair_attempt(estimated_tokens, adapter=adapter)

            return reserve

        route = str(harness_spec["orchestration"]["route"])
        fusion_policy = str(harness_spec["orchestration"]["fusion_policy"])
        scene_profile = build_scene_profile(
            scenario=prepared.scenario,
            subject_type=prepared.subject_type,
            store_id=(
                str(prepared.input_snapshot["store_id"])
                if prepared.input_snapshot.get("store_id") is not None
                else None
            ),
            transcript=prepared.transcript,
            segments=[segment for segment, _text in segment_texts],
        )
        rule_enabled = route in {"rule_only", "rule_llm_fusion"}
        weak_enabled = route in {
            "weak_llm",
            "weak_then_strong_critic",
            "rule_llm_fusion",
        }
        strong_enabled = route == "weak_then_strong_critic"
        rule_results = (
            self._rule_assignments(
                tagger=tagger,
                subject_type=prepared.subject_type,
                definitions=definitions,
                segment_texts=segment_texts,
                refs_by_segment=refs_by_segment,
            )
            if rule_enabled
            else {}
        )
        rule_min_confidence = float(harness_spec["orchestration"].get("rule_min_confidence", 0.95))
        rule_resolved_keys = {
            key
            for key, assignment in rule_results.items()
            if (
                route == "rule_llm_fusion"
                and self._enable_hybrid_rule_short_circuit
                and not bool(definitions[key].get("critical"))
                and not bool(definitions[key].get("critical_values"))
                and float(assignment["confidence"]) >= rule_min_confidence
            )
        }
        llm_definitions = {
            key: definition
            for key, definition in definitions.items()
            if key not in rule_resolved_keys
        }
        short_circuit = weak_enabled and not llm_definitions
        if max_cost_microunits is not None:
            required_priced_adapters: list[LLMAdapter] = []
            if weak_enabled and not short_circuit and self._weak_llm is not None:
                required_priced_adapters.append(self._weak_llm)
            # A selective critic is data-dependent on the weak result. Validate
            # its price before the weak call so missing pricing fails closed.
            if strong_enabled and definitions and self._strong_llm is not None:
                required_priced_adapters.append(self._strong_llm)
            for priced_adapter in required_priced_adapters:
                if (
                    self._estimate_provider_cost(
                        priced_adapter,
                        input_tokens=0,
                        output_tokens=0,
                    )
                    is None
                ):
                    raise _TagBudgetExceededError(
                        "cost budget requires a configured model price snapshot"
                    )
        llm_batches: list[LLMAssignmentBatch] = []
        weak_results: dict[str, dict[str, Any]] = {}
        if weak_enabled and not short_circuit:
            if self._weak_llm is None:
                if route != "rule_llm_fusion":
                    raise AssignmentValidationError(
                        f"Harness route {route} requires the registered weak LLM"
                    )
            else:
                prompt_template = str(harness_spec["generation"]["prompt_template"])
                weak_segment_batches = self._segment_batches_for_input_budget(
                    segment_texts=llm_segment_texts,
                    definitions=llm_definitions,
                    prompt_content=prompt_template,
                    max_input_tokens=int(harness_spec["generation"]["max_input_tokens"]),
                )
                logical_calls = len(weak_segment_batches)
                planned_calls = logical_calls * self._provider_attempt_bound(self._weak_llm)
                planned_input_tokens = planned_calls * int(
                    harness_spec["generation"]["max_input_tokens"]
                )
                planned_output_tokens = planned_calls * self._dynamic_output_tokens(
                    label_count=len(llm_definitions),
                    configured_cap=int(harness_spec["generation"]["max_tokens"]),
                )
                planned_tokens = planned_input_tokens + planned_output_tokens
                if max_provider_calls is not None and reserved_provider_calls + planned_calls > int(
                    max_provider_calls
                ):
                    raise _TagBudgetExceededError(
                        "provider call budget exhausted before weak generation"
                    )
                if (
                    max_provider_tokens is not None
                    and reserved_provider_tokens + planned_tokens > int(max_provider_tokens)
                ):
                    raise _TagBudgetExceededError(
                        "provider token budget exhausted before weak generation"
                    )
                per_call_planned_cost = self._estimate_provider_cost(
                    self._weak_llm,
                    input_tokens=int(harness_spec["generation"]["max_input_tokens"]),
                    output_tokens=self._dynamic_output_tokens(
                        label_count=len(llm_definitions),
                        configured_cap=int(harness_spec["generation"]["max_tokens"]),
                    ),
                )
                planned_cost = (
                    per_call_planned_cost * logical_calls
                    if per_call_planned_cost is not None
                    else None
                )
                if max_cost_microunits is not None and (
                    planned_cost is None
                    or reserved_cost_microunits + planned_cost > int(max_cost_microunits)
                ):
                    raise _TagBudgetExceededError(
                        "provider cost budget exhausted before weak generation"
                    )
                reserved_provider_calls += planned_calls
                reserved_provider_tokens += planned_tokens
                reserved_cost_microunits += planned_cost or 0
                for weak_segment_batch in weak_segment_batches:
                    if max_wall_seconds is not None and perf_counter() - started >= float(
                        max_wall_seconds
                    ):
                        raise _TagBudgetExceededError(
                            "wall-clock budget exhausted before weak generation"
                        )
                    weak_batch = await self._llm_assignments(
                        adapter=self._weak_llm,
                        model_tier="weak",
                        tenant_id=str(unit.tenant_id),
                        unit=unit,
                        subject_type=prepared.subject_type,
                        schema=schema,
                        tagger=tagger,
                        definitions=llm_definitions,
                        transcript="",
                        segment_texts=weak_segment_batch,
                        refs_by_segment=refs_by_segment,
                        input_hash=prepared.input_hash,
                        input_snapshot=prepared.input_snapshot,
                        prompt_content=prompt_template,
                        max_tokens=self._dynamic_output_tokens(
                            label_count=len(llm_definitions),
                            configured_cap=int(harness_spec["generation"]["max_tokens"]),
                        ),
                        usage_context=usage_context,
                        allow_format_repair=(
                            max_provider_calls is None
                            or reserved_provider_calls < int(max_provider_calls)
                        ),
                        repair_budget_reserver=repair_reserver_for(self._weak_llm),
                    )
                    llm_batches.append(weak_batch)
                    for key, assignment in weak_batch.assignments.items():
                        current = weak_results.get(key)
                        if current is None or float(assignment["confidence"]) > float(
                            current["confidence"]
                        ):
                            weak_results[key] = assignment
        critic_results: dict[str, dict[str, Any]] = {}
        critic_definitions: dict[str, dict[str, Any]] = {}
        if strong_enabled and definitions:
            critic_margin = float(
                harness_spec["orchestration"].get("critic_confidence_margin", 0.10)
            )
            max_noncritical_rate = float(
                harness_spec["orchestration"].get("critic_max_noncritical_rate", 0.20)
            )
            critical_keys = {
                key
                for key, definition in definitions.items()
                if bool(definition.get("critical")) or bool(definition.get("critical_values"))
            }
            noncritical_triggers: list[tuple[float, str]] = []
            for key, definition in definitions.items():
                if key in critical_keys:
                    continue
                weak_assignment = weak_results.get(key)
                rule_assignment = rule_results.get(key)
                if weak_assignment is None:
                    if bool(definition.get("required")):
                        noncritical_triggers.append((0.0, key))
                    continue
                threshold = self._threshold_for(
                    tagger,
                    definitions,
                    key,
                    harness_spec,
                    subject_type=prepared.subject_type,
                )
                distance = abs(float(weak_assignment["confidence"]) - threshold)
                conflicts_with_rule = rule_assignment is not None and repr(
                    rule_assignment.get("tag_value")
                ) != repr(weak_assignment.get("tag_value"))
                missing_evidence = bool(definition.get("evidence_required")) and not bool(
                    weak_assignment.get("evidence_refs")
                )
                if conflicts_with_rule or missing_evidence or distance <= critic_margin:
                    priority = -1.0 if conflicts_with_rule or missing_evidence else distance
                    noncritical_triggers.append((priority, key))

            noncritical_definitions = [key for key in definitions if key not in critical_keys]
            noncritical_limit = (
                math.ceil(len(noncritical_definitions) * max_noncritical_rate)
                if noncritical_definitions and max_noncritical_rate > 0
                else 0
            )
            escalated_noncritical = {
                key for _priority, key in sorted(noncritical_triggers)[:noncritical_limit]
            }
            escalated_keys = critical_keys | escalated_noncritical
            critic_definitions = {
                key: definition for key, definition in definitions.items() if key in escalated_keys
            }

        if critic_definitions:
            if self._strong_llm is None:
                raise AssignmentValidationError(
                    "Harness route weak_then_strong_critic requires the registered strong LLM"
                )
            weak_epoch = str(
                getattr(
                    self._weak_llm,
                    "model_epoch",
                    getattr(self._weak_llm, "model", ""),
                )
            )
            strong_epoch = str(
                getattr(
                    self._strong_llm,
                    "model_epoch",
                    getattr(self._strong_llm, "model", ""),
                )
            )
            if not weak_epoch or weak_epoch != strong_epoch:
                relevant_segment_ids = {
                    int(reference["segment_id"])
                    for key in critic_definitions
                    for reference in weak_results.get(key, {}).get("evidence_refs", [])
                    if isinstance(reference, Mapping) and reference.get("segment_id") is not None
                }
                critic_segment_texts = (
                    [
                        (segment, text)
                        for segment, text in segment_texts
                        if int(segment.id) in relevant_segment_ids
                    ]
                    if relevant_segment_ids
                    else segment_texts
                )
                critic_prompt = (
                    str(harness_spec["generation"]["prompt_template"])
                    + "\nReview only the supplied weak candidates and cited evidence."
                )
                critic_weak_candidates = {
                    key: weak_results[key] for key in critic_definitions if key in weak_results
                }
                critic_segment_batches = self._segment_batches_for_input_budget(
                    segment_texts=critic_segment_texts,
                    definitions=critic_definitions,
                    prompt_content=critic_prompt,
                    max_input_tokens=int(harness_spec["generation"]["max_input_tokens"]),
                    weak_candidates=critic_weak_candidates,
                )
                critic_max_tokens = self._dynamic_output_tokens(
                    label_count=len(critic_definitions),
                    configured_cap=int(harness_spec["generation"]["max_tokens"]),
                )
                logical_calls = len(critic_segment_batches)
                planned_calls = logical_calls * self._provider_attempt_bound(self._strong_llm)
                planned_input_tokens = planned_calls * int(
                    harness_spec["generation"]["max_input_tokens"]
                )
                planned_output_tokens = planned_calls * critic_max_tokens
                planned_tokens = planned_input_tokens + planned_output_tokens
                if max_provider_calls is not None and reserved_provider_calls + planned_calls > int(
                    max_provider_calls
                ):
                    raise _TagBudgetExceededError(
                        "provider call budget exhausted before critic generation"
                    )
                if (
                    max_provider_tokens is not None
                    and reserved_provider_tokens + planned_tokens > int(max_provider_tokens)
                ):
                    raise _TagBudgetExceededError(
                        "provider token budget exhausted before critic generation"
                    )
                per_call_planned_cost = self._estimate_provider_cost(
                    self._strong_llm,
                    input_tokens=int(harness_spec["generation"]["max_input_tokens"]),
                    output_tokens=critic_max_tokens,
                )
                planned_cost = (
                    per_call_planned_cost * logical_calls
                    if per_call_planned_cost is not None
                    else None
                )
                if max_cost_microunits is not None and (
                    planned_cost is None
                    or reserved_cost_microunits + planned_cost > int(max_cost_microunits)
                ):
                    raise _TagBudgetExceededError(
                        "provider cost budget exhausted before critic generation"
                    )
                reserved_provider_calls += planned_calls
                reserved_provider_tokens += planned_tokens
                reserved_cost_microunits += planned_cost or 0
                for critic_segment_batch in critic_segment_batches:
                    if max_wall_seconds is not None and perf_counter() - started >= float(
                        max_wall_seconds
                    ):
                        raise _TagBudgetExceededError(
                            "wall-clock budget exhausted before critic generation"
                        )
                    critic_batch = await self._llm_assignments(
                        adapter=self._strong_llm,
                        model_tier="strong",
                        tenant_id=str(unit.tenant_id),
                        unit=unit,
                        subject_type=prepared.subject_type,
                        schema=schema,
                        tagger=tagger,
                        definitions=critic_definitions,
                        transcript="",
                        segment_texts=critic_segment_batch,
                        refs_by_segment=refs_by_segment,
                        input_hash=prepared.input_hash,
                        input_snapshot=prepared.input_snapshot,
                        prompt_content=critic_prompt,
                        max_tokens=critic_max_tokens,
                        usage_context=usage_context,
                        weak_candidates=critic_weak_candidates,
                        allow_format_repair=(
                            max_provider_calls is None
                            or reserved_provider_calls < int(max_provider_calls)
                        ),
                        repair_budget_reserver=repair_reserver_for(self._strong_llm),
                    )
                    llm_batches.append(critic_batch)
                    for key, assignment in critic_batch.assignments.items():
                        item = {**assignment, "source": "critic"}
                        current = critic_results.get(key)
                        if current is None or float(item["confidence"]) > float(
                            current["confidence"]
                        ):
                            critic_results[key] = item
        sources: dict[str, dict[str, dict[str, Any]]] = {}
        if rule_results:
            sources["rule"] = rule_results
        if weak_results:
            sources["weak"] = weak_results
        if critic_results:
            sources["critic"] = critic_results
        candidates, _candidate_conflicts = fuse_assignments(
            sources,
            policy="score_priority",
        )
        fused, conflicts = fuse_assignments(sources, policy=fusion_policy)
        assignments = {
            key: item
            for key, item in fused.items()
            if float(item["confidence"])
            >= self._threshold_for(
                tagger,
                definitions,
                key,
                harness_spec,
                subject_type=prepared.subject_type,
            )
        }
        review_items: list[dict[str, Any]] = []
        for key in conflicts:
            proposed = candidates[key]
            review_items.append(
                {
                    "reason": "conflict",
                    "subject_type": prepared.subject_type,
                    "subject_id": unit.id,
                    "reception_id": unit.reception_id,
                    "tag_key": key,
                    "proposed_value": proposed["tag_value"],
                    "schema_version_id": schema.id,
                    "tagger_version_id": tagger.id,
                    "confidence": proposed["confidence"],
                    "evidence_refs": proposed["evidence_refs"],
                }
            )
        for key, item in candidates.items():
            if key not in assignments and key not in conflicts:
                review_items.append(
                    {
                        "reason": "low_confidence",
                        "subject_type": prepared.subject_type,
                        "subject_id": unit.id,
                        "reception_id": unit.reception_id,
                        "tag_key": key,
                        "proposed_value": item["tag_value"],
                        "schema_version_id": schema.id,
                        "tagger_version_id": tagger.id,
                        "confidence": item["confidence"],
                        "evidence_refs": item["evidence_refs"],
                    }
                )
        for key, definition in definitions.items():
            if bool(definition.get("required")) and key not in assignments and key not in conflicts:
                review_items.append(
                    {
                        "reason": "missing",
                        "subject_type": prepared.subject_type,
                        "subject_id": unit.id,
                        "reception_id": unit.reception_id,
                        "tag_key": key,
                        "schema_version_id": schema.id,
                        "tagger_version_id": tagger.id,
                        "evidence_refs": [],
                    }
                )
            if bool(definition.get("critical")) and key in assignments:
                review_items.append(
                    {
                        "reason": "critical",
                        "subject_type": prepared.subject_type,
                        "subject_id": unit.id,
                        "reception_id": unit.reception_id,
                        "tag_key": key,
                        "proposed_value": assignments[key]["tag_value"],
                        "schema_version_id": schema.id,
                        "tagger_version_id": tagger.id,
                        "confidence": assignments[key]["confidence"],
                        "evidence_refs": assignments[key]["evidence_refs"],
                    }
                )
        representative_audit = int(prepared.input_hash[:4], 16) % 20 == 0
        if representative_audit:
            for key in sorted(definitions):
                audit_candidate = candidates.get(key)
                review_items.append(
                    {
                        "reason": "random",
                        "subject_type": prepared.subject_type,
                        "subject_id": unit.id,
                        "reception_id": unit.reception_id,
                        "tag_key": key,
                        "proposed_value": (
                            audit_candidate["tag_value"] if audit_candidate is not None else None
                        ),
                        "schema_version_id": schema.id,
                        "tagger_version_id": tagger.id,
                        "confidence": (
                            audit_candidate["confidence"] if audit_candidate is not None else None
                        ),
                        "evidence_refs": (
                            audit_candidate["evidence_refs"] if audit_candidate is not None else []
                        ),
                    }
                )
        token_count = sum(batch.token_count for batch in llm_batches)
        provider_input_tokens = sum(batch.provider_input_tokens for batch in llm_batches)
        provider_output_tokens = sum(batch.provider_output_tokens for batch in llm_batches)
        reused_input_tokens = sum(batch.reused_input_tokens for batch in llm_batches)
        reused_output_tokens = sum(batch.reused_output_tokens for batch in llm_batches)
        provider_calls = sum(batch.provider_calls for batch in llm_batches)
        cache_hits = sum(batch.cache_hits for batch in llm_batches)
        cost_microunits = sum(batch.cost_microunits for batch in llm_batches)
        counterfactual_saved_cost_microunits = sum(
            batch.counterfactual_saved_cost_microunits for batch in llm_batches
        )
        unknown_billed_tokens = sum(batch.unknown_billed_tokens for batch in llm_batches)
        if max_cost_microunits is not None and cost_microunits > int(max_cost_microunits):
            raise _TagBudgetExceededError(
                "provider cost budget exhausted during generation settlement"
            )
        latency_ms = max(0, round((perf_counter() - started) * 1_000))
        next_actions = ["create_review_tasks"] if review_items else []
        generation_status = "completed" if llm_batches else "skipped"
        memory_enabled = harness_spec["memory"]["policy"] != "none"
        stage_traces = (
            {
                "stage": "context",
                "tool_name": None,
                "status": "completed",
                "observation": build_stage_observation(
                    status="success",
                    summary="stable scene profile resolved",
                    artifacts=[f"input_hash:{prepared.input_hash}"],
                    details=scene_profile,
                ),
                "latency_ms": 0,
                "token_count": 0,
                "cost_units": 0.0,
            },
            {
                "stage": "tools",
                "tool_name": None,
                "status": "completed",
                "observation": build_stage_observation(
                    status="success",
                    summary=f"registered route tools resolved for {route}",
                    details={
                        "rule": rule_enabled,
                        "weak": weak_enabled and not short_circuit,
                        "strong": bool(critic_results),
                        "critic_requested_tag_count": len(critic_definitions),
                    },
                ),
                "latency_ms": 0,
                "token_count": 0,
                "cost_units": 0.0,
            },
            {
                "stage": "generation",
                "tool_name": ",".join(batch.model for batch in llm_batches) or None,
                "status": generation_status,
                "observation": build_stage_observation(
                    status="success",
                    summary=(
                        "model assignments generated"
                        if llm_batches
                        else "generation skipped by resolved route"
                    ),
                    details={
                        "models": [batch.model for batch in llm_batches],
                        "cached": [batch.cached for batch in llm_batches],
                        "provider_input_tokens": provider_input_tokens,
                        "provider_output_tokens": provider_output_tokens,
                        "reused_input_tokens": reused_input_tokens,
                        "reused_output_tokens": reused_output_tokens,
                        "provider_calls": provider_calls,
                        "cache_hits": cache_hits,
                        "cost_microunits": cost_microunits,
                        "counterfactual_saved_cost_microunits": (
                            counterfactual_saved_cost_microunits
                        ),
                        "cold_cache_cost_microunits": (
                            cost_microunits + counterfactual_saved_cost_microunits
                        ),
                        "unknown_billed_tokens": unknown_billed_tokens,
                    },
                ),
                "latency_ms": latency_ms,
                "token_count": token_count,
                "cost_units": cost_microunits / 1_000_000,
            },
            {
                "stage": "orchestration",
                "tool_name": route,
                "status": "completed",
                "observation": build_stage_observation(
                    status="warning" if conflicts else "success",
                    summary=(
                        f"{len(conflicts)} conflicts routed to review"
                        if conflicts
                        else "candidate sources fused without conflict"
                    ),
                    next_actions=next_actions,
                    details={
                        "route": route,
                        "fusion_policy": fusion_policy,
                        "conflict_tag_keys": list(conflicts),
                    },
                ),
                "latency_ms": 0,
                "token_count": 0,
                "cost_units": 0.0,
            },
            {
                "stage": "memory",
                "tool_name": "experience_cases" if memory_enabled else None,
                "status": "skipped",
                "observation": build_stage_observation(
                    status="warning" if memory_enabled else "success",
                    summary=(
                        "experience retrieval is not materialized for this execution"
                        if memory_enabled
                        else "experience retrieval disabled"
                    ),
                    next_actions=(["materialize_experience_retrieval"] if memory_enabled else []),
                    details=harness_spec["memory"],
                ),
                "latency_ms": 0,
                "token_count": 0,
                "cost_units": 0.0,
            },
            {
                "stage": "output",
                "tool_name": "schema_evidence_validator",
                "status": "completed",
                "observation": build_stage_observation(
                    status="warning" if review_items else "success",
                    summary=(
                        f"{len(assignments)} assignments; "
                        f"{len(review_items)} review tasks requested"
                    ),
                    next_actions=next_actions,
                    details={
                        "assignment_count": len(assignments),
                        "review_item_count": len(review_items),
                        "representative_audit": representative_audit,
                    },
                ),
                "latency_ms": 0,
                "token_count": 0,
                "cost_units": 0.0,
            },
        )
        return PredictionBatch(
            input_hash=prepared.input_hash,
            input_snapshot=prepared.input_snapshot,
            candidates=tuple(candidates.values()),
            assignments=tuple(assignments.values()),
            review_items=tuple(review_items),
            conflict_tag_keys=tuple(conflicts),
            harness_spec=harness_spec,
            scene_profile=scene_profile,
            route=route,
            stage_traces=stage_traces,
            latency_ms=latency_ms,
            token_count=token_count,
            cost_units=cost_microunits / 1_000_000,
            provider_input_tokens=provider_input_tokens,
            provider_output_tokens=provider_output_tokens,
            reused_input_tokens=reused_input_tokens,
            reused_output_tokens=reused_output_tokens,
            provider_calls=provider_calls,
            cache_hits=cache_hits,
            strong_escalations=(len(critic_definitions) if critic_results else 0),
            cost_microunits=cost_microunits,
            counterfactual_saved_cost_microunits=(counterfactual_saved_cost_microunits),
            unknown_billed_tokens=unknown_billed_tokens,
        )

    async def predict_dialogue_unit(
        self,
        *,
        tenant_id: str,
        dialogue_unit_id: int,
        tagger_version_id: int,
        target_tag_keys: Sequence[str] | None = None,
        usage_context: LLMUsageContext | None = None,
    ) -> PredictionBatch:
        """Pure prediction for offline evaluation; writes no run, fact or current row."""

        prepared = await self._prepare_dialogue_unit(
            tenant_id=tenant_id,
            dialogue_unit_id=dialogue_unit_id,
            tagger_version_id=tagger_version_id,
            target_tag_keys=target_tag_keys,
        )
        async with self._factory() as session:
            reusable = await self._find_reusable_business_product(
                session,
                tenant_id=tenant_id,
                dialogue_unit_id=dialogue_unit_id,
                tagger_version_id=tagger_version_id,
                input_hash=prepared.input_hash,
                constrain_deployment=False,
            )
            if reusable is not None:
                source_run, assignments = reusable
                return self._prediction_from_business_product(
                    prepared,
                    source_run=source_run,
                    assignments=assignments,
                )
        return await self._predict_prepared(
            prepared,
            usage_context=usage_context,
        )

    async def predict_reception(
        self,
        *,
        tenant_id: str,
        reception_id: int,
        tagger_version_id: int,
        target_tag_keys: Sequence[str] | None = None,
        usage_context: LLMUsageContext | None = None,
    ) -> PredictionBatch:
        """Pure reception-level prediction with an independent label denominator."""

        prepared = await self._prepare_reception(
            tenant_id=tenant_id,
            reception_id=reception_id,
            tagger_version_id=tagger_version_id,
            target_tag_keys=target_tag_keys,
        )
        return await self._predict_prepared(
            prepared,
            usage_context=usage_context,
        )

    async def predict_frozen_input(
        self,
        *,
        tenant_id: str,
        subject_type: str,
        subject_id: int,
        input_snapshot: dict[str, Any],
        tagger_version_id: int,
        target_tag_keys: Sequence[str] | None = None,
        usage_context: LLMUsageContext | None = None,
    ) -> PredictionBatch:
        """Replay one gold input snapshot without consulting live subject data."""

        prepared = await self._prepare_frozen_input(
            tenant_id=tenant_id,
            subject_type=subject_type,
            subject_id=subject_id,
            input_snapshot=input_snapshot,
            tagger_version_id=tagger_version_id,
            target_tag_keys=target_tag_keys,
        )
        return await self._predict_prepared(
            prepared,
            usage_context=usage_context,
        )

    async def predict_materialized_frozen_input(
        self,
        *,
        tenant_id: str,
        subject_type: str,
        subject_id: int,
        input_snapshot: dict[str, Any],
        baseline_tagger_version_id: int,
        harness_spec: Mapping[str, Any],
        target_tag_keys: Sequence[str] | None = None,
        usage_context: LLMUsageContext | None = None,
    ) -> PredictionBatch:
        """Run one exact optimizer candidate over a frozen gold input snapshot."""

        prepared = await self._prepare_frozen_input(
            tenant_id=tenant_id,
            subject_type=subject_type,
            subject_id=subject_id,
            input_snapshot=input_snapshot,
            tagger_version_id=baseline_tagger_version_id,
            target_tag_keys=target_tag_keys,
            materialized_harness_spec=harness_spec,
        )
        return await self._predict_prepared(
            prepared,
            usage_context=usage_context,
        )

    async def _persist_harness_execution(
        self,
        *,
        tenant_id: str,
        run_id: int,
        dialogue_unit_id: int,
        tagger_version_id: int,
        deployment_id: int | None,
        prediction: PredictionBatch,
    ) -> int:
        """Persist one replayable execution and its six ordered observations."""

        now = datetime.now(UTC)
        async with self._factory() as session, session.begin():
            existing = (
                await session.execute(
                    select(TagHarnessExecution).where(
                        TagHarnessExecution.tenant_id == tenant_id,
                        TagHarnessExecution.extraction_run_id == run_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return int(existing.id)
            execution = TagHarnessExecution(
                tenant_id=tenant_id,
                extraction_run_id=run_id,
                tagger_version_id=tagger_version_id,
                deployment_id=deployment_id,
                subject_type="dialogue_unit",
                subject_id=dialogue_unit_id,
                input_hash=prediction.input_hash,
                scene_profile=prediction.scene_profile,
                resolved_harness_spec=prediction.harness_spec,
                route=prediction.route,
                status="completed",
                output_snapshot={
                    "assignments": list(prediction.assignments),
                    "candidates": list(prediction.candidates),
                    "conflict_tag_keys": list(prediction.conflict_tag_keys),
                    "review_item_count": len(prediction.review_items),
                    "review_items": [
                        {"tag_key": str(item["tag_key"])} for item in prediction.review_items
                    ],
                    "usage": {
                        "provider_input_tokens": prediction.provider_input_tokens,
                        "provider_output_tokens": prediction.provider_output_tokens,
                        "reused_input_tokens": prediction.reused_input_tokens,
                        "reused_output_tokens": prediction.reused_output_tokens,
                        "provider_calls": prediction.provider_calls,
                        "cache_hits": prediction.cache_hits,
                        "strong_escalations": prediction.strong_escalations,
                        "cost_microunits": prediction.cost_microunits,
                        "counterfactual_saved_cost_microunits": (
                            prediction.counterfactual_saved_cost_microunits
                        ),
                        "cold_cache_cost_microunits": (
                            prediction.cost_microunits
                            + prediction.counterfactual_saved_cost_microunits
                        ),
                        "unknown_billed_tokens": prediction.unknown_billed_tokens,
                    },
                },
                summary=(
                    f"{len(prediction.assignments)} assignments, "
                    f"{len(prediction.review_items)} review items"
                ),
                next_actions=(["review_pending_items"] if prediction.review_items else []),
                artifacts=[f"tag_extraction_run:{run_id}"],
                latency_ms=prediction.latency_ms,
                token_count=prediction.token_count,
                cost_units=prediction.cost_units,
                started_at=now,
                finished_at=now,
            )
            session.add(execution)
            await session.flush()
            for sequence_no, trace in enumerate(prediction.stage_traces, start=1):
                observation = dict(trace["observation"])
                session.add(
                    TagHarnessStageTrace(
                        tenant_id=tenant_id,
                        harness_execution_id=execution.id,
                        sequence_no=sequence_no,
                        stage=str(trace["stage"]),
                        tool_name=trace.get("tool_name"),
                        status=str(trace["status"]),
                        observation=observation,
                        summary=str(observation.get("summary") or ""),
                        next_actions=list(observation.get("next_actions") or []),
                        artifacts=list(observation.get("artifacts") or []),
                        latency_ms=int(trace.get("latency_ms", 0)),
                        token_count=int(trace.get("token_count", 0)),
                        cost_units=float(trace.get("cost_units", 0)),
                        started_at=now,
                        finished_at=now,
                    )
                )
            await session.flush()
            return int(execution.id)

    @staticmethod
    def _cached_prediction_batch(
        prepared: PreparedDialogueInput,
        result: ExtractionResult,
    ) -> PredictionBatch:
        harness_spec = resolve_harness_spec(prepared.tagger)
        scene_profile = build_scene_profile(
            scenario=prepared.scenario,
            subject_type=prepared.subject_type,
            store_id=(
                str(prepared.input_snapshot["store_id"])
                if prepared.input_snapshot.get("store_id") is not None
                else None
            ),
            transcript=prepared.transcript,
            segments=[segment for segment, _text in prepared.segment_texts],
        )
        traces: list[dict[str, Any]] = []
        for stage in (
            "context",
            "tools",
            "generation",
            "orchestration",
            "memory",
            "output",
        ):
            completed = stage in {"context", "output"}
            traces.append(
                {
                    "stage": stage,
                    "tool_name": "content_addressed_cache" if stage == "tools" else None,
                    "status": "completed" if completed else "skipped",
                    "observation": build_stage_observation(
                        status="success",
                        summary=(
                            "cached business product replayed"
                            if stage == "output"
                            else f"{stage} reused from the source execution"
                        ),
                        artifacts=[f"tag_extraction_run:{result.run_id}"],
                        details={"cached": True},
                    ),
                    "latency_ms": 0,
                    "token_count": 0,
                    "cost_units": 0.0,
                }
            )
        return PredictionBatch(
            input_hash=result.input_hash,
            input_snapshot=result.input_snapshot,
            candidates=tuple(result.assignments),
            assignments=tuple(result.assignments),
            review_items=(),
            conflict_tag_keys=(),
            harness_spec=harness_spec,
            scene_profile=scene_profile,
            route="cache_reuse",
            stage_traces=tuple(traces),
            latency_ms=0,
            token_count=0,
            cost_units=0.0,
        )

    @staticmethod
    async def _valid_cached_assignments(
        session: AsyncSession,
        *,
        run: TagExtractionRun,
        tenant_id: str,
        dialogue_unit_id: int,
        tagger_version_id: int,
        deployment_id: int | None,
        input_hash: str,
    ) -> tuple[dict[str, Any], ...] | None:
        """Validate that a completed run still points at reusable facts."""

        raw_assignments = run.output_snapshot.get("assignments")
        if not isinstance(raw_assignments, list) or any(
            not isinstance(item, dict) for item in raw_assignments
        ):
            return None
        assignments = tuple(dict(item) for item in raw_assignments)
        if not assignments:
            return assignments
        expected: dict[int, str] = {}
        for assignment in assignments:
            fact_id = assignment.get("fact_id")
            tag_key = assignment.get("tag_key")
            if fact_id is None or tag_key is None:
                return None
            expected[int(fact_id)] = str(tag_key)
        rows = (
            await session.execute(
                select(TagAssignmentFact.id, TagAssignmentFact.tag_key).where(
                    TagAssignmentFact.tenant_id == tenant_id,
                    TagAssignmentFact.id.in_(expected),
                    TagAssignmentFact.subject_type == "dialogue_unit",
                    TagAssignmentFact.subject_id == dialogue_unit_id,
                    TagAssignmentFact.tagger_version_id == tagger_version_id,
                    TagAssignmentFact.deployment_id == deployment_id,
                    TagAssignmentFact.input_hash == input_hash,
                    TagAssignmentFact.tombstone.is_(False),
                )
            )
        ).all()
        actual = {int(row.id): str(row.tag_key) for row in rows}
        return assignments if actual == expected else None

    async def _find_reusable_business_product(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        dialogue_unit_id: int,
        tagger_version_id: int,
        input_hash: str,
        constrain_deployment: bool,
        deployment_id: int | None = None,
        exclude_job_id: int | None = None,
    ) -> tuple[TagExtractionRun, tuple[dict[str, Any], ...]] | None:
        """Find one validated, content-addressed persisted tag product."""

        statement = (
            select(TagExtractionRun)
            .where(
                TagExtractionRun.tenant_id == tenant_id,
                TagExtractionRun.subject_type == "dialogue_unit",
                TagExtractionRun.subject_id == dialogue_unit_id,
                TagExtractionRun.tagger_version_id == tagger_version_id,
                TagExtractionRun.input_hash == input_hash,
                TagExtractionRun.status.in_(("completed", "cached")),
            )
            .order_by(
                TagExtractionRun.finished_at.desc(),
                TagExtractionRun.id.desc(),
            )
            .limit(1)
        )
        if constrain_deployment:
            statement = statement.where(TagExtractionRun.deployment_id == deployment_id)
        if exclude_job_id is not None:
            statement = statement.where(TagExtractionRun.job_id != exclude_job_id)
        source_run = (await session.execute(statement)).scalar_one_or_none()
        if source_run is None:
            return None
        assignments = await self._valid_cached_assignments(
            session,
            run=source_run,
            tenant_id=tenant_id,
            dialogue_unit_id=dialogue_unit_id,
            tagger_version_id=tagger_version_id,
            deployment_id=source_run.deployment_id,
            input_hash=input_hash,
        )
        if assignments is None:
            return None
        return source_run, assignments

    @staticmethod
    def _prediction_from_business_product(
        prepared: PreparedDialogueInput,
        *,
        source_run: TagExtractionRun,
        assignments: tuple[dict[str, Any], ...],
    ) -> PredictionBatch:
        """Rehydrate the pure evaluator result without leaking fact identities."""

        def _without_fact_ids(
            values: tuple[dict[str, Any], ...] | list[dict[str, Any]],
        ) -> tuple[dict[str, Any], ...]:
            return tuple(
                {key: value for key, value in item.items() if key != "fact_id"} for item in values
            )

        clean_assignments = _without_fact_ids(assignments)
        raw_candidates = source_run.output_snapshot.get("candidate_facts")
        candidates = (
            _without_fact_ids(raw_candidates)
            if isinstance(raw_candidates, list)
            and all(isinstance(item, dict) for item in raw_candidates)
            else clean_assignments
        )
        raw_conflicts = source_run.output_snapshot.get("conflict_tag_keys", [])
        conflicts = (
            tuple(str(value) for value in raw_conflicts) if isinstance(raw_conflicts, list) else ()
        )
        harness_spec = resolve_harness_spec(prepared.tagger)
        scene_profile = build_scene_profile(
            scenario=prepared.scenario,
            subject_type=prepared.subject_type,
            store_id=(
                str(prepared.input_snapshot["store_id"])
                if prepared.input_snapshot.get("store_id") is not None
                else None
            ),
            transcript=prepared.transcript,
            segments=[segment for segment, _text in prepared.segment_texts],
        )
        return PredictionBatch(
            input_hash=prepared.input_hash,
            input_snapshot=prepared.input_snapshot,
            candidates=candidates,
            assignments=clean_assignments,
            review_items=(),
            conflict_tag_keys=conflicts,
            harness_spec=harness_spec,
            scene_profile=scene_profile,
            route=str(
                source_run.output_snapshot.get(
                    "harness_route",
                    harness_spec["orchestration"]["route"],
                )
            ),
            stage_traces=(),
            latency_ms=0,
            token_count=0,
            cost_units=0.0,
        )

    async def record_failed_subject(
        self,
        *,
        tenant_id: str,
        subject_type: str,
        subject_id: int,
        tagger_version_id: int,
        job_id: int,
        deployment_id: int | None,
        error: Exception,
        run_origin: str = "system",
        served_current: bool = False,
    ) -> TagExtractionRun:
        """Persist attributable failure lineage even when input preparation failed."""

        now = datetime.now(UTC)
        fallback_hash = canonical_checksum(
            {
                "job_id": job_id,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "tagger_version_id": tagger_version_id,
                "deployment_id": deployment_id,
                "failure_input": "unavailable",
            }
        )
        async with self._factory() as session, session.begin():
            deployment_stage: str | None = None
            deployment_revision: int | None = None
            if deployment_id is not None:
                deployment_snapshot = (
                    await session.execute(
                        select(TagDeployment.status, TagDeployment.revision).where(
                            TagDeployment.id == deployment_id,
                            TagDeployment.tenant_id == tenant_id,
                        )
                    )
                ).one_or_none()
                if deployment_snapshot is None:
                    raise AssignmentValidationError("extraction deployment does not exist")
                deployment_stage = str(deployment_snapshot.status)
                deployment_revision = int(deployment_snapshot.revision)
            run = (
                await session.execute(
                    select(TagExtractionRun)
                    .where(
                        TagExtractionRun.tenant_id == tenant_id,
                        TagExtractionRun.job_id == job_id,
                        TagExtractionRun.subject_type == subject_type,
                        TagExtractionRun.subject_id == subject_id,
                        TagExtractionRun.tagger_version_id == tagger_version_id,
                        TagExtractionRun.deployment_id == deployment_id,
                        TagExtractionRun.status.in_(["running", "failed"]),
                    )
                    .order_by(TagExtractionRun.id.desc())
                    .limit(1)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if run is None:
                run = TagExtractionRun(
                    tenant_id=tenant_id,
                    job_id=job_id,
                    origin=run_origin,
                    deployment_stage=deployment_stage,
                    deployment_revision=deployment_revision,
                    served_current=served_current,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    tagger_version_id=tagger_version_id,
                    deployment_id=deployment_id,
                    input_hash=fallback_hash,
                    input_snapshot={
                        "subject_type": subject_type,
                        "subject_id": subject_id,
                        "tagger_version_id": tagger_version_id,
                        "deployment_id": deployment_id,
                        "input_available": False,
                    },
                    output_snapshot={},
                    status="failed",
                    error_code=error.__class__.__name__[:64],
                    error_message=str(error)[:4_000],
                    started_at=now,
                    finished_at=now,
                )
                session.add(run)
                await session.flush()
            else:
                run.status = "failed"
                run.error_code = error.__class__.__name__[:64]
                run.error_message = str(error)[:4_000]
                run.finished_at = now
            return run

    async def extract_dialogue_unit(
        self,
        *,
        tenant_id: str,
        dialogue_unit_id: int,
        tagger_version_id: int,
        job_id: int,
        deployment_id: int | None,
        actor_user_id: int,
        publish_current: bool = True,
        run_origin: str = "system",
        served_current: bool = False,
        target_tag_keys: Sequence[str] | None = None,
        budget_policy_override: Mapping[str, int] | None = None,
    ) -> ExtractionResult:
        prepared = await self._prepare_dialogue_unit(
            tenant_id=tenant_id,
            dialogue_unit_id=dialogue_unit_id,
            tagger_version_id=tagger_version_id,
            target_tag_keys=target_tag_keys,
        )
        now = datetime.now(UTC)
        cached_result: ExtractionResult | None = None
        async with self._factory() as session, session.begin():
            deployment_stage: str | None = None
            deployment_revision: int | None = None
            if deployment_id is not None:
                deployment_snapshot = (
                    await session.execute(
                        select(TagDeployment.status, TagDeployment.revision).where(
                            TagDeployment.id == deployment_id,
                            TagDeployment.tenant_id == tenant_id,
                        )
                    )
                ).one_or_none()
                if deployment_snapshot is None:
                    raise AssignmentValidationError("extraction deployment does not exist")
                deployment_stage = str(deployment_snapshot.status)
                deployment_revision = int(deployment_snapshot.revision)
            run = (
                await session.execute(
                    select(TagExtractionRun)
                    .where(
                        TagExtractionRun.tenant_id == tenant_id,
                        TagExtractionRun.job_id == job_id,
                        TagExtractionRun.subject_type == "dialogue_unit",
                        TagExtractionRun.subject_id == dialogue_unit_id,
                        TagExtractionRun.input_hash == prepared.input_hash,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if run is not None and run.status in {"completed", "cached"}:
                cached_assignments = await self._valid_cached_assignments(
                    session,
                    run=run,
                    tenant_id=tenant_id,
                    dialogue_unit_id=dialogue_unit_id,
                    tagger_version_id=tagger_version_id,
                    deployment_id=deployment_id,
                    input_hash=prepared.input_hash,
                )
                if cached_assignments is not None:
                    cached_result = ExtractionResult(
                        run_id=run.id,
                        input_hash=prepared.input_hash,
                        input_snapshot=prepared.input_snapshot,
                        assignments=cached_assignments,
                        cached=True,
                    )
            if cached_result is None:
                reusable = await self._find_reusable_business_product(
                    session,
                    tenant_id=tenant_id,
                    dialogue_unit_id=dialogue_unit_id,
                    tagger_version_id=tagger_version_id,
                    input_hash=prepared.input_hash,
                    constrain_deployment=True,
                    deployment_id=deployment_id,
                    exclude_job_id=job_id,
                )
                if reusable is not None:
                    source_run, source_assignments = reusable
                    output_snapshot = dict(source_run.output_snapshot)
                    output_snapshot["reused_run_id"] = source_run.id
                    if run is None:
                        run = TagExtractionRun(
                            tenant_id=tenant_id,
                            job_id=job_id,
                            origin=run_origin,
                            deployment_stage=deployment_stage,
                            deployment_revision=deployment_revision,
                            served_current=served_current,
                            subject_type="dialogue_unit",
                            subject_id=dialogue_unit_id,
                            tagger_version_id=tagger_version_id,
                            deployment_id=deployment_id,
                            input_hash=prepared.input_hash,
                            input_snapshot=prepared.input_snapshot,
                            output_snapshot=output_snapshot,
                            status="cached",
                            started_at=now,
                            finished_at=now,
                        )
                        session.add(run)
                        await session.flush()
                    else:
                        run.tagger_version_id = tagger_version_id
                        run.deployment_id = deployment_id
                        run.input_snapshot = prepared.input_snapshot
                        run.output_snapshot = output_snapshot
                        run.status = "cached"
                        run.error_code = None
                        run.error_message = None
                        run.finished_at = now
                    cached_result = ExtractionResult(
                        run_id=run.id,
                        input_hash=prepared.input_hash,
                        input_snapshot=prepared.input_snapshot,
                        assignments=source_assignments,
                        cached=True,
                    )
            if cached_result is not None:
                run_id = cached_result.run_id
            elif run is None:
                run = TagExtractionRun(
                    tenant_id=tenant_id,
                    job_id=job_id,
                    origin=run_origin,
                    deployment_stage=deployment_stage,
                    deployment_revision=deployment_revision,
                    served_current=served_current,
                    subject_type="dialogue_unit",
                    subject_id=dialogue_unit_id,
                    tagger_version_id=tagger_version_id,
                    deployment_id=deployment_id,
                    input_hash=prepared.input_hash,
                    input_snapshot=prepared.input_snapshot,
                    output_snapshot={},
                    status="running",
                    started_at=now,
                )
                session.add(run)
                await session.flush()
                run_id = run.id
            else:
                run.status = "running"
                run.tagger_version_id = tagger_version_id
                run.deployment_id = deployment_id
                run.input_snapshot = prepared.input_snapshot
                run.output_snapshot = {}
                run.error_code = None
                run.error_message = None
                run.finished_at = None
                run_id = run.id
        if cached_result is not None:
            cached_selected_keys = {
                str(assignment["tag_key"]) for assignment in cached_result.assignments
            }
            if publish_current:
                for assignment in cached_result.assignments:
                    fact_id = assignment.get("fact_id")
                    if fact_id is not None:
                        await self._governance.ensure_current_fact(
                            tenant_id=tenant_id,
                            fact_id=int(fact_id),
                            extraction_run_id=cached_result.run_id,
                        )
                await self._governance.append_assignment_batch(
                    tenant_id=tenant_id,
                    subject_type="dialogue_unit",
                    subject_id=dialogue_unit_id,
                    assignments=[],
                    schema_version_id=int(cached_result.input_snapshot["schema_version_id"]),
                    tagger_version_id=tagger_version_id,
                    extraction_run_id=cached_result.run_id,
                    deployment_id=deployment_id,
                    input_hash=cached_result.input_hash,
                    actor_user_id=actor_user_id,
                    publish_current=True,
                    publish_current_tag_keys=cached_selected_keys,
                    replace_current_tag_keys=set(prepared.definitions),
                )
            await self._persist_harness_execution(
                tenant_id=tenant_id,
                run_id=cached_result.run_id,
                dialogue_unit_id=dialogue_unit_id,
                tagger_version_id=tagger_version_id,
                deployment_id=deployment_id,
                prediction=self._cached_prediction_batch(prepared, cached_result),
            )
            return cached_result

        prediction = await self._predict_prepared(
            prepared,
            budget_policy_override=budget_policy_override,
            usage_context=LLMUsageContext(
                deployment_id=deployment_id,
            ),
        )
        ordered_selected = self._dependency_order(
            prediction.assignments,
            prepared.definitions,
        )
        selected_keys = {str(assignment["tag_key"]) for assignment in ordered_selected}
        ordered_assignments = [
            *ordered_selected,
            *(
                assignment
                for assignment in prediction.candidates
                if str(assignment["tag_key"]) not in selected_keys
            ),
        ]
        facts = await self._governance.append_assignment_batch(
            tenant_id=tenant_id,
            subject_type="dialogue_unit",
            subject_id=dialogue_unit_id,
            assignments=ordered_assignments,
            schema_version_id=int(prediction.input_snapshot["schema_version_id"]),
            tagger_version_id=tagger_version_id,
            extraction_run_id=run_id,
            deployment_id=deployment_id,
            input_hash=prediction.input_hash,
            actor_user_id=actor_user_id,
            publish_current=publish_current,
            publish_current_tag_keys=selected_keys,
            replace_current_tag_keys=set(prepared.definitions),
        )
        persisted_candidates = [
            {**assignment, "fact_id": fact.id}
            for assignment, fact in zip(ordered_assignments, facts, strict=True)
        ]
        persisted_by_key = {
            str(assignment["tag_key"]): assignment for assignment in persisted_candidates
        }
        persisted = [
            persisted_by_key[str(assignment["tag_key"])] for assignment in ordered_selected
        ]
        harness_execution_id = await self._persist_harness_execution(
            tenant_id=tenant_id,
            run_id=run_id,
            dialogue_unit_id=dialogue_unit_id,
            tagger_version_id=tagger_version_id,
            deployment_id=deployment_id,
            prediction=prediction,
        )
        for reason in ("conflict", "low_confidence", "missing", "critical", "random"):
            representative_eligible = (
                run_origin == "serving"
                and deployment_id is not None
                and (deployment_stage == "shadow" or served_current)
            )
            if reason == "random" and not representative_eligible:
                continue
            subjects: list[dict[str, Any]] = []
            for item in prediction.review_items:
                if item["reason"] != reason:
                    continue
                subject = {key: value for key, value in item.items() if key != "reason"}
                subject.update(
                    {
                        "source_deployment_id": deployment_id,
                        "source_extraction_run_id": run_id,
                        "source_harness_execution_id": harness_execution_id,
                    }
                )
                proposed = persisted_by_key.get(str(item["tag_key"]))
                if proposed is not None:
                    subject["proposed_fact_id"] = int(proposed["fact_id"])
                subjects.append(subject)
            if subjects:
                representative_audit = reason == "random" and representative_eligible
                await self._governance.create_review_batch(
                    tenant_id=tenant_id,
                    reason=reason,
                    subjects=subjects,
                    actor_user_id=actor_user_id,
                    batch_id=f"extraction-{run_id}-{reason}",
                    review_bundle_id=f"harness-{harness_execution_id}-{reason}",
                    selection_policy=(
                        "representative_audit"
                        if representative_audit
                        else ("critical_positive" if reason == "critical" else "active_learning")
                    ),
                    selection_policy_version="1",
                    sampling_probability=(0.05 if representative_audit else None),
                    blind_mode=representative_audit or reason == "critical",
                    trusted_sampling_lineage=run_origin == "serving",
                )
        async with self._factory() as session, session.begin():
            run = (
                await session.execute(
                    select(TagExtractionRun)
                    .where(
                        TagExtractionRun.id == run_id,
                        TagExtractionRun.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one()
            run.status = "completed"
            run.output_snapshot = {
                "assignments": persisted,
                "candidate_facts": persisted_candidates,
                "conflict_tag_keys": list(prediction.conflict_tag_keys),
                "review_item_count": len(prediction.review_items),
                "harness_execution_id": harness_execution_id,
                "harness_route": prediction.route,
                "publish_current": publish_current,
            }
            run.finished_at = datetime.now(UTC)
        return ExtractionResult(
            run_id=run_id,
            input_hash=prediction.input_hash,
            input_snapshot=prediction.input_snapshot,
            assignments=tuple(persisted),
            cached=False,
            provider_tokens=(
                prediction.provider_input_tokens
                + prediction.provider_output_tokens
                + prediction.unknown_billed_tokens
            ),
            provider_calls=prediction.provider_calls,
            cost_microunits=prediction.cost_microunits,
        )


@dataclass(slots=True)
class TagExtractorHarnessTrialExecutor:
    """Execute optimizer candidates against immutable gold input snapshots."""

    extractor: TagExtractor
    efficiency_envelope: EfficiencyEnvelope = TOKEN_REDUCTION_V1
    materialized_dimensions: ClassVar[frozenset[str]] = frozenset(
        {"generation", "orchestration", "output"}
    )

    @staticmethod
    def _value_identity(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _macro_f1(
        cls,
        rows: Sequence[tuple[str, Any, Any]],
    ) -> float:
        if not rows:
            return 0.0
        scores: list[float] = []
        by_tag: dict[str, list[tuple[str, str]]] = {}
        for tag_key, actual, predicted in rows:
            by_tag.setdefault(tag_key, []).append(
                (cls._value_identity(actual), cls._value_identity(predicted))
            )
        for tag_rows in by_tag.values():
            classes = sorted(
                {actual for actual, _predicted in tag_rows}
                | {predicted for _actual, predicted in tag_rows}
            )
            for label in classes:
                true_positive = sum(
                    actual == label and predicted == label for actual, predicted in tag_rows
                )
                false_positive = sum(
                    actual != label and predicted == label for actual, predicted in tag_rows
                )
                false_negative = sum(
                    actual == label and predicted != label for actual, predicted in tag_rows
                )
                denominator = (2 * true_positive) + false_positive + false_negative
                scores.append((2 * true_positive / denominator) if denominator else 0.0)
        return sum(scores) / len(scores) if scores else 0.0

    @staticmethod
    def _truth_value(sample: Mapping[str, Any]) -> Any:
        return (
            {"__truth_state__": "absent"}
            if sample.get("truth_state") == "absent"
            else sample.get("gold_value")
        )

    @staticmethod
    def _predicted_value(
        *,
        sample: Mapping[str, Any],
        assignments: Mapping[str, Mapping[str, Any]],
    ) -> Any:
        assignment = assignments.get(str(sample["tag_key"]))
        return {"__truth_state__": "absent"} if assignment is None else assignment.get("tag_value")

    @staticmethod
    def _relative_reduction(*, candidate: int, baseline: int) -> float:
        if baseline > 0:
            return (baseline - candidate) / baseline
        return 0.0 if candidate == 0 else -1.0

    @staticmethod
    def _relative_increase(*, candidate: int, baseline: int) -> float:
        if baseline > 0:
            return (candidate - baseline) / baseline
        return 0.0 if candidate == 0 else 1.0

    @staticmethod
    def _paired_bootstrap_lower_bound(
        reductions: Sequence[float],
        *,
        iterations: int = _EFFICIENCY_BOOTSTRAP_ITERATIONS,
    ) -> float:
        """Deterministic one-sided 95% paired-bootstrap lower bound."""

        if not reductions:
            return -1.0
        if len(reductions) == 1:
            return float(reductions[0])
        rng = Random(0xA6D10)  # noqa: S311 - deterministic statistical bootstrap
        count = len(reductions)
        means = sorted(
            sum(reductions[rng.randrange(count)] for _ in range(count)) / count
            for _ in range(iterations)
        )
        return float(means[max(0, math.floor(iterations * 0.05) - 1)])

    def estimate_trial_budget(
        self,
        candidate: dict[str, Any],
        samples: list[dict[str, Any]],
    ) -> Mapping[str, int]:
        """Conservatively reserve aggregate calls/tokens before Provider I/O."""

        route = str(candidate.get("orchestration", {}).get("route", "weak_llm"))
        route_adapters: tuple[LLMAdapter | None, ...] = {
            "rule_only": (),
            "weak_llm": (self.extractor._weak_llm,),
            "rule_llm_fusion": (self.extractor._weak_llm,),
            "weak_then_strong_critic": (
                self.extractor._weak_llm,
                self.extractor._strong_llm,
            ),
        }.get(route, (self.extractor._weak_llm,))
        if not route_adapters:
            return {
                "provider_calls": 0,
                "provider_tokens": 0,
                "cost_microunits": 0,
            }
        unique_subjects: dict[tuple[str, int, str], Mapping[str, Any]] = {}
        for sample in samples:
            subject_type = sample.get("subject_type")
            subject_id = sample.get("subject_id")
            snapshot = sample.get("input_snapshot")
            if (
                subject_type not in {"dialogue_unit", "reception"}
                or isinstance(subject_id, bool)
                or not isinstance(subject_id, int)
                or not isinstance(snapshot, Mapping)
            ):
                continue
            unique_subjects[
                (
                    str(subject_type),
                    subject_id,
                    canonical_checksum(dict(snapshot)),
                )
            ] = snapshot
        generation = candidate.get("generation", {})
        max_input_tokens = int(generation.get("max_input_tokens", 12_000))
        max_output_tokens = int(generation.get("max_tokens", 2_048))
        prompt_tokens = estimate_prompt_tokens(str(generation.get("prompt_template", "")))
        estimated_tokens = 0
        estimated_calls = 0
        estimated_cost = 0
        cost_complete = True
        for snapshot in unique_subjects.values():
            payload_tokens = estimate_prompt_tokens(
                json.dumps(
                    snapshot,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            input_tokens = min(
                max_input_tokens,
                prompt_tokens + payload_tokens + 512,
            )
            for adapter in route_adapters:
                if adapter is None:
                    estimated_calls += 2
                    estimated_tokens += 2 * (input_tokens + max_output_tokens)
                    cost_complete = False
                    continue
                attempt_bound = TagExtractor._provider_attempt_bound(adapter)
                # The main structured call and its one bounded format repair
                # are both reserved at the larger main-call envelope.
                estimated_calls += 2 * attempt_bound
                estimated_tokens += 2 * attempt_bound * (input_tokens + max_output_tokens)
                call_cost = TagExtractor._estimate_provider_cost(
                    adapter,
                    input_tokens=input_tokens,
                    output_tokens=max_output_tokens,
                )
                if call_cost is None:
                    cost_complete = False
                else:
                    estimated_cost += 2 * call_cost
        estimate = {
            "provider_calls": estimated_calls,
            "provider_tokens": estimated_tokens,
        }
        if cost_complete:
            estimate["cost_microunits"] = estimated_cost
        return estimate

    async def execute_trial(
        self,
        candidate: dict[str, Any],
        samples: list[dict[str, Any]],
        *,
        optimization_run_id: int | None = None,
        optimization_trial_id: int | None = None,
    ) -> Mapping[str, Any]:
        if (optimization_run_id is None) != (optimization_trial_id is None):
            raise ValueError("optimization run and trial correlation IDs must be provided together")
        replay_samples = [
            sample for sample in samples if sample.get("split") in {"train", "validation"}
        ]
        grouped: dict[
            tuple[str, int, str, int, str],
            list[dict[str, Any]],
        ] = {}
        subject_snapshots: dict[tuple[str, int, str, int], set[str]] = {}
        errors: list[dict[str, str]] = []
        for sample in replay_samples:
            snapshot = sample.get("input_snapshot")
            tenant_id = sample.get("tenant_id")
            baseline_id = sample.get("baseline_tagger_version_id")
            subject_type = sample.get("subject_type")
            subject_id = sample.get("subject_id")
            if (
                not isinstance(snapshot, Mapping)
                or not snapshot
                or not isinstance(tenant_id, str)
                or not tenant_id
                or isinstance(baseline_id, bool)
                or not isinstance(baseline_id, int)
                or baseline_id <= 0
                or subject_type not in {"dialogue_unit", "reception"}
                or isinstance(subject_id, bool)
                or not isinstance(subject_id, int)
                or subject_id <= 0
            ):
                errors.append(
                    {
                        "error_code": "invalid_frozen_sample",
                        "subject": f"{subject_type}:{subject_id}",
                    }
                )
                continue
            snapshot_checksum = canonical_checksum(dict(snapshot))
            subject_identity = (
                tenant_id,
                baseline_id,
                str(subject_type),
                subject_id,
            )
            subject_snapshots.setdefault(subject_identity, set()).add(snapshot_checksum)
            grouped.setdefault(
                (tenant_id, baseline_id, str(subject_type), subject_id, snapshot_checksum),
                [],
            ).append(sample)

        ambiguous_subjects = {
            identity for identity, checksums in subject_snapshots.items() if len(checksums) != 1
        }
        if ambiguous_subjects:
            grouped = {
                identity: subject_samples
                for identity, subject_samples in grouped.items()
                if identity[:4] not in ambiguous_subjects
            }
            errors.extend(
                {
                    "error_code": "frozen_snapshot_binding_conflict",
                    "subject": f"{subject_type}:{subject_id}",
                }
                for _tenant_id, _baseline_id, subject_type, subject_id in sorted(ambiguous_subjects)
            )

        predictions: dict[
            tuple[str, int, str, int, str],
            PredictionBatch,
        ] = {}
        for identity, subject_samples in grouped.items():
            tenant_id, baseline_id, subject_type, subject_id, _snapshot_checksum = identity
            usage_context: LLMUsageContext | None = None
            if optimization_run_id is not None and optimization_trial_id is not None:
                subject_correlation = canonical_checksum(
                    {
                        "subject_type": subject_type,
                        "subject_id": subject_id,
                        "snapshot": _snapshot_checksum,
                    }
                )[:16]
                usage_context = LLMUsageContext(
                    logical_request_id=(
                        f"opt:{optimization_run_id}:{optimization_trial_id}:{subject_correlation}"
                    ),
                    optimization_run_id=optimization_run_id,
                    optimization_trial_id=optimization_trial_id,
                    require_durable_ledger=True,
                )
            try:
                predictions[identity] = await self.extractor.predict_materialized_frozen_input(
                    tenant_id=tenant_id,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    input_snapshot=deepcopy(dict(subject_samples[0]["input_snapshot"])),
                    baseline_tagger_version_id=baseline_id,
                    harness_spec=deepcopy(candidate),
                    target_tag_keys=sorted({str(sample["tag_key"]) for sample in subject_samples}),
                    usage_context=usage_context,
                )
            except Exception as exc:
                errors.append(
                    {
                        "error_code": exc.__class__.__name__,
                        "subject": f"{subject_type}:{subject_id}",
                    }
                )

        validation_rows: list[tuple[str, Any, Any]] = []
        baseline_rows: list[tuple[str, Any, Any]] = []
        flips: list[dict[str, Any]] = []
        critical_total = 0
        critical_correct = 0
        evidence_required_total = 0
        evidence_covered = 0
        review_count = 0
        validation_count = 0
        gold_labels: list[dict[str, Any]] = []
        candidate_predictions: dict[
            tuple[str, int],
            Sequence[Mapping[str, Any]],
        ] = {}
        baseline_prediction_maps: dict[
            tuple[str, int],
            dict[str, dict[str, Any]],
        ] = {}
        definition_index: dict[str, dict[str, Any]] = {}
        for sample in replay_samples:
            raw_definitions = sample.get("schema_definitions")
            if isinstance(raw_definitions, Sequence) and not isinstance(
                raw_definitions,
                str | bytes,
            ):
                for definition in raw_definitions:
                    if isinstance(definition, Mapping) and definition.get("key"):
                        definition_index[str(definition["key"])] = deepcopy(dict(definition))
            tag_key = str(sample.get("tag_key", ""))
            if tag_key and tag_key not in definition_index:
                definition_index[tag_key] = {
                    "key": tag_key,
                    "allowed_values": [
                        value
                        for value in (
                            sample.get("gold_value"),
                            sample.get("baseline_predicted_value"),
                        )
                        if value is not None
                    ],
                    "negative_values": [],
                    "critical_values": (
                        [sample.get("gold_value")]
                        if sample.get("is_critical") and sample.get("gold_value") is not None
                        else []
                    ),
                    "evidence_required": bool(sample.get("evidence_required")),
                }
        for identity, subject_samples in grouped.items():
            prediction = predictions.get(identity)
            if prediction is None:
                continue
            prediction_subject_identity = (identity[2], identity[3])
            candidate_predictions[prediction_subject_identity] = tuple(prediction.assignments)
            assignments = {str(item["tag_key"]): item for item in prediction.assignments}
            reviewed_keys = {
                str(item.get("tag_key"))
                for item in prediction.review_items
                if item.get("tag_key") is not None
            }
            for sample in subject_samples:
                if sample.get("split") != "validation":
                    continue
                tag_key = str(sample["tag_key"])
                actual = self._truth_value(sample)
                predicted = self._predicted_value(
                    sample=sample,
                    assignments=assignments,
                )
                baseline_predicted = (
                    {"__truth_state__": "absent"}
                    if sample.get("baseline_predicted_value") is None
                    else sample.get("baseline_predicted_value")
                )
                validation_rows.append((tag_key, actual, predicted))
                baseline_rows.append((tag_key, actual, baseline_predicted))
                truth_identity = self._value_identity(actual)
                candidate_correct = truth_identity == self._value_identity(predicted)
                baseline_correct = truth_identity == self._value_identity(baseline_predicted)
                if candidate_correct != baseline_correct:
                    flips.append(
                        {
                            "subject_type": identity[2],
                            "subject_id": identity[3],
                            "tag_key": tag_key,
                            "gold_value": deepcopy(sample.get("gold_value")),
                            "baseline_value": deepcopy(sample.get("baseline_predicted_value")),
                            "candidate_value": deepcopy(
                                None
                                if isinstance(predicted, Mapping) and "__truth_state__" in predicted
                                else predicted
                            ),
                            "direction": "fixed" if candidate_correct else "broken",
                        }
                    )
                validation_count += 1
                gold_labels.append(
                    {
                        "subject_type": identity[2],
                        "subject_id": identity[3],
                        "tag_key": tag_key,
                        "tag_value": deepcopy(sample.get("gold_value")),
                        "truth_state": str(sample.get("truth_state") or "present"),
                        "evidence_refs": deepcopy(sample.get("gold_evidence_refs") or []),
                    }
                )
                baseline_assignment = sample.get("baseline_assignment")
                if isinstance(baseline_assignment, Mapping):
                    baseline_prediction_maps.setdefault(
                        prediction_subject_identity,
                        {},
                    )[tag_key] = deepcopy(dict(baseline_assignment))
                elif sample.get("baseline_predicted_value") is not None:
                    baseline_prediction_maps.setdefault(
                        prediction_subject_identity,
                        {},
                    )[tag_key] = {
                        "tag_key": tag_key,
                        "tag_value": deepcopy(sample.get("baseline_predicted_value")),
                        "confidence": float(sample.get("score", 0)),
                        "evidence_refs": [],
                    }
                if tag_key in reviewed_keys:
                    review_count += 1
                if bool(sample.get("is_critical")):
                    critical_total += 1
                    critical_correct += int(
                        self._value_identity(actual) == self._value_identity(predicted)
                    )
                if bool(sample.get("evidence_required")) and sample.get("truth_state") == "present":
                    evidence_required_total += 1
                    assignment = assignments.get(tag_key)
                    evidence_covered += int(
                        assignment is not None and bool(assignment.get("evidence_refs"))
                    )

        # Reuse the canonical release evaluator so optimizer feasibility and
        # production promotion cannot disagree on F1, Wilson LCB, evidence or
        # integrity semantics.
        from audio_graphy.services.tag_evaluator import compute_evaluation_summary

        candidate_summary = compute_evaluation_summary(
            gold_labels=gold_labels,
            predictions=candidate_predictions,
            definitions=definition_index,
            extraction_errors=len(errors),
            subject_count=len(
                {(str(item["subject_type"]), int(item["subject_id"])) for item in gold_labels}
            ),
            lineage_violation_count=len(ambiguous_subjects),
        )
        baseline_summary = compute_evaluation_summary(
            gold_labels=gold_labels,
            predictions={
                identity: tuple(assignments.values())
                for identity, assignments in baseline_prediction_maps.items()
            },
            definitions=definition_index,
            extraction_errors=0,
            subject_count=len(
                {(str(item["subject_type"]), int(item["subject_id"])) for item in gold_labels}
            ),
        )
        candidate_macro_f1 = float(candidate_summary.metrics["macro_f1"])
        baseline_macro_f1 = float(baseline_summary.metrics["macro_f1"])
        critical_recall = float(candidate_summary.metrics["critical_recall"])
        critical_recall_lcb = float(candidate_summary.metrics["critical_recall_lcb"])
        evidence_coverage = float(candidate_summary.metrics["evidence_coverage"])

        batches = list(predictions.values())
        provider_input_tokens = sum(batch.provider_input_tokens for batch in batches)
        provider_output_tokens = sum(batch.provider_output_tokens for batch in batches)
        reused_input_tokens = sum(batch.reused_input_tokens for batch in batches)
        reused_output_tokens = sum(batch.reused_output_tokens for batch in batches)
        provider_calls = sum(batch.provider_calls for batch in batches)
        cache_hits = sum(batch.cache_hits for batch in batches)
        cost_microunits = sum(batch.cost_microunits for batch in batches)
        counterfactual_saved_cost_microunits = sum(
            int(getattr(batch, "counterfactual_saved_cost_microunits", 0)) for batch in batches
        )
        unknown_billed_tokens = sum(
            int(getattr(batch, "unknown_billed_tokens", 0)) for batch in batches
        )
        cold_cache_cost_microunits = cost_microunits + counterfactual_saved_cost_microunits
        cold_provider_tokens = (
            provider_input_tokens
            + provider_output_tokens
            + reused_input_tokens
            + reused_output_tokens
        )
        cold_provider_calls = provider_calls + cache_hits

        baseline_executions: dict[str, dict[str, int]] = {}
        baseline_execution_by_subject: dict[
            tuple[str, int, str, int, str],
            str,
        ] = {}
        baseline_measurement_complete = True
        required_baseline_fields = (
            "provider_input_tokens",
            "provider_output_tokens",
            "reused_input_tokens",
            "reused_output_tokens",
            "provider_calls",
            "cache_hits",
            "provider_cold_cost_microunits",
            "provider_latency_ms",
            "unknown_billed_tokens",
        )
        for identity, subject_samples in grouped.items():
            if any(
                not isinstance(sample.get("baseline_reviewed"), bool) for sample in subject_samples
            ):
                baseline_measurement_complete = False
            execution_ids = {
                str(sample["harness_execution_id"])
                for sample in subject_samples
                if sample.get("harness_execution_id") is not None
            }
            if len(execution_ids) != 1:
                baseline_measurement_complete = False
                continue
            execution_id = next(iter(execution_ids))
            measurements: set[tuple[int, ...]] = set()
            for sample in subject_samples:
                values: list[int] = []
                valid = True
                for field in required_baseline_fields:
                    value = sample.get(field)
                    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                        valid = False
                        break
                    values.append(value)
                if valid:
                    measurements.add(tuple(values))
            if len(measurements) != 1:
                baseline_measurement_complete = False
                continue
            (
                baseline_input_tokens,
                baseline_output_tokens,
                baseline_reused_input_tokens,
                baseline_reused_output_tokens,
                baseline_provider_calls,
                baseline_cache_hits,
                baseline_cold_cost,
                baseline_latency,
                baseline_unknown_billed_tokens,
            ) = next(iter(measurements))
            measurement = {
                "cold_tokens": (
                    baseline_input_tokens
                    + baseline_output_tokens
                    + baseline_reused_input_tokens
                    + baseline_reused_output_tokens
                ),
                "cold_cost": baseline_cold_cost,
                "cold_calls": baseline_provider_calls + baseline_cache_hits,
                "latency": baseline_latency,
                "unknown_billed_tokens": baseline_unknown_billed_tokens,
            }
            existing_measurement = baseline_executions.setdefault(
                execution_id,
                measurement,
            )
            if existing_measurement != measurement:
                baseline_measurement_complete = False
                continue
            if execution_id in baseline_execution_by_subject.values():
                baseline_measurement_complete = False
                continue
            baseline_execution_by_subject[identity] = execution_id

        baseline_tokens = sum(value["cold_tokens"] for value in baseline_executions.values())
        baseline_cost = sum(value["cold_cost"] for value in baseline_executions.values())
        baseline_calls = sum(value["cold_calls"] for value in baseline_executions.values())
        baseline_latencies = sorted(value["latency"] for value in baseline_executions.values())
        candidate_latencies = sorted(batch.latency_ms for batch in batches)

        def p95(values: Sequence[int]) -> int:
            return values[max(0, math.ceil(len(values) * 0.95) - 1)] if values else 0

        usage_measurement_complete = not (
            (cold_provider_calls > 0 and cold_provider_tokens <= 0)
            or (cold_provider_calls > 0 and cold_cache_cost_microunits <= 0)
            or unknown_billed_tokens > 0
            or any(
                value["cold_calls"] > 0
                and (
                    value["cold_tokens"] <= 0
                    or value["cold_cost"] <= 0
                    or value["unknown_billed_tokens"] > 0
                )
                for value in baseline_executions.values()
            )
        )
        measurement_complete = (
            not errors
            and len(predictions) == len(grouped)
            and baseline_measurement_complete
            and len(baseline_execution_by_subject) == len(grouped)
            and usage_measurement_complete
        )
        provider_token_delta = cold_provider_tokens - baseline_tokens
        cost_delta = cold_cache_cost_microunits - baseline_cost
        provider_call_delta = cold_provider_calls - baseline_calls
        candidate_p95_latency = p95(candidate_latencies)
        baseline_p95_latency = p95(baseline_latencies)
        p95_latency_delta = candidate_p95_latency - baseline_p95_latency
        p95_latency_regression_rate = self._relative_increase(
            candidate=candidate_p95_latency,
            baseline=baseline_p95_latency,
        )
        cold_token_reduction = self._relative_reduction(
            candidate=cold_provider_tokens,
            baseline=baseline_tokens,
        )
        cold_cost_reduction = self._relative_reduction(
            candidate=cold_cache_cost_microunits,
            baseline=baseline_cost,
        )
        paired_token_reductions: list[float] = []
        for identity, prediction in predictions.items():
            paired_execution_id = baseline_execution_by_subject.get(identity)
            if paired_execution_id is None:
                continue
            baseline_measurement = baseline_executions[paired_execution_id]
            candidate_subject_tokens = (
                prediction.provider_input_tokens
                + prediction.provider_output_tokens
                + prediction.reused_input_tokens
                + prediction.reused_output_tokens
            )
            paired_token_reductions.append(
                self._relative_reduction(
                    candidate=candidate_subject_tokens,
                    baseline=baseline_measurement["cold_tokens"],
                )
            )
        paired_token_reduction_lcb = self._paired_bootstrap_lower_bound(paired_token_reductions)
        review_rate = review_count / validation_count if validation_count else 0.0
        baseline_review_count = sum(
            sample.get("baseline_reviewed") is True
            for identity, subject_samples in grouped.items()
            if identity in predictions
            for sample in subject_samples
            if sample.get("split") == "validation"
        )
        baseline_review_rate = baseline_review_count / validation_count if validation_count else 0.0
        review_rate_delta = review_rate - baseline_review_rate
        envelope = self.efficiency_envelope
        efficiency_gate_results = {
            "measurement_complete": measurement_complete,
            "cold_token_reduction": (cold_token_reduction >= envelope.min_cold_token_reduction),
            "paired_token_reduction_lcb": (
                paired_token_reduction_lcb >= envelope.min_paired_token_reduction_lcb
            ),
            "cold_cost_reduction": (cold_cost_reduction >= envelope.min_cold_cost_reduction),
            "p95_latency_regression": (
                p95_latency_regression_rate <= envelope.max_p95_latency_regression
            ),
            "review_rate_increase": (review_rate_delta <= envelope.max_review_rate_increase),
            "provider_calls_nonincrease": (
                provider_call_delta <= 0 if envelope.require_provider_calls_nonincrease else True
            ),
        }
        efficiency_gate_passed = all(efficiency_gate_results.values())
        quality_gate_passed = (
            measurement_complete
            and candidate_macro_f1 >= 0.80
            and candidate_macro_f1 >= baseline_macro_f1 - 0.01
            and critical_recall_lcb >= 0.95
            and evidence_coverage >= 0.98
            and float(candidate_summary.metrics["error_rate"]) < 0.01
            and float(candidate_summary.metrics["schema_violation_count"]) == 0
            and float(candidate_summary.metrics["evidence_violation_count"]) == 0
            and float(candidate_summary.metrics["lineage_violation_count"]) == 0
        )
        feasible = quality_gate_passed and efficiency_gate_passed
        return {
            "measurement_source": "tag_extractor_frozen_replay",
            "measurement_complete": measurement_complete,
            "provider_input_tokens": provider_input_tokens,
            "provider_output_tokens": provider_output_tokens,
            "provider_tokens": provider_input_tokens + provider_output_tokens,
            "counterfactual_saved_input_tokens": reused_input_tokens,
            "counterfactual_saved_output_tokens": reused_output_tokens,
            "cold_provider_tokens": cold_provider_tokens,
            "provider_token_delta": provider_token_delta,
            "baseline_cold_provider_tokens": baseline_tokens,
            "cold_token_reduction": cold_token_reduction,
            "paired_token_reduction_lcb": paired_token_reduction_lcb,
            "paired_subject_count": len(paired_token_reductions),
            "provider_calls": provider_calls,
            "cold_provider_calls": cold_provider_calls,
            "provider_call_delta": provider_call_delta,
            "baseline_cold_provider_calls": baseline_calls,
            "cache_hits": cache_hits,
            "cost_microunits": cost_microunits,
            "counterfactual_saved_cost_microunits": (counterfactual_saved_cost_microunits),
            "cold_cache_cost_microunits": cold_cache_cost_microunits,
            "cost_delta": cost_delta,
            "baseline_cold_cache_cost_microunits": baseline_cost,
            "cold_cost_reduction": cold_cost_reduction,
            "p95_latency_ms": candidate_p95_latency,
            "baseline_p95_latency_ms": baseline_p95_latency,
            "p95_latency_delta": p95_latency_delta,
            "p95_latency_regression_rate": p95_latency_regression_rate,
            "macro_f1": candidate_macro_f1,
            "baseline_macro_f1": baseline_macro_f1,
            "quality_delta": candidate_macro_f1 - baseline_macro_f1,
            "label_metrics": deepcopy(candidate_summary.label_metrics),
            "baseline_label_metrics": deepcopy(baseline_summary.label_metrics),
            # Regressions first: a reviewer needs to see what the candidate broke
            # before being told what it fixed.
            "flips": sorted(
                flips,
                key=lambda item: (
                    item["direction"] != "broken",
                    str(item["subject_type"]),
                    int(item["subject_id"]),
                    str(item["tag_key"]),
                ),
            )[:_MAX_REPORTED_FLIPS],
            "flip_total": len(flips),
            "critical_recall": critical_recall,
            "critical_recall_lcb": critical_recall_lcb,
            "evidence_coverage": evidence_coverage,
            "schema_violation_count": int(candidate_summary.metrics["schema_violation_count"]),
            "evidence_violation_count": int(candidate_summary.metrics["evidence_violation_count"]),
            "lineage_violation_count": int(candidate_summary.metrics["lineage_violation_count"]),
            "error_rate": float(candidate_summary.metrics["error_rate"]),
            "review_rate": review_rate,
            "baseline_review_rate": baseline_review_rate,
            "review_rate_delta": review_rate_delta,
            "strong_escalations": sum(batch.strong_escalations for batch in batches),
            "unknown_billed_tokens": unknown_billed_tokens,
            "efficiency_envelope": envelope.key,
            "quality_gate_passed": quality_gate_passed,
            "efficiency_gate_results": efficiency_gate_results,
            "efficiency_gate_passed": efficiency_gate_passed,
            "failed_subject_count": len(errors),
            "errors": errors[:16],
            "feasible": feasible,
        }


__all__ = [
    "ExtractionResult",
    "PredictionBatch",
    "TagExtractor",
    "TagExtractorHarnessTrialExecutor",
]
