"""The MVP loop with a real queue: enqueue, claim, compile, persist.

No optional extras are installed here. The builtin proposer is model-free by design,
which is what makes it possible to prove the whole pipeline works before deciding
whether DSPy or TextGrad are worth their dependency weight.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from audio_graphy.models import Base
from audio_graphy.models.prompt_lab import TagPromptArtifact, TagPromptGradient
from audio_graphy.models.tag_governance import TagExtractionJob
from audio_graphy.optimizer_worker import PROMPT_LAB_JOB_TYPES, PromptLabWorker
from audio_graphy.services.prompt_lab import PromptLabService
from audio_graphy.services.tag_governance import TagGovernanceService

_TENANT = "chang_an"


@pytest.fixture
async def worker_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_baseline(factory: async_sessionmaker[AsyncSession]) -> int:
    from audio_graphy.models.tag_governance import (
        TaggerVersion,
        TagSchema,
        TagSchemaVersion,
    )

    async with factory() as session, session.begin():
        schema = TagSchema(tenant_id=_TENANT, key="sales", name="Sales", created_by=9)
        session.add(schema)
        await session.flush()
        version = TagSchemaVersion(
            tenant_id=_TENANT,
            schema_id=schema.id,
            version=1,
            definitions=[{"key": "intent", "allowed_values": ["browse", "purchase"]}],
            checksum="c" * 64,
            created_by=9,
        )
        session.add(version)
        await session.flush()
        tagger = TaggerVersion(
            tenant_id=_TENANT,
            schema_version_id=version.id,
            version="baseline-v1",
            model_version="weak-v1",
            config_checksum="d" * 64,
            prompt_content="基线规则：按 schema 判定标签。",
            thresholds={"intent": 0.7},
            created_by=9,
        )
        session.add(tagger)
        await session.flush()
        return int(tagger.id)


async def _seed_badcases(
    factory: async_sessionmaker[AsyncSession],
    *,
    count: int = 6,
    stage: str = "tag_reasoning",
) -> None:
    from audio_graphy.models.tag_governance import TagBadcase

    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        session.add_all(
            [
                TagBadcase(
                    tenant_id=_TENANT,
                    source_feedback_event_id=index,
                    subject_type="dialogue_unit",
                    subject_id=index,
                    tag_key="intent",
                    failure_stage=stage,
                    failure_mode="correct:missed_label",
                    signature_hash=f"{index:064d}",
                    cluster_key=f"{stage}:intent:missed_label",
                    dataset_split="train",
                    status="open",
                    root_cause={
                        "reason_code": "missed_label",
                        "truth_state": "present",
                        "upstream_routed": stage in {"asr", "vad", "speaker"},
                    },
                    occurrence_count=1,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                for index in range(1, count + 1)
            ]
        )


async def _enqueue(
    factory: async_sessionmaker[AsyncSession],
    *,
    baseline_id: int,
    **compiler: Any,
) -> int:
    service = PromptLabService(factory)
    result = await service.create_compilation(
        tenant_id=_TENANT,
        baseline_tagger_version_id=baseline_id,
        gold_set_version_id=None,
        compiler_config={"compiler": "builtin", **compiler},
        budget={"max_provider_calls": 10},
        actor_user_id=9,
    )
    return int(result["job_id"])


@pytest.mark.asyncio
async def test_a_queued_compile_becomes_a_reviewable_artifact(
    worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    baseline_id = await _seed_baseline(worker_factory)
    await _seed_badcases(worker_factory)
    await _enqueue(worker_factory, baseline_id=baseline_id)

    worker = PromptLabWorker(worker_factory, worker_id="test-worker")
    assert await worker.run_once() is True

    async with worker_factory() as session:
        artifacts = (await session.execute(select(TagPromptArtifact))).scalars().all()
        gradients = (await session.execute(select(TagPromptGradient))).scalars().all()

    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.status == "draft"
    assert artifact.compiler == "builtin"
    assert artifact.rendered_prompt.startswith("基线规则：按 schema 判定标签。")
    assert "intent" in artifact.rendered_prompt
    assert artifact.input_budget_report["fits"] is True
    # A longer prompt costs headroom; the report has to say so.
    assert artifact.input_budget_report["headroom_delta"] < 0
    assert {row.patch_id for row in gradients} == set(artifact.accepted_patch_ids)
    assert all(row.decision == "pending" for row in gradients)


@pytest.mark.asyncio
async def test_the_worker_ignores_jobs_belonging_to_the_tag_worker(
    worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    governance = TagGovernanceService(worker_factory)
    await governance.enqueue_job(
        tenant_id=_TENANT,
        job_type="extract",
        scope={"dialogue_unit_ids": [1]},
        idempotency_key="extract-1",
        created_by=9,
    )

    worker = PromptLabWorker(worker_factory, worker_id="test-worker")

    assert await worker.run_once() is False, "an extract job is not this worker's to run"
    assert PROMPT_LAB_JOB_TYPES == ("prompt_compile",)


@pytest.mark.asyncio
async def test_upstream_failures_alone_produce_no_candidate(
    worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Garbled transcripts are not a prompt problem, so the job fails rather than
    inventing advice that cannot help."""

    baseline_id = await _seed_baseline(worker_factory)
    await _seed_badcases(worker_factory, stage="asr")
    job_id = await _enqueue(worker_factory, baseline_id=baseline_id)

    worker = PromptLabWorker(worker_factory, worker_id="test-worker")
    assert await worker.run_once() is True

    async with worker_factory() as session:
        artifacts = (await session.execute(select(TagPromptArtifact))).scalars().all()
        job = await session.get(TagExtractionJob, job_id)

    assert artifacts == []
    assert job is not None
    assert job.last_error_message
    assert "nothing to propose" in job.last_error_message


@pytest.mark.asyncio
async def test_a_prompt_that_would_not_fit_is_refused_before_it_is_stored(
    worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The preflight exists so an unusable prompt never reaches a trial."""

    baseline_id = await _seed_baseline(worker_factory)
    await _seed_badcases(worker_factory, count=40)
    job_id = await _enqueue(
        worker_factory,
        baseline_id=baseline_id,
        max_patches=32,
        min_cluster_support=1,
        max_prompt_tokens=10,
    )

    worker = PromptLabWorker(worker_factory, worker_id="test-worker")
    assert await worker.run_once() is True

    async with worker_factory() as session:
        artifacts = (await session.execute(select(TagPromptArtifact))).scalars().all()
        job = await session.get(TagExtractionJob, job_id)

    assert artifacts == [], "nothing may be persisted when the preflight fails"
    assert job is not None
    assert job.last_error_message


@pytest.mark.asyncio
async def test_recompiling_identical_evidence_reuses_the_artifact(
    worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    baseline_id = await _seed_baseline(worker_factory)
    await _seed_badcases(worker_factory)
    worker = PromptLabWorker(worker_factory, worker_id="test-worker")

    await _enqueue(worker_factory, baseline_id=baseline_id)
    await worker.run_once()
    # A second compile with a different config, so a new job is created, but the same
    # badcases and therefore the same advice.
    await _enqueue(worker_factory, baseline_id=baseline_id, min_cluster_support=2)
    await worker.run_once()

    async with worker_factory() as session:
        artifacts = (await session.execute(select(TagPromptArtifact))).scalars().all()

    assert len(artifacts) == 1, "identical advice is content-addressed to one row"


@pytest.mark.asyncio
async def test_an_empty_queue_is_not_an_error(
    worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = PromptLabWorker(
        worker_factory,
        worker_id="test-worker",
        lease_ttl=timedelta(seconds=30),
    )

    assert await worker.run_once() is False


@pytest.mark.asyncio
async def test_the_heartbeat_renews_the_lease_until_told_to_stop(
    worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A long compile must not have its job reclaimed out from under it.

    Driven directly rather than through run_once: the renewal interval has a one
    second floor, which is right for production and far longer than a test compile.
    """

    baseline_id = await _seed_baseline(worker_factory)
    await _seed_badcases(worker_factory)
    job_id = await _enqueue(worker_factory, baseline_id=baseline_id)

    governance = TagGovernanceService(worker_factory)
    job = await governance.claim_next_job(
        worker_id="test-worker",
        now=datetime.now(UTC),
        lease_for=timedelta(minutes=5),
        job_types=PROMPT_LAB_JOB_TYPES,
    )
    assert job is not None and job.id == job_id

    beats: list[int] = []
    original = governance.heartbeat_job

    async def _counting_heartbeat(claimed_id: int, **kwargs: Any) -> bool:
        beats.append(claimed_id)
        return await original(claimed_id, **kwargs)

    governance.heartbeat_job = _counting_heartbeat  # type: ignore[method-assign]
    worker = PromptLabWorker(
        worker_factory,
        worker_id="test-worker",
        governance=governance,
        lease_ttl=timedelta(seconds=3),
    )

    stop = asyncio.Event()
    loop_task = asyncio.create_task(
        worker._heartbeat_loop(job=job, revision=[job.revision], stop=stop)
    )
    await asyncio.sleep(1.2)
    stop.set()
    await loop_task

    assert beats == [job_id], "the lease was renewed exactly once in the first interval"


@pytest.mark.asyncio
async def test_the_heartbeat_gives_up_once_the_lease_is_lost(
    worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Renewing a lease another worker already took would be a lie."""

    baseline_id = await _seed_baseline(worker_factory)
    await _seed_badcases(worker_factory)
    await _enqueue(worker_factory, baseline_id=baseline_id)

    governance = TagGovernanceService(worker_factory)
    job = await governance.claim_next_job(
        worker_id="other-worker",
        now=datetime.now(UTC),
        lease_for=timedelta(minutes=5),
        job_types=PROMPT_LAB_JOB_TYPES,
    )
    assert job is not None

    worker = PromptLabWorker(
        worker_factory,
        worker_id="test-worker",
        governance=governance,
        lease_ttl=timedelta(seconds=3),
    )
    stop = asyncio.Event()

    # Owned by other-worker, so the very first renewal fails and the loop returns.
    await asyncio.wait_for(
        worker._heartbeat_loop(job=job, revision=[job.revision], stop=stop),
        timeout=5,
    )

    assert not stop.is_set(), "the loop exited on its own rather than being told to"


def test_tag_definitions_degrade_to_an_empty_mapping() -> None:
    """Missing definitions cost the proposer its allowed-value hint, nothing more."""

    from audio_graphy.optimizer_worker import _definitions_for

    explicit = {"intent": {"key": "intent"}}
    assert _definitions_for({}, session_definitions=explicit) == explicit
    assert _definitions_for(
        {"output": {"thresholds": {"intent": 0.7}}}, session_definitions=None
    ) == {"intent": {"key": "intent"}}
    assert _definitions_for({}, session_definitions=None) == {}
    assert _definitions_for({"output": "not-a-mapping"}, session_definitions=None) == {}
    assert _definitions_for({"output": {}}, session_definitions=None) == {}


@pytest.mark.asyncio
async def test_a_compile_against_a_vanished_baseline_fails_the_job(
    worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The baseline is validated at enqueue time, but it can go away before the run."""

    from audio_graphy.models.tag_governance import TaggerVersion

    baseline_id = await _seed_baseline(worker_factory)
    await _seed_badcases(worker_factory)
    job_id = await _enqueue(worker_factory, baseline_id=baseline_id)
    async with worker_factory() as session, session.begin():
        baseline = await session.get(TaggerVersion, baseline_id)
        assert baseline is not None
        baseline.tenant_id = "someone_else"

    worker = PromptLabWorker(worker_factory, worker_id="test-worker")
    assert await worker.run_once() is True

    async with worker_factory() as session:
        job = await session.get(TagExtractionJob, job_id)
    assert job is not None
    assert "baseline tagger version is not available" in (job.last_error_message or "")


@pytest.mark.asyncio
async def test_a_job_naming_an_unimplemented_compiler_fails_rather_than_downgrades(
    worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The service refuses these at enqueue time; the worker must not trust that.

    A job queued by an older API image, or one whose scope was edited by hand, would
    otherwise be compiled by the template proposer and stored under the name of a
    compiler that never ran.
    """

    from sqlalchemy.orm.attributes import flag_modified

    from audio_graphy.models.prompt_lab import TagPromptArtifact

    baseline_id = await _seed_baseline(worker_factory)
    await _seed_badcases(worker_factory)
    job_id = await _enqueue(worker_factory, baseline_id=baseline_id)
    async with worker_factory() as session, session.begin():
        job = await session.get(TagExtractionJob, job_id)
        assert job is not None
        job.scope["compiler"]["compiler"] = "dspy_mipro"
        flag_modified(job, "scope")

    worker = PromptLabWorker(worker_factory, worker_id="test-worker")
    assert await worker.run_once() is True

    async with worker_factory() as session:
        failed = await session.get(TagExtractionJob, job_id)
        artifacts = (await session.execute(select(TagPromptArtifact))).scalars().all()
    assert failed is not None
    assert failed.last_error_code == "UnsupportedCompilerError"
    assert "dspy_mipro" in (failed.last_error_message or "")
    assert artifacts == [], "a refused compiler must not leave an artifact behind"


@pytest.mark.asyncio
async def test_the_loop_keeps_running_after_a_failed_iteration(
    worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    """One bad job must not take the worker process down."""

    worker = PromptLabWorker(worker_factory, worker_id="test-worker", poll_seconds=0.01)
    calls = {"count": 0}

    async def _flaky_run_once(**_kwargs: Any) -> bool:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("transient failure")
        if calls["count"] >= 3:
            raise asyncio.CancelledError
        return False

    worker.run_once = _flaky_run_once  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await worker.run_forever()

    assert calls["count"] == 3, "the loop survived the exception and kept polling"


class _RecordingAdapter:
    """Counts every call that reaches a provider boundary."""

    def __init__(self) -> None:
        self.model = "test-strong"
        self.calls: list[list[dict[str, Any]]] = []

    async def complete(
        self,
        messages: Any,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        cache_key: str | None = None,
        response_schema: Any = None,
    ) -> Any:
        from audio_graphy.adapters.protocols import LLMResponse

        self.calls.append([dict(message) for message in messages])
        cluster_tag = "intent"
        return LLMResponse(
            text=f"标签 {cluster_tag} 仅在客户明确表态时判定，缺乏文本依据时省略。",
            model=self.model,
            prompt_hash="h" * 64,
            usage={"prompt_tokens": 120, "completion_tokens": 30},
            cost_microunits=42,
        )


def _gateway_over(
    adapter: _RecordingAdapter,
    factory: async_sessionmaker[AsyncSession],
) -> Any:
    from audio_graphy.services.llm_gateway import LLMGateway
    from audio_graphy.services.llm_observability import LLMCallObserver

    return LLMGateway(
        adapter,
        cache=None,
        model_tier="strong",
        max_retries=0,
        observer=LLMCallObserver(factory, model_tier="strong"),
    )


@pytest.mark.asyncio
async def test_a_grounded_compile_spends_nothing_outside_the_ledger(
    worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every provider call must be visible as prompt-lab spend, and no other.

    This is the assertion the whole bridge exists to make true. A proposer that
    reached an adapter directly -- or that reused a purpose belonging to production
    tagging -- would still produce a perfectly good artifact, and the compile's cost
    would land on someone else's line or on nobody's.
    """

    from audio_graphy.models.llm_call_log import LLMCallLog

    adapter = _RecordingAdapter()
    baseline_id = await _seed_baseline(worker_factory)
    await _seed_badcases(worker_factory)
    await _enqueue(worker_factory, baseline_id=baseline_id, compiler="builtin_grounded")

    worker = PromptLabWorker(
        worker_factory,
        worker_id="test-worker",
        strong_llm=_gateway_over(adapter, worker_factory),
    )
    assert await worker.run_once() is True

    async with worker_factory() as session:
        rows = (await session.execute(select(LLMCallLog))).scalars().all()
        artifacts = (await session.execute(select(TagPromptArtifact))).scalars().all()

    assert adapter.calls, "the grounded proposer must actually have called the model"
    # The ledger records a logical request and each provider attempt separately, so
    # the attempts are what compare against the adapter.
    attempts = [row for row in rows if row.event_kind == "provider_attempt" and row.provider_called]
    assert len(attempts) == len(adapter.calls), (
        "every call that reached the adapter must appear in the ledger as an attempt"
    )
    assert rows and all(str(row.purpose).startswith("prompt_lab_") for row in rows), (
        "no part of a compile may be booked under another purpose"
    )

    (artifact,) = artifacts
    assert artifact.compiler == "builtin_grounded"
    assert artifact.compiler_version == "builtin-grounded-v1"


@pytest.mark.asyncio
async def test_a_grounded_compile_without_a_model_fails_instead_of_falling_back(
    worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A model-free worker must not quietly serve a template artifact instead."""

    baseline_id = await _seed_baseline(worker_factory)
    await _seed_badcases(worker_factory)
    job_id = await _enqueue(worker_factory, baseline_id=baseline_id, compiler="builtin_grounded")

    worker = PromptLabWorker(worker_factory, worker_id="test-worker")
    assert await worker.run_once() is True

    async with worker_factory() as session:
        job = await session.get(TagExtractionJob, job_id)
        artifacts = (await session.execute(select(TagPromptArtifact))).scalars().all()

    assert job is not None
    assert "强模型适配器" in (job.last_error_message or "")
    assert artifacts == []


@pytest.mark.asyncio
async def test_the_dspy_compilers_are_still_refused(
    worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    """DSPy-native compilers are named in the public contract but not implemented.

    textgrad_tgd is deliberately absent: it *is* implemented, and is refused later by
    the worker when the optional extra is missing -- a different failure with a
    different message.
    """

    from audio_graphy.services.prompt_lab import PromptLabError

    baseline_id = await _seed_baseline(worker_factory)
    service = PromptLabService(worker_factory)

    for name in ("dspy_mipro", "dspy_bootstrap", "dspy_gepa"):
        with pytest.raises(PromptLabError, match="尚未实现"):
            await service.create_compilation(
                tenant_id=_TENANT,
                baseline_tagger_version_id=baseline_id,
                gold_set_version_id=None,
                compiler_config={"compiler": name},
                budget={},
                actor_user_id=9,
            )


@pytest.mark.asyncio
async def test_textgrad_without_the_extra_is_refused_not_downgraded(
    worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    """CI never installs textgrad, so this is the path every deployment without it hits.

    Falling back to the grounded proposer would stamp the artifact ``textgrad_tgd``
    while no gradient was ever computed, and the gradient panel would present a
    template rationale as a diagnosis.
    """

    adapter = _RecordingAdapter()
    baseline_id = await _seed_baseline(worker_factory)
    await _seed_badcases(worker_factory)
    job_id = await _enqueue(worker_factory, baseline_id=baseline_id, compiler="textgrad_tgd")

    worker = PromptLabWorker(
        worker_factory,
        worker_id="test-worker",
        strong_llm=_gateway_over(adapter, worker_factory),
    )
    assert await worker.run_once() is True

    async with worker_factory() as session:
        job = await session.get(TagExtractionJob, job_id)
        artifacts = (await session.execute(select(TagPromptArtifact))).scalars().all()

    assert job is not None
    assert job.last_error_code == "MissingExtraError"
    assert "textgrad" in (job.last_error_message or "")
    assert artifacts == []
    assert adapter.calls == [], "a refused compiler must not spend provider budget"


@pytest.mark.asyncio
async def test_a_gradient_compile_stores_the_diagnosis_not_the_rationale(
    worker_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The fourth panel reads gradient_text; a restated rationale there is a lie."""

    from audio_graphy.optimizers.gradients import GradientOutcome, TextGradProposer

    class StubStep:
        def run(self, **_: object) -> GradientOutcome:
            return GradientOutcome(
                gradient_text="现行规则只认明确表述，未覆盖客户间接表达意向的说法。",
                proposed_edit="标签 intent 在客户以间接方式表达购买倾向时同样应判定。",
                rounds=2,
            )

    baseline_id = await _seed_baseline(worker_factory)
    await _seed_badcases(worker_factory)
    await _enqueue(worker_factory, baseline_id=baseline_id, compiler="textgrad_tgd")

    worker = PromptLabWorker(worker_factory, worker_id="test-worker")
    # Substitute the proposer the extra would have provided, leaving every other
    # step of the real pipeline in place.
    worker._proposer_for = lambda *a, **k: TextGradProposer(step=StubStep())  # type: ignore[method-assign]
    assert await worker.run_once() is True

    async with worker_factory() as session:
        artifacts = (await session.execute(select(TagPromptArtifact))).scalars().all()
        gradients = (await session.execute(select(TagPromptGradient))).scalars().all()

    (artifact,) = artifacts
    assert artifact.compiler == "textgrad_tgd"
    assert artifact.compiler_version == "textgrad-tgd-v1"
    (gradient,) = gradients
    assert gradient.gradient_text.startswith("现行规则只认明确表述")
    assert "间接方式" in gradient.proposed_edit
    assert gradient.evaluation["gradient_rounds"] == 2
    assert gradient.evaluation["replayed"] is False
    # 6 个样本低于 10，效果不足以采信，记录必须自己说清楚。
    assert gradient.evaluation["low_confidence"] is True
