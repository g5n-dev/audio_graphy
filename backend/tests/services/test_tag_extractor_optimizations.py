"""Regression tests for safe tag-extraction call elimination."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from audio_graphy.adapters.protocols import LLMResponse
from audio_graphy.models import Base
from audio_graphy.models.reception import (
    DialogueUnit,
    Reception,
    ReceptionRecording,
)
from audio_graphy.models.recording import Recording
from audio_graphy.models.segment import Segment
from audio_graphy.models.tag_governance import (
    TagAssignmentCurrent,
    TagAssignmentFact,
    TagExtractionJob,
    TagExtractionRun,
    TaggerVersion,
    TagReviewTask,
    TagSchema,
    TagSchemaVersion,
)
from audio_graphy.services import tag_extractor as tag_extractor_module
from audio_graphy.services.llm_gateway import (
    CachePolicy,
    LLMGateway,
    LLMPriceSnapshot,
    LLMRequest,
)
from audio_graphy.services.tag_extractor import TagExtractor
from audio_graphy.services.tag_governance import AssignmentValidationError


@pytest.fixture
async def extractor_factory() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


class CountingTagLLM:
    model = "tag-test-model"

    def __init__(
        self,
        *,
        confidence: float = 0.91,
        tag_key: str = "intent",
        tag_value: str = "purchase",
        usage: dict[str, int] | None = None,
    ) -> None:
        self.calls = 0
        self.confidence = confidence
        self.tag_key = tag_key
        self.tag_value = tag_value
        self.usage = dict(usage or {})

    async def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        cache_key: str | None = None,
        **generation_kwargs: Any,
    ) -> LLMResponse:
        del messages, temperature, max_tokens, cache_key, generation_kwargs
        self.calls += 1
        return LLMResponse(
            text=json.dumps(
                {
                    "assignments": [
                        {
                            "tag_key": self.tag_key,
                            "tag_value": self.tag_value,
                            "confidence": self.confidence,
                            "evidence_segment_ids": [],
                        }
                    ]
                }
            ),
            model=self.model,
            prompt_hash=f"call-{self.calls}",
            usage=dict(self.usage),
        )


@dataclass(frozen=True)
class SeededExtractor:
    dialogue_unit_id: int
    tagger_version_id: int
    first_job_id: int
    second_job_id: int


async def _seed_extractor(
    factory: async_sessionmaker[AsyncSession],
    *,
    engine: str,
    critical: bool = False,
    critical_values: bool = False,
    rule_confidence: float = 0.95,
    include_uncovered_definition: bool = False,
) -> SeededExtractor:
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        recording = Recording(
            tenant_id="chang_an",
            store_id="S001",
            agent_name="agent_ca",
            path=f"/tmp/tag-extractor-{engine}-{critical}.wav",
            status="indexed",
            pipeline_state="done",
            recorded_at=now,
        )
        session.add(recording)
        await session.flush()
        segment = Segment(
            tenant_id="chang_an",
            recording_id=recording.id,
            idx=0,
            start_sec=0,
            end_sec=2,
            transcript="客户决定购买",
            text_scrubbed="客户决定购买",
            speaker="customer",
            vad_conf=0.99,
        )
        session.add(segment)
        reception = Reception(
            tenant_id="chang_an",
            scenario="automotive",
            store_id="S001",
            status="ready",
            merge_mode="logical",
            started_at=now,
            ended_at=now + timedelta(seconds=2),
            version=1,
        )
        session.add(reception)
        await session.flush()
        session.add(
            ReceptionRecording(
                tenant_id="chang_an",
                reception_id=reception.id,
                recording_id=recording.id,
                sequence_no=0,
                timeline_start_sec=0,
                timeline_end_sec=2,
                source_start_sec=0,
                source_end_sec=2,
                gap_before_sec=0,
                decision_source="manual",
                merge_confidence=1,
                merge_reasons={},
            )
        )
        unit = DialogueUnit(
            tenant_id="chang_an",
            reception_id=reception.id,
            source_recording_id=recording.id,
            unit_index=0,
            version=1,
            start_sec=0,
            end_sec=2,
            topic="购买",
            business_stage="成交意向",
            segment_refs=[{"segment_id": segment.id, "recording_id": recording.id}],
            speaker_refs=["customer"],
            edit_status="auto",
        )
        session.add(unit)
        schema = TagSchema(
            tenant_id="chang_an",
            key=f"extractor-{engine}-{critical}",
            name="抽取体系",
            status="published",
            created_by=1,
        )
        session.add(schema)
        await session.flush()
        schema_version = TagSchemaVersion(
            tenant_id="chang_an",
            schema_id=schema.id,
            version="1",
            definitions=(
                [
                    {
                        "key": "objection",
                        "value_type": "enum",
                        "allowed_values": ["none", "price"],
                        "evidence_required": False,
                        "subject_types": ["dialogue_unit"],
                        "scenarios": ["automotive"],
                        "threshold": 0.7,
                        "critical": False,
                    }
                ]
                if include_uncovered_definition
                else []
            )
            + [
                {
                    "key": "intent",
                    "value_type": "enum",
                    "allowed_values": ["browse", "purchase"],
                    "evidence_required": False,
                    "subject_types": ["dialogue_unit"],
                    "scenarios": ["automotive"],
                    "threshold": 0.7,
                    "critical": critical,
                    "critical_values": ["purchase"] if critical_values else [],
                }
            ],
            checksum=("a" if not critical else "b") * 64,
            status="published",
            created_by=1,
        )
        session.add(schema_version)
        await session.flush()
        tagger = TaggerVersion(
            tenant_id="chang_an",
            schema_version_id=schema_version.id,
            version=f"{engine}-{critical}",
            engine=engine,
            prompt_content="返回结构化 JSON",
            rule_bundle={
                "dsl_version": "1",
                "rules": (
                    [
                        {
                            "tag_key": "intent",
                            "value": "purchase",
                            "contains_any": ["购买"],
                            "confidence": rule_confidence,
                        }
                    ]
                    if engine == "hybrid"
                    else []
                ),
            },
            model_version="tag-test-model",
            thresholds={"intent": 0.7},
            config_checksum=("c" if not critical else "d") * 64,
            status="qualified",
            created_by=1,
        )
        session.add(tagger)
        await session.flush()
        jobs = [
            TagExtractionJob(
                tenant_id="chang_an",
                job_type="extract",
                status="running",
                scope={"dialogue_unit_ids": [unit.id]},
                tagger_version_id=tagger.id,
                idempotency_key=f"extract-{engine}-{critical}-{index}",
                total_items=1,
                completed_items=0,
                failed_items=0,
                attempt_count=1,
                max_attempts=3,
                revision=1,
                lease_owner="test",
                created_by=1,
            )
            for index in range(2)
        ]
        session.add_all(jobs)
        await session.flush()
        return SeededExtractor(
            dialogue_unit_id=unit.id,
            tagger_version_id=tagger.id,
            first_job_id=jobs[0].id,
            second_job_id=jobs[1].id,
        )


async def _set_harness(
    factory: async_sessionmaker[AsyncSession],
    *,
    tagger_version_id: int,
    harness_spec: dict[str, object],
) -> None:
    async with factory() as session, session.begin():
        tagger = await session.get(TaggerVersion, tagger_version_id)
        assert tagger is not None
        tagger.harness_spec = harness_spec


@pytest.mark.asyncio
async def test_completed_prediction_is_reused_before_llm_across_jobs(
    extractor_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_extractor(extractor_factory, engine="llm")
    llm = CountingTagLLM()
    extractor = TagExtractor(extractor_factory, llm=llm)

    first = await extractor.extract_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
        job_id=seeded.first_job_id,
        deployment_id=None,
        actor_user_id=1,
    )
    same_job = await extractor.extract_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
        job_id=seeded.first_job_id,
        deployment_id=None,
        actor_user_id=1,
    )
    second_job = await extractor.extract_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
        job_id=seeded.second_job_id,
        deployment_id=None,
        actor_user_id=1,
    )

    assert llm.calls == 1
    assert same_job.cached is True
    assert second_job.cached is True
    assert second_job.run_id != first.run_id
    assert second_job.assignments[0]["fact_id"] == first.assignments[0]["fact_id"]
    async with extractor_factory() as session:
        cached_run = (
            await session.execute(
                select(TagExtractionRun).where(TagExtractionRun.id == second_job.run_id)
            )
        ).scalar_one()
    assert cached_run.status == "cached"
    assert cached_run.output_snapshot["reused_run_id"] == first.run_id


@pytest.mark.asyncio
async def test_predict_dialogue_unit_reuses_persisted_content_product_before_llm(
    extractor_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The evaluator prediction path reuses a completed cross-job product."""
    seeded = await _seed_extractor(extractor_factory, engine="llm")
    extraction_llm = CountingTagLLM()
    persisted = await TagExtractor(
        extractor_factory,
        llm=extraction_llm,
    ).extract_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
        job_id=seeded.first_job_id,
        deployment_id=None,
        actor_user_id=1,
    )
    evaluation_llm = CountingTagLLM()

    predicted = await TagExtractor(
        extractor_factory,
        llm=evaluation_llm,
    ).predict_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
    )

    expected = tuple(
        {key: value for key, value in assignment.items() if key != "fact_id"}
        for assignment in persisted.assignments
    )
    assert extraction_llm.calls == 1
    assert evaluation_llm.calls == 0
    assert predicted.input_hash == persisted.input_hash
    assert predicted.assignments == expected
    assert all("fact_id" not in assignment for assignment in predicted.assignments)


@pytest.mark.asyncio
async def test_predict_dialogue_unit_does_not_reuse_changed_content_hash(
    extractor_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A transcript snapshot change invalidates the persisted business product."""
    seeded = await _seed_extractor(extractor_factory, engine="llm")
    extraction_llm = CountingTagLLM()
    persisted = await TagExtractor(
        extractor_factory,
        llm=extraction_llm,
    ).extract_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
        job_id=seeded.first_job_id,
        deployment_id=None,
        actor_user_id=1,
    )
    async with extractor_factory() as session, session.begin():
        segment = (await session.execute(select(Segment))).scalar_one()
        segment.transcript = "客户仍在比较"
        segment.text_scrubbed = "客户仍在比较"
        segment.updated_at = datetime.now(UTC) + timedelta(seconds=1)
    evaluation_llm = CountingTagLLM()

    predicted = await TagExtractor(
        extractor_factory,
        llm=evaluation_llm,
    ).predict_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
    )

    assert evaluation_llm.calls == 1
    assert predicted.input_hash != persisted.input_hash


@pytest.mark.asyncio
async def test_low_confidence_review_references_an_append_only_candidate_fact(
    extractor_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_extractor(extractor_factory, engine="llm")

    result = await TagExtractor(
        extractor_factory,
        llm=CountingTagLLM(confidence=0.4),
    ).extract_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
        job_id=seeded.first_job_id,
        deployment_id=None,
        actor_user_id=1,
    )

    assert result.assignments == ()
    async with extractor_factory() as session:
        fact = (await session.execute(select(TagAssignmentFact))).scalar_one()
        review = (
            await session.execute(
                select(TagReviewTask).where(TagReviewTask.reason == "low_confidence")
            )
        ).scalar_one()
        current = (await session.execute(select(TagAssignmentCurrent))).scalar_one_or_none()
        run = (
            await session.execute(
                select(TagExtractionRun).where(TagExtractionRun.id == result.run_id)
            )
        ).scalar_one()
    assert current is None
    assert fact.confidence == pytest.approx(0.4)
    assert review.reason == "low_confidence"
    assert review.proposed_fact_id == fact.id
    assert run.output_snapshot["candidate_facts"][0]["fact_id"] == fact.id


@pytest.mark.asyncio
async def test_successful_targeted_recompute_tombstones_stale_current_on_abstention(
    extractor_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_extractor(extractor_factory, engine="llm")
    await TagExtractor(
        extractor_factory,
        llm=CountingTagLLM(confidence=0.95),
    ).extract_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
        job_id=seeded.first_job_id,
        deployment_id=None,
        actor_user_id=1,
        target_tag_keys=["intent"],
    )
    async with extractor_factory() as session, session.begin():
        segment = (await session.execute(select(Segment))).scalar_one()
        segment.transcript = "客户还在考虑"
        segment.text_scrubbed = "客户还在考虑"
        segment.updated_at = datetime.now(UTC) + timedelta(seconds=1)

    result = await TagExtractor(
        extractor_factory,
        llm=CountingTagLLM(confidence=0.2),
    ).extract_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
        job_id=seeded.second_job_id,
        deployment_id=None,
        actor_user_id=1,
        target_tag_keys=["intent"],
    )

    async with extractor_factory() as session:
        current_fact = (
            await session.execute(
                select(TagAssignmentFact)
                .join(
                    TagAssignmentCurrent,
                    TagAssignmentCurrent.fact_id == TagAssignmentFact.id,
                )
                .where(TagAssignmentCurrent.tag_key == "intent")
            )
        ).scalar_one()
    assert result.assignments == ()
    assert current_fact.tombstone is True
    assert current_fact.extraction_run_id == result.run_id


@pytest.mark.asyncio
async def test_hybrid_rule_short_circuit_is_explicitly_opt_in(
    extractor_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_extractor(extractor_factory, engine="hybrid")
    default_llm = CountingTagLLM()
    opted_in_llm = CountingTagLLM()

    default_result = await TagExtractor(
        extractor_factory,
        llm=default_llm,
    ).predict_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
    )
    opted_in_result = await TagExtractor(
        extractor_factory,
        llm=opted_in_llm,
        enable_hybrid_rule_short_circuit=True,
    ).predict_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
    )

    assert default_llm.calls == 1
    assert opted_in_llm.calls == 0
    assert default_result.assignments == opted_in_result.assignments


@pytest.mark.asyncio
async def test_hybrid_short_circuit_never_skips_critical_definition(
    extractor_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_extractor(extractor_factory, engine="hybrid", critical=True)
    llm = CountingTagLLM()

    await TagExtractor(
        extractor_factory,
        llm=llm,
        enable_hybrid_rule_short_circuit=True,
    ).predict_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
    )

    assert llm.calls == 1


@pytest.mark.asyncio
async def test_hybrid_short_circuit_never_skips_critical_value_definition(
    extractor_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_extractor(
        extractor_factory,
        engine="hybrid",
        critical_values=True,
    )
    llm = CountingTagLLM()

    await TagExtractor(
        extractor_factory,
        llm=llm,
        enable_hybrid_rule_short_circuit=True,
    ).predict_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
    )

    assert llm.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rule_confidence", "include_uncovered_definition"),
    [(0.6, False), (0.95, True)],
)
async def test_hybrid_short_circuit_requires_complete_high_confidence_rules(
    extractor_factory: async_sessionmaker[AsyncSession],
    rule_confidence: float,
    include_uncovered_definition: bool,
) -> None:
    seeded = await _seed_extractor(
        extractor_factory,
        engine="hybrid",
        rule_confidence=rule_confidence,
        include_uncovered_definition=include_uncovered_definition,
    )
    llm = (
        CountingTagLLM(tag_key="objection", tag_value="none")
        if include_uncovered_definition
        else CountingTagLLM()
    )

    await TagExtractor(
        extractor_factory,
        llm=llm,
        enable_hybrid_rule_short_circuit=True,
    ).predict_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
    )

    assert llm.calls == 1


@pytest.mark.asyncio
async def test_llm_assignment_uses_rich_weak_request_and_strict_validator(
    extractor_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed_extractor(extractor_factory, engine="llm")
    captured: list[LLMRequest] = []

    async def capture(_adapter: object, request: LLMRequest) -> LLMResponse:
        captured.append(request)
        return LLMResponse(
            text=json.dumps(
                {
                    "assignments": [
                        {
                            "tag_key": "intent",
                            "tag_value": "purchase",
                            "confidence": 0.91,
                            "evidence_segment_ids": [],
                        }
                    ]
                }
            ),
            model="tag-test-model",
            prompt_hash="provider",
        )

    monkeypatch.setattr(tag_extractor_module, "execute_llm", capture)
    result = await TagExtractor(
        extractor_factory,
        llm=CountingTagLLM(),
    ).predict_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
    )

    assert result.assignments
    request = captured[0]
    assert request.tenant_id == "chang_an"
    assert request.purpose == "dialogue_tag_assignments"
    assert request.model_tier == "weak"
    assert request.cache_policy is CachePolicy.EXACT
    assert request.ttl_seconds == 90 * 24 * 60 * 60
    assert request.prompt_version
    assert request.schema_version
    assert request.parser_version
    assert request.postprocessor_version
    assert request.business_snapshot
    assert request.permission_scope
    assert {getattr(ref, "source_type", None) for ref in request.provenance} >= {
        "dialogue_unit",
        "reception",
        "recording",
    }
    assert request.response_validator is not None
    assert request.response_validator(
        LLMResponse(
            text=json.dumps(
                {
                    "assignments": [
                        {
                            "tag_key": "intent",
                            "tag_value": "purchase",
                            "confidence": 0.8,
                            "evidence_segment_ids": [],
                        }
                    ]
                }
            ),
            model="tag-test-model",
            prompt_hash="valid",
        )
    )
    assert not request.response_validator(
        LLMResponse(
            text=json.dumps(
                {
                    "assignments": [
                        {
                            "tag_key": "unknown",
                            "tag_value": "purchase",
                            "confidence": 0.8,
                            "evidence_segment_ids": [],
                        }
                    ]
                }
            ),
            model="tag-test-model",
            prompt_hash="invalid",
        )
    )


@pytest.mark.asyncio
async def test_llm_request_uses_compact_segment_transport_and_dynamic_output_budget(
    extractor_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed_extractor(extractor_factory, engine="llm")
    captured: list[LLMRequest] = []

    async def capture(_adapter: object, request: LLMRequest) -> LLMResponse:
        captured.append(request)
        return LLMResponse(
            text=json.dumps(
                {
                    "assignments": [
                        {
                            "tag_key": "intent",
                            "tag_value": "purchase",
                            "confidence": 0.91,
                            "evidence_segment_ids": ["s0"],
                        }
                    ]
                }
            ),
            model="tag-test-model",
            prompt_hash="compact-provider",
            usage={"prompt_tokens": 40, "completion_tokens": 12},
        )

    monkeypatch.setattr(tag_extractor_module, "execute_llm", capture)
    result = await TagExtractor(
        extractor_factory,
        llm=CountingTagLLM(),
    ).predict_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
    )

    request = captured[0]
    payload = json.loads(request.messages[1]["content"])
    assignment_items = request.response_schema["properties"]["assignments"]
    evidence_items = assignment_items["items"]["properties"]["evidence_segment_ids"]["items"]
    assert set(payload) == {"schema", "segments"}
    assert set(payload["schema"][0]) == {
        "key",
        "value_type",
        "allowed_values",
    }
    assert payload["segments"] == [
        {
            "id": "s0",
            "speaker": "customer",
            "start_ms": 0,
            "end_ms": 2000,
            "text": "客户决定购买",
        }
    ]
    assert evidence_items == {"type": "string", "enum": ["s0"]}
    assert assignment_items["items"]["properties"]["tag_value"] == {"enum": ["browse", "purchase"]}
    assert assignment_items["maxItems"] == 1
    assert request.max_tokens == 256
    assert request.response_format == {
        "type": "json_schema",
        "name": "tag_assignments_v2",
        "description": (
            "Return only assignments for supplied tag keys and cite only supplied "
            "compact segment IDs."
        ),
    }
    assert request.permission_scope == {
        "tenant_id": "chang_an",
        "access_class": "canonical_tagging_worker",
    }
    assert request.parser_version.endswith("-v2")
    assert (
        result.assignments[0]["evidence_refs"][0]["segment_id"]
        == (result.input_snapshot["segments"][0]["segment_id"])
    )


@pytest.mark.asyncio
async def test_target_tag_keys_reduce_schema_and_are_part_of_the_input_identity(
    extractor_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed_extractor(
        extractor_factory,
        engine="llm",
        include_uncovered_definition=True,
    )
    captured: list[LLMRequest] = []

    async def capture(_adapter: object, request: LLMRequest) -> LLMResponse:
        captured.append(request)
        return LLMResponse(
            text=json.dumps(
                {
                    "assignments": [
                        {
                            "tag_key": "intent",
                            "tag_value": "purchase",
                            "confidence": 0.91,
                            "evidence_segment_ids": [],
                        }
                    ]
                }
            ),
            model="tag-test-model",
            prompt_hash="scoped-provider",
        )

    monkeypatch.setattr(tag_extractor_module, "execute_llm", capture)
    scoped = await TagExtractor(
        extractor_factory,
        llm=CountingTagLLM(),
    ).predict_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
        target_tag_keys=("intent",),
    )

    payload = json.loads(captured[0].messages[1]["content"])
    assert [item["key"] for item in payload["schema"]] == ["intent"]
    assert scoped.input_snapshot["target_tag_keys"] == ["intent"]
    assert {item["tag_key"] for item in scoped.assignments} == {"intent"}

    full = await TagExtractor(
        extractor_factory,
        llm=CountingTagLLM(),
    )._prepare_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
    )
    assert scoped.input_hash != full.input_hash


@pytest.mark.asyncio
async def test_multi_tag_response_schema_binds_each_value_domain_to_its_tag_key(
    extractor_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed_extractor(
        extractor_factory,
        engine="llm",
        include_uncovered_definition=True,
    )
    captured: list[LLMRequest] = []

    async def capture(_adapter: object, request: LLMRequest) -> LLMResponse:
        captured.append(request)
        return LLMResponse(
            text=json.dumps(
                {
                    "assignments": [
                        {
                            "tag_key": "intent",
                            "tag_value": "purchase",
                            "confidence": 0.9,
                            "evidence_segment_ids": [],
                        }
                    ]
                }
            ),
            model="tag-test-model",
            prompt_hash="conditional-schema",
            usage={"prompt_tokens": 20, "completion_tokens": 5},
        )

    monkeypatch.setattr(tag_extractor_module, "execute_llm", capture)
    await TagExtractor(
        extractor_factory,
        llm=CountingTagLLM(),
    ).predict_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
    )

    item_schema = captured[0].response_schema["properties"]["assignments"]["items"]
    branches = {branch["properties"]["tag_key"]["const"]: branch for branch in item_schema["anyOf"]}
    assert branches["intent"]["properties"]["tag_value"] == {"enum": ["browse", "purchase"]}
    assert branches["objection"]["properties"]["tag_value"] == {"enum": ["none", "price"]}
    assert branches["intent"]["properties"]["evidence_segment_ids"] == {
        "$ref": "#/$defs/evidence_optional"
    }
    serialized_schema = json.dumps(captured[0].response_schema, sort_keys=True)
    assert serialized_schema.count('"s0"') == 1


@pytest.mark.asyncio
async def test_empty_target_scope_is_a_zero_call_noop(
    extractor_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_extractor(extractor_factory, engine="llm")
    llm = CountingTagLLM()

    result = await TagExtractor(
        extractor_factory,
        llm=llm,
    ).predict_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
        target_tag_keys=(),
    )

    assert llm.calls == 0
    assert result.assignments == ()
    assert result.candidates == ()
    assert result.token_count == 0


@pytest.mark.asyncio
async def test_high_confidence_rule_removes_only_covered_noncritical_label_from_llm(
    extractor_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed_extractor(
        extractor_factory,
        engine="hybrid",
        include_uncovered_definition=True,
    )
    captured: list[LLMRequest] = []

    async def capture(_adapter: object, request: LLMRequest) -> LLMResponse:
        captured.append(request)
        return LLMResponse(
            text=json.dumps(
                {
                    "assignments": [
                        {
                            "tag_key": "objection",
                            "tag_value": "none",
                            "confidence": 0.91,
                            "evidence_segment_ids": [],
                        }
                    ]
                }
            ),
            model="tag-test-model",
            prompt_hash="cascade-provider",
        )

    monkeypatch.setattr(tag_extractor_module, "execute_llm", capture)
    result = await TagExtractor(
        extractor_factory,
        llm=CountingTagLLM(),
        enable_hybrid_rule_short_circuit=True,
    ).predict_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
    )

    payload = json.loads(captured[0].messages[1]["content"])
    assert [item["key"] for item in payload["schema"]] == ["objection"]
    assert {item["tag_key"] for item in result.assignments} == {"intent", "objection"}
    assert (
        next(item for item in result.assignments if item["tag_key"] == "intent")["source"] == "rule"
    )


@pytest.mark.asyncio
async def test_cached_llm_usage_is_saved_tokens_not_provider_cost(
    extractor_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed_extractor(extractor_factory, engine="llm")

    async def cached(_adapter: object, _request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text=json.dumps(
                {
                    "assignments": [
                        {
                            "tag_key": "intent",
                            "tag_value": "purchase",
                            "confidence": 0.91,
                            "evidence_segment_ids": [],
                        }
                    ]
                }
            ),
            model="tag-test-model",
            prompt_hash="cached-provider",
            cached=True,
            cache_source="mysql",
            provider_called=False,
            usage={"prompt_tokens": 100, "completion_tokens": 20},
        )

    monkeypatch.setattr(tag_extractor_module, "execute_llm", cached)
    result = await TagExtractor(
        extractor_factory,
        llm=CountingTagLLM(),
    ).predict_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
    )

    assert result.token_count == 0
    assert result.provider_input_tokens == 0
    assert result.provider_output_tokens == 0
    assert result.reused_input_tokens == 100
    assert result.reused_output_tokens == 20
    assert result.provider_calls == 0
    assert result.cache_hits == 1
    assert result.cost_units == 0


def _test_price_snapshot() -> LLMPriceSnapshot:
    return LLMPriceSnapshot(
        version="test-price-v1",
        input_microunits_per_million_tokens=1_000_000,
        output_microunits_per_million_tokens=1_000_000,
        cached_prefill_microunits_per_million_tokens=500_000,
    )


@pytest.mark.asyncio
async def test_prediction_batch_settles_actual_and_counterfactual_cache_cost(
    extractor_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed_extractor(extractor_factory, engine="llm")
    await _set_harness(
        extractor_factory,
        tagger_version_id=seeded.tagger_version_id,
        harness_spec={
            "generation": {
                "budget_policy": {
                    "max_provider_calls": None,
                    "max_provider_tokens": None,
                    "max_cost_microunits": 13_000,
                    "max_wall_seconds": None,
                }
            }
        },
    )
    raw = CountingTagLLM(
        usage={"prompt_tokens": 100, "completion_tokens": 20},
    )
    priced_gateway = LLMGateway(
        raw,
        price_snapshot=_test_price_snapshot(),
        max_retries=0,
        retry_base_seconds=0,
    )
    extractor = TagExtractor(extractor_factory, llm=priced_gateway)

    actual = await extractor.predict_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
    )

    assert actual.cost_microunits == 120
    assert actual.counterfactual_saved_cost_microunits == 0
    assert actual.cost_units == 0.00012

    async def cached(_adapter: object, _request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text=json.dumps(
                {
                    "assignments": [
                        {
                            "tag_key": "intent",
                            "tag_value": "purchase",
                            "confidence": 0.91,
                            "evidence_segment_ids": [],
                        }
                    ]
                }
            ),
            model="tag-test-model",
            prompt_hash="cached-provider",
            cached=True,
            cache_source="mysql",
            provider_called=False,
            usage={"prompt_tokens": 100, "completion_tokens": 20},
        )

    monkeypatch.setattr(tag_extractor_module, "execute_llm", cached)
    cached_result = await extractor.predict_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
    )

    assert cached_result.cost_microunits == 0
    assert cached_result.counterfactual_saved_cost_microunits == 120
    assert cached_result.cost_units == 0


@pytest.mark.asyncio
async def test_cost_budget_fails_closed_before_provider_when_price_is_missing(
    extractor_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_extractor(extractor_factory, engine="llm")
    await _set_harness(
        extractor_factory,
        tagger_version_id=seeded.tagger_version_id,
        harness_spec={
            "generation": {
                "budget_policy": {
                    "max_provider_calls": None,
                    "max_provider_tokens": None,
                    "max_cost_microunits": 13_000,
                    "max_wall_seconds": None,
                }
            }
        },
    )
    llm = CountingTagLLM()

    with pytest.raises(AssignmentValidationError, match="price snapshot"):
        await TagExtractor(
            extractor_factory,
            llm=llm,
        ).predict_dialogue_unit(
            tenant_id="chang_an",
            dialogue_unit_id=seeded.dialogue_unit_id,
            tagger_version_id=seeded.tagger_version_id,
        )

    assert llm.calls == 0


@pytest.mark.asyncio
async def test_cost_budget_reserves_retry_bound_before_provider(
    extractor_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_extractor(extractor_factory, engine="llm")
    await _set_harness(
        extractor_factory,
        tagger_version_id=seeded.tagger_version_id,
        harness_spec={
            "generation": {
                "budget_policy": {
                    "max_provider_calls": None,
                    "max_provider_tokens": None,
                    "max_cost_microunits": 30_000,
                    "max_wall_seconds": None,
                }
            }
        },
    )
    raw = CountingTagLLM()
    gateway = LLMGateway(
        raw,
        price_snapshot=_test_price_snapshot(),
        max_retries=2,
        retry_base_seconds=0,
    )

    with pytest.raises(AssignmentValidationError, match="cost budget"):
        await TagExtractor(
            extractor_factory,
            llm=gateway,
        ).predict_dialogue_unit(
            tenant_id="chang_an",
            dialogue_unit_id=seeded.dialogue_unit_id,
            tagger_version_id=seeded.tagger_version_id,
        )

    assert raw.calls == 0


@pytest.mark.asyncio
async def test_high_confidence_noncritical_weak_result_skips_strong_critic(
    extractor_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed_extractor(extractor_factory, engine="llm")
    await _set_harness(
        extractor_factory,
        tagger_version_id=seeded.tagger_version_id,
        harness_spec={
            "tools": {"primary_model": "weak", "critic_model": "strong"},
            "orchestration": {
                "route": "weak_then_strong_critic",
                "fusion_policy": "score_priority",
                "critic_enabled": True,
            },
        },
    )
    requests: list[LLMRequest] = []

    async def capture(_adapter: object, request: LLMRequest) -> LLMResponse:
        requests.append(request)
        return LLMResponse(
            text=json.dumps(
                {
                    "assignments": [
                        {
                            "tag_key": "intent",
                            "tag_value": "purchase",
                            "confidence": 0.95,
                            "evidence_segment_ids": [],
                        }
                    ]
                }
            ),
            model="weak-model",
            prompt_hash="weak",
        )

    weak = CountingTagLLM()
    weak.model = "weak-model"
    strong = CountingTagLLM()
    strong.model = "strong-model"
    monkeypatch.setattr(tag_extractor_module, "execute_llm", capture)

    result = await TagExtractor(
        extractor_factory,
        weak_llm=weak,
        strong_llm=strong,
    ).predict_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
    )

    assert [request.model_tier for request in requests] == ["weak"]
    assert result.provider_calls == 1
    assert result.strong_escalations == 0


@pytest.mark.asyncio
async def test_critic_receives_only_escalated_labels_weak_candidates_and_evidence(
    extractor_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed_extractor(
        extractor_factory,
        engine="llm",
        include_uncovered_definition=True,
    )
    await _set_harness(
        extractor_factory,
        tagger_version_id=seeded.tagger_version_id,
        harness_spec={
            "tools": {"primary_model": "weak", "critic_model": "strong"},
            "orchestration": {
                "route": "weak_then_strong_critic",
                "fusion_policy": "score_priority",
                "critic_enabled": True,
            },
        },
    )
    requests: list[LLMRequest] = []

    async def capture(_adapter: object, request: LLMRequest) -> LLMResponse:
        requests.append(request)
        if request.model_tier == "weak":
            payload = {
                "assignments": [
                    {
                        "tag_key": "intent",
                        "tag_value": "browse",
                        "confidence": 0.75,
                        "evidence_segment_ids": ["s0"],
                    },
                    {
                        "tag_key": "objection",
                        "tag_value": "none",
                        "confidence": 0.99,
                        "evidence_segment_ids": [],
                    },
                ]
            }
            model = "weak-model"
        else:
            payload = {
                "assignments": [
                    {
                        "tag_key": "intent",
                        "tag_value": "purchase",
                        "confidence": 0.95,
                        "evidence_segment_ids": ["s0"],
                    }
                ]
            }
            model = "strong-model"
        return LLMResponse(
            text=json.dumps(payload),
            model=model,
            prompt_hash=model,
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )

    weak = CountingTagLLM()
    weak.model = "weak-model"
    strong = CountingTagLLM()
    strong.model = "strong-model"
    monkeypatch.setattr(tag_extractor_module, "execute_llm", capture)

    result = await TagExtractor(
        extractor_factory,
        weak_llm=weak,
        strong_llm=strong,
    ).predict_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
    )

    assert [request.model_tier for request in requests] == ["weak", "strong"]
    critic_payload = json.loads(requests[1].messages[1]["content"])
    assert set(critic_payload) == {"schema", "segments", "weak_candidates"}
    assert [item["key"] for item in critic_payload["schema"]] == ["intent"]
    assert [item["tag_key"] for item in critic_payload["weak_candidates"]] == ["intent"]
    assert critic_payload["weak_candidates"][0]["evidence_segment_ids"] == ["s0"]
    assert result.strong_escalations == 1
    assert result.provider_calls == 2
    assert {item["tag_key"] for item in result.assignments} == {"intent", "objection"}


@pytest.mark.asyncio
async def test_invalid_json_gets_one_bounded_format_only_repair(
    extractor_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed_extractor(extractor_factory, engine="llm")
    requests: list[LLMRequest] = []

    async def capture(_adapter: object, request: LLMRequest) -> LLMResponse:
        requests.append(request)
        if len(requests) == 1:
            return LLMResponse(
                text='{"assignments":[',
                model="tag-test-model",
                prompt_hash="broken",
                usage={"prompt_tokens": 10, "completion_tokens": 10},
            )
        return LLMResponse(
            text=json.dumps(
                {
                    "assignments": [
                        {
                            "tag_key": "intent",
                            "tag_value": "purchase",
                            "confidence": 0.9,
                            "evidence_segment_ids": [],
                        }
                    ]
                }
            ),
            model="tag-test-model",
            prompt_hash="repaired",
            usage={"prompt_tokens": 5, "completion_tokens": 5},
        )

    monkeypatch.setattr(tag_extractor_module, "execute_llm", capture)
    result = await TagExtractor(
        extractor_factory,
        llm=CountingTagLLM(),
    ).predict_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
    )

    assert [request.purpose for request in requests] == [
        "dialogue_tag_assignments",
        "dialogue_tag_assignments_repair",
    ]
    repair_payload = json.loads(requests[1].messages[1]["content"])
    assert set(repair_payload) == {"invalid_output", "response_schema"}
    assert requests[1].cache_policy is CachePolicy.BYPASS
    assert requests[1].max_tokens == 256
    assert result.provider_calls == 2
    assert result.token_count == 30


@pytest.mark.asyncio
async def test_long_subject_is_segment_chunked_under_the_input_budget(
    extractor_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed_extractor(extractor_factory, engine="llm")
    long_text = "客户明确购买。" * 2_000
    async with extractor_factory() as session, session.begin():
        segment = (await session.execute(select(Segment))).scalar_one()
        segment.transcript = long_text
        segment.text_scrubbed = long_text
        segment.updated_at = datetime.now(UTC) + timedelta(seconds=1)
    requests: list[LLMRequest] = []

    async def capture(_adapter: object, request: LLMRequest) -> LLMResponse:
        requests.append(request)
        return LLMResponse(
            text=json.dumps(
                {
                    "assignments": [
                        {
                            "tag_key": "intent",
                            "tag_value": "purchase",
                            "confidence": 0.91,
                            "evidence_segment_ids": ["s0"],
                        }
                    ]
                }
            ),
            model="tag-test-model",
            prompt_hash=f"chunk-{len(requests)}",
            usage={"prompt_tokens": 100, "completion_tokens": 10},
        )

    monkeypatch.setattr(tag_extractor_module, "execute_llm", capture)
    result = await TagExtractor(
        extractor_factory,
        llm=CountingTagLLM(),
    ).predict_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
    )

    assert len(requests) >= 2
    transported_text = "".join(
        segment_payload["text"]
        for request in requests
        for segment_payload in json.loads(request.messages[1]["content"])["segments"]
    )
    assert transported_text == long_text
    assert all(
        len(request.messages[0]["content"]) + len(request.messages[1]["content"]) <= 12_000
        for request in requests
    )
    assert result.provider_calls == len(requests)
    assert result.assignments[0]["tag_value"] == "purchase"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("remove_current", "expected_source"),
    [(False, "current"), (True, "cached")],
)
async def test_long_reception_uses_tenant_scoped_dialogue_facts_before_chunking(
    extractor_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    remove_current: bool,
    expected_source: str,
) -> None:
    seeded = await _seed_extractor(extractor_factory, engine="llm")
    long_text = "客户继续说明购买需求。" * 5_000 + "RAW_TAIL_MUST_NOT_BE_SENT"
    async with extractor_factory() as session, session.begin():
        tagger = await session.get(TaggerVersion, seeded.tagger_version_id)
        assert tagger is not None
        schema = await session.get(TagSchemaVersion, tagger.schema_version_id)
        assert schema is not None
        schema.definitions = [
            {
                **definition,
                "subject_types": ["dialogue_unit", "reception"],
            }
            for definition in schema.definitions
        ]
        segment = (await session.execute(select(Segment))).scalar_one()
        segment.transcript = long_text
        segment.text_scrubbed = long_text
        segment.updated_at = datetime.now(UTC) + timedelta(seconds=1)

    persisted = await TagExtractor(
        extractor_factory,
        llm=CountingTagLLM(),
    ).extract_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
        job_id=seeded.first_job_id,
        deployment_id=None,
        actor_user_id=1,
    )
    fact_id = int(persisted.assignments[0]["fact_id"])
    async with extractor_factory() as session, session.begin():
        unit = await session.get(DialogueUnit, seeded.dialogue_unit_id)
        assert unit is not None
        reception_id = int(unit.reception_id)
        segment = (await session.execute(select(Segment))).scalar_one()
        source_fact = await session.get(TagAssignmentFact, fact_id)
        assert source_fact is not None
        if remove_current:
            current = (
                await session.execute(
                    select(TagAssignmentCurrent).where(
                        TagAssignmentCurrent.tenant_id == "chang_an",
                        TagAssignmentCurrent.fact_id == fact_id,
                    )
                )
            ).scalar_one()
            await session.delete(current)
        rogue_fact = TagAssignmentFact(
            tenant_id="other_tenant",
            subject_type="dialogue_unit",
            subject_id=seeded.dialogue_unit_id,
            reception_id=reception_id,
            dialogue_unit_id=seeded.dialogue_unit_id,
            tag_key="intent",
            tag_value="browse",
            confidence=1.0,
            evidence_refs=[],
            source="manual",
            schema_version_id=source_fact.schema_version_id,
            tagger_version_id=None,
            extraction_run_id=None,
            deployment_id=None,
            input_hash="e" * 64,
            recipe_hash="f" * 64,
            revision=1,
            tombstone=False,
            actor_user_id=1,
            assigned_at=datetime.now(UTC),
        )
        session.add(rogue_fact)
        await session.flush()
        session.add(
            TagAssignmentCurrent(
                tenant_id="other_tenant",
                subject_type="dialogue_unit",
                subject_id=seeded.dialogue_unit_id,
                tag_key="intent",
                fact_id=rogue_fact.id,
                revision=1,
            )
        )

    requests: list[LLMRequest] = []

    async def capture(_adapter: object, request: LLMRequest) -> LLMResponse:
        requests.append(request)
        return LLMResponse(
            text=json.dumps(
                {
                    "assignments": [
                        {
                            "tag_key": "intent",
                            "tag_value": "purchase",
                            "confidence": 0.91,
                            "evidence_segment_ids": ["s0"],
                        }
                    ]
                }
            ),
            model="tag-test-model",
            prompt_hash="reception-fact-transport",
            usage={"prompt_tokens": 100, "completion_tokens": 10},
        )

    monkeypatch.setattr(tag_extractor_module, "execute_llm", capture)
    extractor = TagExtractor(extractor_factory, llm=CountingTagLLM())
    result = await extractor.predict_reception(
        tenant_id="chang_an",
        reception_id=reception_id,
        tagger_version_id=seeded.tagger_version_id,
    )

    assert len(requests) == 1
    payload = json.loads(requests[0].messages[1]["content"])
    assert len(payload["segments"]) == 1
    assert "dialogue_unit_facts=" in payload["segments"][0]["text"]
    assert '"tag_value":"purchase"' in payload["segments"][0]["text"]
    assert '"tag_value":"browse"' not in payload["segments"][0]["text"]
    assert "RAW_TAIL_MUST_NOT_BE_SENT" not in payload["segments"][0]["text"]
    assert result.assignments[0]["evidence_refs"][0]["segment_id"] == segment.id
    assert result.input_snapshot["transcript"] == long_text
    assert result.input_snapshot["segments"][0]["text"] == long_text
    aggregation = result.input_snapshot["transport_aggregation"]
    assert aggregation["source_reception_input_hash"] == result.input_snapshot["source_input_hash"]
    assert aggregation["source_reception_input_hash"] != result.input_hash
    assert aggregation["dialogue_units"][0]["facts"][0]["fact_id"] == fact_id
    assert aggregation["dialogue_units"][0]["facts"][0]["source"] == expected_source
    provenance = {
        (reference.source_type, reference.source_id) for reference in requests[0].provenance
    }
    assert ("dialogue_unit", str(seeded.dialogue_unit_id)) in provenance
    assert ("tag_assignment_fact", str(fact_id)) in provenance
    assert ("tag_assignment_fact", str(rogue_fact.id)) not in provenance

    replay = await extractor.predict_frozen_input(
        tenant_id="chang_an",
        subject_type="reception",
        subject_id=reception_id,
        input_snapshot=result.input_snapshot,
        tagger_version_id=seeded.tagger_version_id,
    )
    assert replay.input_hash == result.input_hash
    assert json.loads(requests[-1].messages[1]["content"]) == payload


@pytest.mark.asyncio
async def test_provider_call_budget_is_reserved_before_chunked_generation(
    extractor_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed_extractor(extractor_factory, engine="llm")
    await _set_harness(
        extractor_factory,
        tagger_version_id=seeded.tagger_version_id,
        harness_spec={
            "generation": {
                "budget_policy": {
                    "max_provider_calls": 1,
                    "max_provider_tokens": None,
                    "max_cost_microunits": None,
                    "max_wall_seconds": None,
                }
            }
        },
    )
    async with extractor_factory() as session, session.begin():
        segment = (await session.execute(select(Segment))).scalar_one()
        segment.transcript = "客户明确购买。" * 2_000
        segment.text_scrubbed = segment.transcript
        segment.updated_at = datetime.now(UTC) + timedelta(seconds=1)
    calls = 0

    async def capture(_adapter: object, _request: LLMRequest) -> LLMResponse:
        nonlocal calls
        calls += 1
        raise AssertionError("budget must be reserved before any provider call")

    monkeypatch.setattr(tag_extractor_module, "execute_llm", capture)
    with pytest.raises(AssignmentValidationError, match="provider call budget"):
        await TagExtractor(
            extractor_factory,
            llm=CountingTagLLM(),
        ).predict_dialogue_unit(
            tenant_id="chang_an",
            dialogue_unit_id=seeded.dialogue_unit_id,
            tagger_version_id=seeded.tagger_version_id,
        )

    assert calls == 0


@pytest.mark.asyncio
async def test_format_repair_consumes_shared_budget_before_strong_critic(
    extractor_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed_extractor(
        extractor_factory,
        engine="llm",
        critical=True,
    )
    await _set_harness(
        extractor_factory,
        tagger_version_id=seeded.tagger_version_id,
        harness_spec={
            "tools": {"critic_model": "strong"},
            "generation": {
                "budget_policy": {
                    "max_provider_calls": 2,
                    "max_provider_tokens": None,
                    "max_cost_microunits": None,
                    "max_wall_seconds": None,
                }
            },
            "orchestration": {
                "route": "weak_then_strong_critic",
                "critic_enabled": True,
            },
        },
    )
    requests: list[LLMRequest] = []

    async def capture(_adapter: object, request: LLMRequest) -> LLMResponse:
        requests.append(request)
        if len(requests) == 1:
            return LLMResponse(
                text='{"assignments":[',
                model="weak-test-model",
                prompt_hash="invalid-weak",
                usage={"prompt_tokens": 10, "completion_tokens": 5},
            )
        if len(requests) == 2:
            return LLMResponse(
                text=json.dumps(
                    {
                        "assignments": [
                            {
                                "tag_key": "intent",
                                "tag_value": "purchase",
                                "confidence": 0.9,
                                "evidence_segment_ids": [],
                            }
                        ]
                    }
                ),
                model="weak-test-model",
                prompt_hash="repaired-weak",
                usage={"prompt_tokens": 12, "completion_tokens": 6},
            )
        raise AssertionError("strong critic must not cross the shared provider-call budget")

    monkeypatch.setattr(tag_extractor_module, "execute_llm", capture)
    weak = CountingTagLLM()
    weak.model = "weak-test-model"
    strong = CountingTagLLM()
    strong.model = "strong-test-model"

    with pytest.raises(AssignmentValidationError, match="provider call budget"):
        await TagExtractor(
            extractor_factory,
            weak_llm=weak,
            strong_llm=strong,
        ).predict_dialogue_unit(
            tenant_id="chang_an",
            dialogue_unit_id=seeded.dialogue_unit_id,
            tagger_version_id=seeded.tagger_version_id,
        )

    assert [request.purpose for request in requests] == [
        "dialogue_tag_assignments",
        "dialogue_tag_assignments_repair",
    ]


@pytest.mark.asyncio
async def test_materialized_optimizer_candidate_uses_its_exact_prompt_and_harness(
    extractor_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed_extractor(extractor_factory, engine="llm")
    baseline_extractor = TagExtractor(
        extractor_factory,
        llm=CountingTagLLM(),
    )
    prepared = await baseline_extractor._prepare_dialogue_unit(
        tenant_id="chang_an",
        dialogue_unit_id=seeded.dialogue_unit_id,
        tagger_version_id=seeded.tagger_version_id,
    )
    requests: list[LLMRequest] = []

    async def capture(_adapter: object, request: LLMRequest) -> LLMResponse:
        requests.append(request)
        return LLMResponse(
            text=json.dumps(
                {
                    "assignments": [
                        {
                            "tag_key": "intent",
                            "tag_value": "purchase",
                            "confidence": 0.91,
                            "evidence_segment_ids": [],
                        }
                    ]
                }
            ),
            model="tag-test-model",
            prompt_hash="materialized-candidate",
            usage={"prompt_tokens": 31, "completion_tokens": 7},
        )

    monkeypatch.setattr(tag_extractor_module, "execute_llm", capture)
    result = await baseline_extractor.predict_materialized_frozen_input(
        tenant_id="chang_an",
        subject_type="dialogue_unit",
        subject_id=seeded.dialogue_unit_id,
        input_snapshot=prepared.input_snapshot,
        baseline_tagger_version_id=seeded.tagger_version_id,
        harness_spec={
            "generation": {
                "temperature": 0,
                "max_input_tokens": 12_000,
                "max_tokens": 256,
                "response_format": "strict_json",
                "prompt_template": "候选提示词：只输出严格 JSON",
                "budget_policy": {
                    "max_provider_tokens": None,
                    "max_provider_calls": None,
                    "max_cost_microunits": None,
                    "max_wall_seconds": None,
                },
            },
            "orchestration": {
                "route": "weak_llm",
                "fusion_policy": "score_priority",
                "critic_enabled": False,
                "rule_bundle": {"dsl_version": "1", "rules": []},
                "rule_min_confidence": 0.95,
                "critic_confidence_margin": 0.10,
                "critic_max_noncritical_rate": 0.20,
            },
        },
        target_tag_keys=["intent"],
    )

    assert "候选提示词：只输出严格 JSON" in requests[0].messages[0]["content"]
    assert "短 segment id" in requests[0].messages[0]["content"]
    assert requests[0].max_tokens == 256
    assert result.harness_spec["generation"]["prompt_template"] == "候选提示词：只输出严格 JSON"
    assert result.input_snapshot["tagger_checksum"] != prepared.input_snapshot["tagger_checksum"]
    assert result.input_hash != prepared.input_hash
