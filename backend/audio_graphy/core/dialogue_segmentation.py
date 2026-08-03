"""Pure dialogue-unit segmentation for retail sales receptions.

The module intentionally has no database, API, or model-runtime dependency.
It accepts the existing :class:`chunker.SegmentRecord` as well as the richer
``DialogueSegment`` input.  Embeddings and business hints can therefore be
added by an upstream model without changing the segmentation contract.

Boundary decisions combine independent evidence:

* semantic distance;
* pause duration;
* speaker change;
* topic change;
* business-stage transition.

Normal agent/customer turn taking is weak evidence and never creates a
boundary on its own.  Offline and incremental processing share the same
state machine, which makes their finalized outputs deterministic and equal.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from audio_graphy.core.chunker import SegmentRecord


class SalesScenario(StrEnum):
    """Supported retail reception scenarios."""

    GOLD_JEWELRY = "gold_jewelry"
    AUTOMOTIVE = "automotive"
    GENERIC = "generic"


class GoldJewelryStage(StrEnum):
    """Canonical stages for a gold-jewelry store reception."""

    GREETING = "greeting"
    NEEDS = "needs"
    SELECTION_TRY_ON = "selection_try_on"
    PRODUCT_EXPLANATION = "product_explanation"
    PRICE_PROMOTION = "price_promotion"
    OBJECTION_NEGOTIATION = "objection_negotiation"
    PAYMENT = "payment"
    AFTER_SALES = "after_sales"
    CLOSING = "closing"


class AutomotiveStage(StrEnum):
    """Canonical stages for an automotive-sales reception."""

    GREETING = "greeting"
    NEEDS = "needs"
    VEHICLE_INTRO = "vehicle_intro"
    QUOTE = "quote"
    FINANCE = "finance"
    TRADE_IN = "trade_in"
    TEST_DRIVE = "test_drive"
    OBJECTION = "objection"
    APPOINTMENT_ORDER = "appointment_order"
    DELIVERY_AFTER_SALES = "delivery_after_sales"
    CLOSING = "closing"


UNKNOWN_STAGE = "unknown"
UNKNOWN_TOPIC = "未分类"

GOLD_JEWELRY_STAGE_ORDER: tuple[str, ...] = tuple(stage.value for stage in GoldJewelryStage)
AUTOMOTIVE_STAGE_ORDER: tuple[str, ...] = tuple(stage.value for stage in AutomotiveStage)


@dataclass(frozen=True, slots=True)
class DialogueSegment:
    """Extensible normalized input for one ASR/VAD segment."""

    segment_id: str
    start_sec: float
    end_sec: float
    transcript: str
    recording_id: str | int | None = None
    speaker: str | None = None
    vad_conf: float = 1.0
    semantic_embedding: tuple[float, ...] | None = None
    topic_hint: str | None = None
    stage_hint: str | None = None

    def __post_init__(self) -> None:
        if not self.segment_id:
            raise ValueError("segment_id must not be empty")
        if (
            not math.isfinite(self.start_sec)
            or not math.isfinite(self.end_sec)
            or self.start_sec < 0
            or self.end_sec <= self.start_sec
        ):
            raise ValueError("segment times must be finite with 0 <= start_sec < end_sec")
        if not math.isfinite(self.vad_conf) or not 0.0 <= self.vad_conf <= 1.0:
            raise ValueError("vad_conf must be between 0 and 1")
        if self.semantic_embedding is not None and (
            not self.semantic_embedding
            or any(not math.isfinite(value) for value in self.semantic_embedding)
        ):
            raise ValueError("semantic_embedding must contain finite values")
        if self.topic_hint is not None and not self.topic_hint.strip():
            raise ValueError("topic_hint must not be blank")
        if self.stage_hint is not None and not self.stage_hint.strip():
            raise ValueError("stage_hint must not be blank")


@dataclass(frozen=True, slots=True)
class SourceSegmentRef:
    """Stable provenance pointer from a dialogue unit to source audio."""

    recording_id: str | int | None
    segment_id: str
    start_sec: float
    end_sec: float


@dataclass(frozen=True, slots=True)
class BoundarySignal:
    """One explainable component of a boundary score."""

    code: str
    score: float
    detail: str


@dataclass(frozen=True, slots=True)
class StageInference:
    """Business-stage classification plus ordered-state transition metadata."""

    stage: str
    confidence: float
    transition: str
    matched_keywords: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DialogueUnit:
    """A topic/stage coherent unit with evidence-level provenance."""

    unit_index: int
    topic: str
    stage: str
    summary: str
    confidence: float
    stage_confidence: float
    boundary_reason: str
    boundary_score: float
    boundary_signals: tuple[BoundarySignal, ...]
    segment_refs: tuple[SourceSegmentRef, ...]
    start_sec: float
    end_sec: float


SegmentSource = DialogueSegment | SegmentRecord


_GOLD_KEYWORDS: dict[str, tuple[str, ...]] = {
    GoldJewelryStage.GREETING: ("欢迎光临", "您好", "你好", "欢迎到店"),
    GoldJewelryStage.NEEDS: (
        "预算",
        "送人",
        "自戴",
        "婚戒",
        "项链",
        "手镯",
        "想买",
        "想看",
        "喜欢什么",
        "主要是",
    ),
    GoldJewelryStage.SELECTION_TRY_ON: ("试戴", "选款", "看看这款", "戴一下", "挑一款"),
    GoldJewelryStage.PRODUCT_EXPLANATION: (
        "足金",
        "纯度",
        "克重",
        "工费",
        "材质",
        "999",
        "计价",
    ),
    GoldJewelryStage.PRICE_PROMOTION: ("金价", "价格", "活动", "优惠", "满减", "折扣价"),
    GoldJewelryStage.OBJECTION_NEGOTIATION: (
        "太贵",
        "贵",
        "申请",
        "再便宜",
        "谈优惠",
        "折扣",
        "考虑一下",
    ),
    GoldJewelryStage.PAYMENT: ("付款", "刷卡", "微信", "支付宝", "结账", "收款"),
    GoldJewelryStage.AFTER_SALES: ("保修", "售后", "以旧换新", "清洗", "换款", "维修"),
    GoldJewelryStage.CLOSING: ("感谢光临", "慢走", "欢迎下次", "再见"),
}

_AUTO_KEYWORDS: dict[str, tuple[str, ...]] = {
    AutomotiveStage.GREETING: ("欢迎到店", "欢迎光临", "您好", "你好", "看车"),
    AutomotiveStage.NEEDS: (
        "预算",
        "家用",
        "通勤",
        "几口人",
        "需求",
        "想看",
        "SUV",
        "轿车",
    ),
    AutomotiveStage.VEHICLE_INTRO: (
        "配置",
        "续航",
        "动力",
        "高配",
        "低配",
        "车型",
        "空间",
        "这款车",
    ),
    AutomotiveStage.QUOTE: ("报价", "裸车", "落地价", "指导价", "保险", "购置税"),
    AutomotiveStage.FINANCE: ("分期", "首付", "贷款", "月供", "利率", "金融"),
    AutomotiveStage.TRADE_IN: ("置换", "旧车", "置换补贴", "二手车"),
    AutomotiveStage.TEST_DRIVE: ("试驾", "试乘", "预约试驾"),
    AutomotiveStage.OBJECTION: ("太贵", "价格高", "担心", "再谈", "竞品", "考虑"),
    AutomotiveStage.APPOINTMENT_ORDER: ("定金", "订金", "锁单", "下单", "预约到店"),
    AutomotiveStage.DELIVERY_AFTER_SALES: (
        "提车",
        "交付",
        "上牌",
        "保养",
        "售后",
        "质保",
    ),
    AutomotiveStage.CLOSING: ("感谢到店", "随时联系", "慢走", "再见", "回去考虑"),
}

_STAGE_TOPICS: dict[str, str] = {
    "greeting": "接待问候",
    "needs": "需求了解",
    "selection_try_on": "选款试戴",
    "product_explanation": "产品讲解",
    "price_promotion": "价格优惠",
    "objection_negotiation": "异议议价",
    "payment": "成交支付",
    "after_sales": "售后说明",
    "vehicle_intro": "车型配置",
    "quote": "报价方案",
    "finance": "金融方案",
    "trade_in": "置换方案",
    "test_drive": "试乘试驾",
    "objection": "异议处理",
    "appointment_order": "预约下订",
    "delivery_after_sales": "交付售后",
    "closing": "结束送别",
}


class DialogueSegmenter:
    """Hybrid rule/semantic segmenter shared by batch and realtime paths."""

    ALGORITHM_VERSION = "dialogue-hybrid-v2"

    def __init__(
        self,
        *,
        boundary_threshold: float = 0.5,
        medium_pause_sec: float = 3.0,
        long_pause_sec: float = 8.0,
        summary_max_chars: int = 180,
    ) -> None:
        if not 0.0 < boundary_threshold <= 1.0:
            raise ValueError("boundary_threshold must be in (0, 1]")
        if medium_pause_sec < 0 or long_pause_sec <= medium_pause_sec:
            raise ValueError("pause thresholds must satisfy 0 <= medium < long")
        if summary_max_chars < 1:
            raise ValueError("summary_max_chars must be positive")
        self.boundary_threshold = boundary_threshold
        self.medium_pause_sec = medium_pause_sec
        self.long_pause_sec = long_pause_sec
        self.summary_max_chars = summary_max_chars

    def infer_stage(
        self,
        text: str,
        *,
        scenario: SalesScenario | str,
        previous_stage: str | None = None,
        stage_hint: str | None = None,
    ) -> StageInference:
        """Infer a scenario-specific stage without forbidding jumps or backtracks."""
        normalized_scenario = SalesScenario(scenario)
        order, keywords = self._stage_model(normalized_scenario)

        if stage_hint:
            stage = str(stage_hint)
            if normalized_scenario != SalesScenario.GENERIC and stage not in order:
                raise ValueError(
                    f"stage_hint {stage!r} is outside the {normalized_scenario.value} state space"
                )
            return StageInference(
                stage=stage,
                confidence=0.98,
                transition=self._transition(previous_stage, stage, order),
            )

        matches: list[tuple[int, int, str, tuple[str, ...]]] = []
        for position, stage in enumerate(order):
            found = tuple(keyword for keyword in keywords.get(stage, ()) if keyword in text)
            if found:
                # Longer phrases carry more intent than isolated characters.
                strength = sum(max(1, len(keyword)) for keyword in found)
                matches.append((strength, -position, stage, found))

        if matches:
            strength, _, stage, found = max(matches)
            confidence = min(0.96, 0.64 + 0.05 * len(found) + 0.01 * strength)
        elif previous_stage and previous_stage != UNKNOWN_STAGE:
            stage = previous_stage
            found = ()
            confidence = 0.56
        else:
            stage = UNKNOWN_STAGE
            found = ()
            confidence = 0.35

        return StageInference(
            stage=stage,
            confidence=round(confidence, 4),
            transition=self._transition(previous_stage, stage, order),
            matched_keywords=found,
        )

    def segment(
        self,
        segments: Sequence[SegmentSource],
        *,
        scenario: SalesScenario | str,
        recording_id: str | int | None = None,
    ) -> list[DialogueUnit]:
        """Segment a complete recording using the incremental state machine."""
        state = self.new_incremental_state(
            scenario=scenario,
            default_recording_id=recording_id,
        )
        output: list[DialogueUnit] = []
        ordered = sorted(
            (self._normalize_segment(segment, recording_id) for segment in segments),
            key=lambda segment: (segment.start_sec, segment.end_sec, segment.segment_id),
        )
        normalized: list[DialogueSegment] = []
        by_identity: dict[tuple[str | int | None, str], DialogueSegment] = {}
        for segment in ordered:
            identity = (segment.recording_id, segment.segment_id)
            previous = by_identity.get(identity)
            if previous is None:
                by_identity[identity] = segment
                normalized.append(segment)
                continue
            if previous != segment:
                raise ValueError("conflicting duplicate segment_id within one recording")
        for segment in normalized:
            output.extend(state.push(segment))
        output.extend(state.finalize())
        return output

    def new_incremental_state(
        self,
        *,
        scenario: SalesScenario | str,
        default_recording_id: str | int | None = None,
    ) -> DialogueSegmentationState:
        """Create per-stream mutable state for realtime segmentation."""
        return DialogueSegmentationState(
            segmenter=self,
            scenario=SalesScenario(scenario),
            default_recording_id=default_recording_id,
        )

    def score_boundary(
        self,
        previous: DialogueSegment,
        current: DialogueSegment,
        *,
        previous_stage: str,
        current_stage: str,
        previous_topic: str,
        current_topic: str,
    ) -> tuple[float, tuple[BoundarySignal, ...]]:
        """Return a capped hybrid boundary score and explainable signals."""
        signals: list[BoundarySignal] = []
        pause = max(0.0, current.start_sec - previous.end_sec)
        if pause >= self.long_pause_sec:
            signals.append(BoundarySignal("long_pause", 0.55, f"pause={pause:.2f}s"))
        elif pause >= self.medium_pause_sec:
            signals.append(BoundarySignal("pause", 0.30, f"pause={pause:.2f}s"))

        semantic_distance = self._cosine_distance(
            previous.semantic_embedding,
            current.semantic_embedding,
        )
        if semantic_distance is not None and semantic_distance >= 0.60:
            signals.append(
                BoundarySignal(
                    "semantic_shift",
                    0.35,
                    f"cosine_distance={semantic_distance:.3f}",
                )
            )
        elif semantic_distance is not None and semantic_distance >= 0.35:
            signals.append(
                BoundarySignal(
                    "semantic_shift",
                    0.22,
                    f"cosine_distance={semantic_distance:.3f}",
                )
            )

        if previous_stage != current_stage and UNKNOWN_STAGE not in {
            previous_stage,
            current_stage,
        }:
            signals.append(
                BoundarySignal(
                    "business_stage_change",
                    0.25,
                    f"{previous_stage}->{current_stage}",
                )
            )

        if previous_topic != current_topic and UNKNOWN_TOPIC not in {previous_topic, current_topic}:
            signals.append(
                BoundarySignal(
                    "topic_change",
                    0.20,
                    f"{previous_topic}->{current_topic}",
                )
            )

        if previous.speaker and current.speaker and previous.speaker != current.speaker:
            signals.append(
                BoundarySignal(
                    "speaker_change",
                    0.08,
                    f"{previous.speaker}->{current.speaker}",
                )
            )

        return round(min(1.0, sum(signal.score for signal in signals)), 4), tuple(signals)

    def _normalize_segment(
        self,
        segment: SegmentSource,
        default_recording_id: str | int | None,
    ) -> DialogueSegment:
        if isinstance(segment, DialogueSegment):
            if segment.recording_id is not None or default_recording_id is None:
                return segment
            return DialogueSegment(
                segment_id=segment.segment_id,
                recording_id=default_recording_id,
                start_sec=segment.start_sec,
                end_sec=segment.end_sec,
                transcript=segment.transcript,
                speaker=segment.speaker,
                vad_conf=segment.vad_conf,
                semantic_embedding=segment.semantic_embedding,
                topic_hint=segment.topic_hint,
                stage_hint=segment.stage_hint,
            )

        return DialogueSegment(
            segment_id=str(segment.idx),
            recording_id=default_recording_id,
            start_sec=segment.start_sec,
            end_sec=segment.end_sec,
            transcript=segment.transcript,
            speaker=segment.speaker,
            vad_conf=segment.vad_conf,
        )

    @staticmethod
    def _stage_model(
        scenario: SalesScenario,
    ) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
        if scenario == SalesScenario.GOLD_JEWELRY:
            return GOLD_JEWELRY_STAGE_ORDER, _GOLD_KEYWORDS
        if scenario == SalesScenario.AUTOMOTIVE:
            return AUTOMOTIVE_STAGE_ORDER, _AUTO_KEYWORDS
        return (), {}

    @staticmethod
    def _transition(
        previous_stage: str | None,
        current_stage: str,
        order: tuple[str, ...],
    ) -> str:
        if previous_stage is None or previous_stage == UNKNOWN_STAGE:
            return "start"
        if previous_stage == current_stage:
            return "stay"
        try:
            difference = order.index(current_stage) - order.index(previous_stage)
        except ValueError:
            return "change"
        if difference == 1:
            return "advance"
        if difference > 1:
            return "jump"
        return "backtrack"

    @staticmethod
    def _cosine_distance(
        left: tuple[float, ...] | None,
        right: tuple[float, ...] | None,
    ) -> float | None:
        if left is None or right is None or len(left) != len(right) or not left:
            return None
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if left_norm == 0 or right_norm == 0:
            return None
        similarity = sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)
        return max(0.0, min(2.0, 1.0 - similarity))


class DialogueSegmentationState:
    """Mutable per-stream state; use one instance per recording/session."""

    def __init__(
        self,
        *,
        segmenter: DialogueSegmenter,
        scenario: SalesScenario,
        default_recording_id: str | int | None,
    ) -> None:
        self._segmenter = segmenter
        self._scenario = scenario
        self._default_recording_id = default_recording_id
        self._current: list[tuple[DialogueSegment, StageInference, str]] = []
        self._unit_index = 0
        self._start_reason = "recording_start"
        self._start_score = 0.0
        self._start_signals: tuple[BoundarySignal, ...] = ()
        self._finalized = False
        self._seen_segments: dict[
            tuple[str | int | None, str],
            DialogueSegment,
        ] = {}
        self._last_order_key: tuple[float, float, str] | None = None

    def push(self, source: SegmentSource) -> list[DialogueUnit]:
        """Consume one chronological segment and emit at most one closed unit."""
        if self._finalized:
            raise RuntimeError("cannot push after finalize")
        segment = self._segmenter._normalize_segment(
            source,
            self._default_recording_id,
        )
        identity = (segment.recording_id, segment.segment_id)
        seen = self._seen_segments.get(identity)
        if seen is not None:
            if seen == segment:
                return []
            raise ValueError("conflicting duplicate segment_id within one recording")
        order_key = (segment.start_sec, segment.end_sec, segment.segment_id)
        if self._last_order_key is not None and order_key < self._last_order_key:
            raise ValueError("incremental segments must be pushed in chronological order")
        self._seen_segments[identity] = segment
        self._last_order_key = order_key

        has_text = bool(segment.transcript.strip())
        if not has_text:
            segment = DialogueSegment(
                segment_id=segment.segment_id,
                start_sec=segment.start_sec,
                end_sec=segment.end_sec,
                transcript=segment.transcript,
                recording_id=segment.recording_id,
                speaker=segment.speaker,
                vad_conf=segment.vad_conf,
            )
        previous_stage = self._current[-1][1].stage if self._current else None
        inference = self._segmenter.infer_stage(
            segment.transcript,
            scenario=self._scenario,
            previous_stage=previous_stage,
            stage_hint=segment.stage_hint,
        )
        topic = (
            self._current[-1][2]
            if not has_text and self._current
            else segment.topic_hint or _STAGE_TOPICS.get(inference.stage, UNKNOWN_TOPIC)
        )

        if not self._current:
            self._current.append((segment, inference, topic))
            return []

        previous, previous_inference, previous_topic = self._current[-1]
        score, signals = self._segmenter.score_boundary(
            previous,
            segment,
            previous_stage=previous_inference.stage,
            current_stage=inference.stage,
            previous_topic=previous_topic,
            current_topic=topic,
        )
        if score < self._segmenter.boundary_threshold:
            self._current.append((segment, inference, topic))
            return []

        closed = self._build_current()
        self._current = [(segment, inference, topic)]
        self._start_score = score
        self._start_signals = signals
        self._start_reason = "+".join(signal.code for signal in signals)
        return [closed]

    def finalize(self) -> list[DialogueUnit]:
        """Flush the final open unit; repeated calls are idempotent."""
        if self._finalized:
            return []
        self._finalized = True
        if not self._current:
            return []
        return [self._build_current()]

    def _build_current(self) -> DialogueUnit:
        segments = [item[0] for item in self._current]
        inferences = [item[1] for item in self._current]
        topics = [item[2] for item in self._current]
        stage = self._mode([inference.stage for inference in inferences])
        topic = self._mode(topics)
        text = " ".join(
            segment.transcript.strip() for segment in segments if segment.transcript.strip()
        )
        if len(text) > self._segmenter.summary_max_chars:
            text = f"{text[: self._segmenter.summary_max_chars - 1]}…"
        classification_confidence = sum(inference.confidence for inference in inferences) / len(
            inferences
        )
        stage_confidences = [
            inference.confidence for inference in inferences if inference.stage == stage
        ]
        stage_confidence = (
            sum(stage_confidences) / len(stage_confidences) if stage_confidences else 0.0
        )
        audio_confidence = sum(segment.vad_conf for segment in segments) / len(segments)
        confidence = round(
            min(1.0, classification_confidence * 0.65 + audio_confidence * 0.35),
            4,
        )
        unit = DialogueUnit(
            unit_index=self._unit_index,
            topic=topic,
            stage=stage,
            summary=text,
            confidence=confidence,
            stage_confidence=round(min(1.0, max(0.0, stage_confidence)), 4),
            boundary_reason=self._start_reason,
            boundary_score=self._start_score,
            boundary_signals=self._start_signals,
            segment_refs=tuple(
                SourceSegmentRef(
                    recording_id=segment.recording_id,
                    segment_id=segment.segment_id,
                    start_sec=segment.start_sec,
                    end_sec=segment.end_sec,
                )
                for segment in segments
            ),
            start_sec=segments[0].start_sec,
            end_sec=max(segment.end_sec for segment in segments),
        )
        self._unit_index += 1
        return unit

    @staticmethod
    def _mode(values: list[str]) -> str:
        counts = Counter(values)
        # Counter preserves insertion order for ties, keeping the result stable.
        return max(counts, key=counts.__getitem__)
