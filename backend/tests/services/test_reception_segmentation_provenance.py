"""Generation and capability contracts for automatic dialogue segmentation."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import select

from audio_graphy.adapters.protocols import EmbeddingResult
from audio_graphy.core.dialogue_segmentation import DialogueSegment, DialogueSegmenter
from audio_graphy.errors import ConflictError
from audio_graphy.models import (
    DialogueUnit,
    ProvenanceEvent,
    Reception,
    ReceptionRecording,
    Recording,
    RecordingPipelineRun,
    Segment,
    TagAssignmentCurrent,
    TagAssignmentFact,
    TagExtractionJob,
)
from audio_graphy.schemas.receptions import ReceptionMergeRequest, ReceptionSegmentRequest
from audio_graphy.services.receptions import (
    ReceptionService,
    ReceptionTimelineSliceOverride,
)

TENANT_ID = "chang_an"


class _CapturingSegmenter(DialogueSegmenter):
    def __init__(self) -> None:
        super().__init__()
        self.inputs: tuple[DialogueSegment, ...] = ()

    def segment(
        self,
        segments: Sequence[Any],
        *,
        scenario: Any,
        recording_id: str | int | None = None,
    ) -> list[Any]:
        self.inputs = tuple(segments)
        return super().segment(
            segments,
            scenario=scenario,
            recording_id=recording_id,
        )


class _BatchEmbed:
    model = "semantic-test-v1"
    dim = 2

    def __init__(self, vectors: Sequence[tuple[float, float]]) -> None:
        self._vectors = tuple(vectors)
        self.calls: list[tuple[str, ...]] = []

    async def embed_texts(self, texts: Sequence[str]) -> Sequence[EmbeddingResult]:
        self.calls.append(tuple(texts))
        return tuple(
            EmbeddingResult(vector=vector, dim=2, model=self.model)
            for vector in self._vectors
        )


class _UnavailableEmbed:
    model = "semantic-test-v1"
    dim = 2

    async def embed_texts(self, texts: Sequence[str]) -> Sequence[EmbeddingResult]:
        raise OSError("embedding provider is unavailable")


async def _seed_reception_with_generations(
    session_factory: Any,
    *,
    include_active_run: bool = True,
) -> tuple[int, int, int | None]:
    now = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)
    async with session_factory() as session, session.begin():
        recording = Recording(
            tenant_id=TENANT_ID,
            store_id="S001",
            agent_name="agent",
            path="/tmp/dialogue-generation.wav",
            status="indexed",
            pipeline_state="done",
            recorded_at=now,
            indexed_at=now,
            audio_duration_ms=20_000,
        )
        session.add(recording)
        await session.flush()

        active_run_id: int | None = None
        if include_active_run:
            stale_run = RecordingPipelineRun(
                tenant_id=TENANT_ID,
                recording_id=recording.id,
                generation=1,
                idempotency_key=f"segment-stale-{recording.id}",
                source_fingerprint="a" * 64,
                config_fingerprint="b" * 64,
                state="ready",
                required_projections=[],
                completed_projections=[],
            )
            active_run = RecordingPipelineRun(
                tenant_id=TENANT_ID,
                recording_id=recording.id,
                generation=2,
                idempotency_key=f"segment-active-{recording.id}",
                source_fingerprint="c" * 64,
                config_fingerprint="d" * 64,
                state="ready",
                required_projections=[],
                completed_projections=[],
            )
            session.add_all([stale_run, active_run])
            await session.flush()
            active_run_id = active_run.id
            recording.active_pipeline_run_id = active_run.id
            session.add_all(
                [
                    Segment(
                        tenant_id=TENANT_ID,
                        recording_id=recording.id,
                        pipeline_run_id=stale_run.id,
                        generation=1,
                        idx=0,
                        start_sec=0.0,
                        end_sec=1.0,
                        transcript="过期代际内容绝不能进入切分",
                        speaker="agent",
                        vad_conf=0.99,
                    ),
                    Segment(
                        tenant_id=TENANT_ID,
                        recording_id=recording.id,
                        pipeline_run_id=active_run.id,
                        generation=2,
                        idx=0,
                        start_sec=0.0,
                        end_sec=1.0,
                        transcript="您好，欢迎光临",
                        speaker="agent",
                        vad_conf=0.99,
                    ),
                    Segment(
                        tenant_id=TENANT_ID,
                        recording_id=recording.id,
                        pipeline_run_id=active_run.id,
                        generation=2,
                        idx=1,
                        start_sec=4.5,
                        end_sec=5.5,
                        transcript="这款车可以安排试驾",
                        speaker="customer",
                        vad_conf=0.98,
                    ),
                ]
            )
        else:
            session.add_all(
                [
                    Segment(
                        tenant_id=TENANT_ID,
                        recording_id=recording.id,
                        pipeline_run_id=None,
                        generation=0,
                        idx=0,
                        start_sec=0.0,
                        end_sec=1.0,
                        transcript="旧数据您好",
                        speaker="agent",
                        vad_conf=0.99,
                    ),
                    Segment(
                        tenant_id=TENANT_ID,
                        recording_id=recording.id,
                        pipeline_run_id=None,
                        generation=1,
                        idx=0,
                        start_sec=2.0,
                        end_sec=3.0,
                        transcript="未激活的新代际",
                        speaker="customer",
                        vad_conf=0.99,
                    ),
                ]
            )

        reception = Reception(
            tenant_id=TENANT_ID,
            scenario="automotive",
            store_id="S001",
            agent_name="agent",
            status="confirmed",
            merge_mode="logical",
            merge_confidence=1.0,
            started_at=now,
            ended_at=now,
            version=1,
        )
        session.add(reception)
        await session.flush()
        session.add(
            ReceptionRecording(
                tenant_id=TENANT_ID,
                reception_id=reception.id,
                recording_id=recording.id,
                sequence_no=0,
                timeline_start_sec=0.0,
                timeline_end_sec=20.0,
                source_start_sec=0.0,
                source_end_sec=20.0,
                gap_before_sec=0.0,
                decision_source="manual",
                merge_confidence=1.0,
                merge_reasons={},
            )
        )
        return reception.id, recording.id, active_run_id


async def _reception_segmentation_event(
    session_factory: Any,
    reception_id: int,
) -> ProvenanceEvent:
    async with session_factory() as session:
        result = await session.execute(
            select(ProvenanceEvent).where(
                ProvenanceEvent.tenant_id == TENANT_ID,
                ProvenanceEvent.reception_id == reception_id,
                ProvenanceEvent.object_type == "reception",
                ProvenanceEvent.event_type == "derived",
            )
        )
        return result.scalar_one()


@pytest.mark.asyncio
async def test_segmentation_reads_only_active_generation_and_persists_semantic_provenance(
    session_factory: Any,
    tmp_path: Path,
) -> None:
    reception_id, recording_id, _active_run_id = await _seed_reception_with_generations(
        session_factory
    )
    embed = _BatchEmbed(((1.0, 0.0), (0.0, 1.0)))
    segmenter = _CapturingSegmenter()
    service = ReceptionService(
        session_factory,
        audio_root=tmp_path,
        embed_adapter=embed,
    )

    await service.segment_reception(
        reception_id,
        TENANT_ID,
        ReceptionSegmentRequest(
            expected_version=1,
            algorithm_version="client-claimed-version",
        ),
        actor="user:1",
        segmenter=segmenter,
    )

    assert embed.calls == [("您好，欢迎光临", "这款车可以安排试驾")]
    assert [item.transcript for item in segmenter.inputs] == [
        "您好，欢迎光临",
        "这款车可以安排试驾",
    ]
    assert [item.semantic_embedding for item in segmenter.inputs] == [
        (1.0, 0.0),
        (0.0, 1.0),
    ]

    event = await _reception_segmentation_event(session_factory, reception_id)
    assert event.algorithm_version == DialogueSegmenter.ALGORITHM_VERSION
    assert event.payload["algorithm_version"] == DialogueSegmenter.ALGORITHM_VERSION
    assert len(event.payload["config_hash"]) == 64
    assert event.payload["capability"] == "rules+semantic"
    assert "semantic_shift" in event.payload["enabled_signals"]
    assert event.payload["input_generation"] == {str(recording_id): 2}
    assert event.payload["legacy_fallback_recording_ids"] == []


@pytest.mark.asyncio
async def test_embedding_failure_is_explicit_rules_only_and_never_claims_semantic(
    session_factory: Any,
    tmp_path: Path,
) -> None:
    reception_id, recording_id, _active_run_id = await _seed_reception_with_generations(
        session_factory
    )
    segmenter = _CapturingSegmenter()
    service = ReceptionService(
        session_factory,
        audio_root=tmp_path,
        embed_adapter=_UnavailableEmbed(),
    )

    await service.segment_reception(
        reception_id,
        TENANT_ID,
        ReceptionSegmentRequest(expected_version=1),
        actor="user:1",
        segmenter=segmenter,
    )

    assert all(item.semantic_embedding is None for item in segmenter.inputs)
    event = await _reception_segmentation_event(session_factory, reception_id)
    assert event.payload["capability"] == "rules-only"
    assert "semantic_shift" not in event.payload["enabled_signals"]
    assert event.payload["semantic_embedding"]["status"] == "unavailable"
    assert event.payload["semantic_embedding"]["error_type"] == "OSError"
    assert event.payload["input_generation"] == {str(recording_id): 2}


@pytest.mark.asyncio
async def test_legacy_fallback_reads_only_generation_zero_without_pipeline_run(
    session_factory: Any,
    tmp_path: Path,
) -> None:
    reception_id, recording_id, _ = await _seed_reception_with_generations(
        session_factory,
        include_active_run=False,
    )
    segmenter = _CapturingSegmenter()
    service = ReceptionService(session_factory, audio_root=tmp_path)

    await service.segment_reception(
        reception_id,
        TENANT_ID,
        ReceptionSegmentRequest(expected_version=1),
        actor="user:1",
        segmenter=segmenter,
    )

    assert [item.transcript for item in segmenter.inputs] == ["旧数据您好"]
    event = await _reception_segmentation_event(session_factory, reception_id)
    assert event.payload["input_generation"] == {str(recording_id): "legacy"}
    assert event.payload["legacy_fallback_recording_ids"] == [recording_id]
    assert event.payload["capability"] == "rules-only"


@pytest.mark.asyncio
async def test_segmentation_revalidates_generation_after_embedding_without_holding_write_lock(
    session_factory: Any,
    tmp_path: Path,
) -> None:
    reception_id, recording_id, _ = await _seed_reception_with_generations(
        session_factory
    )

    class _ConcurrentGenerationActivation(_BatchEmbed):
        async def embed_texts(
            self,
            texts: Sequence[str],
        ) -> Sequence[EmbeddingResult]:
            async with session_factory() as session, session.begin():
                recording = await session.get(Recording, recording_id)
                assert recording is not None
                replacement = RecordingPipelineRun(
                    tenant_id=TENANT_ID,
                    recording_id=recording_id,
                    generation=3,
                    idempotency_key=f"segment-concurrent-{recording_id}",
                    source_fingerprint="e" * 64,
                    config_fingerprint="f" * 64,
                    state="ready",
                    required_projections=[],
                    completed_projections=[],
                )
                session.add(replacement)
                await session.flush()
                session.add(
                    Segment(
                        tenant_id=TENANT_ID,
                        recording_id=recording_id,
                        pipeline_run_id=replacement.id,
                        generation=3,
                        idx=0,
                        start_sec=0.0,
                        end_sec=1.0,
                        transcript="并发激活的新代际",
                        speaker="agent",
                        vad_conf=0.99,
                    )
                )
                recording.active_pipeline_run_id = replacement.id
            return await super().embed_texts(texts)

    service = ReceptionService(
        session_factory,
        audio_root=tmp_path,
        embed_adapter=_ConcurrentGenerationActivation(((1.0, 0.0), (0.0, 1.0))),
    )

    with pytest.raises(ConflictError) as captured:
        await service.segment_reception(
            reception_id,
            TENANT_ID,
            ReceptionSegmentRequest(expected_version=1),
            actor="user:1",
        )

    assert captured.value.code == "SEGMENTATION_INPUT_CHANGED"


@pytest.mark.asyncio
async def test_receptions_api_factory_injects_runtime_embedding_adapter(
    session_factory: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from audio_graphy.api import receptions as receptions_api

    embed = _BatchEmbed(((1.0, 0.0),))
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(working_dir=tmp_path),
                adapter_bundle=SimpleNamespace(embed=embed),
                audio_assembler=None,
                audio_crypto=None,
            )
        )
    )
    monkeypatch.setattr(
        receptions_api,
        "get_session_factory",
        lambda _request: session_factory,
    )

    service = receptions_api._service(cast(Any, request))
    capability = await service._semantic_segmentation_inputs(
        (
            DialogueSegment(
                segment_id="1",
                recording_id=1,
                start_sec=0.0,
                end_sec=1.0,
                transcript="真实运行时注入",
            ),
        )
    )

    assert embed.calls == [("真实运行时注入",)]
    assert capability.status == "enabled"
    assert capability.segments[0].semantic_embedding == (1.0, 0.0)


@pytest.mark.asyncio
async def test_first_automatic_segmentation_always_enqueues_new_units_for_recompute(
    session_factory: Any,
    tmp_path: Path,
) -> None:
    reception_id, _recording_id, _active_run_id = (
        await _seed_reception_with_generations(session_factory)
    )
    service = ReceptionService(session_factory, audio_root=tmp_path)

    await service.segment_reception(
        reception_id,
        TENANT_ID,
        ReceptionSegmentRequest(expected_version=1),
        actor="user:1",
    )

    async with session_factory() as session:
        unit_ids = list(
            (
                await session.execute(
                    select(DialogueUnit.id)
                    .where(DialogueUnit.reception_id == reception_id)
                    .order_by(DialogueUnit.id)
                )
            ).scalars()
        )
        jobs = list(
            (
                await session.execute(
                    select(TagExtractionJob).where(
                        TagExtractionJob.tenant_id == TENANT_ID
                    )
                )
            ).scalars()
        )
    assert len(unit_ids) > 0
    assert len(jobs) == 1
    assert jobs[0].scope["dialogue_unit_ids"] == unit_ids
    assert jobs[0].scope["invalidated_dialogue_unit_ids"] == unit_ids


@pytest.mark.asyncio
async def test_automatic_resegmentation_keeps_facts_but_job_references_only_new_units(
    session_factory: Any,
    tmp_path: Path,
) -> None:
    reception_id, _recording_id, _active_run_id = (
        await _seed_reception_with_generations(session_factory)
    )
    service = ReceptionService(session_factory, audio_root=tmp_path)
    await service.segment_reception(
        reception_id,
        TENANT_ID,
        ReceptionSegmentRequest(expected_version=1),
        actor="user:1",
    )
    async with session_factory() as session, session.begin():
        old_ids = list(
            (
                await session.execute(
                    select(DialogueUnit.id)
                    .where(DialogueUnit.reception_id == reception_id)
                    .order_by(DialogueUnit.id)
                )
            ).scalars()
        )
        fact = TagAssignmentFact(
            tenant_id=TENANT_ID,
            subject_type="dialogue_unit",
            subject_id=old_ids[0],
            reception_id=reception_id,
            dialogue_unit_id=old_ids[0],
            tag_key="intent",
            tag_value="purchase",
            confidence=0.9,
            evidence_refs=[],
            source="llm",
            schema_version_id=1,
            tagger_version_id=1,
            input_hash="7" * 64,
            revision=1,
            tombstone=False,
            assigned_at=datetime.now(UTC),
        )
        session.add(fact)
        await session.flush()
        session.add(
            TagAssignmentCurrent(
                tenant_id=TENANT_ID,
                subject_type="dialogue_unit",
                subject_id=old_ids[0],
                tag_key="intent",
                fact_id=fact.id,
                revision=1,
            )
        )

    await service.segment_reception(
        reception_id,
        TENANT_ID,
        ReceptionSegmentRequest(expected_version=2, replace_auto=True),
        actor="user:1",
    )

    async with session_factory() as session:
        new_ids = list(
            (
                await session.execute(
                    select(DialogueUnit.id)
                    .where(DialogueUnit.reception_id == reception_id)
                    .order_by(DialogueUnit.id)
                )
            ).scalars()
        )
        jobs = list(
            (
                await session.execute(
                    select(TagExtractionJob)
                    .where(TagExtractionJob.tenant_id == TENANT_ID)
                    .order_by(TagExtractionJob.id)
                )
            ).scalars()
        )
        current_ids = list(
            (
                await session.execute(
                    select(TagAssignmentCurrent.subject_id).where(
                        TagAssignmentCurrent.tenant_id == TENANT_ID
                    )
                )
            ).scalars()
        )
        facts = list((await session.execute(select(TagAssignmentFact))).scalars())
    assert set(old_ids).isdisjoint(new_ids)
    assert len(jobs) == 2
    assert jobs[-1].scope["dialogue_unit_ids"] == new_ids
    assert set(jobs[-1].scope["invalidated_dialogue_unit_ids"]) == {
        *old_ids,
        *new_ids,
    }
    assert current_ids == []
    assert len(facts) == 1


@pytest.mark.asyncio
async def test_timeline_geometry_change_clears_current_before_deleting_dialogue_unit(
    session_factory: Any,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        recording = Recording(
            tenant_id=TENANT_ID,
            store_id="S001",
            path="/tmp/timeline-current.wav",
            audio_duration_ms=20_000,
        )
        reception = Reception(
            tenant_id=TENANT_ID,
            scenario="custom",
            store_id="S001",
            status="ready",
            merge_mode="logical",
            started_at=now,
            ended_at=now + timedelta(seconds=20),
            version=1,
        )
        session.add_all([recording, reception])
        await session.flush()
        session.add(
            ReceptionRecording(
                tenant_id=TENANT_ID,
                reception_id=reception.id,
                recording_id=recording.id,
                sequence_no=0,
                timeline_start_sec=0.0,
                timeline_end_sec=20.0,
                source_start_sec=0.0,
                source_end_sec=20.0,
                source_start_ms=0,
                source_end_ms=20_000,
                timeline_start_ms=0,
                timeline_end_ms=20_000,
                gap_before_ms=0,
                gap_before_sec=0.0,
                decision_source="manual",
                merge_confidence=1.0,
                merge_reasons={},
            )
        )
        unit = DialogueUnit(
            tenant_id=TENANT_ID,
            reception_id=reception.id,
            source_recording_id=recording.id,
            unit_index=0,
            version=1,
            start_sec=0.0,
            end_sec=20.0,
            boundary_reasons=[],
            segment_refs=[],
            speaker_refs=[],
            edit_status="auto",
        )
        session.add(unit)
        await session.flush()
        fact = TagAssignmentFact(
            tenant_id=TENANT_ID,
            subject_type="dialogue_unit",
            subject_id=unit.id,
            reception_id=reception.id,
            dialogue_unit_id=unit.id,
            tag_key="intent",
            tag_value="purchase",
            confidence=0.9,
            evidence_refs=[],
            source="llm",
            input_hash="8" * 64,
            revision=1,
            tombstone=False,
            assigned_at=now,
        )
        session.add(fact)
        await session.flush()
        session.add(
            TagAssignmentCurrent(
                tenant_id=TENANT_ID,
                subject_type="dialogue_unit",
                subject_id=unit.id,
                tag_key="intent",
                fact_id=fact.id,
                revision=1,
            )
        )
        reception_id = reception.id
        recording_id = recording.id
        old_unit_id = unit.id

    service = ReceptionService(session_factory, audio_root=tmp_path)
    await service.merge_recordings(
        reception_id,
        TENANT_ID,
        ReceptionMergeRequest(
            recording_ids=[recording_id],
            mode="logical",
            expected_version=1,
        ),
        actor="user:1",
        timeline_override={
            recording_id: ReceptionTimelineSliceOverride(
                source_start_sec=1.0,
                source_end_sec=20.0,
            )
        },
    )

    async with session_factory() as session:
        unit = await session.get(DialogueUnit, old_unit_id)
        currents = list(
            (await session.execute(select(TagAssignmentCurrent))).scalars()
        )
        facts = list((await session.execute(select(TagAssignmentFact))).scalars())
    assert unit is None
    assert currents == []
    assert len(facts) == 1
    assert facts[0].subject_id == old_unit_id
