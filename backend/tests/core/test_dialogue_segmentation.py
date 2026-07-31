"""Dialogue segmentation domain tests.

The segmenter is deliberately pure: these tests exercise business semantics
without a database, model server, or API layer.
"""

from __future__ import annotations

import math

import pytest

from audio_graphy.core.chunker import SegmentRecord
from audio_graphy.core.dialogue_segmentation import (
    AutomotiveStage,
    DialogueSegment,
    DialogueSegmenter,
    GoldJewelryStage,
    SalesScenario,
)


def _segment(
    segment_id: str,
    start: float,
    end: float,
    text: str,
    *,
    speaker: str = "agent",
    embedding: tuple[float, ...] | None = None,
    topic: str | None = None,
    stage: str | None = None,
) -> DialogueSegment:
    return DialogueSegment(
        segment_id=segment_id,
        recording_id="rec-1",
        start_sec=start,
        end_sec=end,
        transcript=text,
        speaker=speaker,
        semantic_embedding=embedding,
        topic_hint=topic,
        stage_hint=stage,
    )


class TestStageModels:
    def test_gold_stage_dictionary_covers_key_reception_steps(self) -> None:
        segmenter = DialogueSegmenter()

        cases = {
            "欢迎光临，您好": GoldJewelryStage.GREETING,
            "您是想买婚戒还是项链，预算多少": GoldJewelryStage.NEEDS,
            "这款可以试戴一下": GoldJewelryStage.SELECTION_TRY_ON,
            "这是足金999，克重和工费在这里": GoldJewelryStage.PRODUCT_EXPLANATION,
            "今天金价有活动，可以给您优惠": GoldJewelryStage.PRICE_PROMOTION,
            "您觉得贵的话我再申请一下折扣": GoldJewelryStage.OBJECTION_NEGOTIATION,
            "可以刷卡付款，也支持微信": GoldJewelryStage.PAYMENT,
            "保修和以旧换新的规则我给您说明": GoldJewelryStage.AFTER_SALES,
            "感谢光临，慢走": GoldJewelryStage.CLOSING,
        }

        for text, expected in cases.items():
            inferred = segmenter.infer_stage(text, scenario=SalesScenario.GOLD_JEWELRY)
            assert inferred.stage == expected

    def test_automotive_dictionary_covers_key_reception_steps(self) -> None:
        segmenter = DialogueSegmenter()

        cases = {
            "您好欢迎到店看车": AutomotiveStage.GREETING,
            "家用还是通勤，预算大概多少": AutomotiveStage.NEEDS,
            "这款SUV是高配，续航和配置更好": AutomotiveStage.VEHICLE_INTRO,
            "裸车报价二十万，落地价我算一下": AutomotiveStage.QUOTE,
            "可以做分期，首付三成贷款两年": AutomotiveStage.FINANCE,
            "旧车可以置换并有补贴": AutomotiveStage.TRADE_IN,
            "我帮您预约试驾": AutomotiveStage.TEST_DRIVE,
            "您担心价格高，我再谈优惠": AutomotiveStage.OBJECTION,
            "交定金后帮您锁单": AutomotiveStage.APPOINTMENT_ORDER,
            "提车时办理上牌，后续保养在这里": AutomotiveStage.DELIVERY_AFTER_SALES,
            "感谢到店，回去考虑好随时联系": AutomotiveStage.CLOSING,
        }

        for text, expected in cases.items():
            inferred = segmenter.infer_stage(text, scenario=SalesScenario.AUTOMOTIVE)
            assert inferred.stage == expected

    def test_stage_state_allows_jump_and_backtrack(self) -> None:
        segmenter = DialogueSegmenter()

        jump = segmenter.infer_stage(
            "今天直接刷卡付款",
            scenario=SalesScenario.GOLD_JEWELRY,
            previous_stage=GoldJewelryStage.NEEDS,
        )
        backtrack = segmenter.infer_stage(
            "我再确认一下您主要是送人还是自戴，预算多少",
            scenario=SalesScenario.GOLD_JEWELRY,
            previous_stage=GoldJewelryStage.PRICE_PROMOTION,
        )

        assert jump.stage == GoldJewelryStage.PAYMENT
        assert jump.transition == "jump"
        assert backtrack.stage == GoldJewelryStage.NEEDS
        assert backtrack.transition == "backtrack"


class TestHybridBoundaryScoring:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"segment_id": "bad", "start_sec": math.nan, "end_sec": 1.0, "transcript": "text"},
            {"segment_id": "bad", "start_sec": 0.0, "end_sec": math.inf, "transcript": "text"},
            {"segment_id": "bad", "start_sec": 1.0, "end_sec": 1.0, "transcript": "text"},
            {"segment_id": "", "start_sec": 0.0, "end_sec": 1.0, "transcript": "text"},
        ],
    )
    def test_rejects_non_finite_zero_length_or_unidentified_segments(
        self,
        kwargs: dict[str, object],
    ) -> None:
        with pytest.raises(ValueError):
            DialogueSegment(**kwargs)  # type: ignore[arg-type]

    def test_batch_deduplicates_identical_segment_ids(self) -> None:
        segment = _segment("same", 0, 1, "您好")

        units = DialogueSegmenter().segment(
            [segment, segment],
            scenario=SalesScenario.GOLD_JEWELRY,
        )

        assert len(units) == 1
        assert [ref.segment_id for ref in units[0].segment_refs] == ["same"]

    def test_batch_rejects_conflicting_duplicate_segment_ids(self) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            DialogueSegmenter().segment(
                [
                    _segment("same", 0, 1, "您好"),
                    _segment("same", 1, 2, "不同内容"),
                ],
                scenario=SalesScenario.GOLD_JEWELRY,
            )

    def test_empty_text_cannot_inject_stage_topic_or_semantic_boundary(self) -> None:
        units = DialogueSegmenter().segment(
            [
                _segment(
                    "first",
                    0,
                    1,
                    "请问您的预算",
                    embedding=(1.0, 0.0),
                    topic="需求",
                    stage=GoldJewelryStage.NEEDS,
                ),
                _segment(
                    "empty",
                    1.1,
                    2,
                    "   ",
                    embedding=(0.0, 1.0),
                    topic="伪造主题",
                    stage=GoldJewelryStage.PAYMENT,
                ),
            ],
            scenario=SalesScenario.GOLD_JEWELRY,
        )

        assert len(units) == 1
        assert units[0].stage == GoldJewelryStage.NEEDS
        assert units[0].topic == "需求"

    def test_rejects_stage_hint_outside_scenario_state_space(self) -> None:
        with pytest.raises(ValueError, match="stage_hint"):
            DialogueSegmenter().segment(
                [_segment("bad", 0, 1, "文本", stage="not-a-gold-stage")],
                scenario=SalesScenario.GOLD_JEWELRY,
            )

    def test_combines_semantics_stage_speaker_and_pause(self) -> None:
        segments = [
            _segment(
                "s1",
                0,
                3,
                "您喜欢什么款式",
                embedding=(1.0, 0.0),
                topic="需求",
                stage=GoldJewelryStage.NEEDS,
            ),
            _segment(
                "s2",
                3.2,
                6,
                "我想看看项链",
                speaker="customer",
                embedding=(0.95, 0.05),
                topic="需求",
                stage=GoldJewelryStage.NEEDS,
            ),
            _segment(
                "s3",
                10,
                14,
                "今天金价和优惠是这样",
                embedding=(0.0, 1.0),
                topic="报价",
                stage=GoldJewelryStage.PRICE_PROMOTION,
            ),
        ]

        units = DialogueSegmenter().segment(
            segments,
            scenario=SalesScenario.GOLD_JEWELRY,
        )

        assert len(units) == 2
        assert tuple(ref.segment_id for ref in units[0].segment_refs) == ("s1", "s2")
        assert tuple(ref.segment_id for ref in units[1].segment_refs) == ("s3",)
        assert units[1].boundary_score >= 0.5
        assert "semantic_shift" in units[1].boundary_reason
        assert "business_stage_change" in units[1].boundary_reason
        assert "pause" in units[1].boundary_reason

    def test_speaker_change_alone_does_not_split_normal_turn_taking(self) -> None:
        segments = [
            _segment("s1", 0, 2, "您预算多少", speaker="agent"),
            _segment("s2", 2.1, 4, "两万元左右", speaker="customer"),
        ]

        units = DialogueSegmenter().segment(
            segments,
            scenario=SalesScenario.GOLD_JEWELRY,
        )

        assert len(units) == 1

    def test_long_pause_is_a_hard_boundary(self) -> None:
        segments = [
            _segment("s1", 0, 2, "我给您找一下", topic="选款"),
            _segment("s2", 13, 15, "这款您试试", topic="选款"),
        ]

        units = DialogueSegmenter().segment(
            segments,
            scenario=SalesScenario.GOLD_JEWELRY,
        )

        assert len(units) == 2
        assert "long_pause" in units[1].boundary_reason

    def test_accepts_existing_chunker_segment_record(self) -> None:
        records = [
            SegmentRecord(
                idx=0,
                start_sec=0,
                end_sec=2,
                transcript="您好欢迎光临",
                speaker="agent",
                vad_conf=0.98,
            ),
            SegmentRecord(
                idx=1,
                start_sec=2.2,
                end_sec=4,
                transcript="您好",
                speaker="customer",
                vad_conf=0.97,
            ),
        ]

        units = DialogueSegmenter().segment(
            records,
            scenario=SalesScenario.GOLD_JEWELRY,
            recording_id="legacy-recording",
        )

        assert len(units) == 1
        assert tuple(ref.segment_id for ref in units[0].segment_refs) == ("0", "1")
        assert all(ref.recording_id == "legacy-recording" for ref in units[0].segment_refs)

    def test_empty_input_returns_no_units(self) -> None:
        assert DialogueSegmenter().segment([], scenario=SalesScenario.AUTOMOTIVE) == []


class TestIncrementalSegmentation:
    def test_realtime_and_offline_results_are_identical(self) -> None:
        segments = [
            _segment(
                "s1",
                0,
                2,
                "您好，想看什么车",
                embedding=(1.0, 0.0),
                stage=AutomotiveStage.GREETING,
            ),
            _segment(
                "s2",
                2.1,
                5,
                "家用SUV，预算二十万",
                speaker="customer",
                embedding=(0.9, 0.1),
                stage=AutomotiveStage.NEEDS,
            ),
            _segment(
                "s3",
                9,
                12,
                "首付三成可以分期",
                embedding=(0.0, 1.0),
                stage=AutomotiveStage.FINANCE,
            ),
        ]
        segmenter = DialogueSegmenter()

        offline = segmenter.segment(segments, scenario=SalesScenario.AUTOMOTIVE)
        state = segmenter.new_incremental_state(scenario=SalesScenario.AUTOMOTIVE)
        realtime = []
        for segment in segments:
            realtime.extend(state.push(segment))
        realtime.extend(state.finalize())

        assert realtime == offline

    def test_incremental_push_only_emits_finalized_units(self) -> None:
        state = DialogueSegmenter().new_incremental_state(scenario=SalesScenario.GOLD_JEWELRY)

        assert state.push(_segment("s1", 0, 2, "您好")) == []
        assert state.push(_segment("s2", 2.1, 4, "您想看什么")) == []
        emitted = state.push(
            _segment(
                "s3",
                15,
                18,
                "现在给您结账",
                stage=GoldJewelryStage.PAYMENT,
            )
        )

        assert len(emitted) == 1
        assert tuple(ref.segment_id for ref in emitted[0].segment_refs) == ("s1", "s2")
        assert len(state.finalize()) == 1
        assert state.finalize() == []


class TestDialogueUnitEvidence:
    def test_unit_preserves_source_time_summary_topic_and_confidence(self) -> None:
        units = DialogueSegmenter().segment(
            [
                _segment(
                    "s1",
                    1,
                    3,
                    "这款是足金999",
                    topic="材质讲解",
                    stage=GoldJewelryStage.PRODUCT_EXPLANATION,
                ),
                _segment(
                    "s2",
                    3.1,
                    6,
                    "克重是十二克，工费按件计算",
                    speaker="customer",
                    topic="材质讲解",
                    stage=GoldJewelryStage.PRODUCT_EXPLANATION,
                ),
            ],
            scenario=SalesScenario.GOLD_JEWELRY,
        )

        unit = units[0]
        assert unit.start_sec == 1
        assert unit.end_sec == 6
        assert unit.topic == "材质讲解"
        assert unit.stage == GoldJewelryStage.PRODUCT_EXPLANATION
        assert "足金999" in unit.summary
        assert "工费" in unit.summary
        assert 0 <= unit.confidence <= 1
        assert unit.boundary_reason == "recording_start"
