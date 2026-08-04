"""Policy the prompt-lab service enforces on behalf of callers.

Two of these matter more than the rest. Applying review decisions must be idempotent
or a double-clicked submit mints a second candidate that nobody approved twice. And
verbatim customer speech must be refused before it is stored, because once a prompt
is copied into an immutable TaggerVersion there is no route back out.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from audio_graphy.models import Base
from audio_graphy.models.prompt_lab import (
    TagPromptArtifact,
    TagPromptDemoSource,
    TagPromptGradient,
)
from audio_graphy.optimizers.artifacts import (
    CompiledPromptArtifact,
    PromptDemo,
    PromptPatch,
)
from audio_graphy.services.prompt_lab import (
    PatchDecision,
    PromptLabError,
    PromptLabPrivacyError,
    PromptLabService,
)

_TENANT = "chang_an"


@pytest.fixture
async def lab_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    # Without this the connection is closed by the garbage collector, which raises a
    # ResourceWarning that `filterwarnings = ["error"]` turns into a test failure --
    # attributed to whichever test happened to trigger the collection.
    await engine.dispose()


def _patch(patch_id: str, *, ordinal: int, body: str) -> PromptPatch:
    return PromptPatch(
        patch_id=patch_id,
        kind="rule_clarification",
        origin="builtin",
        ordinal=ordinal,
        body=body,
        rationale=f"cluster {patch_id}",
        target_tag_keys=("intent",),
    )


def _demo(demo_id: str, *, mode: str = "synthetic") -> PromptDemo:
    return PromptDemo(
        demo_id=demo_id,
        gold_label_id=1,
        subject_type="dialogue_unit",
        subject_id=42,
        rendered_text=f"示例 {demo_id}",
        redaction_mode=mode,  # type: ignore[arg-type]
        source_checksum="a" * 64,
        reception_id=7,
        segment_ids=(1, 2),
    )


def _artifact(**overrides: Any) -> CompiledPromptArtifact:
    defaults: dict[str, Any] = {
        "baseline_prompt": "基线规则",
        "header": "基线规则",
        "compiler": "builtin",
        "compiler_version": "builtin-proposer-v1",
        "metric_version": "prompt-lab-metric-v1",
        "patches": (
            _patch("p1", ordinal=1, body="规则一"),
            _patch("p2", ordinal=2, body="规则二"),
        ),
        "demos": (_demo("d1"),),
        "accepted_patch_ids": frozenset({"p1", "p2"}),
    }
    return CompiledPromptArtifact(**(defaults | overrides))


async def _persist(service: PromptLabService, artifact: CompiledPromptArtifact) -> Any:
    return await service.persist_artifact(
        tenant_id=_TENANT,
        compilation_id=1,
        artifact=artifact,
        baseline_tagger_version_id=1,
        gold_set_version_id=None,
        actor_user_id=9,
        gradients=[
            {
                "patch_id": patch.patch_id,
                "tag_key": "intent",
                "failure_stage": "tag_reasoning",
                "gradient_text": f"诊断 {patch.patch_id}",
                "proposed_edit": patch.body,
                "evaluation": {"support": 6},
            }
            for patch in artifact.patches
        ],
    )


@pytest.mark.asyncio
async def test_persisting_the_same_artifact_twice_reuses_the_row(
    lab_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = PromptLabService(lab_factory)

    first = await _persist(service, _artifact())
    second = await _persist(service, _artifact())

    assert first.id == second.id
    async with lab_factory() as session:
        rows = (await session.execute(select(TagPromptArtifact))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_demo_provenance_is_recorded_for_every_inlined_example(
    lab_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Erasure needs a route from a served prompt back to the source conversation."""

    service = PromptLabService(lab_factory)
    artifact = await _persist(service, _artifact())

    async with lab_factory() as session:
        sources = (
            (
                await session.execute(
                    select(TagPromptDemoSource).where(
                        TagPromptDemoSource.artifact_id == artifact.id
                    )
                )
            )
            .scalars()
            .all()
        )

    assert [source.demo_id for source in sources] == ["d1"]
    assert sources[0].reception_id == 7
    assert sources[0].segment_ids == [1, 2]


@pytest.mark.asyncio
async def test_verbatim_demos_are_refused_before_they_can_be_stored(
    lab_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = PromptLabService(lab_factory)

    with pytest.raises(PromptLabPrivacyError, match="verbatim"):
        await _persist(service, _artifact(demos=(_demo("d1", mode="verbatim"),)))

    async with lab_factory() as session:
        rows = (await session.execute(select(TagPromptArtifact))).scalars().all()
    assert rows == [], "nothing may be persisted when the privacy check fails"


@pytest.mark.asyncio
async def test_rejecting_a_patch_creates_a_child_and_supersedes_the_parent(
    lab_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = PromptLabService(lab_factory)
    parent = await _persist(service, _artifact())

    child = await service.apply_patch_decisions(
        tenant_id=_TENANT,
        artifact_id=parent.id,
        decisions=[
            PatchDecision(patch_id="p1", accepted=True),
            PatchDecision(patch_id="p2", accepted=False, note="会与总则冲突"),
        ],
        actor_user_id=9,
    )

    assert child.id != parent.id
    assert child.parent_artifact_id == parent.id
    assert "规则一" in child.rendered_prompt
    assert "规则二" not in child.rendered_prompt

    async with lab_factory() as session:
        refreshed = await session.get(TagPromptArtifact, parent.id)
        gradients = (
            (
                await session.execute(
                    select(TagPromptGradient).where(TagPromptGradient.artifact_id == parent.id)
                )
            )
            .scalars()
            .all()
        )
    assert refreshed is not None
    assert refreshed.status == "superseded"
    decisions = {row.patch_id: row.decision for row in gradients}
    assert decisions == {"p1": "accepted", "p2": "rejected"}
    assert next(row.decision_note for row in gradients if row.patch_id == "p2")


@pytest.mark.asyncio
async def test_resubmitting_the_same_decisions_is_idempotent(
    lab_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A double-clicked submit must not mint a second candidate."""

    service = PromptLabService(lab_factory)
    parent = await _persist(service, _artifact())
    decisions = [
        PatchDecision(patch_id="p1", accepted=True),
        PatchDecision(patch_id="p2", accepted=False),
    ]

    first = await service.apply_patch_decisions(
        tenant_id=_TENANT,
        artifact_id=parent.id,
        decisions=decisions,
        actor_user_id=9,
    )
    # The parent is superseded now, so resubmitting has to go through the child.
    second = await service.apply_patch_decisions(
        tenant_id=_TENANT,
        artifact_id=first.id,
        decisions=[PatchDecision(patch_id="p1", accepted=True)],
        actor_user_id=9,
    )

    assert second.id == first.id, "an unchanged accepted set must resolve to the same row"
    async with lab_factory() as session:
        rows = (await session.execute(select(TagPromptArtifact))).scalars().all()
    assert len(rows) == 2, "one compile plus one review, not three"


@pytest.mark.asyncio
async def test_a_superseded_artifact_refuses_further_decisions(
    lab_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.tag_governance import GovernanceConflictError

    service = PromptLabService(lab_factory)
    parent = await _persist(service, _artifact())
    await service.apply_patch_decisions(
        tenant_id=_TENANT,
        artifact_id=parent.id,
        decisions=[PatchDecision(patch_id="p1", accepted=True)],
        actor_user_id=9,
    )

    with pytest.raises(GovernanceConflictError, match="draft or review"):
        await service.apply_patch_decisions(
            tenant_id=_TENANT,
            artifact_id=parent.id,
            decisions=[PatchDecision(patch_id="p2", accepted=True)],
            actor_user_id=9,
        )


@pytest.mark.asyncio
async def test_an_artifact_from_another_tenant_is_not_reachable(
    lab_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = PromptLabService(lab_factory)
    artifact = await _persist(service, _artifact())

    from audio_graphy.services.prompt_lab import PromptLabError

    with pytest.raises(PromptLabError, match="not found"):
        await service.get_artifact(tenant_id="other_tenant", artifact_id=artifact.id)


async def _seed_baseline(
    factory: async_sessionmaker[AsyncSession],
    *,
    gold_set_status: str = "frozen",
) -> tuple[int, int]:
    """Create the minimum a compilation needs to point at: a baseline and a gold set."""

    from datetime import UTC, datetime

    from audio_graphy.models.tag_governance import (
        TaggerVersion,
        TagGoldSet,
        TagGoldSetVersion,
        TagSchema,
        TagSchemaVersion,
    )

    async with factory() as session, session.begin():
        schema = TagSchema(tenant_id=_TENANT, key="sales", name="Sales", created_by=9)
        session.add(schema)
        await session.flush()
        schema_version = TagSchemaVersion(
            tenant_id=_TENANT,
            schema_id=schema.id,
            version=1,
            definitions=[{"key": "intent"}],
            checksum="c" * 64,
            created_by=9,
        )
        session.add(schema_version)
        await session.flush()
        tagger = TaggerVersion(
            tenant_id=_TENANT,
            schema_version_id=schema_version.id,
            version="baseline-v1",
            model_version="weak-v1",
            config_checksum="d" * 64,
            created_by=9,
        )
        gold_set = TagGoldSet(
            tenant_id=_TENANT,
            schema_version_id=schema_version.id,
            key="gold",
            name="gold",
            created_by=9,
        )
        session.add_all([tagger, gold_set])
        await session.flush()
        gold_version = TagGoldSetVersion(
            tenant_id=_TENANT,
            gold_set_id=gold_set.id,
            version=1,
            status=gold_set_status,
            frozen_at=datetime.now(UTC) if gold_set_status == "frozen" else None,
        )
        session.add(gold_version)
        await session.flush()
        return int(tagger.id), int(gold_version.id)


@pytest.mark.asyncio
async def test_a_compilation_is_queued_not_executed(
    lab_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The service records intent; the worker does the compiling."""

    from audio_graphy.models.tag_governance import TagExtractionJob

    service = PromptLabService(lab_factory)
    baseline_id, gold_version_id = await _seed_baseline(lab_factory)

    result = await service.create_compilation(
        tenant_id=_TENANT,
        baseline_tagger_version_id=baseline_id,
        gold_set_version_id=gold_version_id,
        compiler_config={"compiler": "builtin", "max_patches": 4},
        budget={"max_provider_calls": 120, "max_provider_tokens": 1_500_000},
        actor_user_id=9,
    )

    async with lab_factory() as session:
        job = await session.get(TagExtractionJob, result["job_id"])
    assert job is not None
    assert job.job_type == "prompt_compile"
    assert job.scope["compilation_id"] == result["compilation_id"]
    assert job.scope["budget"]["max_provider_calls"] == 120


@pytest.mark.asyncio
async def test_an_identical_compilation_request_reuses_its_job(
    lab_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = PromptLabService(lab_factory)
    baseline_id, gold_version_id = await _seed_baseline(lab_factory)
    request: dict[str, Any] = {
        "tenant_id": _TENANT,
        "baseline_tagger_version_id": baseline_id,
        "gold_set_version_id": gold_version_id,
        "compiler_config": {"compiler": "builtin"},
        "budget": {"max_provider_calls": 10},
        "actor_user_id": 9,
    }

    first = await service.create_compilation(**request)
    second = await service.create_compilation(**request)

    assert first == second


@pytest.mark.asyncio
async def test_an_unimplemented_compiler_is_refused_instead_of_downgraded(
    lab_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The schema names compilers that do not exist yet; queueing one must fail.

    Silently compiling with ``builtin`` would stamp the artifact ``compiler:
    dspy_mipro`` while the patches came from the template proposer -- and every
    later comparison between compilers would be reading a lie.
    """

    from audio_graphy.models.tag_governance import TagExtractionJob
    from audio_graphy.services.prompt_lab import PromptLabError

    service = PromptLabService(lab_factory)
    baseline_id, _ = await _seed_baseline(lab_factory)

    with pytest.raises(PromptLabError, match="尚未实现"):
        await service.create_compilation(
            tenant_id=_TENANT,
            baseline_tagger_version_id=baseline_id,
            gold_set_version_id=None,
            compiler_config={"compiler": "dspy_mipro"},
            budget={},
            actor_user_id=9,
        )

    # 拒绝要发生在入队之前，否则队列里会留下一个注定失败的任务。
    async with lab_factory() as session:
        queued = await session.scalar(
            select(func.count())
            .select_from(TagExtractionJob)
            .where(TagExtractionJob.job_type == "prompt_compile")
        )
    assert queued == 0


@pytest.mark.asyncio
async def test_a_compilation_must_read_a_frozen_gold_set(
    lab_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Compiling against a mutable gold set would make the run unreproducible."""

    from audio_graphy.services.prompt_lab import PromptLabError

    service = PromptLabService(lab_factory)
    baseline_id, gold_version_id = await _seed_baseline(lab_factory, gold_set_status="draft")

    with pytest.raises(PromptLabError, match="frozen"):
        await service.create_compilation(
            tenant_id=_TENANT,
            baseline_tagger_version_id=baseline_id,
            gold_set_version_id=gold_version_id,
            compiler_config={"compiler": "builtin"},
            budget={},
            actor_user_id=9,
        )


@pytest.mark.asyncio
async def test_a_compilation_cannot_point_at_another_tenants_baseline(
    lab_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audio_graphy.services.prompt_lab import PromptLabError

    service = PromptLabService(lab_factory)
    baseline_id, _ = await _seed_baseline(lab_factory)

    with pytest.raises(PromptLabError, match="baseline tagger version not found"):
        await service.create_compilation(
            tenant_id="other_tenant",
            baseline_tagger_version_id=baseline_id,
            gold_set_version_id=None,
            compiler_config={"compiler": "builtin"},
            budget={},
            actor_user_id=9,
        )


@pytest.mark.asyncio
async def test_only_actionable_badcases_reach_the_compiler(
    lab_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Resolved cases and holdout-lane cases are not evidence for a prompt change."""

    from datetime import UTC, datetime

    from audio_graphy.models.tag_governance import TagBadcase

    now = datetime.now(UTC)
    async with lab_factory() as session, session.begin():
        session.add_all(
            [
                TagBadcase(
                    tenant_id=_TENANT,
                    # A badcase must trace to the evidence that produced it.
                    source_feedback_event_id=index,
                    subject_type="dialogue_unit",
                    subject_id=index,
                    tag_key="intent",
                    failure_stage="tag_reasoning",
                    failure_mode="correct:missed",
                    signature_hash=f"{index}" * 8,
                    dataset_split=split,
                    status=status,
                    root_cause={"reason_code": "missed", "upstream_routed": False},
                    occurrence_count=3,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                for index, (status, split) in enumerate(
                    [
                        ("open", "train"),
                        ("reopened", "validation"),
                        ("resolved", "train"),
                        ("open", "holdout"),
                    ],
                    start=1,
                )
            ]
        )

    rows = await PromptLabService(lab_factory).badcase_rows_for_compile(tenant_id=_TENANT)

    assert sorted(row["id"] for row in rows) == [1, 2]
    assert all(row["occurrence_count"] == 3 for row in rows)


@pytest.mark.asyncio
async def test_gradients_and_artifacts_can_be_listed_and_filtered(
    lab_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = PromptLabService(lab_factory)
    parent = await _persist(service, _artifact())

    all_gradients = await service.list_gradients(tenant_id=_TENANT, artifact_id=parent.id)
    assert {row.patch_id for row in all_gradients} == {"p1", "p2"}

    await service.apply_patch_decisions(
        tenant_id=_TENANT,
        artifact_id=parent.id,
        decisions=[PatchDecision(patch_id="p1", accepted=True)],
        actor_user_id=9,
    )

    accepted = await service.list_gradients(
        tenant_id=_TENANT,
        artifact_id=parent.id,
        decision="accepted",
    )
    assert [row.patch_id for row in accepted] == ["p1"]

    drafts = await service.list_artifacts(tenant_id=_TENANT, status="draft")
    superseded = await service.list_artifacts(tenant_id=_TENANT, status="superseded")
    assert len(drafts) == 1
    assert [row.id for row in superseded] == [parent.id]


@pytest.mark.asyncio
async def test_readiness_counts_reviewed_labels_and_ignores_silver(
    lab_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Silver rows are visible so the gap is legible, but they never close it."""

    from audio_graphy.models.prompt_lab import TagSilverLabel

    async with lab_factory() as session, session.begin():
        session.add_all(
            [
                TagSilverLabel(
                    tenant_id=_TENANT,
                    subject_type="dialogue_unit",
                    subject_id=index,
                    tag_key="intent",
                    evidence_refs=[],
                    truth_state="present",
                    truth_tier="t1",
                    split="train",
                    teacher_model_tier="strong",
                    agreement_count=3,
                    source="strong_critic",
                )
                for index in range(1, 41)
            ]
        )

    readiness = await PromptLabService(lab_factory).readiness(tenant_id=_TENANT)

    (domain,) = readiness.domains
    assert domain.domain == "dialogue_unit:intent"
    assert domain.silver_count == 40
    assert domain.gold_count == 0
    assert domain.feedback_count == 0
    assert domain.meets_threshold is False
    assert "domain_support_below_30:dialogue_unit:intent" in readiness.blockers
    # 30 missing subjects at five minutes each.
    assert readiness.annotation_hours_remaining == 2.5


@pytest.mark.asyncio
async def test_readiness_names_the_gap_and_prices_it_in_human_hours(
    lab_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An empty tenant should say what is missing, not merely that it is not ready."""

    service = PromptLabService(lab_factory)

    readiness = await service.readiness(tenant_id=_TENANT)

    assert readiness.ready is False
    assert "reviewed_feedback_below_200" in readiness.blockers
    assert "no_reviewed_domains" in readiness.blockers
    assert "no_frozen_gold_set" in readiness.blockers
    assert readiness.gold_label_total == 0
    payload = readiness.as_payload()
    assert payload["annotation_hours_remaining"] == 0.0
    assert payload["domains"] == []


async def _seed_badcase(
    factory: async_sessionmaker[AsyncSession],
    *,
    index: int,
) -> None:
    """One open, train-lane badcase — the kind a compile reads."""

    from datetime import UTC, datetime

    from audio_graphy.models.tag_governance import TagBadcase

    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        session.add(
            TagBadcase(
                tenant_id=_TENANT,
                source_feedback_event_id=index,
                subject_type="dialogue_unit",
                subject_id=index,
                tag_key="intent",
                failure_stage="tag_reasoning",
                failure_mode="correct:missed",
                signature_hash=f"{index:08d}" * 8,
                dataset_split="train",
                status="open",
                root_cause={"reason_code": "missed", "upstream_routed": False},
                occurrence_count=3,
                first_seen_at=now,
                last_seen_at=now,
            )
        )


@pytest.mark.asyncio
async def test_recompiling_after_new_feedback_is_a_new_compilation(
    lab_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The corpus is an input, so it has to be inside the idempotency key.

    It was not: compilation_id hashed only (tenant, baseline, gold set, compiler)
    while the worker read whatever badcases were open at the moment it ran. After a
    review round the identical request resolved to the already-finished job and
    returned 202 with nothing left to run — and changing the budget to force a
    re-run raised 409 instead, because the budget is in scope but not in the id.
    Recompiling the same baseline was impossible by any route.
    """

    service = PromptLabService(lab_factory)
    baseline_id, gold_version_id = await _seed_baseline(lab_factory)
    request: dict[str, Any] = {
        "tenant_id": _TENANT,
        "baseline_tagger_version_id": baseline_id,
        "gold_set_version_id": gold_version_id,
        "compiler_config": {"compiler": "builtin"},
        "budget": {"max_provider_calls": 10},
        "actor_user_id": 9,
    }

    await _seed_badcase(lab_factory, index=1)
    first = await service.create_compilation(**request)
    # Same corpus, same request: still one job.
    assert await service.create_compilation(**request) == first

    await _seed_badcase(lab_factory, index=2)
    second = await service.create_compilation(**request)

    assert second["compilation_id"] != first["compilation_id"]
    assert second["job_id"] != first["job_id"]


@pytest.mark.asyncio
async def test_asking_for_demos_no_compiler_emits_is_refused(
    lab_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Same rule as an unimplemented compiler: refuse, do not silently ignore.

    Both shipped proposers hardcode ``demos=()``. A request for four inline
    examples under a masking policy compiled fine, produced zero demos, and
    reported ``demo_count: 0`` — with nothing saying the request had been dropped
    and the privacy control the schema advertises unable to ever fire.
    """

    service = PromptLabService(lab_factory)
    baseline_id, gold_version_id = await _seed_baseline(lab_factory)

    with pytest.raises(PromptLabError, match="demo_count"):
        await service.create_compilation(
            tenant_id=_TENANT,
            baseline_tagger_version_id=baseline_id,
            gold_set_version_id=gold_version_id,
            compiler_config={"compiler": "builtin", "demo_count": 4},
            budget={"max_provider_calls": 10},
            actor_user_id=9,
        )


@pytest.mark.asyncio
async def test_a_child_artifact_carries_the_reasoning_for_its_surviving_patches(
    lab_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Gradients follow the patches they argued for.

    They were written once against the compiled parent and ``list_gradients``
    filters strictly by artifact_id, so the tab for a re-materialized child came
    back empty under the text "内置编译器只在存在错误簇时才产出建议" — false about a
    child that carries those very patches. It also made a second review round
    impossible: nothing to decide on the child, and 409 on the superseded parent.
    """

    service = PromptLabService(lab_factory)
    parent = await _persist(service, _artifact())

    child = await service.apply_patch_decisions(
        tenant_id=_TENANT,
        artifact_id=parent.id,
        decisions=[
            PatchDecision(patch_id="p1", accepted=True),
            PatchDecision(patch_id="p2", accepted=False),
        ],
        actor_user_id=9,
    )

    parent_gradients = await service.list_gradients(tenant_id=_TENANT, artifact_id=parent.id)
    child_gradients = await service.list_gradients(tenant_id=_TENANT, artifact_id=child.id)

    # The parent keeps the audit trail of what was decided.
    assert {g.patch_id: g.decision for g in parent_gradients} == {
        "p1": "accepted",
        "p2": "rejected",
    }
    # The child gets the reasoning for what it kept, open for a further round.
    assert [g.patch_id for g in child_gradients] == ["p1"]
    assert child_gradients[0].decision == "pending"
    assert child_gradients[0].gradient_text == "诊断 p1"
    assert child_gradients[0].iteration == 2


@pytest.mark.asyncio
async def test_a_child_artifact_is_priced_as_itself_not_as_its_parent(
    lab_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The budget report must describe the prompt shipped beside it.

    ``prompt_token_estimate`` was recomputed from the child while
    ``input_budget_report`` was copied from the parent verbatim, so the review UI
    priced an accepted one-patch prompt using the rejected three-patch prompt's
    headroom — two numbers on one resource describing different prompts.
    """

    service = PromptLabService(lab_factory)
    parent = await service.persist_artifact(
        tenant_id=_TENANT,
        compilation_id=1,
        artifact=_artifact(
            patches=(
                _patch("p1", ordinal=1, body="规则一" * 40),
                _patch("p2", ordinal=2, body="规则二" * 40),
            ),
            accepted_patch_ids=frozenset({"p1", "p2"}),
        ),
        baseline_tagger_version_id=1,
        gold_set_version_id=None,
        actor_user_id=9,
        input_budget_report={
            "prompt_tokens": 900,
            "schema_tokens": 1_500,
            "fixed_tokens": 2_400,
            "usable_tokens": 4_000,
            "headroom_tokens": 1_600,
            "baseline_fixed_tokens": 1_800,
            "baseline_headroom_tokens": 2_200,
            "headroom_delta": -600,
            "headroom_shrink_ratio": 0.27,
            "fits": True,
        },
    )

    child = await service.apply_patch_decisions(
        tenant_id=_TENANT,
        artifact_id=parent.id,
        decisions=[
            PatchDecision(patch_id="p1", accepted=True),
            PatchDecision(patch_id="p2", accepted=False),
        ],
        actor_user_id=9,
    )

    report = dict(child.input_budget_report)
    # Dropping a patch can only free headroom, never consume more.
    assert report["prompt_tokens"] < 900
    assert report["headroom_tokens"] > 1_600
    assert report["headroom_delta"] > -600
    # What the schema and the budget fix: carried over untouched.
    assert report["schema_tokens"] == 1_500
    assert report["usable_tokens"] == 4_000
    assert report["baseline_headroom_tokens"] == 2_200
    # And the derived fields stay consistent with each other.
    assert report["fixed_tokens"] == report["prompt_tokens"] + 1_500
    assert report["headroom_tokens"] == 4_000 - report["fixed_tokens"]


async def _persist_against_baseline(
    service: PromptLabService,
    factory: async_sessionmaker[AsyncSession],
) -> tuple[Any, int]:
    """An artifact whose baseline is a real TaggerVersion row, as promotion needs."""

    baseline_id, _ = await _seed_baseline(factory)
    artifact = await service.persist_artifact(
        tenant_id=_TENANT,
        compilation_id=1,
        artifact=_artifact(),
        baseline_tagger_version_id=baseline_id,
        gold_set_version_id=None,
        actor_user_id=9,
    )
    return artifact, baseline_id


@pytest.mark.asyncio
async def test_promoting_an_artifact_mints_a_draft_prompt_lab_tagger_version(
    lab_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Promotion is the lab's only exit: evaluations read tagger versions, not artifacts.

    The candidate must carry the artifact's rendered prompt, the prompt_lab origin
    and draft status — draft, because promotion feeds the normal evaluation and
    deployment gates rather than bypassing them.
    """

    from audio_graphy.models.tag_governance import TaggerVersion

    service = PromptLabService(lab_factory)
    artifact, baseline_id = await _persist_against_baseline(service, lab_factory)

    promoted, candidate = await service.promote_artifact(
        tenant_id=_TENANT,
        artifact_id=artifact.id,
        version_suffix="r1",
        change_summary="采纳两条聚类补丁后的候选提示词",
        actor_user_id=9,
    )

    assert promoted.status == "accepted"
    assert promoted.candidate_tagger_version_id == candidate.id
    assert candidate.origin == "prompt_lab"
    assert candidate.status == "draft"
    assert candidate.prompt_artifact_id == artifact.id
    assert candidate.parent_version_id == baseline_id
    assert candidate.version == "baseline-v1-lab-r1"
    # The served prompt is exactly what was reviewed, both as the flat column and
    # inside the harness spec the extractor will actually execute.
    assert candidate.prompt_content == str(artifact.rendered_prompt).strip()
    assert candidate.harness_spec is not None
    assert (
        candidate.harness_spec["generation"]["prompt_template"]
        == str(artifact.rendered_prompt).strip()
    )

    async with lab_factory() as session:
        stored = await session.get(TaggerVersion, candidate.id)
    assert stored is not None
    assert stored.change_summary == "采纳两条聚类补丁后的候选提示词"


@pytest.mark.asyncio
async def test_promoting_twice_resolves_to_the_same_candidate_version(
    lab_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A double-clicked promote must not mint a second tagger version."""

    from audio_graphy.models.tag_governance import TaggerVersion

    service = PromptLabService(lab_factory)
    artifact, baseline_id = await _persist_against_baseline(service, lab_factory)
    request: dict[str, Any] = {
        "tenant_id": _TENANT,
        "artifact_id": artifact.id,
        "version_suffix": "r1",
        "change_summary": "采纳两条聚类补丁后的候选提示词",
        "actor_user_id": 9,
    }

    _, first = await service.promote_artifact(**request)
    # Even a different suffix resolves to the existing candidate: the artifact
    # already points at its version, and that pointer is the idempotency key.
    request["version_suffix"] = "r2"
    _, second = await service.promote_artifact(**request)

    assert second.id == first.id
    async with lab_factory() as session:
        total = await session.scalar(
            select(func.count())
            .select_from(TaggerVersion)
            .where(TaggerVersion.origin == "prompt_lab")
        )
    assert total == 1, "one promotion, not one per click"
    assert baseline_id != first.id


@pytest.mark.asyncio
async def test_a_superseded_artifact_cannot_be_promoted(
    lab_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Only the reviewed head of the lineage may become a candidate version."""

    from audio_graphy.services.tag_governance import GovernanceConflictError

    service = PromptLabService(lab_factory)
    parent, _ = await _persist_against_baseline(service, lab_factory)
    await service.apply_patch_decisions(
        tenant_id=_TENANT,
        artifact_id=parent.id,
        decisions=[PatchDecision(patch_id="p1", accepted=True)],
        actor_user_id=9,
    )

    with pytest.raises(GovernanceConflictError, match="draft or review"):
        await service.promote_artifact(
            tenant_id=_TENANT,
            artifact_id=parent.id,
            version_suffix="r1",
            change_summary="被取代的产物不应再晋级",
            actor_user_id=9,
        )


@pytest.mark.asyncio
async def test_a_composed_version_wider_than_the_column_is_refused(
    lab_factory: async_sessionmaker[AsyncSession],
) -> None:
    """tagger_versions.version is VARCHAR(64) and promotion composes into it.

    baseline + "-lab-" + suffix reaches 101 chars at the schema's own limits.
    Strict MySQL answers 1406 -- a 500 that names no field -- and a non-strict
    server truncates, which is worse: two suffixes collapse to one string under
    UNIQUE(tenant_id, version). These tests run on SQLite, which ignores
    declared widths entirely, so nothing but this guard can catch it here.
    """

    from audio_graphy.models.tag_governance import TaggerVersion
    from audio_graphy.services.prompt_lab import PromptLabError

    service = PromptLabService(lab_factory)
    artifact, baseline_id = await _persist_against_baseline(service, lab_factory)
    long_baseline = "release-" + "x" * 40
    async with lab_factory() as session, session.begin():
        baseline = await session.get(TaggerVersion, baseline_id)
        assert baseline is not None
        baseline.version = long_baseline

    with pytest.raises(PromptLabError, match="exceeds the 64-char limit"):
        await service.promote_artifact(
            tenant_id=_TENANT,
            artifact_id=artifact.id,
            version_suffix="r" * 32,
            change_summary="超长基线名 + 最长后缀",
            actor_user_id=9,
        )

    # The refusal has to be actionable: it names the room the suffix actually has.
    with pytest.raises(PromptLabError, match="leaves 11 chars for the suffix"):
        await service.promote_artifact(
            tenant_id=_TENANT,
            artifact_id=artifact.id,
            version_suffix="r" * 32,
            change_summary="超长基线名 + 最长后缀",
            actor_user_id=9,
        )

    # And the boundary is inclusive: exactly 64 still promotes.
    _, candidate = await service.promote_artifact(
        tenant_id=_TENANT,
        artifact_id=artifact.id,
        version_suffix="r" * 11,
        change_summary="正好落在列宽上",
        actor_user_id=9,
    )
    assert len(candidate.version) == 64


@pytest.mark.asyncio
async def test_promotion_is_tenant_scoped(
    lab_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = PromptLabService(lab_factory)
    artifact, _ = await _persist_against_baseline(service, lab_factory)

    with pytest.raises(PromptLabError, match="not found"):
        await service.promote_artifact(
            tenant_id="other_tenant",
            artifact_id=artifact.id,
            version_suffix="r1",
            change_summary="跨租户的晋级必须不可达",
            actor_user_id=9,
        )
