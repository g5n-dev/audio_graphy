"""Worker process that compiles prompt candidates from reviewed feedback.

Deliberately separate from :mod:`audio_graphy.tag_worker`. The two share a queue but
claim disjoint job types, and this one is the only process that may import the
optional optimizer extras -- so the API image never has to carry DSPy or TextGrad.

A compile produces a *draft artifact*, nothing more. Whether that draft ever reaches
production is decided by the existing evaluation and deployment gates, on evidence
this worker has no say in.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audio_graphy.adapters.protocols import LLMAdapter
from audio_graphy.models.tag_governance import TagExtractionJob, TaggerVersion
from audio_graphy.optimizers.artifacts import CompiledPromptArtifact
from audio_graphy.optimizers.availability import textgrad_status
from audio_graphy.optimizers.gradients import TextGradProposer
from audio_graphy.optimizers.lm_bridge import (
    GatewayLM,
    LMBudget,
    LoopRunner,
    OptimizerLMConfig,
)
from audio_graphy.optimizers.proposers import (
    BuiltinGroundedProposer,
    BuiltinProposer,
    PromptProposer,
    ProposalRequest,
    assert_compiler_supported,
    cluster_badcases,
)
from audio_graphy.services.prompt_lab import PromptLabService
from audio_graphy.services.tag_extractor import prompt_input_budget_report
from audio_graphy.services.tag_governance import GovernanceError, TagGovernanceService
from audio_graphy.services.tag_harness_runtime import (
    materialize_trial_candidate,
    resolve_harness_spec,
)

logger = logging.getLogger(__name__)

PROMPT_LAB_JOB_TYPES: tuple[str, ...] = ("prompt_compile",)

_DEFAULT_POLL_SECONDS = 3.0
_DEFAULT_LEASE = timedelta(minutes=15)
_HEARTBEAT_FRACTION = 3


class PromptCompileError(GovernanceError):
    """Raised when a compile job cannot produce a usable candidate."""


class PromptLabWorker:
    """Lease-based consumer for ``prompt_compile`` jobs."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        worker_id: str,
        service: PromptLabService | None = None,
        governance: TagGovernanceService | None = None,
        poll_seconds: float = _DEFAULT_POLL_SECONDS,
        lease_ttl: timedelta = _DEFAULT_LEASE,
        actor_user_id: int = 0,
        strong_llm: LLMAdapter | None = None,
    ) -> None:
        self._factory = factory
        self._worker_id = worker_id
        self._governance = governance or TagGovernanceService(factory)
        self._service = service or PromptLabService(factory, governance=self._governance)
        self._poll_seconds = poll_seconds
        self._lease_ttl = lease_ttl
        self._actor_user_id = actor_user_id
        # Absent in the model-free deployment. Compilers that need one refuse rather
        # than quietly producing a template artifact under another compiler's name.
        self._strong_llm = strong_llm

    async def run_once(self, *, now: datetime | None = None) -> bool:
        """Claim and execute at most one compile job. Returns whether one ran."""

        claimed_at = now or datetime.now(UTC)
        job = await self._governance.claim_next_job(
            worker_id=self._worker_id,
            now=claimed_at,
            lease_for=self._lease_ttl,
            job_types=PROMPT_LAB_JOB_TYPES,
        )
        if job is None:
            return False

        revision = [job.revision]
        stop = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat_loop(job=job, revision=revision, stop=stop))
        try:
            artifact_id = await self._compile(job)
            await self._governance.advance_job_progress(
                tenant_id=str(job.tenant_id),
                job_id=job.id,
                worker_id=self._worker_id,
                expected_revision=revision[0],
                success=True,
                item_ref={"stage": "materialize", "artifact_id": artifact_id},
                now=datetime.now(UTC),
                lease_for=self._lease_ttl,
            )
        except Exception as exc:
            logger.exception("prompt compile failed job=%s", job.id)
            await self._governance.defer_job_failure(
                tenant_id=str(job.tenant_id),
                job_id=job.id,
                worker_id=self._worker_id,
                expected_revision=revision[0],
                error_code=type(exc).__name__,
                error_message=str(exc)[:1_000],
                now=datetime.now(UTC),
            )
        finally:
            stop.set()
            with contextlib.suppress(Exception):
                await heartbeat
        return True

    async def run_forever(self) -> None:
        while True:
            try:
                worked = await self.run_once()
            except Exception:
                logger.exception("prompt lab worker loop error")
                worked = False
            if not worked:
                await asyncio.sleep(self._poll_seconds)

    async def _heartbeat_loop(
        self,
        *,
        job: TagExtractionJob,
        revision: list[int],
        stop: asyncio.Event,
    ) -> None:
        interval = max(1.0, self._lease_ttl.total_seconds() / _HEARTBEAT_FRACTION)
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
            if stop.is_set():
                return
            alive = await self._governance.heartbeat_job(
                job.id,
                tenant_id=str(job.tenant_id),
                worker_id=self._worker_id,
                expected_revision=revision[0],
                now=datetime.now(UTC),
                lease_for=self._lease_ttl,
            )
            if not alive:
                logger.warning("prompt compile lease lost job=%s", job.id)
                return

    def _proposer_for(
        self,
        compiler_config: Mapping[str, object],
        *,
        tenant_id: str,
        compilation_id: int,
        budget: Mapping[str, object],
    ) -> PromptProposer:
        """Pick the proposer the request asked for, or refuse.

        The service already rejects unimplemented compilers at enqueue time. This
        second check is not redundant: a job may have been queued by an older API
        image, or its ``scope`` edited by hand, and silently compiling with the
        wrong proposer would mislabel the artifact rather than fail.
        """

        name = str(compiler_config.get("compiler", "builtin"))
        assert_compiler_supported(name)
        if name == "builtin":
            return BuiltinProposer()

        if self._strong_llm is None:
            raise PromptCompileError(
                f"编译器 {name} 需要一个可用的强模型适配器，当前 worker 未配置。"
            )
        purpose = (
            "prompt_lab_gradient_repair"
            if name == "textgrad_tgd"
            else "prompt_lab_instruction_proposal"
        )
        # Captured on the worker's own loop; the proposer runs in a thread and hands
        # each call back here rather than starting a loop of its own.
        lm = GatewayLM(
            adapter=self._strong_llm,
            config=OptimizerLMConfig(
                tenant_id=tenant_id,
                purpose=purpose,
                compilation_id=compilation_id,
            ),
            runner=LoopRunner.for_running_loop(),
            budget=LMBudget(
                max_calls=_as_positive_int(budget.get("max_provider_calls")),
                max_tokens=_as_positive_int(budget.get("max_provider_tokens")),
                max_cost_microunits=_as_positive_int(budget.get("max_cost_microunits")),
            ),
        )
        if name == "builtin_grounded":
            return BuiltinGroundedProposer(writer=lm)
        return self._textgrad_proposer(lm, compiler_config)

    def _textgrad_proposer(
        self,
        lm: GatewayLM,
        compiler_config: Mapping[str, object],
    ) -> PromptProposer:
        """Build the TextGrad proposer, or say plainly that the extra is missing.

        Refusing beats degrading to the grounded proposer: the artifact would be
        stamped ``textgrad_tgd`` while no gradient was ever computed, and the gradient
        panel would show a diagnosis that is really just a template rationale.
        """

        textgrad_status().require()
        # Imported here, not at module scope: the API image has no textgrad, and a
        # top-level import would make this module unimportable there.
        from audio_graphy.optimizers.textgrad_bridge import (
            GatewayTextGradEngine,
            LibraryGradientStep,
        )

        iterations = _as_positive_int(compiler_config.get("textgrad_iterations")) or 2
        return TextGradProposer(
            step=LibraryGradientStep(GatewayTextGradEngine(lm)),
            iterations=iterations,
        )

    async def _compile(self, job: TagExtractionJob) -> int:
        tenant_id = str(job.tenant_id)
        scope = dict(job.scope or {})
        compiler_config = dict(scope.get("compiler") or {})
        baseline_id = int(scope["baseline_tagger_version_id"])

        async with self._factory() as session:
            baseline = await session.get(TaggerVersion, baseline_id)
            if baseline is None or str(baseline.tenant_id) != tenant_id:
                raise PromptCompileError("baseline tagger version is not available")
            baseline_spec = resolve_harness_spec(baseline)
            definitions = _definitions_for(baseline_spec, session_definitions=None)

        badcases = await self._service.badcase_rows_for_compile(tenant_id=tenant_id)
        proposer = self._proposer_for(
            compiler_config,
            tenant_id=tenant_id,
            compilation_id=int(scope["compilation_id"]),
            budget=dict(scope.get("budget") or {}),
        )
        request = ProposalRequest(
            baseline_prompt=str(baseline_spec.get("generation", {}).get("prompt_template", "")),
            clusters=cluster_badcases(badcases),
            definitions=definitions,
            max_patches=int(compiler_config.get("max_patches", 8)),
            min_cluster_support=int(compiler_config.get("min_cluster_support", 3)),
        )
        # Always a thread, even for the model-free proposer. ``propose`` is
        # synchronous by contract -- that is what lets DSPy and TextGrad drive it --
        # and a model-backed one blocks on the gateway, which would stall this loop
        # and, worse, deadlock against the very call it is waiting for.
        artifact = await asyncio.to_thread(proposer.propose, request)
        if not artifact.patches:
            raise PromptCompileError(
                "no badcase cluster met the support threshold, so there is nothing to propose"
            )

        budget_report = _preflight(
            artifact=artifact,
            baseline_spec=baseline_spec,
            definitions=definitions,
            max_prompt_tokens=int(compiler_config.get("max_prompt_tokens", 3_072)),
        )

        row = await self._service.persist_artifact(
            tenant_id=tenant_id,
            compilation_id=int(scope["compilation_id"]),
            artifact=artifact,
            baseline_tagger_version_id=baseline_id,
            gold_set_version_id=scope.get("gold_set_version_id"),
            actor_user_id=job.created_by or self._actor_user_id,
            input_budget_report=budget_report,
            gradients=_gradient_rows(artifact, proposer),
        )
        logger.info(
            "compiled prompt artifact tenant=%s artifact=%s patches=%d",
            tenant_id,
            row.id,
            len(artifact.patches),
        )
        return int(row.id)


def _gradient_rows(
    artifact: CompiledPromptArtifact,
    proposer: PromptProposer,
) -> list[dict[str, object]]:
    """Per-patch evidence rows, preferring a real diagnosis over a restated rationale.

    Only the gradient proposer computes an actual critique. For the others the
    rationale is the honest thing to store -- it says which cluster motivated the
    rule -- but it must not be dressed up as a diagnosis the compiler never made,
    which is why ``gradient_rounds``/``replayed`` travel alongside it.
    """

    recorded: Mapping[str, Mapping[str, object]] = getattr(proposer, "gradients", {}) or {}
    rows: list[dict[str, object]] = []
    for patch in artifact.patches:
        evaluation = dict(recorded.get(patch.patch_id) or {})
        diagnosis = str(evaluation.pop("gradient_text", "") or "")
        if not evaluation:
            evaluation = {
                "source_badcase_count": len(patch.source_badcase_ids),
                "gradient_rounds": 0,
                "replayed": False,
            }
        rows.append(
            {
                "patch_id": patch.patch_id,
                "tag_key": patch.target_tag_keys[0] if patch.target_tag_keys else None,
                "failure_stage": "tag_reasoning",
                "gradient_text": diagnosis or patch.rationale,
                "proposed_edit": patch.body,
                "evaluation": evaluation,
                "source_badcase_id": (
                    patch.source_badcase_ids[0] if patch.source_badcase_ids else None
                ),
            }
        )
    return rows


def _as_positive_int(value: object) -> int | None:
    """Budget caps arrive from a JSON column; anything unusable means "uncapped here".

    The durable ledger enforces the same caps independently, so a malformed value
    loosens this in-process guard rather than removing the limit.
    """

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _definitions_for(
    baseline_spec: dict[str, object],
    *,
    session_definitions: dict[str, dict[str, object]] | None,
) -> dict[str, dict[str, object]]:
    """Tag definitions the proposer needs to spell out allowed values.

    The builtin proposer degrades gracefully without them -- it falls back to the
    generic rule -- so an empty mapping is a valid answer rather than an error.
    """

    if session_definitions:
        return session_definitions
    thresholds = baseline_spec.get("output", {})
    if isinstance(thresholds, dict):
        keys = thresholds.get("thresholds")
        if isinstance(keys, dict):
            return {str(key): {"key": str(key)} for key in keys}
    return {}


def _preflight(
    *,
    artifact: CompiledPromptArtifact,
    baseline_spec: dict[str, object],
    definitions: dict[str, dict[str, object]],
    max_prompt_tokens: int,
) -> dict[str, object]:
    """Refuse a prompt that would not survive the input budget it will serve under.

    Catching this here rather than at trial time matters: a prompt that overflows the
    budget raises inside the extractor, and one that merely shrinks the headroom makes
    long subjects split into extra provider calls -- which the efficiency envelope
    rejects no matter how good the prompt is.
    """

    candidate = materialize_trial_candidate(
        baseline_spec,
        prompt_mode="replace",
        prompt_template=artifact.render(),
        max_prompt_template_tokens=max_prompt_tokens,
    )
    report = prompt_input_budget_report(
        candidate,
        baseline=baseline_spec,
        definitions=definitions,
    )
    if not report.fits:
        raise PromptCompileError(
            "the compiled prompt exceeds the per-call input budget "
            f"({report.fixed_tokens} > {report.usable_tokens} tokens)"
        )
    return {
        "prompt_tokens": report.prompt_tokens,
        "schema_tokens": report.schema_tokens,
        "fixed_tokens": report.fixed_tokens,
        "usable_tokens": report.usable_tokens,
        "headroom_tokens": report.headroom_tokens,
        "baseline_fixed_tokens": report.baseline_fixed_tokens,
        "baseline_headroom_tokens": report.baseline_headroom_tokens,
        "headroom_delta": report.headroom_delta,
        "headroom_shrink_ratio": report.headroom_shrink_ratio,
        "fits": report.fits,
    }


async def _main() -> None:
    from audio_graphy.config import build_adapters, get_settings
    from audio_graphy.db import create_db_engine, create_session_factory
    from audio_graphy.services.llm_runtime import build_llm_runtime

    settings = get_settings()
    engine = create_db_engine(settings)
    factory = create_session_factory(engine)
    adapters = build_adapters(settings)
    # The runtime bundle is what wraps the raw adapter in the gateway: caching, the
    # durable ledger and the price snapshot all live there. Handing the proposer a
    # bare adapter would work and would spend money off the books.
    llm_runtime = await build_llm_runtime(settings, factory, adapters)
    worker = PromptLabWorker(
        factory,
        worker_id=f"prompt-lab-{id(factory):x}",
        strong_llm=llm_runtime.bundle.strong_llm,
    )
    try:
        await worker.run_forever()
    finally:
        # aclose cancels the cache-cleanup task the runtime spawned; without it the
        # process hangs on shutdown. The adapters own httpx clients of their own.
        with contextlib.suppress(Exception):
            await llm_runtime.aclose()
        for adapter in (adapters.strong_llm, adapters.weak_llm):
            close = getattr(adapter, "aclose", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    await close()
        await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())


if __name__ == "__main__":
    main()


__all__ = ["PROMPT_LAB_JOB_TYPES", "PromptCompileError", "PromptLabWorker", "main"]
