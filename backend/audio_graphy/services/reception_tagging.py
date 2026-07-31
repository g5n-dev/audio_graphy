"""Evidence-bound dialogue tag production and database-backed insights."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, cast

from sqlalchemy import and_, desc, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.analytics.tag_insights import analyze_tag_insights
from audio_graphy.core.pii import scrubbed_segment_text
from audio_graphy.errors import ConflictError, NotFoundError, ValidationError
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
)
from audio_graphy.schemas.reception_tags import (
    ALL_DIALOGUE_TARGET_LABELS,
    MAX_EVIDENCE_SUMMARY_ITEMS,
    MAX_RECEPTION_OUTPUT_EVIDENCE_REFS,
    CorrectDialogueTagRequest,
    DeriveDialogueTagsRequest,
    DialogueTargetLabel,
)
from audio_graphy.schemas.tag_insights import (
    MAX_GROUPS,
    AnalyzeTagInsightsRequest,
    AnalyzeTagInsightsResponse,
    EvidenceRef,
    MergeStrategy,
    TagAssignment,
    TagGroup,
    TimeWindow,
    TrendGranularity,
)
from audio_graphy.services.stage_projection import (
    project_stage_change_in_session,
)
from audio_graphy.services.tag_governance import (
    AssignmentValidationError,
    GovernanceConflictError,
    TagGovernanceService,
)

Scenario = Literal["gold", "automotive", "custom"]
MissingReason = Literal[
    "no_verified_segment_evidence",
    "missing_stage",
    "no_rule_match",
]


@dataclass(frozen=True, slots=True)
class SegmentEvidence:
    """Verified persistent segment used by a rule."""

    segment_id: int
    recording_id: int
    source_start_sec: float
    source_end_sec: float
    text: str
    timeline_start_sec: float | None = None
    timeline_end_sec: float | None = None

    def to_reference(self) -> dict[str, Any]:
        """Return an explicit, unambiguous source/timeline evidence span."""
        has_timeline = self.timeline_start_sec is not None and self.timeline_end_sec is not None
        if self.timeline_start_sec is not None and self.timeline_end_sec is not None:
            primary_start = self.timeline_start_sec
            primary_end = self.timeline_end_sec
        else:
            primary_start = self.source_start_sec
            primary_end = self.source_end_sec
        reference: dict[str, Any] = {
            "ref_id": f"segment:{self.segment_id}",
            "kind": "audio",
            "segment_id": self.segment_id,
            "recording_id": self.recording_id,
            "coordinate_space": ("reception_timeline" if has_timeline else "source"),
            "source_start_sec": self.source_start_sec,
            "source_end_sec": self.source_end_sec,
            "start_ms": round(primary_start * 1_000),
            "end_ms": round(primary_end * 1_000),
        }
        if has_timeline:
            reference["timeline_start_sec"] = self.timeline_start_sec
            reference["timeline_end_sec"] = self.timeline_end_sec
        return reference


@dataclass(frozen=True, slots=True)
class UnitEvidence:
    """One persistent dialogue unit with its verified segment references."""

    unit_id: int
    unit_index: int
    start_sec: float
    end_sec: float
    business_stage: str | None
    segments: tuple[SegmentEvidence, ...]


@dataclass(frozen=True, slots=True)
class DerivedRuleTag:
    label_key: DialogueTargetLabel
    label_value: str
    confidence: float
    evidence: tuple[SegmentEvidence, ...]


@dataclass(frozen=True, slots=True)
class _RuleOption:
    value: str
    phrases: tuple[str, ...]
    confidence: float


@dataclass(frozen=True, slots=True)
class MissingTag:
    dialogue_unit_id: int
    unit_index: int
    label_key: DialogueTargetLabel
    reason: MissingReason


@dataclass(frozen=True, slots=True)
class DeriveTagsResult:
    assignments: list[DialogueTagAssignment]
    missing: list[MissingTag]
    superseded_count: int
    no_op: bool


@dataclass(frozen=True, slots=True)
class CorrectDialogueTagResult:
    reception_id: int
    reception_version: int
    superseded_assignment_id: int
    assignment: DialogueTagAssignment


@dataclass(frozen=True, slots=True)
class EvidenceSummary:
    reception_id: int
    dialogue_unit_id: int
    group_id: str
    label_key: str
    label_value: str
    confidence: float | None
    evidence_count: int
    evidence_refs: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class _InsightAssignmentRow:
    reception: Reception
    unit: DialogueUnit
    group_key: str
    group_version: str
    label_key: str
    label_value: Any
    confidence: float | None
    source: str
    priority: int
    evidence_refs: list[Any]
    assigned_at: datetime


def _canonical_group(fact: TagAssignmentFact) -> tuple[str, str]:
    return (
        f"schema.{fact.schema_version_id or 0}",
        (f"tagger.{fact.tagger_version_id}" if fact.tagger_version_id is not None else "manual"),
    )


def _canonical_workspace_group_version(fact: TagAssignmentFact) -> str:
    tagger_ref = (
        f"tagger:{fact.tagger_version_id}"
        if fact.tagger_version_id is not None
        else "tagger:manual"
    )
    return f"schema:{fact.schema_version_id}|{tagger_ref}"


def _canonical_assignment_projection(
    fact: TagAssignmentFact,
) -> DialogueTagAssignment:
    value = fact.tag_value
    label_value = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    )
    return DialogueTagAssignment(
        id=fact.id,
        tenant_id=fact.tenant_id,
        reception_id=cast(int, fact.reception_id),
        dialogue_unit_id=fact.dialogue_unit_id or fact.subject_id,
        group_key="canonical",
        group_version=_canonical_workspace_group_version(fact),
        label_key=fact.tag_key,
        label_value=label_value,
        confidence=fact.confidence,
        source=fact.source,
        priority=1_000 if fact.source == "manual" else 500,
        evidence_refs=deepcopy(fact.evidence_refs),
        model_run_id=f"fact:{fact.id}",
        is_current=True,
        assigned_at=fact.assigned_at,
    )


def _select_evidence_subset(
    raw_refs: object,
    requested_ref_ids: Sequence[str],
    *,
    assignment_id: int,
) -> list[dict[str, Any]]:
    original_refs: dict[str, dict[str, Any]] = {}
    if isinstance(raw_refs, list):
        for raw in raw_refs:
            if not isinstance(raw, dict):
                continue
            ref_id = raw.get("ref_id")
            if isinstance(ref_id, str) and ref_id and ref_id not in original_refs:
                original_refs[ref_id] = raw
    unknown_refs = [ref_id for ref_id in requested_ref_ids if ref_id not in original_refs]
    if unknown_refs:
        raise ValidationError(
            "Manual evidence must be a subset of the current assignment evidence",
            code="TAG_EVIDENCE_SUBSET_INVALID",
            detail={
                "assignment_id": assignment_id,
                "unknown_ref_ids": unknown_refs,
            },
        )
    return [deepcopy(original_refs[ref_id]) for ref_id in requested_ref_ids]


@dataclass(frozen=True, slots=True)
class ReceptionInsightsResult:
    page: int
    page_size: int
    total_receptions: int
    returned_reception_ids: list[int]
    total_assignments: int
    loaded_assignment_count: int
    assignment_limit: int
    truncated: bool
    assignment_truncated: bool
    group_truncated: bool
    difference_truncated: bool
    evidence_truncated: bool
    evidence_ref_count: int
    evidence_summary_total: int
    evidence_summary_limit: int
    evidence_summary_truncated: bool
    selection_mode: Literal["current", "exact_versions"]
    selected_group_ids: list[str]
    insights: AnalyzeTagInsightsResponse | None
    evidence_summary: list[EvidenceSummary]


_RULES: dict[
    Scenario,
    dict[DialogueTargetLabel, tuple[_RuleOption, ...]],
] = {
    "gold": {
        "intent": (
            _RuleOption(
                "high",
                (
                    "今天就买",
                    "我就要这个",
                    "帮我包起来",
                    "现在付款",
                    "直接下单",
                    "交定金",
                ),
                0.92,
            ),
            _RuleOption(
                "medium",
                ("想买", "考虑一下", "先看看", "比较一下", "试戴"),
                0.82,
            ),
        ),
        "objection": (
            _RuleOption(
                "price",
                ("价格太高", "有点贵", "太贵了", "工费太高", "超过预算"),
                0.9,
            ),
            _RuleOption(
                "product",
                ("纯度不够", "克重不合适", "款式不喜欢", "没有证书"),
                0.86,
            ),
            _RuleOption(
                "trust",
                ("担心是假", "怕买到假", "怎么保真", "不相信证书"),
                0.88,
            ),
            _RuleOption(
                "decision",
                ("要和家人商量", "需要家人同意", "再比较几家"),
                0.84,
            ),
        ),
        "next_step": (
            _RuleOption("try_on", ("安排试戴", "先试戴", "试戴一下"), 0.9),
            _RuleOption("quotation", ("给个报价", "核算价格", "算一下价格"), 0.88),
            _RuleOption("reservation", ("预留", "留货", "交定金"), 0.91),
            _RuleOption("follow_up", ("稍后联系", "电话回访", "微信跟进"), 0.86),
            _RuleOption("checkout", ("安排结账", "现在付款", "去收银台"), 0.93),
        ),
        "compliance_risk": (
            _RuleOption(
                "absolute_promise",
                ("保证升值", "稳赚不赔", "绝对保值", "肯定涨价"),
                0.96,
            ),
            _RuleOption(
                "off_book_payment",
                ("转到个人账户", "私下转账", "个人收款码"),
                0.98,
            ),
            _RuleOption(
                "privacy_exposure",
                ("把身份证号发我", "把银行卡号发我", "说一下验证码"),
                0.97,
            ),
        ),
    },
    "automotive": {
        "intent": (
            _RuleOption(
                "high",
                (
                    "今天就订车",
                    "我今天就订车",
                    "现在交定金",
                    "直接签合同",
                    "确定买这款",
                ),
                0.93,
            ),
            _RuleOption(
                "medium",
                ("想买这款", "先看车", "考虑一下", "比较一下", "先询价"),
                0.82,
            ),
        ),
        "objection": (
            _RuleOption(
                "price",
                ("价格太高", "有点贵", "太贵了", "落地价太高", "超过预算"),
                0.91,
            ),
            _RuleOption(
                "product",
                ("配置不够", "油耗太高", "续航不够", "空间太小"),
                0.87,
            ),
            _RuleOption(
                "trust",
                ("担心质量", "担心事故", "质保太短", "售后不放心"),
                0.87,
            ),
            _RuleOption(
                "timing",
                ("等车太久", "交付太慢", "提车周期太长"),
                0.87,
            ),
            _RuleOption(
                "decision",
                ("要和家人商量", "需要家人同意", "再比较几家"),
                0.84,
            ),
        ),
        "next_step": (
            _RuleOption("test_drive", ("安排试驾", "先试驾", "试驾一下", "试驾"), 0.92),
            _RuleOption(
                "quotation",
                ("给个报价", "算落地价", "算贷款方案", "出个方案"),
                0.89,
            ),
            _RuleOption("reservation", ("交定金", "安排订车", "锁定车辆"), 0.93),
            _RuleOption("follow_up", ("电话回访", "微信跟进", "稍后联系"), 0.86),
        ),
        "compliance_risk": (
            _RuleOption(
                "misleading_finance",
                ("保证贷款通过", "绝对零利息", "没有任何费用"),
                0.96,
            ),
            _RuleOption(
                "off_book_payment",
                ("转到个人账户", "私下转账", "个人收款码"),
                0.98,
            ),
            _RuleOption(
                "privacy_exposure",
                ("把身份证号发我", "把银行卡号发我", "说一下验证码"),
                0.97,
            ),
        ),
    },
    "custom": {
        "intent": (
            _RuleOption("high", ("今天就买", "现在下单", "现在付款"), 0.9),
            _RuleOption("medium", ("考虑一下", "比较一下", "先看看"), 0.8),
        ),
        "objection": (
            _RuleOption(
                "price",
                ("价格太高", "有点贵", "太贵了", "超过预算"),
                0.88,
            ),
            _RuleOption("decision", ("要和家人商量", "再比较几家"), 0.82),
        ),
        "next_step": (
            _RuleOption("quotation", ("给个报价", "出个方案"), 0.86),
            _RuleOption("follow_up", ("电话回访", "稍后联系", "微信跟进"), 0.84),
            _RuleOption("checkout", ("安排结账", "现在付款"), 0.9),
        ),
        "compliance_risk": (
            _RuleOption(
                "off_book_payment",
                ("转到个人账户", "私下转账", "个人收款码"),
                0.98,
            ),
            _RuleOption(
                "privacy_exposure",
                ("把身份证号发我", "把银行卡号发我", "说一下验证码"),
                0.97,
            ),
        ),
    },
}

_LABEL_ORDER: tuple[DialogueTargetLabel, ...] = ALL_DIALOGUE_TARGET_LABELS
_SAFE_EVIDENCE_KEYS = {
    "ref_id",
    "kind",
    "segment_id",
    "recording_id",
    "start_ms",
    "end_ms",
    "timeline_start_sec",
    "timeline_end_sec",
    "timeline_start_ms",
    "timeline_end_ms",
    "source_start_sec",
    "source_end_sec",
    "source_start_ms",
    "source_end_ms",
    "coordinate_space",
    "start_sec",
    "end_sec",
}


class ReceptionRuleTagger:
    """Small deterministic baseline; absent evidence intentionally means no tag."""

    def derive(
        self,
        *,
        scenario: Scenario,
        unit: UnitEvidence,
        target_labels: frozenset[DialogueTargetLabel],
    ) -> list[DerivedRuleTag]:
        if not unit.segments:
            return []

        result: list[DerivedRuleTag] = []
        for label_key in _LABEL_ORDER:
            if label_key not in target_labels:
                continue
            if label_key == "stage":
                if unit.business_stage:
                    result.append(
                        DerivedRuleTag(
                            label_key="stage",
                            label_value=unit.business_stage,
                            confidence=0.95,
                            evidence=unit.segments,
                        )
                    )
                continue

            best: tuple[int, int, _RuleOption, tuple[SegmentEvidence, ...]] | None = None
            for option_index, option in enumerate(_RULES[scenario].get(label_key, ())):
                matching_segments: list[SegmentEvidence] = []
                match_count = 0
                for segment in unit.segments:
                    normalized = segment.text.casefold()
                    segment_matches = sum(
                        phrase.casefold() in normalized for phrase in option.phrases
                    )
                    if segment_matches:
                        matching_segments.append(segment)
                        match_count += segment_matches
                if not matching_segments:
                    continue
                candidate = (
                    match_count,
                    -option_index,
                    option,
                    tuple(matching_segments),
                )
                if best is None or candidate[:2] > best[:2]:
                    best = candidate

            if best is None:
                continue
            match_count, _neg_index, option, matching = best
            result.append(
                DerivedRuleTag(
                    label_key=label_key,
                    label_value=option.value,
                    confidence=min(
                        0.99,
                        round(option.confidence + 0.01 * (match_count - 1), 4),
                    ),
                    evidence=matching,
                )
            )
        return result


def _segment_ref_id(raw: object) -> tuple[int, int | None] | None:
    if not isinstance(raw, dict):
        return None
    try:
        segment_id = int(raw["segment_id"])
        recording_id = int(raw["recording_id"]) if raw.get("recording_id") is not None else None
    except (KeyError, TypeError, ValueError):
        return None
    if segment_id <= 0 or (recording_id is not None and recording_id <= 0):
        return None
    return segment_id, recording_id


def _evidence_json(evidence: Sequence[SegmentEvidence]) -> list[dict[str, Any]]:
    return [segment.to_reference() for segment in evidence]


def _valid_span(
    raw: object,
    *,
    start_key: str,
    end_key: str,
) -> tuple[float, float] | None:
    if not isinstance(raw, dict):
        return None
    start = raw.get(start_key)
    end = raw.get(end_key)
    if (
        isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, (int, float))
        or not isinstance(end, (int, float))
    ):
        return None
    start_value = float(start)
    end_value = float(end)
    if start_value < 0 or end_value <= start_value:
        return None
    return start_value, end_value


def _stable_model_run_id(
    reception_id: int,
    request: DeriveDialogueTagsRequest,
) -> str:
    if request.model_run_id:
        return request.model_run_id
    seed = (
        f"{reception_id}:{request.group_key}:{request.group_version}:"
        f"{','.join(request.target_labels)}"
    )
    digest = hashlib.sha256(seed.encode()).hexdigest()[:20]
    return f"rule:{request.group_version}:{digest}"[:128]


def _matches_version_assignment(
    assignment: DialogueTagAssignment,
    *,
    group_version: str,
    value: str,
    confidence: float,
    evidence_refs: list[dict[str, Any]],
    priority: int,
    model_run_id: str,
) -> bool:
    return (
        assignment.group_version == group_version
        and assignment.label_value == value
        and assignment.confidence == confidence
        and assignment.evidence_refs == evidence_refs
        and assignment.priority == priority
        and assignment.model_run_id == model_run_id
        and assignment.source == "rule"
    )


def _same_assignment(
    assignment: DialogueTagAssignment,
    **expected: Any,
) -> bool:
    return assignment.is_current and _matches_version_assignment(
        assignment,
        **expected,
    )


def _assignment_snapshot(assignment: DialogueTagAssignment) -> dict[str, Any]:
    return {
        "id": assignment.id,
        "dialogue_unit_id": assignment.dialogue_unit_id,
        "group_key": assignment.group_key,
        "group_version": assignment.group_version,
        "label_key": assignment.label_key,
        "label_value": assignment.label_value,
        "confidence": assignment.confidence,
        "source": assignment.source,
        "priority": assignment.priority,
        "evidence_refs": deepcopy(assignment.evidence_refs),
        "model_run_id": assignment.model_run_id,
        "is_current": assignment.is_current,
        "assigned_at": assignment.assigned_at.isoformat(),
    }


def _manual_group_version(base_version: str, reception_version: int) -> str:
    suffix = f"-manual-r{reception_version}"
    prefix_limit = 64 - len(suffix)
    if prefix_limit <= 0:
        return f"manual-r{reception_version}"[:64]
    return f"{base_version[:prefix_limit]}{suffix}"


def _safe_evidence_refs(raw_refs: object) -> list[dict[str, Any]]:
    if not isinstance(raw_refs, list):
        return []
    result: list[dict[str, Any]] = []
    for raw in raw_refs[:16]:
        if not isinstance(raw, dict):
            continue
        clean = {
            str(key): value
            for key, value in raw.items()
            if str(key) in _SAFE_EVIDENCE_KEYS and isinstance(value, (str, int, float))
        }
        if clean:
            result.append(clean)
    return result


def _provenance_parent_refs(
    dialogue_unit_id: int,
    evidence_refs: object,
) -> list[dict[str, Any]]:
    parents: list[dict[str, Any]] = [{"type": "dialogue_unit", "id": dialogue_unit_id}]
    seen_segments: set[int] = set()
    for evidence in _safe_evidence_refs(evidence_refs):
        segment_id = evidence.get("segment_id")
        if isinstance(segment_id, int) and segment_id > 0 and segment_id not in seen_segments:
            parents.append({"type": "segment", "id": segment_id})
            seen_segments.add(segment_id)
    return parents


def _analytics_evidence(raw_refs: object) -> list[EvidenceRef]:
    result: list[EvidenceRef] = []
    for index, raw in enumerate(_safe_evidence_refs(raw_refs)):
        recording_id = raw.get("recording_id")
        if recording_id is None:
            continue
        ref_id = raw.get("ref_id") or (
            f"segment:{raw['segment_id']}"
            if raw.get("segment_id") is not None
            else f"audio:{recording_id}:{index}"
        )
        start_ms_raw = raw.get("timeline_start_ms", raw.get("start_ms"))
        end_ms_raw = raw.get("timeline_end_ms", raw.get("end_ms"))
        if start_ms_raw is None or end_ms_raw is None:
            start_ms_raw = raw.get("source_start_ms")
            end_ms_raw = raw.get("source_end_ms")
        if start_ms_raw is None or end_ms_raw is None:
            start_sec = raw.get("timeline_start_sec", raw.get("start_sec"))
            end_sec = raw.get("timeline_end_sec", raw.get("end_sec"))
            if isinstance(start_sec, (int, float)) and isinstance(end_sec, (int, float)):
                start_ms_raw = round(float(start_sec) * 1_000)
                end_ms_raw = round(float(end_sec) * 1_000)
        try:
            start_ms = int(start_ms_raw) if start_ms_raw is not None else None
            end_ms = int(end_ms_raw) if end_ms_raw is not None else None
            if start_ms is not None and end_ms is not None and (start_ms < 0 or end_ms <= start_ms):
                start_ms = end_ms = None
            result.append(
                EvidenceRef(
                    ref_id=str(ref_id)[:128],
                    kind="audio",
                    recording_id=str(recording_id)[:128],
                    start_ms=start_ms,
                    end_ms=end_ms,
                )
            )
        except (TypeError, ValueError):
            continue
    return result


class ReceptionTaggingService:
    """Transactional dialogue tag derivation and bounded analytics reads."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        tagger: ReceptionRuleTagger | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._tagger = tagger or ReceptionRuleTagger()

    async def _load_reception(
        self,
        session: AsyncSession,
        *,
        reception_id: int,
        tenant_id: str,
        for_update: bool = False,
    ) -> Reception:
        statement = select(Reception).where(
            Reception.id == reception_id,
            Reception.tenant_id == tenant_id,
        )
        if for_update:
            statement = statement.with_for_update()
        reception = (await session.execute(statement)).scalar_one_or_none()
        if reception is None:
            raise NotFoundError(
                "Reception not found",
                code="RECEPTION_NOT_FOUND",
            )
        return reception

    async def _unit_evidence(
        self,
        session: AsyncSession,
        *,
        reception: Reception,
        tenant_id: str,
    ) -> list[UnitEvidence]:
        units = list(
            (
                await session.execute(
                    select(DialogueUnit)
                    .where(
                        DialogueUnit.tenant_id == tenant_id,
                        DialogueUnit.reception_id == reception.id,
                    )
                    .order_by(DialogueUnit.unit_index, DialogueUnit.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        mappings = list(
            (
                await session.execute(
                    select(ReceptionRecording).where(
                        ReceptionRecording.tenant_id == tenant_id,
                        ReceptionRecording.reception_id == reception.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        mapping_by_recording = {mapping.recording_id: mapping for mapping in mappings}
        recording_ids = set(mapping_by_recording)
        referenced_ids = {
            parsed[0]
            for unit in units
            for raw in unit.segment_refs
            if (parsed := _segment_ref_id(raw)) is not None
        }
        segments_by_id: dict[int, Segment] = {}
        if referenced_ids and recording_ids:
            segments = (
                (
                    await session.execute(
                        select(Segment).where(
                            Segment.tenant_id == tenant_id,
                            Segment.id.in_(referenced_ids),
                            Segment.recording_id.in_(recording_ids),
                        )
                    )
                )
                .scalars()
                .all()
            )
            segments_by_id = {segment.id: segment for segment in segments}

        result: list[UnitEvidence] = []
        for unit in units:
            verified: list[SegmentEvidence] = []
            seen: set[int] = set()
            for raw in unit.segment_refs:
                parsed = _segment_ref_id(raw)
                if parsed is None:
                    continue
                segment_id, referenced_recording_id = parsed
                segment = segments_by_id.get(segment_id)
                if (
                    segment is None
                    or segment.id in seen
                    or (
                        referenced_recording_id is not None
                        and referenced_recording_id != segment.recording_id
                    )
                ):
                    continue
                mapping = mapping_by_recording[segment.recording_id]
                source_span = _valid_span(
                    raw,
                    start_key="source_start_sec",
                    end_key="source_end_sec",
                )
                if (
                    source_span is None
                    or source_span[0] < segment.start_sec - 0.05
                    or source_span[1] > segment.end_sec + 0.05
                ):
                    source_span = (segment.start_sec, segment.end_sec)
                mapping_source_end = mapping.source_end_sec
                if mapping_source_end is None:
                    mapping_source_end = mapping.source_start_sec + (
                        mapping.timeline_end_sec - mapping.timeline_start_sec
                    )
                source_start = max(
                    source_span[0],
                    segment.start_sec,
                    mapping.source_start_sec,
                )
                source_end = min(
                    source_span[1],
                    segment.end_sec,
                    mapping_source_end,
                )
                if source_end <= source_start:
                    continue

                timeline_span = _valid_span(
                    raw,
                    start_key="timeline_start_sec",
                    end_key="timeline_end_sec",
                )
                if timeline_span is not None:
                    expected_start = (
                        mapping.timeline_start_sec + source_start - mapping.source_start_sec
                    )
                    expected_end = (
                        mapping.timeline_start_sec + source_end - mapping.source_start_sec
                    )
                    if (
                        abs(timeline_span[0] - expected_start) > 0.05
                        or abs(timeline_span[1] - expected_end) > 0.05
                    ):
                        timeline_span = None

                # Manual dialogue splits may retain the parent segment ref.
                # Clip both coordinate systems to this unit so playback jumps
                # to the actual child span instead of the pre-split segment.
                if timeline_span is not None:
                    timeline_start = max(
                        timeline_span[0],
                        unit.start_sec,
                    )
                    timeline_end = min(
                        timeline_span[1],
                        unit.end_sec,
                    )
                    if timeline_end <= timeline_start:
                        continue
                    source_start += timeline_start - timeline_span[0]
                    source_end -= timeline_span[1] - timeline_end
                    timeline_span = (timeline_start, timeline_end)

                seen.add(segment.id)
                verified.append(
                    SegmentEvidence(
                        segment_id=segment.id,
                        recording_id=segment.recording_id,
                        source_start_sec=source_start,
                        source_end_sec=source_end,
                        text=scrubbed_segment_text(
                            segment.text_scrubbed,
                            segment.transcript,
                        ),
                        timeline_start_sec=(
                            timeline_span[0] if timeline_span is not None else None
                        ),
                        timeline_end_sec=(timeline_span[1] if timeline_span is not None else None),
                    )
                )
            result.append(
                UnitEvidence(
                    unit_id=unit.id,
                    unit_index=unit.unit_index,
                    start_sec=unit.start_sec,
                    end_sec=unit.end_sec,
                    business_stage=unit.business_stage,
                    segments=tuple(verified),
                )
            )
        return result

    async def correct_assignment(
        self,
        *,
        reception_id: int,
        assignment_id: int,
        tenant_id: str,
        request: CorrectDialogueTagRequest,
        actor: str,
        actor_user_id: int,
    ) -> CorrectDialogueTagResult:
        """Append one manual assignment while preserving the superseded fact."""

        async with self._session_factory() as session, session.begin():
            reception = await self._load_reception(
                session,
                reception_id=reception_id,
                tenant_id=tenant_id,
                for_update=True,
            )
            if reception.version != request.expected_reception_version:
                raise ConflictError(
                    "Reception version changed; reload before correcting the tag",
                    code="RECEPTION_VERSION_CONFLICT",
                    detail={
                        "reception_id": reception.id,
                        "expected_version": request.expected_reception_version,
                        "actual_version": reception.version,
                    },
                )

            is_canonical = request.expected_group_version.startswith("schema:")
            if is_canonical:
                fact = (
                    await session.execute(
                        select(TagAssignmentFact).where(
                            TagAssignmentFact.id == assignment_id,
                            TagAssignmentFact.tenant_id == tenant_id,
                            TagAssignmentFact.reception_id == reception.id,
                            TagAssignmentFact.subject_type == "dialogue_unit",
                            TagAssignmentFact.tombstone.is_(False),
                        )
                    )
                ).scalar_one_or_none()
                if fact is None:
                    raise NotFoundError(
                        "Dialogue tag assignment not found",
                        code="DIALOGUE_TAG_NOT_FOUND",
                    )
                actual_group_version = _canonical_workspace_group_version(fact)
                current_fact_id = (
                    await session.execute(
                        select(TagAssignmentCurrent.fact_id).where(
                            TagAssignmentCurrent.tenant_id == tenant_id,
                            TagAssignmentCurrent.subject_type == fact.subject_type,
                            TagAssignmentCurrent.subject_id == fact.subject_id,
                            TagAssignmentCurrent.tag_key == fact.tag_key,
                        )
                    )
                ).scalar_one_or_none()
                if (
                    current_fact_id != fact.id
                    or actual_group_version != request.expected_group_version
                ):
                    raise ConflictError(
                        "Dialogue tag group version changed; reload before correcting",
                        code="TAG_GROUP_VERSION_CONFLICT",
                        detail={
                            "assignment_id": fact.id,
                            "expected_group_version": request.expected_group_version,
                            "actual_group_version": actual_group_version,
                            "is_current": current_fact_id == fact.id,
                        },
                    )
                selected_evidence = _select_evidence_subset(
                    fact.evidence_refs,
                    request.evidence_ref_ids,
                    assignment_id=fact.id,
                )
                dialogue_unit_id = fact.dialogue_unit_id or fact.subject_id
                stage_change = (
                    await project_stage_change_in_session(
                        session,
                        tenant_id=tenant_id,
                        reception_id=reception.id,
                        dialogue_unit_id=dialogue_unit_id,
                        stage=request.label_value,
                        evidence_refs=selected_evidence,
                    )
                    if fact.tag_key == "stage"
                    else None
                )
                governance = TagGovernanceService(self._session_factory)
                try:
                    (
                        superseded_fact,
                        corrected_fact,
                    ) = await governance.append_manual_correction_in_session(
                        session,
                        tenant_id=tenant_id,
                        expected_fact_id=fact.id,
                        tag_value=request.label_value,
                        evidence_refs=selected_evidence,
                        reason=request.reason,
                        actor_user_id=actor_user_id,
                    )
                    await governance.project_manual_correction_in_session(
                        session,
                        tenant_id=tenant_id,
                        reception=reception,
                        superseded_fact=superseded_fact,
                        corrected_fact=corrected_fact,
                        reason=request.reason,
                        actor_user_id=actor_user_id,
                        stage_change=stage_change,
                    )
                except GovernanceConflictError as exc:
                    raise ConflictError(
                        "Dialogue tag current fact changed; reload before correcting",
                        code="TAG_GROUP_VERSION_CONFLICT",
                        detail={"assignment_id": fact.id},
                    ) from exc
                except AssignmentValidationError as exc:
                    raise ValidationError(
                        str(exc),
                        code="TAG_ASSIGNMENT_INVALID",
                        detail={"assignment_id": fact.id},
                    ) from exc

                return CorrectDialogueTagResult(
                    reception_id=reception.id,
                    reception_version=reception.version,
                    superseded_assignment_id=superseded_fact.id,
                    assignment=_canonical_assignment_projection(corrected_fact),
                )

            assignment = (
                await session.execute(
                    select(DialogueTagAssignment)
                    .where(
                        DialogueTagAssignment.id == assignment_id,
                        DialogueTagAssignment.tenant_id == tenant_id,
                        DialogueTagAssignment.reception_id == reception.id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if assignment is None:
                raise NotFoundError(
                    "Dialogue tag assignment not found",
                    code="DIALOGUE_TAG_NOT_FOUND",
                )
            if (
                not assignment.is_current
                or assignment.group_version != request.expected_group_version
            ):
                raise ConflictError(
                    "Dialogue tag group version changed; reload before correcting",
                    code="TAG_GROUP_VERSION_CONFLICT",
                    detail={
                        "assignment_id": assignment.id,
                        "expected_group_version": request.expected_group_version,
                        "actual_group_version": assignment.group_version,
                        "is_current": assignment.is_current,
                    },
                )

            selected_evidence = _select_evidence_subset(
                assignment.evidence_refs,
                request.evidence_ref_ids,
                assignment_id=assignment.id,
            )

            before = _assignment_snapshot(assignment)
            now = datetime.now(UTC)
            next_reception_version = reception.version + 1
            manual_version = _manual_group_version(
                assignment.group_version,
                next_reception_version,
            )
            stage_change = (
                await project_stage_change_in_session(
                    session,
                    tenant_id=tenant_id,
                    reception_id=reception.id,
                    dialogue_unit_id=assignment.dialogue_unit_id,
                    stage=request.label_value,
                    evidence_refs=selected_evidence,
                )
                if assignment.label_key == "stage"
                else None
            )
            assignment.is_current = False
            corrected = DialogueTagAssignment(
                tenant_id=tenant_id,
                reception_id=reception.id,
                dialogue_unit_id=assignment.dialogue_unit_id,
                group_key=assignment.group_key,
                group_version=manual_version,
                label_key=assignment.label_key,
                label_value=request.label_value,
                confidence=1.0,
                source="manual",
                priority=assignment.priority,
                evidence_refs=selected_evidence,
                model_run_id=f"{actor}:{manual_version}"[:128],
                is_current=True,
                assigned_at=now,
            )
            session.add(corrected)
            reception.version = next_reception_version
            await session.flush()
            after = _assignment_snapshot(corrected)

            session.add_all(
                [
                    ProvenanceEvent(
                        tenant_id=tenant_id,
                        reception_id=reception.id,
                        object_type="dialogue_tag_assignment",
                        object_ref=str(assignment.id),
                        event_type="superseded",
                        actor=actor,
                        algorithm_version=manual_version,
                        parent_refs=_provenance_parent_refs(
                            assignment.dialogue_unit_id,
                            assignment.evidence_refs,
                        ),
                        evidence_refs=_safe_evidence_refs(assignment.evidence_refs),
                        payload={
                            "reason": request.reason,
                            "before": before,
                            "superseded_by_assignment_id": corrected.id,
                            "reception_version": reception.version,
                        },
                        occurred_at=now,
                    ),
                    ProvenanceEvent(
                        tenant_id=tenant_id,
                        reception_id=reception.id,
                        object_type="dialogue_tag_assignment",
                        object_ref=str(corrected.id),
                        event_type="edited",
                        actor=actor,
                        algorithm_version=manual_version,
                        parent_refs=[
                            {
                                "type": "dialogue_tag_assignment",
                                "id": assignment.id,
                            },
                            *_provenance_parent_refs(
                                corrected.dialogue_unit_id,
                                corrected.evidence_refs,
                            ),
                        ],
                        evidence_refs=_safe_evidence_refs(corrected.evidence_refs),
                        payload={
                            "reason": request.reason,
                            "before": before,
                            "after": after,
                            "expected_group_version": request.expected_group_version,
                            "reception_version": reception.version,
                        },
                        occurred_at=now,
                    ),
                ]
            )
            if stage_change is not None:
                session.add(
                    ProvenanceEvent(
                        tenant_id=tenant_id,
                        reception_id=reception.id,
                        object_type="dialogue_unit",
                        object_ref=str(assignment.dialogue_unit_id),
                        event_type="edited",
                        actor=actor,
                        algorithm_version="manual-tag-edit-v1",
                        parent_refs=[
                            {
                                "type": "dialogue_tag_assignment",
                                "id": corrected.id,
                            }
                        ],
                        evidence_refs=_safe_evidence_refs(corrected.evidence_refs),
                        payload={
                            "reason": request.reason,
                            **stage_change,
                            "reception_version": reception.version,
                        },
                        occurred_at=now,
                    )
                )
            return CorrectDialogueTagResult(
                reception_id=reception.id,
                reception_version=reception.version,
                superseded_assignment_id=assignment.id,
                assignment=corrected,
            )

    async def derive(
        self,
        *,
        reception_id: int,
        tenant_id: str,
        request: DeriveDialogueTagsRequest,
        actor: str,
    ) -> DeriveTagsResult:
        target_labels = frozenset(request.target_labels)
        async with self._session_factory() as session, session.begin():
            reception = await self._load_reception(
                session,
                reception_id=reception_id,
                tenant_id=tenant_id,
                for_update=True,
            )
            units = await self._unit_evidence(
                session,
                reception=reception,
                tenant_id=tenant_id,
            )
            scenario = cast(Scenario, reception.scenario)
            planned: dict[tuple[int, DialogueTargetLabel], DerivedRuleTag] = {}
            missing: list[MissingTag] = []
            unit_by_id = {unit.unit_id: unit for unit in units}
            for unit in units:
                derived = {
                    item.label_key: item
                    for item in self._tagger.derive(
                        scenario=scenario,
                        unit=unit,
                        target_labels=target_labels,
                    )
                }
                for label_key in _LABEL_ORDER:
                    if label_key not in target_labels:
                        continue
                    item = derived.get(label_key)
                    if item is not None:
                        planned[(unit.unit_id, label_key)] = item
                        continue
                    reason: MissingReason = (
                        "no_verified_segment_evidence"
                        if not unit.segments
                        else "missing_stage"
                        if label_key == "stage"
                        else "no_rule_match"
                    )
                    missing.append(
                        MissingTag(
                            dialogue_unit_id=unit.unit_id,
                            unit_index=unit.unit_index,
                            label_key=label_key,
                            reason=reason,
                        )
                    )

            existing = list(
                (
                    await session.execute(
                        select(DialogueTagAssignment)
                        .where(
                            DialogueTagAssignment.tenant_id == tenant_id,
                            DialogueTagAssignment.reception_id == reception.id,
                            DialogueTagAssignment.group_key == request.group_key,
                            DialogueTagAssignment.label_key.in_(request.target_labels),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            current = {
                (item.dialogue_unit_id, item.label_key): item
                for item in existing
                if item.is_current
            }
            manual_current = {key: item for key, item in current.items() if item.source == "manual"}
            protected_keys = set(manual_current)
            automatic_planned = {
                key: item for key, item in planned.items() if key not in protected_keys
            }
            automatic_current = {
                key: item for key, item in current.items() if key not in protected_keys
            }
            missing = [
                item
                for item in missing
                if (item.dialogue_unit_id, item.label_key) not in protected_keys
            ]
            by_version = {
                (
                    item.dialogue_unit_id,
                    item.group_version,
                    item.label_key,
                ): item
                for item in existing
            }
            model_run_id = _stable_model_run_id(reception.id, request)

            # A group version is immutable. Reusing it after evidence or rule
            # output changes would overwrite history because the existing
            # database uniqueness contract permits one row per version/cell.
            # Require callers to advance ``group_version`` instead.
            planned_keys = set(automatic_planned)
            reused_version_rows = [
                item
                for item in existing
                if item.group_version == request.group_version
                and (item.dialogue_unit_id, item.label_key) not in protected_keys
            ]
            version_conflicts: list[dict[str, Any]] = []
            for persisted in reused_version_rows:
                key = (
                    persisted.dialogue_unit_id,
                    cast(DialogueTargetLabel, persisted.label_key),
                )
                planned_tag = automatic_planned.get(key)
                if planned_tag is None or not _matches_version_assignment(
                    persisted,
                    group_version=request.group_version,
                    value=planned_tag.label_value,
                    confidence=planned_tag.confidence,
                    evidence_refs=_evidence_json(planned_tag.evidence),
                    priority=request.priority,
                    model_run_id=model_run_id,
                ):
                    version_conflicts.append(
                        {
                            "dialogue_unit_id": persisted.dialogue_unit_id,
                            "label_key": persisted.label_key,
                        }
                    )
            persisted_version_keys = {
                (
                    item.dialogue_unit_id,
                    cast(DialogueTargetLabel, item.label_key),
                )
                for item in reused_version_rows
            }
            version_conflicts.extend(
                {
                    "dialogue_unit_id": unit_id,
                    "label_key": label_key,
                }
                for unit_id, label_key in sorted(planned_keys - persisted_version_keys)
                if reused_version_rows
            )
            if version_conflicts:
                raise ConflictError(
                    "Tag group_version is immutable; use a new version",
                    code="TAG_VERSION_REUSE_CONFLICT",
                    detail={
                        "group_key": request.group_key,
                        "group_version": request.group_version,
                        "cells": version_conflicts,
                    },
                )

            exact = len(automatic_current) == len(automatic_planned)
            if exact:
                for (unit_id, label_key), item in automatic_planned.items():
                    current_assignment = automatic_current.get((unit_id, label_key))
                    if current_assignment is None or not _same_assignment(
                        current_assignment,
                        group_version=request.group_version,
                        value=item.label_value,
                        confidence=item.confidence,
                        evidence_refs=_evidence_json(item.evidence),
                        priority=request.priority,
                        model_run_id=model_run_id,
                    ):
                        exact = False
                        break
            if exact:
                return DeriveTagsResult(
                    assignments=sorted(
                        current.values(),
                        key=lambda item: (
                            unit_by_id[item.dialogue_unit_id].unit_index,
                            _LABEL_ORDER.index(cast(DialogueTargetLabel, item.label_key)),
                        ),
                    ),
                    missing=missing,
                    superseded_count=0,
                    no_op=True,
                )

            now = datetime.now(UTC)
            superseded = list(automatic_current.values())
            for old in superseded:
                old.is_current = False
                session.add(
                    ProvenanceEvent(
                        tenant_id=tenant_id,
                        reception_id=reception.id,
                        object_type="dialogue_tag_assignment",
                        object_ref=str(old.id),
                        event_type="superseded",
                        actor=actor,
                        algorithm_version=request.group_version,
                        parent_refs=_provenance_parent_refs(
                            old.dialogue_unit_id,
                            old.evidence_refs,
                        ),
                        evidence_refs=_safe_evidence_refs(old.evidence_refs),
                        payload={
                            "group_key": old.group_key,
                            "group_version": old.group_version,
                            "label_key": old.label_key,
                            "label_value": old.label_value,
                            "superseded_by_version": request.group_version,
                        },
                        occurred_at=now,
                    )
                )

            persisted_assignments: list[DialogueTagAssignment] = []
            for (unit_id, label_key), item in automatic_planned.items():
                evidence_refs = _evidence_json(item.evidence)
                assignment = by_version.get((unit_id, request.group_version, label_key))
                if assignment is None:
                    assignment = DialogueTagAssignment(
                        tenant_id=tenant_id,
                        reception_id=reception.id,
                        dialogue_unit_id=unit_id,
                        group_key=request.group_key,
                        group_version=request.group_version,
                        label_key=label_key,
                        label_value=item.label_value,
                        confidence=item.confidence,
                        source="rule",
                        priority=request.priority,
                        evidence_refs=evidence_refs,
                        model_run_id=model_run_id,
                        is_current=True,
                        assigned_at=now,
                    )
                    session.add(assignment)
                else:
                    assignment.label_value = item.label_value
                    assignment.confidence = item.confidence
                    assignment.source = "rule"
                    assignment.priority = request.priority
                    assignment.evidence_refs = evidence_refs
                    assignment.model_run_id = model_run_id
                    assignment.is_current = True
                    assignment.assigned_at = now
                persisted_assignments.append(assignment)

            await session.flush()
            for assignment in persisted_assignments:
                session.add(
                    ProvenanceEvent(
                        tenant_id=tenant_id,
                        reception_id=reception.id,
                        object_type="dialogue_tag_assignment",
                        object_ref=str(assignment.id),
                        event_type="derived",
                        actor=actor,
                        algorithm_version=request.group_version,
                        parent_refs=_provenance_parent_refs(
                            assignment.dialogue_unit_id,
                            assignment.evidence_refs,
                        ),
                        evidence_refs=_safe_evidence_refs(assignment.evidence_refs),
                        payload={
                            "group_key": assignment.group_key,
                            "group_version": assignment.group_version,
                            "label_key": assignment.label_key,
                            "label_value": assignment.label_value,
                            "confidence": assignment.confidence,
                            "source": assignment.source,
                            "model_run_id": assignment.model_run_id,
                        },
                        occurred_at=now,
                    )
                )

            return DeriveTagsResult(
                assignments=sorted(
                    [*manual_current.values(), *persisted_assignments],
                    key=lambda assignment: (
                        unit_by_id[assignment.dialogue_unit_id].unit_index,
                        _LABEL_ORDER.index(
                            cast(
                                DialogueTargetLabel,
                                assignment.label_key,
                            )
                        ),
                    ),
                ),
                missing=missing,
                superseded_count=len(superseded),
                no_op=not superseded and not persisted_assignments,
            )

    async def insights(
        self,
        *,
        tenant_id: str,
        store_ids: Sequence[str],
        agent_names: Sequence[str],
        scenarios: Sequence[str],
        started_from: datetime | None,
        started_to: datetime | None,
        reception_ids: Sequence[int],
        group_keys: Sequence[str],
        group_ids: Sequence[str],
        forced_agent_user_id: int | None,
        page: int,
        page_size: int,
        assignment_limit: int,
        matrix_limit: int,
        difference_limit: int,
        evidence_summary_limit: int,
        merge_strategy: MergeStrategy,
        trend_granularity: TrendGranularity,
        top_n_co_occurrences: int,
    ) -> ReceptionInsightsResult:
        if not 1 <= evidence_summary_limit <= MAX_EVIDENCE_SUMMARY_ITEMS:
            raise ValidationError(
                "evidence_summary_limit is outside the supported range",
                code="TAG_INSIGHTS_EVIDENCE_SUMMARY_LIMIT",
                detail={"max_items": MAX_EVIDENCE_SUMMARY_ITEMS},
            )
        for name, values, limit in (
            ("store_id", store_ids, 50),
            ("agent_name", agent_names, 50),
            ("scenario", scenarios, 3),
            ("reception_id", reception_ids, 100),
            ("group_key", group_keys, 20),
            ("group_id", group_ids, MAX_GROUPS),
        ):
            if len(values) > limit:
                raise ValidationError(
                    f"Too many {name} filters",
                    code="TAG_INSIGHTS_FILTER_LIMIT",
                    detail={"filter": name, "max_items": limit},
                )
        if group_keys and group_ids:
            raise ValidationError(
                "group_key and group_id filters cannot be combined",
                code="TAG_INSIGHTS_GROUP_FILTER_CONFLICT",
                detail={"filters": ["group_key", "group_id"]},
            )
        if len(group_ids) != len(set(group_ids)):
            raise ValidationError(
                "group_id filters must be unique",
                code="TAG_INSIGHTS_DUPLICATE_GROUP_ID",
            )
        selected_exact_pairs: list[tuple[str, str]] = []
        for group_id in group_ids:
            group_key, separator, group_version = group_id.partition("@")
            if not separator or not group_key or not group_version:
                raise ValidationError(
                    "group_id must use key@version",
                    code="TAG_INSIGHTS_GROUP_ID_FORMAT",
                    detail={"group_id": group_id},
                )
            selected_exact_pairs.append((group_key, group_version))
        selection_mode: Literal["current", "exact_versions"] = (
            "exact_versions" if selected_exact_pairs else "current"
        )
        if started_from and started_to and started_to <= started_from:
            raise ValidationError(
                "started_to must be greater than started_from",
                code="TAG_INSIGHTS_TIME_ORDER",
            )
        if any(reception_id <= 0 for reception_id in reception_ids):
            raise ValidationError(
                "reception_id filters must be positive",
                code="TAG_INSIGHTS_RECEPTION_ID",
            )

        reception_predicates: list[Any] = [Reception.tenant_id == tenant_id]
        if store_ids:
            reception_predicates.append(Reception.store_id.in_(store_ids))
        if forced_agent_user_id is not None:
            reception_predicates.append(
                Reception.agent_user_id == forced_agent_user_id,
            )
        elif agent_names:
            reception_predicates.append(Reception.agent_name.in_(agent_names))
        if scenarios:
            reception_predicates.append(Reception.scenario.in_(scenarios))
        if started_from:
            reception_predicates.append(Reception.started_at >= started_from)
        if started_to:
            reception_predicates.append(Reception.started_at < started_to)
        if reception_ids:
            reception_predicates.append(Reception.id.in_(reception_ids))

        async with self._session_factory() as session:
            total_receptions = int(
                (
                    await session.execute(
                        select(func.count(Reception.id)).where(*reception_predicates)
                    )
                ).scalar_one()
            )
            page_ids = list(
                (
                    await session.execute(
                        select(Reception.id)
                        .where(*reception_predicates)
                        .order_by(
                            Reception.started_at.desc(),
                            Reception.id.desc(),
                        )
                        .offset((page - 1) * page_size)
                        .limit(page_size)
                    )
                )
                .scalars()
                .all()
            )
            if not page_ids:
                return ReceptionInsightsResult(
                    page=page,
                    page_size=page_size,
                    total_receptions=total_receptions,
                    returned_reception_ids=[],
                    total_assignments=0,
                    loaded_assignment_count=0,
                    assignment_limit=assignment_limit,
                    truncated=False,
                    assignment_truncated=False,
                    group_truncated=False,
                    difference_truncated=False,
                    evidence_truncated=False,
                    evidence_ref_count=0,
                    evidence_summary_total=0,
                    evidence_summary_limit=evidence_summary_limit,
                    evidence_summary_truncated=False,
                    selection_mode=selection_mode,
                    selected_group_ids=list(group_ids),
                    insights=None,
                    evidence_summary=[],
                )

            canonical_stmt = (
                select(TagAssignmentFact, DialogueUnit, Reception)
                .join(
                    DialogueUnit,
                    and_(
                        DialogueUnit.id == TagAssignmentFact.dialogue_unit_id,
                        DialogueUnit.tenant_id == tenant_id,
                    ),
                )
                .join(
                    Reception,
                    and_(
                        Reception.id == TagAssignmentFact.reception_id,
                        Reception.tenant_id == tenant_id,
                    ),
                )
                .where(
                    TagAssignmentFact.tenant_id == tenant_id,
                    TagAssignmentFact.subject_type == "dialogue_unit",
                    TagAssignmentFact.reception_id.in_(page_ids),
                )
            )
            if selection_mode == "current":
                canonical_stmt = canonical_stmt.join(
                    TagAssignmentCurrent,
                    and_(
                        TagAssignmentCurrent.fact_id == TagAssignmentFact.id,
                        TagAssignmentCurrent.tenant_id == tenant_id,
                    ),
                )
            else:
                canonical_pairs: list[Any] = []
                for group_key, group_version in selected_exact_pairs:
                    if not group_key.startswith("schema."):
                        continue
                    try:
                        schema_version_id = int(group_key.removeprefix("schema."))
                    except ValueError:
                        continue
                    if group_version == "manual":
                        canonical_pairs.append(
                            and_(
                                TagAssignmentFact.schema_version_id == schema_version_id,
                                TagAssignmentFact.tagger_version_id.is_(None),
                            )
                        )
                    elif group_version.startswith("tagger."):
                        try:
                            tagger_version_id = int(group_version.removeprefix("tagger."))
                        except ValueError:
                            continue
                        canonical_pairs.append(
                            and_(
                                TagAssignmentFact.schema_version_id == schema_version_id,
                                TagAssignmentFact.tagger_version_id == tagger_version_id,
                            )
                        )
                if canonical_pairs:
                    canonical_stmt = canonical_stmt.where(or_(*canonical_pairs))
                else:
                    canonical_stmt = canonical_stmt.where(TagAssignmentFact.id < 0)
            canonical_rows = (await session.execute(canonical_stmt)).all()
            canonical_group_times: dict[tuple[str, str], datetime] = {}
            for fact, _unit, _reception in canonical_rows:
                if fact.tombstone:
                    continue
                pair = _canonical_group(fact)
                if group_keys and pair[0] not in group_keys:
                    continue
                canonical_group_times[pair] = max(
                    canonical_group_times.get(pair, fact.assigned_at),
                    fact.assigned_at,
                )

            canonical_current_cells = (
                select(
                    TagAssignmentFact.dialogue_unit_id,
                    TagAssignmentFact.tag_key,
                )
                .join(
                    TagAssignmentCurrent,
                    and_(
                        TagAssignmentCurrent.fact_id == TagAssignmentFact.id,
                        TagAssignmentCurrent.tenant_id == tenant_id,
                    ),
                )
                .where(
                    TagAssignmentFact.tenant_id == tenant_id,
                    TagAssignmentFact.subject_type == "dialogue_unit",
                    TagAssignmentFact.reception_id.in_(page_ids),
                )
            )
            base_tag_predicates: list[Any] = [
                DialogueTagAssignment.tenant_id == tenant_id,
                DialogueTagAssignment.reception_id.in_(page_ids),
            ]
            if selected_exact_pairs:
                selected_group_pairs = selected_exact_pairs
                group_truncated = False
            else:
                base_tag_predicates.append(DialogueTagAssignment.is_current.is_(True))
                if group_keys:
                    base_tag_predicates.append(DialogueTagAssignment.group_key.in_(group_keys))
                group_time = func.max(DialogueTagAssignment.assigned_at)
                group_rows = (
                    await session.execute(
                        select(
                            DialogueTagAssignment.group_key,
                            DialogueTagAssignment.group_version,
                            group_time.label("latest"),
                        )
                        .where(*base_tag_predicates)
                        .group_by(
                            DialogueTagAssignment.group_key,
                            DialogueTagAssignment.group_version,
                        )
                        .order_by(
                            desc(group_time),
                            DialogueTagAssignment.group_key,
                            DialogueTagAssignment.group_version,
                        )
                        .limit(MAX_GROUPS + 1)
                    )
                ).all()
                all_group_times = {(str(row[0]), str(row[1])): row[2] for row in group_rows}
                all_group_times.update(canonical_group_times)
                ordered_groups = sorted(
                    all_group_times,
                    key=lambda pair: (
                        all_group_times[pair],
                        pair[0],
                        pair[1],
                    ),
                    reverse=True,
                )
                group_truncated = len(ordered_groups) > MAX_GROUPS
                selected_group_pairs = ordered_groups[:MAX_GROUPS]
            selected_group_ids = [
                f"{group_key}@{version}" for group_key, version in selected_group_pairs
            ]
            if not selected_group_pairs:
                return ReceptionInsightsResult(
                    page=page,
                    page_size=page_size,
                    total_receptions=total_receptions,
                    returned_reception_ids=page_ids,
                    total_assignments=0,
                    loaded_assignment_count=0,
                    assignment_limit=assignment_limit,
                    truncated=False,
                    assignment_truncated=False,
                    group_truncated=group_truncated,
                    difference_truncated=False,
                    evidence_truncated=False,
                    evidence_ref_count=0,
                    evidence_summary_total=0,
                    evidence_summary_limit=evidence_summary_limit,
                    evidence_summary_truncated=False,
                    selection_mode=selection_mode,
                    selected_group_ids=selected_group_ids,
                    insights=None,
                    evidence_summary=[],
                )

            pair_predicate = or_(
                *[
                    and_(
                        DialogueTagAssignment.group_key == group_key,
                        DialogueTagAssignment.group_version == version,
                    )
                    for group_key, version in selected_group_pairs
                ]
            )
            assignment_predicates = [*base_tag_predicates, pair_predicate]
            if selection_mode == "current":
                assignment_predicates.append(
                    tuple_(
                        DialogueTagAssignment.dialogue_unit_id,
                        DialogueTagAssignment.label_key,
                    ).not_in(canonical_current_cells)
                )
            legacy_total_assignments = int(
                (
                    await session.execute(
                        select(func.count(DialogueTagAssignment.id)).where(*assignment_predicates)
                    )
                ).scalar_one()
            )
            legacy_rows = (
                await session.execute(
                    select(
                        DialogueTagAssignment,
                        DialogueUnit,
                        Reception,
                    )
                    .join(
                        DialogueUnit,
                        and_(
                            DialogueUnit.id == DialogueTagAssignment.dialogue_unit_id,
                            DialogueUnit.tenant_id == tenant_id,
                        ),
                    )
                    .join(
                        Reception,
                        and_(
                            Reception.id == DialogueTagAssignment.reception_id,
                            Reception.tenant_id == tenant_id,
                        ),
                    )
                    .where(*assignment_predicates)
                    .order_by(
                        Reception.started_at,
                        Reception.id,
                        DialogueUnit.unit_index,
                        DialogueTagAssignment.label_key,
                        DialogueTagAssignment.id,
                    )
                    .limit(assignment_limit + 1)
                )
            ).all()
            selected_pair_set = set(selected_group_pairs)
            canonical_by_cell: dict[
                tuple[int, str, tuple[str, str]],
                tuple[TagAssignmentFact, DialogueUnit, Reception],
            ] = {}
            for fact, unit, reception in canonical_rows:
                pair = _canonical_group(fact)
                if pair not in selected_pair_set:
                    continue
                key = (unit.id, fact.tag_key, pair)
                previous = canonical_by_cell.get(key)
                if previous is None or previous[0].revision < fact.revision:
                    canonical_by_cell[key] = (fact, unit, reception)
            selected_canonical_rows = [
                row for row in canonical_by_cell.values() if not row[0].tombstone
            ]
            total_assignments = legacy_total_assignments + len(selected_canonical_rows)
            insight_rows = [
                _InsightAssignmentRow(
                    reception=reception,
                    unit=unit,
                    group_key=assignment.group_key,
                    group_version=assignment.group_version,
                    label_key=assignment.label_key,
                    label_value=assignment.label_value,
                    confidence=assignment.confidence,
                    source=assignment.source,
                    priority=assignment.priority,
                    evidence_refs=list(assignment.evidence_refs),
                    assigned_at=assignment.assigned_at,
                )
                for assignment, unit, reception in legacy_rows
            ]
            insight_rows.extend(
                _InsightAssignmentRow(
                    reception=reception,
                    unit=unit,
                    group_key=_canonical_group(fact)[0],
                    group_version=_canonical_group(fact)[1],
                    label_key=fact.tag_key,
                    label_value=fact.tag_value,
                    confidence=fact.confidence,
                    source=fact.source,
                    priority=100 if fact.source == "manual" else 10,
                    evidence_refs=list(fact.evidence_refs),
                    assigned_at=fact.assigned_at,
                )
                for fact, unit, reception in selected_canonical_rows
            )
            insight_rows.sort(
                key=lambda row: (
                    row.reception.started_at,
                    row.reception.id,
                    row.unit.unit_index,
                    row.label_key,
                    row.assigned_at,
                )
            )
            assignment_truncated = len(insight_rows) > assignment_limit
            rows = insight_rows[:assignment_limit]

        sources_by_group: dict[tuple[str, str], set[str]] = defaultdict(set)
        priorities_by_group: dict[tuple[str, str], list[int]] = defaultdict(list)
        analytics_assignments: list[TagAssignment] = []
        evidence_summary: list[EvidenceSummary] = []
        for assignment in rows:
            unit = assignment.unit
            reception = assignment.reception
            group_pair = (assignment.group_key, assignment.group_version)
            group_id = f"{assignment.group_key}@{assignment.group_version}"
            sources_by_group[group_pair].add(assignment.source)
            priorities_by_group[group_pair].append(assignment.priority)
            start_ms = round(unit.start_sec * 1_000)
            end_ms = round(unit.end_sec * 1_000)
            if start_ms < 0 or end_ms <= start_ms or end_ms > 86_400_000:
                raise ValidationError(
                    "Dialogue window is outside the supported 24-hour range",
                    code="TAG_INSIGHTS_WINDOW_RANGE",
                    detail={"dialogue_unit_id": unit.id},
                )
            clean_refs = _safe_evidence_refs(assignment.evidence_refs)
            analytics_assignments.append(
                TagAssignment(
                    group_key=assignment.group_key,
                    group_version=assignment.group_version,
                    group_id=group_id,
                    target_id=f"reception:{reception.id}/unit:{unit.id}",
                    window=TimeWindow(
                        start_ms=start_ms,
                        end_ms=end_ms,
                    ),
                    label_key=assignment.label_key,
                    value=assignment.label_value,
                    confidence=assignment.confidence,
                    evidence_refs=_analytics_evidence(clean_refs),
                    is_manual=assignment.source == "manual",
                    occurred_at=assignment.assigned_at,
                    store_id=reception.store_id,
                    agent_id=reception.agent_name,
                )
            )
            if len(evidence_summary) < evidence_summary_limit:
                evidence_summary.append(
                    EvidenceSummary(
                        reception_id=reception.id,
                        dialogue_unit_id=unit.id,
                        group_id=group_id,
                        label_key=assignment.label_key,
                        label_value=str(assignment.label_value),
                        confidence=assignment.confidence,
                        evidence_count=len(clean_refs),
                        evidence_refs=clean_refs,
                    )
                )

        groups = [
            TagGroup(
                group_key=group_key,
                version=version,
                group_id=f"{group_key}@{version}",
                source=(
                    next(iter(sources_by_group[(group_key, version)]))
                    if len(sources_by_group[(group_key, version)]) == 1
                    else "mixed"
                ),
                priority=max(
                    priorities_by_group[(group_key, version)],
                    default=0,
                ),
            )
            for group_key, version in selected_group_pairs
            if sources_by_group[(group_key, version)]
        ]
        groups.sort(
            key=lambda group: (
                -group.priority,
                group.group_id or group.group_key,
            )
        )
        insights = (
            analyze_tag_insights(
                AnalyzeTagInsightsRequest(
                    tenant_id=tenant_id,
                    merge_strategy=merge_strategy,
                    groups=groups,
                    assignments=analytics_assignments,
                    trend_granularity=trend_granularity,
                    top_n_co_occurrences=top_n_co_occurrences,
                    matrix_limit=matrix_limit,
                    difference_limit=difference_limit,
                ),
                tenant_id=tenant_id,
            )
            if analytics_assignments
            else None
        )
        analytics_evidence_count = (
            insights.output_budget.evidence_ref_count if insights is not None else 0
        )
        remaining_evidence_refs = max(
            MAX_RECEPTION_OUTPUT_EVIDENCE_REFS - analytics_evidence_count,
            0,
        )
        selected_summary_refs: list[list[dict[str, Any]]] = [[] for _item in evidence_summary]
        max_refs_per_summary = max(
            (len(item.evidence_refs) for item in evidence_summary),
            default=0,
        )
        for evidence_index in range(max_refs_per_summary):
            for item_index, item in enumerate(evidence_summary):
                if remaining_evidence_refs <= 0:
                    break
                if evidence_index < len(item.evidence_refs):
                    selected_summary_refs[item_index].append(item.evidence_refs[evidence_index])
                    remaining_evidence_refs -= 1
            if remaining_evidence_refs <= 0:
                break
        bounded_evidence_summary = [
            replace(item, evidence_refs=selected_summary_refs[index])
            for index, item in enumerate(evidence_summary)
        ]
        summary_refs_truncated = any(
            len(selected_summary_refs[index]) < len(item.evidence_refs)
            for index, item in enumerate(evidence_summary)
        )
        summary_ref_count = sum(len(item.evidence_refs) for item in bounded_evidence_summary)
        evidence_summary_truncated = len(rows) > evidence_summary_limit or summary_refs_truncated
        evidence_truncated = (
            insights.evidence_truncated if insights is not None else False
        ) or summary_refs_truncated
        difference_truncated = insights.difference_truncated if insights is not None else False
        truncated = (
            assignment_truncated
            or (insights.truncated if insights is not None else False)
            or evidence_summary_truncated
            or total_assignments > assignment_limit
        )
        return ReceptionInsightsResult(
            page=page,
            page_size=page_size,
            total_receptions=total_receptions,
            returned_reception_ids=page_ids,
            total_assignments=total_assignments,
            loaded_assignment_count=len(rows),
            assignment_limit=assignment_limit,
            truncated=truncated,
            assignment_truncated=(assignment_truncated or total_assignments > assignment_limit),
            group_truncated=group_truncated,
            difference_truncated=difference_truncated,
            evidence_truncated=evidence_truncated,
            evidence_ref_count=analytics_evidence_count + summary_ref_count,
            evidence_summary_total=len(rows),
            evidence_summary_limit=evidence_summary_limit,
            evidence_summary_truncated=evidence_summary_truncated,
            selection_mode=selection_mode,
            selected_group_ids=selected_group_ids,
            insights=insights,
            evidence_summary=bounded_evidence_summary,
        )


__all__ = [
    "DeriveTagsResult",
    "EvidenceSummary",
    "ReceptionInsightsResult",
    "ReceptionRuleTagger",
    "ReceptionTaggingService",
    "SegmentEvidence",
    "UnitEvidence",
]
