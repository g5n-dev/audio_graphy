"""Application service for offline prompt compilation.

This layer owns persistence and policy; it never talks to a model. The compiler
itself lives in :mod:`audio_graphy.optimizers` and is invoked by the worker, so this
module stays importable whether or not the optional extras are installed.

Two invariants are enforced here rather than trusted to callers:

* A demonstration may only come from a human-reviewed gold label in the train lane.
  Silver labels feed statistics -- clustering, uncertainty ordering -- never answers.
* Applying review decisions is idempotent. ``rematerialize`` is a pure function over
  the accepted set, so resubmitting the same decisions resolves to the artifact that
  already exists instead of minting a second candidate.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.models.prompt_lab import (
    TagPromptArtifact,
    TagPromptDemoSource,
    TagPromptGradient,
    TagSilverLabel,
)
from audio_graphy.models.tag_governance import (
    TagBadcase,
    TaggerVersion,
    TagGoldLabel,
    TagGoldSetVersion,
    TagSchemaVersion,
)
from audio_graphy.optimizers.artifacts import (
    CompiledPromptArtifact,
    PromptArtifactError,
    artifact_from_payload,
    rematerialize,
)
from audio_graphy.optimizers.proposers import (
    UnsupportedCompilerError,
    assert_compiler_supported,
)
from audio_graphy.services.tag_governance import (
    GovernanceConflictError,
    GovernanceError,
    TagGovernanceService,
    canonical_checksum,
)

logger = logging.getLogger(__name__)

_COMPILE_JOB_TYPE = "prompt_compile"
_FEEDBACK_THRESHOLD = 200
_DOMAIN_THRESHOLD = 30
_LIVE_ARTIFACT_STATUSES = ("draft", "review")


class PromptLabError(GovernanceError):
    """Raised when a prompt-lab operation is not allowed in the current state."""


class PromptLabNotFoundError(PromptLabError):
    """Raised when a referenced row does not exist inside the caller's tenant.

    Absence and "belongs to someone else" are deliberately the same error: telling
    them apart would let a caller probe another tenant's id space.
    """


class PromptLabPrivacyError(PromptLabError):
    """Raised when an artifact would persist content that must not be served."""


@dataclass(frozen=True, slots=True)
class DomainCoverage:
    """How much certified feedback exists for one subject_type:tag_key pair."""

    domain: str
    gold_count: int
    silver_count: int
    feedback_count: int

    @property
    def meets_threshold(self) -> bool:
        return self.feedback_count >= _DOMAIN_THRESHOLD


@dataclass(frozen=True, slots=True)
class PromptLabReadiness:
    """Whether there is enough reviewed data to compile against, and what is missing."""

    tenant_id: str
    gold_label_total: int
    silver_label_total: int
    feedback_total: int
    feedback_threshold: int
    domain_threshold: int
    domains: tuple[DomainCoverage, ...]
    blockers: tuple[str, ...]
    frozen_gold_set_versions: int
    pending_artifacts: int

    @property
    def ready(self) -> bool:
        return not self.blockers

    @property
    def annotation_hours_remaining(self) -> float:
        """Rough human cost of clearing the domain gaps, at five minutes per subject.

        Deliberately surfaced: the number is the difference between "we should try
        prompt optimisation" and "someone has to label for a day and a half first".
        """

        missing = sum(max(0, _DOMAIN_THRESHOLD - domain.feedback_count) for domain in self.domains)
        return round(missing * 5 / 60, 1)

    def as_payload(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "ready": self.ready,
            "gold_label_total": self.gold_label_total,
            "silver_label_total": self.silver_label_total,
            "feedback_total": self.feedback_total,
            "feedback_threshold": self.feedback_threshold,
            "domain_threshold": self.domain_threshold,
            "frozen_gold_set_versions": self.frozen_gold_set_versions,
            "pending_artifacts": self.pending_artifacts,
            "annotation_hours_remaining": self.annotation_hours_remaining,
            "domains": [
                {
                    "domain": domain.domain,
                    "gold_count": domain.gold_count,
                    "silver_count": domain.silver_count,
                    "feedback_count": domain.feedback_count,
                    "meets_threshold": domain.meets_threshold,
                }
                for domain in self.domains
            ],
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True, slots=True)
class PatchDecision:
    patch_id: str
    accepted: bool
    note: str | None = None


class PromptLabService:
    """Persistence and policy for prompt compilation runs."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        governance: TagGovernanceService | None = None,
    ) -> None:
        self._factory = factory
        self._governance = governance or TagGovernanceService(factory)

    # ---------------------------------------------------------------- readiness

    async def readiness(self, *, tenant_id: str) -> PromptLabReadiness:
        """Report whether enough reviewed data exists to compile against."""

        async with self._factory() as session:
            gold_rows = (
                await session.execute(
                    select(
                        TagGoldLabel.subject_type,
                        TagGoldLabel.tag_key,
                        func.count(TagGoldLabel.id),
                    )
                    .where(TagGoldLabel.tenant_id == tenant_id)
                    .group_by(TagGoldLabel.subject_type, TagGoldLabel.tag_key)
                )
            ).all()
            silver_rows = (
                await session.execute(
                    select(
                        TagSilverLabel.subject_type,
                        TagSilverLabel.tag_key,
                        func.count(TagSilverLabel.id),
                    )
                    .where(TagSilverLabel.tenant_id == tenant_id)
                    .group_by(TagSilverLabel.subject_type, TagSilverLabel.tag_key)
                )
            ).all()
            frozen_versions = int(
                (
                    await session.execute(
                        select(func.count(TagGoldSetVersion.id)).where(
                            TagGoldSetVersion.tenant_id == tenant_id,
                            TagGoldSetVersion.status == "frozen",
                        )
                    )
                ).scalar()
                or 0
            )
            pending = int(
                (
                    await session.execute(
                        select(func.count(TagPromptArtifact.id)).where(
                            TagPromptArtifact.tenant_id == tenant_id,
                            TagPromptArtifact.status.in_(_LIVE_ARTIFACT_STATUSES),
                        )
                    )
                ).scalar()
                or 0
            )

        gold_by_domain = {f"{row[0]}:{row[1]}": int(row[2]) for row in gold_rows}
        silver_by_domain = {f"{row[0]}:{row[1]}": int(row[2]) for row in silver_rows}
        domains = tuple(
            DomainCoverage(
                domain=domain,
                gold_count=gold_by_domain.get(domain, 0),
                silver_count=silver_by_domain.get(domain, 0),
                # Only human-reviewed labels count toward the gate. Silver rows are
                # visible so the gap is legible, but they never close it.
                feedback_count=gold_by_domain.get(domain, 0),
            )
            for domain in sorted(set(gold_by_domain) | set(silver_by_domain))
        )
        gold_total = sum(gold_by_domain.values())
        blockers: list[str] = []
        if gold_total < _FEEDBACK_THRESHOLD:
            blockers.append("reviewed_feedback_below_200")
        blockers.extend(
            f"domain_support_below_30:{domain.domain}"
            for domain in domains
            if not domain.meets_threshold
        )
        if not domains:
            blockers.append("no_reviewed_domains")
        if frozen_versions == 0:
            blockers.append("no_frozen_gold_set")

        return PromptLabReadiness(
            tenant_id=tenant_id,
            gold_label_total=gold_total,
            silver_label_total=sum(silver_by_domain.values()),
            feedback_total=gold_total,
            feedback_threshold=_FEEDBACK_THRESHOLD,
            domain_threshold=_DOMAIN_THRESHOLD,
            domains=domains,
            blockers=tuple(blockers),
            frozen_gold_set_versions=frozen_versions,
            pending_artifacts=pending,
        )

    # ------------------------------------------------------------- compilation

    async def create_compilation(
        self,
        *,
        tenant_id: str,
        baseline_tagger_version_id: int,
        gold_set_version_id: int | None,
        compiler_config: Mapping[str, Any],
        budget: Mapping[str, Any],
        actor_user_id: int,
    ) -> dict[str, Any]:
        """Queue a compile job. The worker does the compiling; this only records intent."""

        # 在入队前拒，而不是让 worker 三十秒后把任务判失败：编译器是否实现是静态事实，
        # 每个镜像都一样，没有理由让用户去任务日志里找答案。
        try:
            assert_compiler_supported(str(compiler_config.get("compiler", "builtin")))
        except UnsupportedCompilerError as exc:
            raise PromptLabError(str(exc)) from exc

        async with self._factory() as session:
            baseline = await session.get(TaggerVersion, baseline_tagger_version_id)
            if baseline is None or str(baseline.tenant_id) != tenant_id:
                raise PromptLabNotFoundError("baseline tagger version not found for this tenant")
            if gold_set_version_id is not None:
                gold_version = await session.get(TagGoldSetVersion, gold_set_version_id)
                if gold_version is None or str(gold_version.tenant_id) != tenant_id:
                    raise PromptLabNotFoundError("gold set version not found for this tenant")
                if str(gold_version.status) != "frozen":
                    raise PromptLabError("a compilation must read a frozen gold set version")

        compilation_id = int(
            canonical_checksum(
                {
                    "tenant_id": tenant_id,
                    "baseline": baseline_tagger_version_id,
                    "gold_set_version_id": gold_set_version_id,
                    "compiler": dict(compiler_config),
                }
            )[:12],
            16,
        )
        job = await self._governance.enqueue_job(
            tenant_id=tenant_id,
            job_type=_COMPILE_JOB_TYPE,
            scope={
                "compilation_id": compilation_id,
                "baseline_tagger_version_id": baseline_tagger_version_id,
                "gold_set_version_id": gold_set_version_id,
                "compiler": dict(compiler_config),
                "purpose": "prompt_compile",
                # Budgets travel in scope.budget, where enqueue_job validates them and
                # the durable ledger picks them up.
                "budget": dict(budget),
            },
            idempotency_key=f"prompt-compile:{tenant_id}:{compilation_id}",
            created_by=actor_user_id,
        )
        return {"compilation_id": compilation_id, "job_id": int(job.id)}

    # ---------------------------------------------------------------- artifacts

    async def persist_artifact(
        self,
        *,
        tenant_id: str,
        compilation_id: int,
        artifact: CompiledPromptArtifact,
        baseline_tagger_version_id: int,
        gold_set_version_id: int | None,
        actor_user_id: int,
        optimization_run_id: int | None = None,
        input_budget_report: Mapping[str, Any] | None = None,
        gradients: Sequence[Mapping[str, Any]] = (),
        parent_artifact_id: int | None = None,
    ) -> TagPromptArtifact:
        """Store a compiled artifact, its gradients and its demo provenance."""

        assert_demo_privacy_policy(artifact)
        checksum = artifact.checksum()
        async with self._factory() as session, session.begin():
            existing = (
                await session.execute(
                    select(TagPromptArtifact).where(
                        TagPromptArtifact.tenant_id == tenant_id,
                        TagPromptArtifact.artifact_checksum == checksum,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing

            row = TagPromptArtifact(
                tenant_id=tenant_id,
                compilation_id=compilation_id,
                optimization_run_id=optimization_run_id,
                baseline_tagger_version_id=baseline_tagger_version_id,
                gold_set_version_id=gold_set_version_id,
                parent_artifact_id=parent_artifact_id,
                compiler=artifact.compiler,
                compiler_version=artifact.compiler_version,
                metric_version=artifact.metric_version,
                status="draft",
                baseline_prompt=artifact.baseline_prompt,
                header=artifact.header,
                rendered_prompt=artifact.render(),
                patches=[patch.as_payload() for patch in artifact.patches],
                demos=[demo.as_payload() for demo in artifact.demos],
                accepted_patch_ids=sorted(artifact.accepted_patch_ids),
                prompt_token_estimate=artifact.prompt_token_estimate,
                input_budget_report=dict(input_budget_report or {}),
                redaction_report=_redaction_report(artifact),
                artifact_checksum=checksum,
                created_by=actor_user_id,
            )
            session.add(row)
            await session.flush()

            for demo in artifact.demos:
                session.add(
                    TagPromptDemoSource(
                        tenant_id=tenant_id,
                        artifact_id=row.id,
                        demo_id=demo.demo_id,
                        gold_label_id=demo.gold_label_id,
                        reception_id=demo.reception_id,
                        subject_type=demo.subject_type,
                        subject_id=demo.subject_id,
                        segment_ids=list(demo.segment_ids),
                        recording_ids=list(demo.recording_ids),
                        redaction_mode=demo.redaction_mode,
                        source_checksum=demo.source_checksum,
                    )
                )
            for gradient in gradients:
                session.add(
                    TagPromptGradient(
                        tenant_id=tenant_id,
                        artifact_id=row.id,
                        patch_id=str(gradient["patch_id"]),
                        iteration=int(gradient.get("iteration", 1)),
                        source_badcase_id=gradient.get("source_badcase_id"),
                        tag_key=gradient.get("tag_key"),
                        failure_stage=gradient.get("failure_stage"),
                        failure_mode=gradient.get("failure_mode"),
                        gradient_text=str(gradient.get("gradient_text", "")),
                        proposed_edit=str(gradient.get("proposed_edit", "")),
                        decision="pending",
                        evaluation=dict(gradient.get("evaluation") or {}),
                        llm_logical_request_id=gradient.get("llm_logical_request_id"),
                    )
                )
            await session.flush()
            return row

    async def apply_patch_decisions(
        self,
        *,
        tenant_id: str,
        artifact_id: int,
        decisions: Sequence[PatchDecision],
        dropped_demo_ids: Sequence[str] = (),
        actor_user_id: int,
    ) -> TagPromptArtifact:
        """Re-materialize an artifact from a reviewer's accept/reject decisions.

        Idempotent by construction: the child's checksum is a pure function of the
        accepted set, so a double submit resolves to the row that already exists.
        """

        async with self._factory() as session, session.begin():
            parent = await self._load_artifact(
                session, tenant_id=tenant_id, artifact_id=artifact_id
            )
            if str(parent.status) not in _LIVE_ARTIFACT_STATUSES:
                raise GovernanceConflictError(
                    "only draft or review artifacts accept patch decisions"
                )

            accepted = {decision.patch_id for decision in decisions if decision.accepted}
            try:
                child = rematerialize(
                    _artifact_from_row(parent),
                    accepted_patch_ids=accepted,
                    dropped_demo_ids=dropped_demo_ids,
                )
            except PromptArtifactError as exc:
                raise PromptLabError(str(exc)) from exc

            assert_demo_privacy_policy(child)
            checksum = child.checksum()
            existing = (
                await session.execute(
                    select(TagPromptArtifact).where(
                        TagPromptArtifact.tenant_id == tenant_id,
                        TagPromptArtifact.artifact_checksum == checksum,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                await self._record_decisions(
                    session,
                    artifact_id=parent.id,
                    decisions=decisions,
                    actor_user_id=actor_user_id,
                )
                return existing

            row = TagPromptArtifact(
                tenant_id=tenant_id,
                compilation_id=parent.compilation_id,
                optimization_run_id=parent.optimization_run_id,
                baseline_tagger_version_id=parent.baseline_tagger_version_id,
                gold_set_version_id=parent.gold_set_version_id,
                parent_artifact_id=parent.id,
                compiler=parent.compiler,
                compiler_version=parent.compiler_version,
                metric_version=parent.metric_version,
                status="draft",
                baseline_prompt=child.baseline_prompt,
                header=child.header,
                rendered_prompt=child.render(),
                patches=[patch.as_payload() for patch in child.patches],
                demos=[demo.as_payload() for demo in child.demos],
                accepted_patch_ids=sorted(child.accepted_patch_ids),
                prompt_token_estimate=child.prompt_token_estimate,
                input_budget_report=dict(parent.input_budget_report or {}),
                redaction_report=_redaction_report(child),
                artifact_checksum=checksum,
                created_by=actor_user_id,
            )
            session.add(row)
            await session.flush()

            surviving = {demo.demo_id for demo in child.demos}
            for demo in child.demos:
                if demo.demo_id not in surviving:
                    continue
                session.add(
                    TagPromptDemoSource(
                        tenant_id=tenant_id,
                        artifact_id=row.id,
                        demo_id=demo.demo_id,
                        gold_label_id=demo.gold_label_id,
                        reception_id=demo.reception_id,
                        subject_type=demo.subject_type,
                        subject_id=demo.subject_id,
                        segment_ids=list(demo.segment_ids),
                        recording_ids=list(demo.recording_ids),
                        redaction_mode=demo.redaction_mode,
                        source_checksum=demo.source_checksum,
                    )
                )

            await self._record_decisions(
                session,
                artifact_id=parent.id,
                decisions=decisions,
                actor_user_id=actor_user_id,
            )
            parent.status = "superseded"
            await session.flush()
            return row

    async def list_gradients(
        self,
        *,
        tenant_id: str,
        artifact_id: int,
        decision: str | None = None,
    ) -> list[TagPromptGradient]:
        async with self._factory() as session:
            query = select(TagPromptGradient).where(
                TagPromptGradient.tenant_id == tenant_id,
                TagPromptGradient.artifact_id == artifact_id,
            )
            if decision is not None:
                query = query.where(TagPromptGradient.decision == decision)
            rows = (
                await session.execute(
                    query.order_by(TagPromptGradient.iteration, TagPromptGradient.id)
                )
            ).scalars()
            return list(rows)

    async def get_artifact(self, *, tenant_id: str, artifact_id: int) -> TagPromptArtifact:
        async with self._factory() as session:
            return await self._load_artifact(session, tenant_id=tenant_id, artifact_id=artifact_id)

    async def list_artifacts(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        limit: int = 50,
    ) -> list[TagPromptArtifact]:
        async with self._factory() as session:
            query = select(TagPromptArtifact).where(TagPromptArtifact.tenant_id == tenant_id)
            if status is not None:
                query = query.where(TagPromptArtifact.status == status)
            rows = (
                await session.execute(
                    query.order_by(
                        TagPromptArtifact.created_at.desc(), TagPromptArtifact.id.desc()
                    ).limit(max(1, min(limit, 200)))
                )
            ).scalars()
            return list(rows)

    # ------------------------------------------------------------ badcase feed

    async def badcase_rows_for_compile(
        self,
        *,
        tenant_id: str,
        schema_version_id: int | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Fetch open badcases as plain mappings for the compiler to cluster.

        Returned as mappings rather than ORM rows so the clustering rules in
        :mod:`audio_graphy.optimizers.proposers` stay testable without a database.
        """

        async with self._factory() as session:
            query = select(TagBadcase).where(
                TagBadcase.tenant_id == tenant_id,
                TagBadcase.status.in_(["open", "reopened"]),
                TagBadcase.dataset_split.in_(["train", "validation"]),
            )
            if schema_version_id is not None:
                query = query.join(
                    TagSchemaVersion,
                    TagSchemaVersion.id == schema_version_id,
                )
            rows = (
                await session.execute(
                    query.order_by(TagBadcase.occurrence_count.desc(), TagBadcase.id).limit(
                        max(1, min(limit, 2_000))
                    )
                )
            ).scalars()
            return [
                {
                    "id": int(row.id),
                    "tag_key": str(row.tag_key),
                    "failure_stage": str(row.failure_stage),
                    "failure_mode": str(row.failure_mode),
                    "cluster_key": row.cluster_key,
                    "occurrence_count": int(row.occurrence_count),
                    "root_cause": dict(row.root_cause or {}),
                }
                for row in rows
            ]

    # -------------------------------------------------------------- internals

    @staticmethod
    async def _load_artifact(
        session: AsyncSession,
        *,
        tenant_id: str,
        artifact_id: int,
    ) -> TagPromptArtifact:
        row = await session.get(TagPromptArtifact, artifact_id)
        if row is None or str(row.tenant_id) != tenant_id:
            raise PromptLabNotFoundError("prompt artifact not found for this tenant")
        return row

    @staticmethod
    async def _record_decisions(
        session: AsyncSession,
        *,
        artifact_id: int,
        decisions: Sequence[PatchDecision],
        actor_user_id: int,
    ) -> None:
        if not decisions:
            return
        by_patch = {decision.patch_id: decision for decision in decisions}
        rows = (
            await session.execute(
                select(TagPromptGradient).where(
                    TagPromptGradient.artifact_id == artifact_id,
                    TagPromptGradient.patch_id.in_(list(by_patch)),
                )
            )
        ).scalars()
        now = datetime.now(UTC)
        for row in rows:
            decision = by_patch[str(row.patch_id)]
            row.decision = "accepted" if decision.accepted else "rejected"
            row.decided_by = actor_user_id
            row.decided_at = now
            row.decision_note = decision.note


def _artifact_from_row(row: TagPromptArtifact) -> CompiledPromptArtifact:
    return artifact_from_payload(
        {
            "baseline_prompt": row.baseline_prompt,
            "header": row.header,
            "compiler": row.compiler,
            "compiler_version": row.compiler_version,
            "metric_version": row.metric_version,
            "patches": list(row.patches or []),
            "demos": list(row.demos or []),
            "accepted_patch_ids": list(row.accepted_patch_ids or []),
        }
    )


def _redaction_report(artifact: CompiledPromptArtifact) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for demo in artifact.demos:
        counts[demo.redaction_mode] = counts.get(demo.redaction_mode, 0) + 1
    return {"demo_count": len(artifact.demos), "by_redaction_mode": counts}


def assert_demo_privacy_policy(artifact: CompiledPromptArtifact) -> None:
    """Refuse to persist an artifact carrying content that must not be served.

    A demonstration inlined into a prompt is copied into an immutable TaggerVersion
    and sent to the provider on every request thereafter. Verbatim customer speech
    has no route back out, so it is rejected before it can be stored at all.
    """

    verbatim = sorted(demo.demo_id for demo in artifact.demos if demo.redaction_mode == "verbatim")
    if verbatim:
        raise PromptLabPrivacyError(
            "verbatim demonstrations cannot be persisted into a served prompt: "
            + ", ".join(verbatim)
        )
